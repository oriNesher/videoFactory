"""Subtitles with Whisper: settings, line splitting, SRT files and records.

The working in-app version of `legacy/Whisper/transcribe.py`, which turned the
final edited video into short Hebrew SRT subtitles. What is kept from it: the
transcription recipe and the splitting rules — a line ends
at a word limit, a character limit, a pause, or punctuation once it has enough
words. Their defaults are the script's.

What changes: the input is a clip a cutting run produced (named by run id and
output id, never by path), the work is a queued, cancellable job, and the SRT
lands in that run's own directory next to the clip it belongs to:

    …\\cuts\\<run-id>\\subtitles\\
      clip-0001.srt               the subtitles, UTF-8 with BOM for Premiere
      clip-0001.transcript.json   Whisper's words with their timestamps
      clip-0001.json              what produced the SRT, and the last attempt

Transcribing a clip again replaces its SRT, but only once the new one is
complete: a failed or cancelled attempt leaves the previous file in place.
"""

import importlib.util
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import cutting, jobs, storage

RECORD_SCHEMA_VERSION = 1

SUBTITLES_DIRECTORY = "subtitles"

# What a job transcribes: the run's merged video (merging first when needed),
# or clips named one by one.
TARGET_MERGED = "merged"
TARGET_OUTPUTS = "outputs"
SETTINGS_KEY = "subtitles"
MAX_OUTPUTS_PER_JOB = cutting.MAX_SOURCES_PER_RUN

ATTEMPT_RUNNING = "running"
ATTEMPT_SUCCEEDED = "succeeded"
ATTEMPT_FAILED = "failed"
ATTEMPT_CANCELLED = "cancelled"
# Derived, never stored: the record says running but its job has stopped.
ATTEMPT_INTERRUPTED = "interrupted"

# --- model and language ------------------------------------------------------

# One model, chosen by the user: ivrit.ai's large-v3-turbo fine-tuned on
# Hebrew, in the CTranslate2 format faster-whisper loads directly. It is far
# more accurate on Hebrew than the generic models at about Medium's speed on a
# CPU. faster-whisper downloads it from Hugging Face on first use (1.6 GB) into
# the standard cache. To change it, change these three lines.
MODEL = "ivrit-ai/whisper-large-v3-turbo-ct2"
MODEL_LABEL = "ivrit.ai Whisper large-v3 turbo (Hebrew)"
MODEL_DOWNLOAD = "about 1.6 GB"

# Every video is in Hebrew; detection would only add a way to be wrong.
LANGUAGE = "he"

# --- line settings -----------------------------------------------------------

SETTINGS_SPEC: dict[str, dict] = {
    "max_words": {
        "type": "integer",
        "default": 5,
        "min": 1,
        "max": 20,
        "step": 1,
        "unit": "words",
        "label": "Words per line",
        "description": "The most words shown at once.",
    },
    "max_characters": {
        "type": "integer",
        "default": 30,
        "min": 5,
        "max": 120,
        "step": 1,
        "unit": "characters",
        "label": "Characters per line",
        "description": "A longer line is split before this.",
    },
    "min_words": {
        "type": "integer",
        "default": 2,
        "min": 1,
        "max": 20,
        "step": 1,
        "unit": "words",
        "label": "Words before a punctuation split",
        "description": "A line ends at a comma or full stop only after this many words.",
    },
    "pause_seconds": {
        "type": "number",
        "default": 0.45,
        "min": 0.0,
        "max": 5.0,
        "step": 0.05,
        "unit": "seconds",
        "label": "Split on pause",
        "description": "A pause at least this long starts a new line.",
    },
}

_SENTENCE_ENDINGS = (".", "!", "?", "…", ",", ":", ";")


class SubtitleError(storage.ProjectError):
    """A subtitle problem the user can act on."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# --- availability -----------------------------------------------------------


def whisper_installed() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def _hugging_face_cache() -> Path:
    """Where faster-whisper keeps downloaded models, honouring the overrides."""
    explicit = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    if explicit:
        return Path(explicit)
    home = os.environ.get("HF_HOME")
    if home:
        return Path(home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def model_downloaded(repository: str = MODEL) -> bool:
    folder = "models--" + repository.replace("/", "--")
    return (_hugging_face_cache() / folder / "snapshots").is_dir()


# --- settings ---------------------------------------------------------------


def default_settings() -> dict:
    return {name: spec["default"] for name, spec in SETTINGS_SPEC.items()}


def settings_catalog() -> dict:
    return {
        "installed": whisper_installed(),
        "defaults": default_settings(),
        "parameters": [{"name": name, **spec} for name, spec in SETTINGS_SPEC.items()],
        "model": {
            "id": MODEL,
            "label": MODEL_LABEL,
            "download": MODEL_DOWNLOAD,
            "downloaded": model_downloaded(),
        },
        "language": "Hebrew",
    }


def validate_settings(raw: Any) -> dict:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SubtitleError("The subtitle settings are invalid.")

    for name in raw:
        if name not in SETTINGS_SPEC:
            raise SubtitleError(
                'Setting "%s" is not recognised. Available settings: %s.'
                % (name, ", ".join(SETTINGS_SPEC))
            )

    validated: dict = {}
    for name, spec in SETTINGS_SPEC.items():
        value = raw.get(name)
        if value is None:
            validated[name] = spec["default"]
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            raise SubtitleError('Setting "%s" must be a number.' % spec["label"])
        if spec["type"] == "integer":
            if float(value) != int(value):
                raise SubtitleError('Setting "%s" must be a whole number.' % spec["label"])
            value = int(value)
        else:
            value = round(float(value), 4)
        if value < spec["min"] or value > spec["max"]:
            raise SubtitleError(
                'Setting "%s" must be between %s and %s (%s).'
                % (spec["label"], spec["min"], spec["max"], spec["unit"])
            )
        validated[name] = value

    return validated


def read_settings(project: dict) -> dict:
    stored = project.get("settings", {}).get(SETTINGS_KEY)
    if not isinstance(stored, dict):
        return {"settings": default_settings()}
    try:
        return {"settings": validate_settings(stored.get("settings"))}
    except SubtitleError:
        # An older or hand-edited block must never stop a project opening.
        return {"settings": default_settings()}


def save_settings(project_id: str, raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise SubtitleError("The subtitle settings are invalid.")

    project = storage.read_project(project_id)
    project["settings"][SETTINGS_KEY] = {
        "settings": validate_settings(raw.get("settings")),
        "saved_at": now(),
    }
    return read_settings(storage.write_project(project))


# --- the job's input snapshot -----------------------------------------------


def describe_output(project_id: str, run_id: str, output_id: Any) -> dict:
    """One video of a run, resolved inside the run's own directory."""
    path, entry = cutting.resolve_output(project_id, run_id, output_id)
    return {
        "output_id": output_id,
        "filename": entry.get("filename") or path.name,
        "source_filename": entry.get("source_filename"),
        "path": str(path),
        "duration_seconds": entry.get("duration_seconds"),
    }


def build_job_input(project_id: str, raw: Any) -> dict:
    """Validate a subtitle request and freeze what the job will process.

    Outputs are named by run id and output id and resolved inside that run's
    own directory by the cutting module — the same lookup that serves them for
    playback — so a request can never point Whisper at an arbitrary file.
    """
    if not isinstance(raw, dict):
        raise SubtitleError("The subtitle request is invalid.")

    if not whisper_installed():
        raise SubtitleError(
            "Whisper is not installed. Stop the backend and run: "
            ".venv\\Scripts\\python.exe -m pip install -r requirements.txt",
            status_code=503,
        )

    storage.read_project(project_id)
    run_id = cutting.validate_run_id(raw.get("run_id"))
    manifest = cutting.read_manifest(project_id, run_id)

    output_ids = raw.get("output_ids")

    # No clips named: the run's merged video. Resolved when the job runs, not
    # now, because a run cut before merging became automatic has none yet —
    # the job merges its clips first, in the background.
    if output_ids is None:
        if not cutting.has_merged_video(manifest):
            cutting.check_mergeable(manifest)
        return {
            "run_id": manifest["run_id"],
            "target": TARGET_MERGED,
            "outputs": [],
            "model": MODEL,
            "language": LANGUAGE,
            "settings": validate_settings(raw.get("settings")),
        }

    if not isinstance(output_ids, list) or not output_ids:
        raise SubtitleError("Choose at least one clip to transcribe.")
    if len(output_ids) > MAX_OUTPUTS_PER_JOB:
        raise SubtitleError("Up to %d clips can be transcribed at once." % MAX_OUTPUTS_PER_JOB)
    if len(set(map(str, output_ids))) != len(output_ids):
        raise SubtitleError("The same clip was chosen more than once.")

    outputs = [describe_output(project_id, run_id, output_id) for output_id in output_ids]

    # Model and language are frozen into the snapshot, so a retry of an old job
    # still runs with what it was submitted with.
    return {
        "run_id": manifest["run_id"],
        "target": TARGET_OUTPUTS,
        "outputs": outputs,
        "model": MODEL,
        "language": LANGUAGE,
        "settings": validate_settings(raw.get("settings")),
    }


# --- splitting words into subtitle lines ------------------------------------


@dataclass
class Line:
    start: float
    end: float
    text: str


def clean_text(text: str) -> str:
    return " ".join(str(text).strip().split())


def _ends_phrase(text: str) -> bool:
    return text.rstrip().endswith(_SENTENCE_ENDINGS)


def split_segment(segment: dict, settings: dict) -> list[Line]:
    """One Whisper segment → short subtitle lines. The legacy rules, unchanged.

    A line is closed when it reaches the word limit; when the next word would
    push it past the character limit; before a pause of at least
    `pause_seconds`; or at punctuation, once it holds `min_words` words.
    """
    max_words = settings["max_words"]
    max_characters = settings["max_characters"]
    min_words = settings["min_words"]
    pause = settings["pause_seconds"]

    words = [
        word for word in segment.get("words") or [] if clean_text(word.get("word", ""))
    ]

    if not words:
        text = clean_text(segment.get("text", ""))
        return [Line(segment["start"], segment["end"], text)] if text else []

    lines: list[Line] = []
    current: list[str] = []
    start: float | None = None
    end: float | None = None

    def flush() -> None:
        nonlocal current, start, end
        if current and start is not None and end is not None:
            lines.append(Line(start, end, clean_text(" ".join(current))))
        current, start, end = [], None, None

    for index, word in enumerate(words):
        text = clean_text(word["word"])
        following = words[index + 1] if index + 1 < len(words) else None

        proposed = current + [text]
        if current and (
            len(proposed) > max_words
            or len(clean_text(" ".join(proposed))) > max_characters
        ):
            flush()

        if start is None:
            start = word["start"]
        current.append(text)
        end = word["end"]

        if (
            len(current) >= max_words
            or len(clean_text(" ".join(current))) >= max_characters
            or (following is not None and following["start"] - word["end"] >= pause)
            or (len(current) >= min_words and _ends_phrase(text))
            or following is None
        ):
            flush()

    return lines


def build_lines(transcript: dict, settings: dict) -> list[Line]:
    lines: list[Line] = []
    for segment in transcript.get("segments") or []:
        lines.extend(split_segment(segment, settings))
    return [line for line in lines if line.text]


def _timestamp(seconds: float, separator: str) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1_000)
    return "%02d:%02d:%02d%s%03d" % (hours, minutes, secs, separator, millis)


# RIGHT-TO-LEFT MARK: invisible, and a strong right-to-left character. Many
# subtitle renderers — Premiere among them — lay a line out left-to-right, so a
# Hebrew line's final full stop or comma lands on the wrong side, and a line
# that starts with a number or a Latin word runs the wrong way. A mark at each
# end makes every line right-to-left wherever it is shown.
RLM = "‏"


def rtl(text: str) -> str:
    return "%s%s%s" % (RLM, text, RLM)


def render_srt(lines: list[Line]) -> str:
    blocks = [
        "%d\n%s --> %s\n%s\n"
        % (number, _timestamp(line.start, ","), _timestamp(line.end, ","), rtl(line.text))
        for number, line in enumerate(lines, start=1)
    ]
    return "\n".join(blocks)


_SRT_TIMESTAMP = re.compile(r"(\d{2}:\d{2}:\d{2}),(\d{3})")


def srt_to_vtt(srt: str) -> str:
    """WebVTT for the browser's <track>: the same cues, with the dot separator."""
    body = _SRT_TIMESTAMP.sub(r"\1.\2", srt.lstrip("﻿").replace("\r\n", "\n"))
    return "WEBVTT\n\n" + body


# --- files and records ------------------------------------------------------


def subtitles_directory(project_id: str, run_id: str) -> Path:
    return cutting.run_directory(project_id, run_id) / SUBTITLES_DIRECTORY


def srt_path(project_id: str, run_id: str, output_id: str) -> Path:
    return subtitles_directory(project_id, run_id) / ("%s.srt" % output_id)


def transcript_path(project_id: str, run_id: str, output_id: str) -> Path:
    return subtitles_directory(project_id, run_id) / ("%s.transcript.json" % output_id)


def record_path(project_id: str, run_id: str, output_id: str) -> Path:
    return subtitles_directory(project_id, run_id) / ("%s.json" % output_id)


def read_record(project_id: str, run_id: str, output_id: str) -> dict:
    try:
        data = storage.read_json(record_path(project_id, run_id, output_id))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = {}
    return {
        "schema_version": RECORD_SCHEMA_VERSION,
        "output_id": output_id,
        "current": data.get("current") if isinstance(data.get("current"), dict) else None,
        "attempt": data.get("attempt") if isinstance(data.get("attempt"), dict) else None,
    }


def write_record(project_id: str, run_id: str, record: dict) -> None:
    path = record_path(project_id, run_id, record["output_id"])
    try:
        storage.write_json_atomic(path, record)
    except OSError as error:
        raise SubtitleError(
            "Saving the subtitle details failed: %s" % error, status_code=500
        ) from error


def write_text_atomic(path: Path, text: str, encoding: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.tmp" % path.stem[:24])
    with open(temporary, "w", encoding=encoding, newline="\n") as handle:
        handle.write(text)
    os.replace(temporary, path)


def _job_is_active(project_id: str, job_id: Any) -> bool:
    if not isinstance(job_id, str):
        return False
    try:
        return jobs.read_job(project_id, job_id)["status"] in jobs.ACTIVE_STATUSES
    except storage.ProjectError:
        return False


def describe_run_subtitles(project_id: str, run_id: str) -> dict:
    """Every clip of a run that has subtitles or an attempt, keyed by output id.

    An attempt still marked running whose job has stopped belonged to a
    backend that was shut down mid-transcription; it is reported as
    interrupted rather than left spinning forever.
    """
    directory = subtitles_directory(project_id, run_id)
    if not directory.is_dir():
        return {}

    described: dict = {}
    for path in directory.glob("*.json"):
        output_id = path.stem
        if "." in output_id:  # the .transcript.json files
            continue
        try:
            cutting.validate_output_id(output_id)
        except storage.ProjectError:
            continue

        record = read_record(project_id, run_id, output_id)
        attempt = record["attempt"]
        if (
            attempt
            and attempt.get("status") == ATTEMPT_RUNNING
            and not _job_is_active(project_id, attempt.get("job_id"))
        ):
            attempt = {**attempt, "status": ATTEMPT_INTERRUPTED}

        current = record["current"]
        available = bool(current) and srt_path(project_id, run_id, output_id).is_file()
        described[output_id] = {
            "output_id": output_id,
            "available": available,
            "current": current if available else None,
            "attempt": attempt,
        }

    return described


def resolve_srt(project_id: str, run_id: str, output_id: str) -> Path:
    """The SRT of one clip, or refuse. Ids only; the path is built here."""
    storage.validate_project_id(project_id)
    cutting.validate_run_id(run_id)
    cutting.validate_output_id(output_id)

    path = srt_path(project_id, run_id, output_id)
    if not path.is_file():
        raise cutting.RunNotFound("This clip has no subtitles yet.")
    return path
