"""Boundary-only trimming: find where a take's sound starts and ends.

The recording workflow this serves: a script is recorded as short takes, one
clip per successful take, and the clips are joined. What needs removing is the
dead time *around* each take — the pause between pressing record and speaking,
and between the last word and pressing stop. Pauses *inside* the take are
delivery and stay.

So this mode keeps exactly one continuous interval per clip: from the first
qualifying sound to the last, plus padding. It never joins islands.

**Why not Auto-Editor.** Checked against 31.3.2: its model is "cut every
inactive section", tuned by `--margin` and `--smooth MINCUT,MINCLIP`. A huge
MINCUT does stop it cutting internal pauses, but it stops it cutting a leading
or trailing silence shorter than MINCUT too — the two cannot be separated, and
the padding would then be one number for both ends of every section. Detection
is therefore done here, on FFmpeg-decoded audio, and the retained interval is
rendered with FFmpeg (`cutting.build_trim_command`).

**What detection is.** Audio is decoded to 16 kHz PCM and reduced to a peak
envelope: the loudest sample in each 10 ms, as a share of full scale — the same
0–1 scale as Auto-Editor's threshold. A frame at or above the threshold is
*active*. Active frames separated by less than `BRIDGE_SECONDS` form one run,
and a run at least `min_activity_seconds` long *qualifies*. The boundary is the
start of the first qualifying run and the end of the last.

This is loudness, not speech recognition. A cough, a door or a keyboard is
"activity" exactly as a word is; the minimum-activity rule only rejects sounds
that are *short*. Nothing here is labelled speech.

**Analysing only the ends.** Decoding starts with a window at each end of the
file and grows it (×2) until a qualifying run is found or the two windows
meet. A window edge is never used as a boundary: a run cut off by
the edge that does not qualify on its visible part simply triggers the next
expansion. The result is identical to a full scan (tested), and the search
falls back to one full decode when the file is short or the windows meet.
"""

import hashlib
import json
import os
import subprocess
import time
from array import array
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import media, processes, storage

ANALYSIS_SCHEMA_VERSION = 1

# Bumped whenever detection would give a different answer for the same file and
# settings, so cached analyses from an older algorithm are never reused.
ALGORITHM_VERSION = 1

ANALYSIS_DIRECTORY = "cut-analysis"

# --- the envelope -------------------------------------------------------------

ENVELOPE_SAMPLE_RATE = 16000
FRAME_SECONDS = 0.01
_SAMPLES_PER_FRAME = int(ENVELOPE_SAMPLE_RATE * FRAME_SECONDS)

# A dip below the threshold shorter than this does not end a run: the closure
# of a "t" or a "k" and the gap between two syllables are both silence on a
# 10 ms scale, and neither is the end of a word.
BRIDGE_SECONDS = 0.15

# The first look at each end. Most takes have a second or two of dead time, so
# this nearly always contains the boundary; when it does not, the window grows.
INITIAL_WINDOW_SECONDS = 12.0
WINDOW_GROWTH = 2.0

# Activity this close to the first or last frame is reported as "already
# sounding" rather than as a detected boundary.
EDGE_TOLERANCE_SECONDS = 0.05

# The shortest interval that will be rendered. Below this FFmpeg can produce a
# file with no video frame in it.
MIN_RETAINED_SECONDS = 0.1

MAX_OVERRIDE_SECONDS = 24 * 60 * 60.0

STATUS_DETECTED = "detected"
STATUS_NO_ACTIVITY = "no_activity"
STATUS_NO_AUDIO = "no_audio"

ORIGIN_DETECTED = "detected"
ORIGIN_MANUAL = "manual"
ORIGIN_KEPT = "kept_whole"
# No boundary was found on this side, so the source's own limit is used.
ORIGIN_SOURCE_LIMIT = "source_limit"

SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"

# --- settings ----------------------------------------------------------------
#
# Separate from the five full-clip settings on purpose. The threshold means the
# same thing in both modes, but the padding does not: Auto-Editor's margin is
# applied around *every* section it keeps, this padding only at the two ends of
# the clip. Sharing keys would let one mode quietly inherit numbers tuned for
# the other.

SETTINGS_SPEC: dict[str, dict] = {
    "detection_threshold": {
        "type": "number",
        "default": 0.04,
        "min": 0.001,
        "max": 1.0,
        "step": 0.005,
        "unit": "0–1",
        "label": "Detection threshold",
        "description": (
            "How loud a sound must be to count as activity, as a share of full "
            "scale. Lower catches quieter sound (and more background noise). "
            "This measures loudness only: it cannot tell speech from a cough."
        ),
    },
    "leading_padding_seconds": {
        "type": "number",
        "default": 0.15,
        "min": 0.0,
        "max": 5.0,
        "step": 0.05,
        "unit": "seconds",
        "label": "Padding before the start",
        "description": (
            "Kept before the first detected sound, so a soft first consonant "
            "or a breath is not clipped."
        ),
    },
    "trailing_padding_seconds": {
        "type": "number",
        "default": 0.35,
        "min": 0.0,
        "max": 5.0,
        "step": 0.05,
        "unit": "seconds",
        "label": "Padding after the end",
        "description": (
            "Kept after the last detected sound, so the last word can decay "
            "and the join has a moment to breathe."
        ),
    },
    "min_activity_seconds": {
        "type": "number",
        "default": 0.2,
        "min": 0.0,
        "max": 5.0,
        "step": 0.05,
        "unit": "seconds",
        "label": "Ignore sounds shorter than",
        "description": (
            "A sound shorter than this is treated as noise (a click, a bump) "
            "and cannot set the start or the end."
        ),
    },
}


class BoundaryError(storage.ProjectError):
    """A boundary problem the user can act on."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def default_settings() -> dict:
    return {name: spec["default"] for name, spec in SETTINGS_SPEC.items()}


def validate_settings(raw: Any) -> dict:
    """Check the boundary parameters against the spec. Unknown keys are refused."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise BoundaryError("The boundary settings are invalid.")

    for name in raw:
        if name not in SETTINGS_SPEC:
            raise BoundaryError(
                'Setting "%s" is not recognised in boundary-only mode. Available '
                "settings: %s." % (name, ", ".join(SETTINGS_SPEC))
            )

    validated: dict = {}
    for name, spec in SETTINGS_SPEC.items():
        value = raw.get(name)
        if value is None:
            validated[name] = spec["default"]
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            raise BoundaryError('Setting "%s" must be a number.' % spec["label"])
        value = float(value)
        if value < spec["min"] or value > spec["max"]:
            raise BoundaryError(
                'Setting "%s" must be between %s and %s (%s).'
                % (spec["label"], spec["min"], spec["max"], spec["unit"])
            )
        validated[name] = round(value, 4)

    return validated


# --- manual overrides --------------------------------------------------------

_OVERRIDE_KEYS = ("start_seconds", "end_seconds", "keep_whole")


def validate_override(raw: Any, label: str = "This clip") -> dict | None:
    """One clip's manual boundary. Returns None when nothing is overridden.

    `start_seconds` and `end_seconds` are positions in the *source*, each
    optional: one side can be set by hand while the other stays detected.
    `keep_whole` keeps the original clip untouched and wins over both.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BoundaryError("%s: the manual boundary is invalid." % label)

    for name in raw:
        if name not in _OVERRIDE_KEYS:
            raise BoundaryError(
                '%s: "%s" is not a manual boundary field.' % (label, name)
            )

    keep_whole = raw.get("keep_whole")
    if keep_whole is not None and not isinstance(keep_whole, bool):
        raise BoundaryError('%s: "keep whole clip" must be true or false.' % label)

    cleaned: dict = {}
    for name, side in (("start_seconds", "start"), ("end_seconds", "end")):
        value = raw.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            raise BoundaryError("%s: the manual %s must be a number of seconds." % (label, side))
        value = float(value)
        if value < 0 or value > MAX_OVERRIDE_SECONDS:
            raise BoundaryError("%s: the manual %s cannot be negative." % (label, side))
        cleaned[name] = round(value, 3)

    if keep_whole:
        return {"keep_whole": True}

    if (
        "start_seconds" in cleaned
        and "end_seconds" in cleaned
        and cleaned["end_seconds"] - cleaned["start_seconds"] < MIN_RETAINED_SECONDS
    ):
        raise BoundaryError(
            "%s: the manual end (%.2f s) must be at least %.1f s after the manual "
            "start (%.2f s)."
            % (label, cleaned["end_seconds"], MIN_RETAINED_SECONDS, cleaned["start_seconds"])
        )

    return cleaned or None


def validate_overrides(raw: Any, known_ids: set[str] | None = None) -> dict:
    """Every clip's manual boundary, keyed by source id. Empty entries vanish.

    With `known_ids`, an override for a source the project no longer has is
    dropped rather than refused: removing a take must not make the saved form
    unusable.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise BoundaryError("The manual boundaries are invalid.")

    validated: dict = {}
    for source_id, entry in raw.items():
        if not isinstance(source_id, str):
            raise BoundaryError("The manual boundaries are invalid.")
        if known_ids is not None and source_id not in known_ids:
            continue
        override = validate_override(entry)
        if override is not None:
            validated[source_id] = override

    return validated


def check_override_fits(override: dict | None, duration: float, filename: str) -> None:
    """Refuse a manual boundary that lies outside the file it belongs to."""
    if not override or override.get("keep_whole"):
        return

    start = override.get("start_seconds")
    end = override.get("end_seconds")
    # A little slack: durations are shown rounded, and typing the shown value
    # must not be refused for being four milliseconds past the end.
    slack = 0.05

    if start is not None and start > duration - MIN_RETAINED_SECONDS:
        raise BoundaryError(
            '"%s": the manual start (%.2f s) is at or past the end of the clip '
            "(%.2f s)." % (filename, start, duration)
        )
    if end is not None and end > duration + slack:
        raise BoundaryError(
            '"%s": the manual end (%.2f s) is past the end of the clip (%.2f s).'
            % (filename, end, duration)
        )
    if end is not None and end < MIN_RETAINED_SECONDS:
        raise BoundaryError(
            '"%s": the manual end (%.2f s) leaves nothing to keep.' % (filename, end)
        )


# --- decoding the envelope ---------------------------------------------------


def build_envelope_command(
    path: str, start: float, length: float, channels: int | None = None
) -> list[str]:
    """Decode a stretch of the audio to raw PCM on stdout. Video is skipped.

    `-ss` before `-i` with decoding is sample-accurate: FFmpeg seeks to the
    packet before the position and discards what precedes it.

    The channels are left as recorded and the loudest of them is taken per
    frame. Remixing would change the level being measured — FFmpeg attenuates
    a mono track by 3 dB when it spreads it over two channels — and the
    threshold is only meaningful against the level that was recorded. Only a
    track whose channel count is unknown is mixed down, to one channel.
    """
    layout = [] if channels else ["-ac", "1"]
    return [
        "ffmpeg.exe", "-hide_banner", "-nostdin", "-v", "error",
        "-ss", "%.6f" % max(0.0, start),
        "-t", "%.6f" % max(0.0, length),
        "-i", str(path),
        "-vn", "-sn", "-dn",
        "-map", "0:a:0",
        *layout,
        "-ar", str(ENVELOPE_SAMPLE_RATE),
        "-f", "s16le", "-",
    ]


def envelope_from_pcm(data: bytes, channels: int = 1) -> list[float]:
    """Peak of every 10 ms of interleaved 16-bit PCM, as a share of full scale."""
    frame = _SAMPLES_PER_FRAME * max(1, channels)
    usable = len(data) - (len(data) % 2)
    samples = array("h")
    samples.frombytes(data[:usable])
    if samples.itemsize != 2:  # pragma: no cover - 'h' is two bytes everywhere
        raise BoundaryError("Unsupported platform for audio analysis.", status_code=500)

    envelope: list[float] = []
    for index in range(0, len(samples), frame):
        chunk = samples[index : index + frame]
        # A trailing sliver is a partial frame; half a frame is still evidence.
        if len(chunk) < frame // 2:
            break
        envelope.append(max(max(chunk), -min(chunk)) / 32768.0)

    return envelope


def decode_envelope(
    path: str,
    start: float,
    length: float,
    cancelled: Callable[[], bool] | None = None,
    channels: int | None = None,
) -> list[float]:
    """Decode `length` seconds of audio from `start` into a peak envelope.

    Raises `processes.ProcessCancelled` when cancelled: the decoder is killed,
    not left running.
    """
    command = build_envelope_command(path, start, length, channels)

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as error:
        raise BoundaryError(
            'Tool "ffmpeg.exe" was not found on PATH. Check the "Tools" tab.',
            status_code=500,
        ) from error
    except OSError as error:
        raise BoundaryError("FFmpeg could not be run: %s" % error, status_code=500) from error

    chunks: list[bytes] = []
    stream = process.stdout
    try:
        while True:
            if cancelled is not None and cancelled():
                processes.terminate_tree(process)
                raise processes.ProcessCancelled()
            block = stream.read(1 << 16) if stream is not None else b""
            if not block:
                break
            chunks.append(block)
    finally:
        if process.poll() is None and cancelled is not None and cancelled():
            processes.terminate_tree(process)

    errors = b""
    try:
        if process.stderr is not None:
            errors = process.stderr.read() or b""
        process.wait(timeout=processes.KILL_GRACE_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        processes.terminate_tree(process)
    finally:
        for handle in (process.stdout, process.stderr):
            try:
                if handle is not None:
                    handle.close()
            except OSError:  # pragma: no cover
                pass

    data = b"".join(chunks)
    if process.returncode not in (0, None) and not data:
        detail = errors.decode("utf-8", errors="replace").strip().splitlines()
        raise BoundaryError(
            "The audio of %s could not be decoded for analysis: %s"
            % (os.path.basename(str(path)), detail[-1] if detail else "no detail")
        )

    return envelope_from_pcm(data, channels or 1)


# --- detection ----------------------------------------------------------------


def find_runs(envelope: list[float], threshold: float, base_seconds: float = 0.0) -> list[tuple]:
    """Stretches of activity, as `(start, end)` in seconds. `end` is exclusive.

    Active frames closer together than `BRIDGE_SECONDS` belong to one run.
    """
    bridge_frames = int(round(BRIDGE_SECONDS / FRAME_SECONDS))
    runs: list[tuple] = []
    start: int | None = None
    last = -1

    for index, value in enumerate(envelope):
        if value < threshold:
            continue
        if start is None:
            start = index
        elif index - last - 1 > bridge_frames:
            runs.append((start, last + 1))
            start = index
        last = index

    if start is not None:
        runs.append((start, last + 1))

    return [
        (base_seconds + first * FRAME_SECONDS, base_seconds + end * FRAME_SECONDS)
        for first, end in runs
    ]


def qualifying(runs: list[tuple], min_activity: float) -> list[tuple]:
    # Half a frame of slack, so a run of exactly the minimum is not lost to
    # floating-point rounding.
    return [run for run in runs if run[1] - run[0] >= min_activity - FRAME_SECONDS / 2]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _statistics(windows: list[dict], threshold: float, min_activity: float) -> dict:
    """Levels inside and outside the qualifying runs of what was decoded."""
    active: list[float] = []
    inactive: list[float] = []
    peak = 0.0

    for window in windows:
        envelope = window["envelope"]
        base = window["start"]
        if envelope:
            peak = max(peak, max(envelope))
        spans = qualifying(find_runs(envelope, threshold, base), min_activity)
        cursor = 0
        for first, end in spans:
            first_index = int(round((first - base) / FRAME_SECONDS))
            end_index = int(round((end - base) / FRAME_SECONDS))
            inactive.extend(envelope[cursor:first_index])
            active.extend(envelope[first_index:end_index])
            cursor = end_index
        inactive.extend(envelope[cursor:])

    def rounded(value: float | None) -> float | None:
        return None if value is None else round(value, 5)

    return {
        "peak": rounded(peak) if windows else None,
        # The typical level outside the detected activity: room tone, hiss.
        "background_level": rounded(_median(inactive)),
        # The typical level inside it.
        "activity_level": rounded(_median(active)),
    }


def detect(
    path: str,
    duration: float,
    has_audio: bool,
    settings: dict,
    *,
    cancelled: Callable[[], bool] | None = None,
    initial_window: float = INITIAL_WINDOW_SECONDS,
    decoder: Callable[..., list[float]] | None = None,
    channels: int | None = None,
) -> dict:
    """Find the first and last qualifying activity of one file.

    Independent of padding and of manual overrides — those are applied by
    `resolve`, so changing them never needs another decode. `decoder` is the
    seam the tests use to feed a synthetic envelope.
    """
    if decoder is not None:
        decode = decoder
    else:
        def decode(path, start, length, cancelled):
            return decode_envelope(path, start, length, cancelled, channels)

    threshold = settings["detection_threshold"]
    min_activity = settings["min_activity_seconds"]
    started = time.monotonic()

    result: dict = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "duration_seconds": round(duration, 6),
        "has_audio": bool(has_audio),
        "detection_threshold": threshold,
        "min_activity_seconds": min_activity,
        "status": STATUS_NO_AUDIO,
        "activity_start_seconds": None,
        "activity_end_seconds": None,
        "starts_immediately": False,
        "runs_to_end": False,
        "stats": {"peak": None, "background_level": None, "activity_level": None},
        "windows": [],
        "decoded_seconds": 0.0,
        "full_scan": False,
        "expansions": 0,
        "analysis_seconds": 0.0,
        "created_at": _now(),
    }

    if not has_audio or duration <= 0:
        return result

    decoded = 0.0
    expansions = 0
    full: dict | None = None
    total_frames = int(duration / FRAME_SECONDS + 0.5)
    first_frames = max(1, int(initial_window / FRAME_SECONDS + 0.5))

    def read(first: int, last: int | None) -> list[float]:
        """The envelope of frames `[first, last)`; `last=None` reads to the end.

        A piece that stops short of the end of the file is fitted to exactly
        its frame count, so pieces decoded separately line up on the same
        10 ms grid as one continuous decode would.
        """
        nonlocal decoded
        start = first * FRAME_SECONDS
        length = (duration if last is None else last * FRAME_SECONDS) - start
        envelope = decode(path, start, length, cancelled)
        decoded += length
        if last is not None:
            wanted = last - first
            envelope = (envelope + [0.0] * wanted)[:wanted]
        return envelope

    def window(first: int, envelope: list[float]) -> dict:
        start = first * FRAME_SECONDS
        return {
            "start": start,
            "end": min(duration, start + len(envelope) * FRAME_SECONDS),
            "envelope": envelope,
        }

    def spans(piece: dict) -> list[tuple]:
        return qualifying(
            find_runs(piece["envelope"], threshold, piece["start"]), min_activity
        )

    # --- the start: the first qualifying run, searched from the beginning ----

    if total_frames <= first_frames * 2:
        # Short enough that two windows would cover it anyway: one decode.
        full = head = window(0, read(0, None))
    else:
        reached = first_frames
        head = window(0, read(0, reached))
        while not spans(head):
            # Nothing qualifies in what was decoded. That is not evidence of
            # silence beyond it — and the window's edge is not a boundary —
            # so decode the next stretch and look again.
            expansions += 1
            target = int(reached * WINDOW_GROWTH)
            if target >= total_frames:
                full = head = window(0, head["envelope"] + read(reached, None))
                break
            head = window(0, head["envelope"] + read(reached, target))
            reached = target

    head_spans = spans(head)

    # --- the end: the last qualifying run, searched from the end -------------

    if full is not None:
        tail = full
    else:
        head_frames = len(head["envelope"])
        covered = first_frames
        begin = total_frames - covered

        if begin <= head_frames:
            # The start was found so late that the last window reaches into
            # what is already decoded: finish the one pass instead.
            full = head = tail = window(0, head["envelope"] + read(head_frames, None))
        else:
            tail = window(begin, read(begin, None))
            while not spans(tail):
                expansions += 1
                covered = int(covered * WINDOW_GROWTH)
                earlier = max(0, total_frames - covered)
                if earlier <= head_frames:
                    # The two windows meet. Decode only the gap between them,
                    # so no stretch of audio is ever decoded twice.
                    gap = read(head_frames, begin) if begin > head_frames else []
                    full = head = tail = window(
                        0, head["envelope"] + gap + tail["envelope"]
                    )
                    break
                tail = window(earlier, read(earlier, begin) + tail["envelope"])
                begin = earlier

    tail_spans = spans(tail)

    windows = [full] if full is not None else [head, tail]

    if head_spans and tail_spans and tail_spans[-1][1] <= head_spans[0][0]:
        # Cannot happen on one consistent timeline; if a container's timestamps
        # disagree with themselves, trust one full decode rather than guess.
        full = tail = head = window(0, read(0, None))
        head_spans = tail_spans = spans(head)
        windows = [head]

    result["windows"] = [
        {"start_seconds": round(w["start"], 3), "end_seconds": round(w["end"], 3)}
        for w in windows
    ]
    result["decoded_seconds"] = round(decoded, 3)
    result["full_scan"] = full is not None
    result["expansions"] = expansions
    result["stats"] = _statistics(windows, threshold, min_activity)
    result["analysis_seconds"] = round(time.monotonic() - started, 3)

    if not head_spans or not tail_spans:
        result["status"] = STATUS_NO_ACTIVITY
        return result

    first = max(0.0, head_spans[0][0])
    last = min(duration, tail_spans[-1][1])

    result["status"] = STATUS_DETECTED
    result["activity_start_seconds"] = round(first, 3)
    result["activity_end_seconds"] = round(last, 3)
    result["starts_immediately"] = first <= EDGE_TOLERANCE_SECONDS
    result["runs_to_end"] = last >= duration - EDGE_TOLERANCE_SECONDS
    return result


# --- resolving the retained interval -----------------------------------------


def _usable_frame_rate(frame_rate: Any) -> float | None:
    # Some containers report a time base (90000) where a frame rate belongs.
    if isinstance(frame_rate, (int, float)) and 1.0 <= frame_rate <= 240.0:
        return float(frame_rate)
    return None


def _snap(value: float, frame_rate: float | None, *, up: bool) -> float:
    """Move a time outward to the frame grid, so video and audio start together."""
    if frame_rate is None:
        return value
    frames = value * frame_rate
    whole = int(frames + (1 - 1e-6 if up else 1e-6))
    return whole / frame_rate


def _warning(code: str, severity: str, message: str) -> dict:
    return {"code": code, "severity": severity, "message": message}


def resolve(
    detection: dict,
    settings: dict,
    override: dict | None = None,
    frame_rate: Any = None,
) -> dict:
    """The one continuous interval of the source that will be kept.

    Used by the analysis job, every preview sample and the final render, so
    all three agree by construction. Pure: no file is touched.
    """
    duration = float(detection["duration_seconds"])
    status = detection["status"]
    lead = settings["leading_padding_seconds"]
    trail = settings["trailing_padding_seconds"]
    override = override or {}
    rate = _usable_frame_rate(frame_rate)
    warnings: list[dict] = []

    activity_start = detection.get("activity_start_seconds")
    activity_end = detection.get("activity_end_seconds")
    detected = status == STATUS_DETECTED

    if override.get("keep_whole"):
        start, start_origin = 0.0, ORIGIN_KEPT
        end, end_origin = duration, ORIGIN_KEPT
    else:
        if override.get("start_seconds") is not None:
            start, start_origin = float(override["start_seconds"]), ORIGIN_MANUAL
        elif detected:
            start, start_origin = activity_start - lead, ORIGIN_DETECTED
            if start < 0:
                start = 0.0
                if not detection.get("starts_immediately") and lead > 0:
                    warnings.append(_warning(
                        "leading_padding_clamped", SEVERITY_INFO,
                        "The padding before the start reaches the beginning of "
                        "the clip, so less than %.2f s could be kept." % lead,
                    ))
        else:
            start, start_origin = 0.0, ORIGIN_SOURCE_LIMIT

        if override.get("end_seconds") is not None:
            end, end_origin = min(float(override["end_seconds"]), duration), ORIGIN_MANUAL
        elif detected:
            end, end_origin = activity_end + trail, ORIGIN_DETECTED
            if end > duration:
                end = duration
                if not detection.get("runs_to_end") and trail > 0:
                    warnings.append(_warning(
                        "trailing_padding_clamped", SEVERITY_INFO,
                        "The padding after the end reaches the end of the clip, "
                        "so less than %.2f s could be kept." % trail,
                    ))
        else:
            end, end_origin = duration, ORIGIN_SOURCE_LIMIT

    start = max(0.0, min(_snap(start, rate, up=False), duration))
    end = max(0.0, min(_snap(end, rate, up=True), duration))

    if end - start < MIN_RETAINED_SECONDS:
        raise BoundaryError(
            "The start (%.2f s) and end (%.2f s) leave less than %.1f s to keep. "
            "Adjust the manual boundary or keep the whole clip."
            % (start, end, MIN_RETAINED_SECONDS)
        )

    stats = detection.get("stats") or {}
    threshold = detection.get("detection_threshold", settings["detection_threshold"])
    automatic = ORIGIN_DETECTED in (start_origin, end_origin) or ORIGIN_SOURCE_LIMIT in (
        start_origin, end_origin
    )

    if automatic:
        if status == STATUS_NO_AUDIO:
            warnings.append(_warning(
                "no_audio", SEVERITY_WARN,
                "This clip has no audio track, so there is nothing to detect. The "
                "whole clip is kept unless you set the start and end by hand.",
            ))
        elif status == STATUS_NO_ACTIVITY:
            peak = stats.get("peak")
            warnings.append(_warning(
                "no_activity", SEVERITY_WARN,
                "No sound stayed above the threshold (%s) for at least %s s%s. No "
                "boundary was found, so nothing is trimmed: the whole clip is kept. "
                "Lower the threshold, or set the start and end by hand."
                % (
                    threshold,
                    detection.get("min_activity_seconds"),
                    "" if peak is None else " — the loudest moment measured %.3f" % peak,
                ),
            ))
        else:
            background = stats.get("background_level")
            level = stats.get("activity_level")
            if background is not None and background >= threshold * 0.5:
                warnings.append(_warning(
                    "background_near_threshold", SEVERITY_WARN,
                    "The background level (%.3f) is close to the threshold (%s), so "
                    "the boundary may be following noise. Listen to the previews, or "
                    "raise the threshold." % (background, threshold),
                ))
            if level is not None and level < threshold * 1.5:
                warnings.append(_warning(
                    "activity_near_threshold", SEVERITY_WARN,
                    "The detected sound is only just above the threshold (typical "
                    "level %.3f against %s), so quiet words at either end may have "
                    "been missed. Listen to the previews, or lower the threshold."
                    % (level, threshold),
                ))
            if start_origin == ORIGIN_DETECTED and detection.get("starts_immediately"):
                warnings.append(_warning(
                    "starts_immediately", SEVERITY_INFO,
                    "Sound is already above the threshold at the first frame, so "
                    "nothing is removed from the start.",
                ))
            if end_origin == ORIGIN_DETECTED and detection.get("runs_to_end"):
                warnings.append(_warning(
                    "runs_to_end", SEVERITY_INFO,
                    "Sound is still above the threshold at the last frame, so "
                    "nothing is removed from the end.",
                ))

    if duration < 1.0:
        warnings.append(_warning(
            "very_short_clip", SEVERITY_INFO,
            "This clip is under a second long (%.2f s)." % duration,
        ))

    return {
        "status": status,
        "start_seconds": round(start, 6),
        "end_seconds": round(end, 6),
        "start_origin": start_origin,
        "end_origin": end_origin,
        "source_duration_seconds": round(duration, 6),
        "retained_seconds": round(end - start, 6),
        "removed_leading_seconds": round(start, 6),
        "removed_trailing_seconds": round(max(duration - end, 0.0), 6),
        "activity_start_seconds": activity_start,
        "activity_end_seconds": activity_end,
        "leading_padding_seconds": lead,
        "trailing_padding_seconds": trail,
        "override": dict(override) or None,
        "warnings": warnings,
        "needs_review": any(w["severity"] == SEVERITY_WARN for w in warnings),
        "stats": stats,
    }


# --- the analysis cache -------------------------------------------------------
#
# Detection depends only on the file's bytes and the two detection settings, so
# its result is kept per project and reused by the state endpoint, the preview
# samples and the final render. Padding and overrides are not part of the key.


def analysis_directory(project_id: str) -> Path:
    return storage.project_directory(project_id) / "intermediates" / ANALYSIS_DIRECTORY


def analysis_key(digest: str, settings: dict) -> str:
    canonical = json.dumps(
        [
            ALGORITHM_VERSION,
            digest,
            settings["detection_threshold"],
            settings["min_activity_seconds"],
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def read_analysis(project_id: str, digest: str, settings: dict) -> dict | None:
    path = analysis_directory(project_id) / ("%s.json" % analysis_key(digest, settings))
    try:
        data = storage.read_json(path)
    except (OSError, ValueError):
        return None

    if (
        not isinstance(data, dict)
        or data.get("algorithm_version") != ALGORITHM_VERSION
        or data.get("source_digest") != digest
    ):
        return None
    return data


def write_analysis(project_id: str, digest: str, settings: dict, detection: dict) -> None:
    path = analysis_directory(project_id) / ("%s.json" % analysis_key(digest, settings))
    try:
        storage.write_json_atomic(path, detection)
    except OSError:
        # A cache that cannot be written costs a repeated decode, nothing more.
        pass


def _channel_count(audio: dict | None) -> int | None:
    channels = (audio or {}).get("channels")
    if isinstance(channels, int) and not isinstance(channels, bool) and 1 <= channels <= 16:
        return channels
    return None


def analyse_source(
    project_id: str,
    source: dict,
    settings: dict,
    *,
    cancelled: Callable[[], bool] | None = None,
    force: bool = False,
) -> tuple[dict, bool]:
    """Detection for one source snapshot, from the cache when it is there.

    Returns `(detection, reused)`.
    """
    digest = source["fingerprint"]["digest"]

    if not force:
        cached = read_analysis(project_id, digest, settings)
        if cached is not None:
            return cached, True

    detection = detect(
        source["path"],
        float(source["duration_seconds"]),
        bool(source.get("audio")),
        settings,
        cancelled=cancelled,
        channels=_channel_count(source.get("audio")),
    )
    detection["source_digest"] = digest
    detection["frame_rate"] = (source.get("video") or {}).get("frame_rate")
    write_analysis(project_id, digest, settings, detection)
    return detection, False


# --- fingerprints, cached ----------------------------------------------------

_fingerprint_cache: dict[tuple, dict] = {}


def cached_fingerprint(path: str) -> dict:
    """`media.fingerprint`, remembered while the file's size and mtime hold.

    The state endpoint runs on every form change; re-reading two megabytes per
    take each time would be wasted work.
    """
    try:
        status = os.stat(path)
    except OSError as error:
        raise media.MediaError("The file could not be read: %s" % error) from error

    key = (path, status.st_size, status.st_mtime_ns)
    if key not in _fingerprint_cache:
        _fingerprint_cache[key] = media.fingerprint(path)
    return _fingerprint_cache[key]
