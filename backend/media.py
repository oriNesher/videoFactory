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
