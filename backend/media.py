"""Inspecting media files with FFprobe, before and after processing.

Two jobs, both about not trusting things blindly:

**Before.** Cutting a two-gigabyte take is expensive and a failure halfway
through is unpleasant, so every input is probed first: it must exist, be
readable by FFprobe, carry a decodable video stream, and — because this
milestone's cutting is *audio-driven* — carry an audio stream too. A take with
no audio is not a tool failure to be reported as a crash; it is a validation
error with a sentence explaining why this mode cannot work on it.

**After.** A zero exit code is not proof that a file is playable. Auto-Editor
and FFmpeg both have failure modes where the process succeeds and the artefact
is wrong, so every produced file is probed again and its real duration measured.

Fingerprints identify *which bytes* a run actually consumed, so a run manifest
stays meaningful after the user re-records a take under the same filename.
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

PROBE_TIMEOUT_SECONDS = 60

# How much of a file is hashed at each end. Hashing a multi-gigabyte recording
# in full would cost minutes per run for no practical gain: size plus both ends
# separates "a different take" from "the same take" perfectly well, and the
# manifest says exactly what was hashed so nobody mistakes it for a checksum.
FINGERPRINT_CHUNK_BYTES = 1024 * 1024
FINGERPRINT_METHOD = "sha256:size+head1m+tail1m"


class MediaError(Exception):
    """A media problem the user can act on, phrased for them."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _ffprobe(path: str, arguments: list[str]) -> str:
    """Run FFprobe with an argument list. Never through a shell."""
    command = ["ffprobe.exe", "-v", "error", *arguments, str(path)]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        raise MediaError(
            "FFprobe was not found on PATH. Install FFmpeg or add it to PATH."
        ) from error
    except subprocess.TimeoutExpired as error:
        raise MediaError("Probing the file took too long and was stopped: %s" % path) from error
    except OSError as error:
        raise MediaError("FFprobe could not be run: %s" % error) from error

    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        reason = detail[-1] if detail else "FFprobe reported an error with no detail."
        raise MediaError("FFprobe could not read the file: %s" % reason)

    return result.stdout or ""


def _to_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"),) else None


def _frame_rate(value: Any) -> float | None:
    """`r_frame_rate` arrives as "25/1" — or "0/0" for a stream without one."""
    if not isinstance(value, str) or "/" not in value:
        return _to_float(value)

    numerator, _, denominator = value.partition("/")
    top = _to_float(numerator)
    bottom = _to_float(denominator)
    if top is None or not bottom:
        return None
    return top / bottom


def probe(path: str) -> dict:
    """Return a normalised description of a media file.

    Raises `MediaError` when FFprobe cannot read it at all. A file that reads
    but is unsuitable is *not* an error here — `describe_input` decides that,
    so the caller can report a specific reason.
    """
    if not os.path.isfile(path):
        raise MediaError("File not found: %s" % path)

    raw = _ffprobe(
        path,
        [
            "-show_entries",
            "stream=index,codec_type,codec_name,width,height,r_frame_rate,"
            "pix_fmt,sample_rate,channels,duration",
            "-show_entries",
            "format=duration,format_name,size",
            "-of",
            "json",
        ],
    )

    try:
        data = json.loads(raw)
    except ValueError as error:
        raise MediaError("FFprobe returned an invalid response for: %s" % path) from error

    streams = data.get("streams") or []
    container = data.get("format") or {}

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = _to_float(container.get("duration"))
    if duration is None and video is not None:
        duration = _to_float(video.get("duration"))

    described: dict = {
        "path": path,
        "filename": os.path.basename(path),
        "duration_seconds": duration,
        "format_name": container.get("format_name") or "",
        "size_bytes": int(_to_float(container.get("size")) or 0) or None,
        "has_video": video is not None,
        "has_audio": audio is not None,
        "video": None,
        "audio": None,
    }

    if video is not None:
        described["video"] = {
            "codec_name": video.get("codec_name") or "",
            "width": video.get("width"),
            "height": video.get("height"),
            "pix_fmt": video.get("pix_fmt") or "",
            "frame_rate": _frame_rate(video.get("r_frame_rate")),
        }

    if audio is not None:
        described["audio"] = {
            "codec_name": audio.get("codec_name") or "",
            "sample_rate": int(_to_float(audio.get("sample_rate")) or 0) or None,
            "channels": audio.get("channels"),
        }

    return described


# Below this peak the track carries no recoverable sound at all. A silent AAC
# track measures around -91 dB; real room tone sits far above it.
SILENT_PEAK_DB = -70.0

# Mean level below which a track holds no *sustained* sound — a noise floor with
# the occasional transient, not speech. Measured on this machine: a screen
# recording with no microphone input averaged -65.6 dB while peaking at -34.4,
# and no threshold produced a usable cut from it. Speech, even recorded quietly,
# averages well above this.
NO_SPEECH_MEAN_DB = -55.0


def measure_audio_level(path: str) -> dict:
    """Peak and mean loudness of the audio track, via FFmpeg's `volumedetect`.

    Cheap because video decoding is skipped (`-vn`): a 35-second take measures
    in about a tenth of a second. Worth doing before every run, because the
    alternative is discovering that a whole recording was silent only after
    rendering it.

    Never raises: a level that cannot be measured is reported as unknown rather
    than blocking a run that might be fine.
    """
    unknown = {"max_db": None, "mean_db": None, "peak_ratio": None}

    try:
        result = subprocess.run(
            ["ffmpeg.exe", "-hide_banner", "-nostdin", "-vn", "-i", str(path),
             "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return unknown

    levels: dict = dict(unknown)
    for line in (result.stderr or "").splitlines():
        for key, label in (("max_db", "max_volume:"), ("mean_db", "mean_volume:")):
            if label in line:
                try:
                    levels[key] = float(line.split(label, 1)[1].strip().split()[0])
                except (IndexError, ValueError):
                    pass

    if levels["max_db"] is not None:
        # Auto-Editor's threshold is a 0–1 ratio of full scale, so convert.
        levels["peak_ratio"] = round(10 ** (levels["max_db"] / 20), 6)

    return levels


def suggested_threshold(level: dict | None) -> float | None:
    """A threshold that could plausibly separate speech from silence.

    Based on the *mean* level, not the peak. Speech has to stay above the
    threshold for a sustained stretch to be kept, so a single loud transient
    says nothing about where the line should go — a take peaking at 0.019 with
    a mean of 0.0005 produced nothing at half its peak, which is how this came
    to be mean-based.

    Returns None when the track has no sustained sound to work with, because
    inventing a number there sends the user round the loop again.
    """
    if not level:
        return None

    mean_db = level.get("mean_db")
    if mean_db is None or mean_db <= NO_SPEECH_MEAN_DB:
        return None

    # A little above the mean: quiet passages fall below it, speech stays over.
    mean_ratio = 10 ** (mean_db / 20)
    return max(0.001, min(1.0, round(mean_ratio * 1.5, 4)))


def describe_input(path: str, *, require_audio: bool = True) -> dict:
    """Probe a *source* file and refuse it if this mode cannot process it.

    `require_audio` exists because the requirement is specific to audio-driven
    cutting, not to media in general: when a later milestone adds a mode that
    does not listen to the audio, it passes `False` rather than duplicating
    this function.
    """
    described = probe(path)
    filename = described["filename"]

    if not described["has_video"]:
        raise MediaError(
            'File "%s" has no video track, so no trimmed clip can be produced from it.'
            % filename
        )

    if require_audio and not described["has_audio"]:
        raise MediaError(
            'File "%s" has no audio track. Cutting in this mode is driven by audio '
            "loudness, so speech and silence cannot be detected in it. Add an "
            "audio track or remove it from the selection." % filename
        )

    duration = described["duration_seconds"]
    if duration is None or duration <= 0:
        raise MediaError(
            'The duration of file "%s" cannot be determined. It may be corrupt or still being written.'
            % filename
        )

    described["audio_level"] = None

    if require_audio:
        level = measure_audio_level(path)
        described["audio_level"] = level
        peak = level["max_db"]

        mean = level["mean_db"]

        # An audio track that exists but carries no usable sound is the same
        # problem as no audio track at all, and has to be said differently: no
        # threshold can find speech in silence, so advising a lower one would
        # cost the user a full render to learn nothing.
        if peak is not None and peak <= SILENT_PEAK_DB:
            raise MediaError(
                'File "%s" has an audio track, but it is completely silent '
                "(peak %.1f dB). Audio-driven cutting has nothing to detect in "
                "it — this usually means the recording captured no microphone "
                "input. Lowering the audio threshold cannot help. Use a take "
                "with real sound, or remove this one from the selection."
                % (filename, peak)
            )

        # Sound, but no *sustained* sound: a noise floor with the odd transient.
        if mean is not None and mean <= NO_SPEECH_MEAN_DB:
            raise MediaError(
                'File "%s" has an audio track with no speech in it: it averages '
                "%.1f dB and only peaks at %.1f dB, which is a noise floor "
                "rather than a voice. No audio threshold can separate speech "
                "from silence here. This is what a screen or camera recording "
                "looks like when the microphone was not captured — check the "
                "recording's audio input and re-record, or remove this file "
                "from the selection." % (filename, mean, peak if peak is not None else 0.0)
            )

    return described


def verify_output(path: str, *, require_audio: bool = True) -> dict:
    """Probe a *produced* file. A zero exit code alone is never enough."""
    if not os.path.isfile(path):
        raise MediaError("The tool reported success but no output file was created: %s" % path)

    if os.path.getsize(path) == 0:
        raise MediaError("The output file that was created is empty: %s" % os.path.basename(path))

    described = probe(path)

    if not described["has_video"]:
        raise MediaError(
            "Output file %s was created but has no video track." % described["filename"]
        )
    if require_audio and not described["has_audio"]:
        raise MediaError(
            "Output file %s was created but has no audio track." % described["filename"]
        )

    duration = described["duration_seconds"]
    if duration is None or duration <= 0:
        raise MediaError(
            "Output file %s was created but its duration cannot be read." % described["filename"]
        )

    return described


def fingerprint(path: str) -> dict:
    """Identify the bytes of a file cheaply and stably.

    Size and both ends, not the whole file: see `FINGERPRINT_CHUNK_BYTES`. The
    method name is stored alongside the digest so a future change can be told
    apart from a changed file.
    """
    try:
        size = os.path.getsize(path)
        digest = hashlib.sha256()
        digest.update(str(size).encode("ascii"))

        with open(path, "rb") as handle:
            digest.update(handle.read(FINGERPRINT_CHUNK_BYTES))
            if size > FINGERPRINT_CHUNK_BYTES * 2:
                handle.seek(-FINGERPRINT_CHUNK_BYTES, os.SEEK_END)
                digest.update(handle.read(FINGERPRINT_CHUNK_BYTES))

        modified_ns = os.stat(path).st_mtime_ns
    except OSError as error:
        raise MediaError("The file could not be read to fingerprint it: %s" % error) from error

    return {
        "method": FINGERPRINT_METHOD,
        "digest": "sha256:%s" % digest.hexdigest(),
        "size_bytes": size,
        "modified_ns": modified_ns,
    }


# --- comparing clips for concatenation --------------------------------------


def video_geometry(described: dict) -> tuple:
    """The properties that must match for frames to survive concatenation."""
    video = described.get("video") or {}
    return (video.get("width"), video.get("height"))


def stream_signature(described: dict) -> tuple:
    """Everything that must match for `-c copy` concatenation to be valid.

    Stricter than `video_geometry` on purpose: FFmpeg will happily stream-copy
    clips whose codecs or sample rates differ and hand back a file that plays
    wrong, so the decision to copy is made here rather than by trying it.
    """
    video = described.get("video") or {}
    audio = described.get("audio") or {}
    frame_rate = video.get("frame_rate")

    return (
        video.get("codec_name"),
        video.get("width"),
        video.get("height"),
        video.get("pix_fmt"),
        round(frame_rate, 3) if isinstance(frame_rate, float) else frame_rate,
        audio.get("codec_name"),
        audio.get("sample_rate"),
        audio.get("channels"),
    )


def geometry_label(described: dict) -> str:
    video = described.get("video") or {}
    width, height = video.get("width"), video.get("height")
    if not width or not height:
        return "unknown"
    return "%d×%d" % (width, height)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
