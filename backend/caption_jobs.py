"""The captions module's queued jobs.

- `caption_transcription` — Whisper on one input video; writes an immutable
  transcript and caption revision 1.
- `caption_model_download` — fetch a Whisper model into the Hugging Face cache.
  The only step that needs the internet, and only ever on request.
- `caption_renderer_setup` — download Remotion's headless browser and build
  the render bundle, once, on request.
- `caption_export` — burn captions into an MP4 with Remotion, or write an SRT,
  from an immutable snapshot taken when the export was requested.

Exports live in the project's `exports` folder, one directory per run:

    exports/captions/<export-id>/
      export.json      status, what it was made from, verification
      snapshot.json    the exact props, captions and style rendered
      <name>.captioned.mp4  or  <name>.srt
      logs/render.log
"""

import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from . import caption_inputs, captions, cutting, jobs, media, processes, storage, whisper_service
from .config import REPO_ROOT, get_workspace_root

TRANSCRIPTION_JOB = "caption_transcription"
MODEL_DOWNLOAD_JOB = "caption_model_download"
RENDERER_SETUP_JOB = "caption_renderer_setup"
EXPORT_JOB = "caption_export"

CAPTION_JOB_TYPES = (TRANSCRIPTION_JOB, MODEL_DOWNLOAD_JOB, RENDERER_SETUP_JOB, EXPORT_JOB)

WORKER_SCRIPT = Path(__file__).with_name("whisper_worker.py")
FRONTEND_DIRECTORY = REPO_ROOT / "frontend"
RENDER_SCRIPT = FRONTEND_DIRECTORY / "scripts" / "render-captions.mjs"

EXPORT_SCHEMA_VERSION = 1
EXPORTS_SUBDIRECTORY = "captions"
FORMAT_MP4 = "mp4"
FORMAT_SRT = "srt"
FORMATS = (FORMAT_MP4, FORMAT_SRT)

EXPORT_RUNNING = "running"
EXPORT_SUCCEEDED = "succeeded"
EXPORT_FAILED = "failed"
EXPORT_CANCELLED = "cancelled"
EXPORT_INTERRUPTED = "interrupted"  # derived: running, but its job has stopped

# Browser-compatible H.264: the same quality settings the cutting module uses.
EXPORT_SETTINGS = {
    "container": "mp4",
    "video_codec": "h264",
    "pixel_format": "yuv420p",
    "crf": 18,
    "x264_preset": "medium",
    "audio_codec": "aac",
    "audio_bitrate": "192k",
}

# A rendered file may differ from the composition's length by one video frame
# plus AAC's encoder padding (up to two 1024-sample frames at 44.1 kHz ≈ 0.046 s).
AAC_PADDING_TOLERANCE_SECONDS = 0.05

PROGRESS_INTERVAL_SECONDS = 0.5


def _now() -> str:
    return captions.now()


# --- renderer readiness ----------------------------------------------------------


def node_executable() -> str | None:
    return shutil.which("node")


def browser_downloaded() -> bool:
    """Remotion keeps it under node_modules/.remotion when run from frontend/."""
    folder = FRONTEND_DIRECTORY / "node_modules" / ".remotion" / "chrome-headless-shell"
    if not folder.is_dir():
        return False
    return any(path.is_file() for path in folder.rglob("chrome-headless-shell*"))


def renderer_status() -> dict:
    node = node_executable()
    packages = (FRONTEND_DIRECTORY / "node_modules" / "@remotion" / "renderer").is_dir()
    browser = browser_downloaded()
    problems = []
    if node is None:
        problems.append("Node.js was not found on PATH. Install Node.js 22 or later.")
    if not packages:
        problems.append(
            "The Remotion packages are not installed. Run: cd frontend; npm.cmd install"
        )
    if not browser:
        problems.append(
            'The headless browser Remotion renders with is not downloaded. Press "Prepare '
            'renderer" (needs internet once, about 100 MB).'
        )
    return {
        "ready": not problems,
        "node": node,
        "packages_installed": packages,
        "browser_downloaded": browser,
        "problems": problems,
    }


def bundle_cache_directory() -> Path:
    return get_workspace_root() / "cache" / "remotion-bundles"


# --- transcription ---------------------------------------------------------------


def build_transcription_input(project_id: str, raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise captions.CaptionError("The transcription request is invalid.")
    settings = captions.validate_transcription_settings(raw.get("settings"))
    force = raw.get("force") is True

    if settings["device"] == "cuda" and captions.cuda_device_count() == 0:
        raise captions.CaptionError(captions.CUDA_UNAVAILABLE, status_code=503)
    if not captions.model_downloaded(settings["model"]):
        model = captions.MODELS_BY_ID[settings["model"]]
        raise captions.CaptionError(
            'The model "%s" is not downloaded yet. Press "Download model" first: it needs '
            "internet once (%s) and is saved in %s. Transcription itself then runs offline."
            % (model["label"], model["download"], captions.hugging_face_cache()),
            status_code=409,
        )

    described = caption_inputs.resolve_for_job(project_id, raw.get("input_key"))
    if not described["metadata"]["has_audio"]:
        raise captions.CaptionError(
            'The video "%s" has no audio track, so there is nothing to transcribe.'
            % described["filename"]
        )

    for record in jobs.list_jobs(project_id):
        if (
            record["type"] == TRANSCRIPTION_JOB
            and record["status"] in jobs.ACTIVE_STATUSES
            and record["input"].get("input", {}).get("key") == described["key"]
        ):
            raise captions.CaptionError("This video is already being transcribed.", status_code=409)

    project = storage.read_project(project_id)
    return {
        "input": described,
        "settings": settings,
        "cpu_threads": captions.default_cpu_threads(),
        "reuse_key": captions.reuse_key(described["fingerprint"]["digest"], settings),
        "grouping": captions.read_grouping(project),
        "force": force,
    }


def _duration_label(seconds: float) -> str:
    whole = int(max(0, seconds))
    return "%d:%02d" % divmod(whole, 60)


def _write_log(path: Path, text: str) -> str | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", errors="replace", newline="\n")
    except OSError:
        return None
    return str(path)


WHISPER_FAILURES = {
    "model_missing": 'The model is not in the local cache. Press "Download model" in the Captions section, then try again.',
    "device": "The GPU could not be used for transcription. Choose CPU, or install the NVIDIA driver, CUDA 12 and cuDNN 9.",
    "no_audio": "No audio could be decoded from this video.",
    "load": "The Whisper model could not be loaded.",
    "crashed": "The Whisper worker stopped unexpectedly.",
    "failed": "Transcription failed.",
}


def run_transcription(context: jobs.JobContext, job_input: dict | None = None) -> dict:
    """`job_input` is passed by a plan's action; the button's job uses its own."""
    project_id = context.project_id
    job_input = job_input if job_input is not None else context.input
    described = job_input["input"]
    settings = job_input["settings"]
    path = described["path"]

    context.progress("Checking the video…")
    if not os.path.isfile(path):
        raise jobs.JobFailed('The video "%s" is no longer where it was.' % described["filename"])
    try:
        current = caption_inputs.fingerprint(path)
    except storage.ProjectError as error:
        raise jobs.JobFailed(error.message) from error
    if current["digest"] != described["fingerprint"]["digest"]:
        raise jobs.JobFailed(
            "The video changed after this transcription was requested. Start it again "
            "so the captions are made for the video as it is now."
        )
    if not captions.model_downloaded(settings["model"]):
        raise jobs.JobFailed(WHISPER_FAILURES["model_missing"])

    transcript_id = captions.new_transcript_id()
    directory = captions.transcript_directory(project_id, transcript_id)
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / ".whisper-output.json"
    duration = float(described["metadata"]["duration_seconds"])
    state = {"loaded": False, "position": 0.0, "last": 0.0, "load_seconds": None, "reused": False}

    def report(force: bool = False) -> None:
        moment = time.monotonic()
        if not force and moment - state["last"] < PROGRESS_INTERVAL_SECONDS:
            return
        state["last"] = moment
        if not state["loaded"]:
            context.progress("Loading the Whisper model from the local cache…")
            return
        context.progress(
            "Transcribing — reached %s of %s. (Measured at the end of the last recognised "
            "phrase, so it moves in steps and jumps over silence.)"
            % (_duration_label(state["position"]), _duration_label(duration)),
            percent=min(99.0, 100.0 * state["position"] / duration) if duration > 0 else None,
        )

    def on_event(event: dict) -> None:
        kind = event.get("event")
        if kind == "ready":
            state["loaded"] = True
            state["reused"] = bool(event.get("reused"))
            state["load_seconds"] = event.get("load_seconds")
            report(force=True)
        elif kind == "progress":
            try:
                state["position"] = float(event.get("position") or 0.0)
            except (TypeError, ValueError):
                return
            report()
        elif kind == "loading":
            report(force=True)

    def discard() -> None:
        shutil.rmtree(directory, ignore_errors=True)

    report(force=True)
    request = {
        "id": uuid.uuid4().hex,
        "input": path,
        "output": str(output),
        "language": settings["language"],
        "beam_size": settings["beam_size"],
        "vad_filter": settings["vad_filter"],
        "condition_on_previous_text": True,
    }
    started = time.monotonic()
    try:
        outcome = whisper_service.service.transcribe(
            model=settings["model"],
            device=settings["device"],
            compute_type=settings["compute_type"],
            cpu_threads=job_input["cpu_threads"],
            request=request,
            cancelled=lambda: context.cancelled,
            on_event=on_event,
        )
    except whisper_service.WhisperCancelled:
        discard()
        raise jobs.JobCancelled() from None
    except whisper_service.WhisperFailed as failure:
        discard()
        log = _write_log(
            captions.captions_root(project_id) / "logs" / ("transcription-%s.log" % context.job_id),
            failure.log or "",
        )
        message = "%s\n%s" % (WHISPER_FAILURES.get(failure.kind, WHISPER_FAILURES["failed"]), failure.message)
        if log:
            message += "\nFull log: %s" % log
        raise jobs.JobFailed(message.strip()) from None

    elapsed = round(time.monotonic() - started, 2)
    try:
        result = storage.read_json(output)
    except (OSError, ValueError) as error:
        discard()
        raise jobs.JobFailed("Whisper's output could not be read: %s" % error) from error

    segments = result.get("segments") or []
    word_count = sum(len(segment.get("words") or []) for segment in segments)
    model = captions.MODELS_BY_ID[settings["model"]]
    transcript = {
        "schema_version": captions.TRANSCRIPT_SCHEMA_VERSION,
        "kind": "video_factory.transcript",
        "id": transcript_id,
        "created_at": _now(),
        "job_id": context.job_id,
        "retranscription": bool(job_input.get("force")),
        "reuse_key": job_input["reuse_key"],
        "input": {
            "key": described["key"],
            "kind": described["kind"],
            "label": described["label"],
            "filename": described["filename"],
            "path": path,
            "fingerprint": described["fingerprint"],
            "metadata": described["metadata"],
        },
        "settings": {
            **settings,
            "cpu_threads": job_input["cpu_threads"],
            "condition_on_previous_text": True,
            "word_timestamps": True,
            "recipe_version": captions.TRANSCRIPTION_RECIPE_VERSION,
        },
        "model": {"id": model["id"], "repository": model["repository"], "label": model["label"]},
        "result": {
            "language": result.get("language") or settings["language"],
            "language_probability": result.get("language_probability"),
            "duration_seconds": result.get("duration_seconds"),
            "duration_after_vad_seconds": result.get("duration_after_vad_seconds"),
            "word_count": word_count,
            "text": " ".join(captions.clean_text(segment.get("text", "")) for segment in segments).strip(),
        },
        "whisper": {
            "reused_loaded_model": state["reused"],
            "model_load_seconds": state["load_seconds"],
            "elapsed_seconds": elapsed,
        },
        "segments": segments,
    }

    try:
        captions.write_transcript(project_id, transcript)
        revision = captions.create_revision(
            project_id,
            transcript,
            captions=captions.group_transcript(transcript, job_input["grouping"]),
            grouping=job_input["grouping"],
            origin=captions.ORIGIN_GROUPING,
            parent=None,
        )
    except storage.ProjectError as error:
        discard()
        raise jobs.JobFailed(error.message) from error
    finally:
        output.unlink(missing_ok=True)
    _write_log(directory / "logs" / "whisper.log", outcome.get("log") or "")

    count = len(revision["captions"])
    return {
        "transcript_id": transcript_id,
        "revision": revision["revision"],
        "input_key": described["key"],
        "word_count": word_count,
        "caption_count": count,
        "language": transcript["result"]["language"],
        "reused_loaded_model": state["reused"],
        "elapsed_seconds": elapsed,
        "summary": (
            "Transcribed %d words into %d captions in %.0f s." % (word_count, count, elapsed)
            if word_count
            else "No speech was recognised in this video."
        ),
    }


# --- model download --------------------------------------------------------------


def build_model_download_input(project_id: str, raw: Any) -> dict:
    if not isinstance(raw, dict) or raw.get("model") not in captions.MODELS_BY_ID:
        raise captions.CaptionError("Choose a model to download.")
    for record in jobs.list_jobs(project_id):
        if record["type"] == MODEL_DOWNLOAD_JOB and record["status"] in jobs.ACTIVE_STATUSES:
            raise captions.CaptionError("A model is already being downloaded.", status_code=409)
    return {"model": raw["model"]}


def run_model_download(context: jobs.JobContext) -> dict:
    model = captions.MODELS_BY_ID[context.input["model"]]
    if captions.model_downloaded(model["id"]):
        return {"model": model["id"], "summary": "%s is already downloaded." % model["label"]}

    command = [
        sys.executable, "-u", str(WORKER_SCRIPT), "--download",
        "--model", model["id"], "--repository", model["repository"],
    ]
    pending = [""]
    last = [0.0]

    def on_output(chunk: str) -> None:
        pending[0] += chunk
        *lines, pending[0] = pending[0].split("\n")
        for line in lines:
            if not line.startswith("@@"):
                continue
            try:
                event = json.loads(line[2:])
            except ValueError:
                continue
            if event.get("event") == "download" and time.monotonic() - last[0] > PROGRESS_INTERVAL_SECONDS:
                last[0] = time.monotonic()
                megabytes = (event.get("bytes") or 0) / 1_048_576
                context.progress(
                    "Downloading %s — %.0f MB so far (%s in total)." % (model["label"], megabytes, model["download"])
                )

    context.progress("Connecting to Hugging Face to download %s (%s)…" % (model["label"], model["download"]))
    try:
        result = processes.run(command, cancelled=lambda: context.cancelled, on_output=on_output)
    except processes.ProcessCancelled:
        raise jobs.JobCancelled() from None
    except processes.ProcessStartFailed as error:
        raise jobs.JobFailed(error.message) from error

    if not result.ok or not captions.model_downloaded(model["id"]):
        raise jobs.JobFailed(
            "The download did not finish. Check the internet connection and try again "
            "(a resumed download continues where it stopped).\n%s" % result.last_lines()
        )
    return {"model": model["id"], "summary": "%s was downloaded. Transcription now runs offline." % model["label"]}


# --- renderer setup --------------------------------------------------------------


class _EventReader:
    """Collect `@@` JSON events from arbitrarily split output chunks."""

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, chunk: str) -> list[dict]:
        self._pending += chunk.replace("\r", "\n")
        *lines, self._pending = self._pending.split("\n")
        events = []
        for line in lines:
            line = line.strip()
            if line.startswith("@@"):
                try:
                    events.append(json.loads(line[2:]))
                except ValueError:
                    continue
        return events


def build_renderer_setup_input(project_id: str, raw: Any) -> dict:
    if node_executable() is None:
        raise captions.CaptionError("Node.js was not found on PATH. Install Node.js 22 or later.", status_code=503)
    if not (FRONTEND_DIRECTORY / "node_modules" / "@remotion" / "renderer").is_dir():
        raise captions.CaptionError(
            "The Remotion packages are not installed. Run: cd frontend; npm.cmd install", status_code=503
        )
    return {}


def run_renderer_setup(context: jobs.JobContext) -> dict:
    command = [
        node_executable() or "node", str(RENDER_SCRIPT), "--setup",
        "--bundle-cache", str(bundle_cache_directory()),
    ]
    reader = _EventReader()
    errors: list[str] = []

    def on_output(chunk: str) -> None:
        for event in reader.feed(chunk):
            kind = event.get("event")
            if kind == "browser_download":
                context.progress("Downloading the headless browser…", percent=float(event.get("percent") or 0))
            elif kind == "stage" and event.get("stage") == "bundling":
                context.progress("Building the render bundle (about a minute, once)…")
            elif kind == "error":
                errors.append(event.get("message") or "")

    context.progress("Checking the headless browser…")
    try:
        result = processes.run(command, cancelled=lambda: context.cancelled, on_output=on_output, cwd=str(FRONTEND_DIRECTORY))
    except processes.ProcessCancelled:
        raise jobs.JobCancelled() from None
    except processes.ProcessStartFailed as error:
        raise jobs.JobFailed(error.message) from error
    if not result.ok:
        raise jobs.JobFailed("Preparing the renderer failed: %s" % (errors[-1] if errors else result.last_lines()))
    return {"summary": "The renderer is ready. Exports now run offline."}


# --- exports ---------------------------------------------------------------------


def exports_root(project_id: str) -> Path:
    return storage.project_directory(project_id) / "exports" / EXPORTS_SUBDIRECTORY


def export_directory(project_id: str, export_id: str) -> Path:
    return exports_root(project_id) / validate_export_id(export_id)


def validate_export_id(raw: Any) -> str:
    if not isinstance(raw, str) or len(raw) != 12 or not all(c in "0123456789abcdef" for c in raw):
        raise captions.CaptionError("Export not found.", status_code=404)
    return raw


def build_export_input(project_id: str, raw: Any) -> dict:
    """Freeze everything an export depends on, now.

    The job record holds this snapshot — captions, style, composition and
    encoder settings — so editing captions or style afterwards, or retrying
    the job later, renders exactly what was asked for here.
    """
    if not isinstance(raw, dict):
        raise captions.CaptionError("The export request is invalid.")
    export_format = raw.get("format")
    if export_format not in FORMATS:
        raise captions.CaptionError("Choose MP4 or SRT.")

    transcript = captions.read_transcript(project_id, raw.get("transcript_id"))
    revision = captions.read_revision(project_id, transcript["id"], raw.get("revision"))

    video_key = raw.get("input_key") or transcript["input"]["key"]
    if video_key == transcript["input"]["key"]:
        described = caption_inputs.resolve_for_job(project_id, video_key)
        stale = captions.staleness(transcript, described["fingerprint"])
    else:
        # Captions drawn over a different file are only allowed when that file
        # is a zoom of exactly the bytes they were timed on (timing lineage).
        kind, _ = caption_inputs.parse_key(video_key)
        identity = caption_inputs.timing_identity(project_id, video_key) if kind == caption_inputs.KIND_ZOOM else None
        if identity is None or identity["key"] != transcript["input"]["key"]:
            raise captions.CaptionError(
                "These captions belong to a different video and cannot be placed on this one.", status_code=409
            )
        stale = captions.staleness(transcript, identity["fingerprint"])
        described = caption_inputs.resolve_for_job(project_id, video_key)
    if stale["stale"]:
        raise captions.CaptionError(stale["reason"], status_code=409)

    duration = transcript["input"]["metadata"]["duration_seconds"]
    if abs(described["metadata"]["duration_seconds"] - duration) > 0.1:
        raise captions.CaptionError("The video's length no longer matches the captions' timing.", status_code=409)
    normalised, errors = captions.normalise_captions(revision["captions"], duration)
    if errors:
        raise captions.CaptionValidationFailed(errors)
    if not normalised:
        raise captions.CaptionError("This revision has no captions to export.")

    if export_format == FORMAT_MP4:
        status = renderer_status()
        if not status["ready"]:
            raise captions.CaptionError(" ".join(status["problems"]), status_code=503)
        if not described["metadata"]["has_audio"]:
            raise captions.CaptionError("The video has no audio track; an export needs the original audio.")

    project = storage.read_project(project_id)
    style = captions.read_style(project)
    composition = caption_inputs.composition_metadata(described["metadata"])
    language = transcript["result"]["language"]

    return {
        "format": export_format,
        "requested_at": _now(),
        "input": {
            "key": described["key"],
            "label": described["label"],
            "filename": described["filename"],
            "path": described["path"],
            "fingerprint": described["fingerprint"],
            "metadata": described["metadata"],
        },
        "transcript_id": transcript["id"],
        "revision": revision["revision"],
        "revision_created_at": revision["created_at"],
        "language": language,
        "style": style["style"],
        "style_revision": style["style_revision"],
        "composition": composition,
        "export_settings": dict(EXPORT_SETTINGS) if export_format == FORMAT_MP4 else None,
        "captions": normalised,
    }


def _write_export(directory: Path, record: dict) -> None:
    try:
        storage.write_json_atomic(directory / "export.json", record)
    except OSError as error:
        raise jobs.JobFailed("The export record could not be saved: %s" % error) from error


def _probe_streams(path: str) -> list[dict]:
    raw = media._ffprobe(  # noqa: SLF001 - the shared FFprobe runner
        path, ["-show_entries", "stream=codec_type,codec_name,width,height", "-of", "json"]
    )
    return json.loads(raw).get("streams") or []


def verify_render(path: str, composition: dict, source_duration: float) -> dict:
    """FFprobe the rendered file. Raises `media.MediaError` with the reason."""
    described = media.verify_output(path, require_audio=True)
    streams = _probe_streams(path)
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if len(video) != 1 or len(audio) != 1:
        raise media.MediaError(
            "The export has %d video and %d audio streams; exactly one of each was expected."
            % (len(video), len(audio))
        )
    if video[0].get("codec_name") != "h264" or audio[0].get("codec_name") != "aac":
        raise media.MediaError("The export is not H.264 + AAC.")
    if (video[0].get("width"), video[0].get("height")) != (composition["width"], composition["height"]):
        raise media.MediaError(
            "The export is %s×%s, not the expected %d×%d."
            % (video[0].get("width"), video[0].get("height"), composition["width"], composition["height"])
        )

    expected = composition["duration_in_frames"] / composition["fps"]
    tolerance = 1.0 / composition["fps"] + AAC_PADDING_TOLERANCE_SECONDS
    measured = described["duration_seconds"]
    difference = measured - expected
    if abs(difference) > tolerance:
        raise media.MediaError(
            "The export lasts %.3f s; %.3f s was expected (tolerance ±%.3f s)."
            % (measured, expected, tolerance)
        )
    return {
        "checked_at": _now(),
        "duration_seconds": round(measured, 3),
        "expected_seconds": round(expected, 3),
        "source_seconds": round(source_duration, 3),
        "difference_seconds": round(difference, 3),
        "tolerance_seconds": round(tolerance, 3),
        "width": composition["width"],
        "height": composition["height"],
        "video_codec": "h264",
        "audio_codec": "aac",
        "streams": {"video": 1, "audio": 1},
    }


def run_export(context: jobs.JobContext) -> dict:
    project_id = context.project_id
    snapshot = context.input
    export_id = uuid.uuid4().hex[:12]
    directory = export_directory(project_id, export_id)
    directory.mkdir(parents=True, exist_ok=False)

    stem = cutting.safe_stem(snapshot["input"]["filename"], "captions")
    is_mp4 = snapshot["format"] == FORMAT_MP4
    filename = "%s.captioned.mp4" % stem if is_mp4 else "%s.srt" % stem

    record = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "kind": "video_factory.caption_export",
        "export_id": export_id,
        "project_id": project_id,
        "job_id": context.job_id,
        "format": snapshot["format"],
        "status": EXPORT_RUNNING,
        "created_at": _now(),
        "finished_at": None,
        "input_key": snapshot["input"]["key"],
        "input_label": snapshot["input"]["label"],
        "input_fingerprint": snapshot["input"]["fingerprint"]["digest"],
        "transcript_id": snapshot["transcript_id"],
        "revision": snapshot["revision"],
        "style_revision": snapshot["style_revision"],
        "composition": snapshot["composition"],
        "export_settings": snapshot["export_settings"],
        "caption_count": len(snapshot["captions"]),
        "output": None,
        "verification": None,
        "error": None,
        "log_path": None,
    }

    final = directory / filename
    partial = directory / (".partial%s" % final.suffix)
    if len(str(final)) > cutting.WINDOWS_MAX_PATH:
        record.update(status=EXPORT_FAILED, error="The export path is too long for Windows: %s. Use a shorter workspace path." % final, finished_at=_now())
        _write_export(directory, record)
        raise jobs.JobFailed(record["error"])

    try:
        storage.write_json_atomic(directory / "snapshot.json", snapshot)
    except OSError as error:
        raise jobs.JobFailed("The export snapshot could not be saved: %s" % error) from error
    _write_export(directory, record)

    def finish(status: str, error: str | None = None) -> None:
        partial.unlink(missing_ok=True)
        record.update(status=status, error=error, finished_at=_now())
        _write_export(directory, record)

    path = snapshot["input"]["path"]
    try:
        current = caption_inputs.fingerprint(path) if os.path.isfile(path) else None
    except storage.ProjectError:
        current = None
    if current is None or current["digest"] != snapshot["input"]["fingerprint"]["digest"]:
        message = (
            "The video changed or disappeared after this export was requested, so the "
            "captions no longer belong to it. Nothing was exported."
        )
        finish(EXPORT_FAILED, message)
        raise jobs.JobFailed(message)

    if not is_mp4:
        context.progress("Writing the SRT…")
        text = captions.render_srt(snapshot["captions"], captions.direction_for(snapshot["language"]))
        try:
            # UTF-8 with a byte-order mark: Premiere reads Hebrew SRT correctly
            # only with it, and every other player ignores it.
            with open(partial, "w", encoding="utf-8-sig", newline="\n") as handle:
                handle.write(text)
            os.replace(partial, final)
        except OSError as error:
            finish(EXPORT_FAILED, "The SRT could not be written: %s" % error)
            raise jobs.JobFailed(record["error"]) from error
        record["output"] = {"filename": filename, "size_bytes": final.stat().st_size, "media_type": "application/x-subrip"}
        record["verification"] = {"checked_at": _now(), "cues": len(snapshot["captions"]), "encoding": "utf-8 with BOM"}
        finish(EXPORT_SUCCEEDED)
        return {"export_id": export_id, "format": FORMAT_SRT, "summary": "SRT with %d captions written." % len(snapshot["captions"])}

    return _render_mp4(context, snapshot, record, directory, partial, final, finish)


def _render_mp4(context, snapshot, record, directory, partial, final, finish) -> dict:
    props = captions.composition_props(
        snapshot["composition"], snapshot["captions"], snapshot["style"], snapshot["language"]
    )
    settings = snapshot["export_settings"]
    render_composition(
        context,
        directory,
        {
            "props": props,
            "input_path": snapshot["input"]["path"],
            "output_path": str(partial),
            "bundle_cache_dir": str(bundle_cache_directory()),
            "crf": settings["crf"],
            "x264_preset": settings["x264_preset"],
            "concurrency": None,
        },
        snapshot["composition"]["duration_in_frames"],
        record,
        finish,
    )

    context.progress("Checking the exported file with FFprobe…", percent=100.0)
    try:
        verification = verify_render(
            str(partial), snapshot["composition"], snapshot["input"]["metadata"]["duration_seconds"]
        )
        os.replace(partial, final)
    except (media.MediaError, OSError, ValueError) as error:
        message = "The rendered file failed verification and was discarded: %s" % getattr(error, "message", error)
        finish(EXPORT_FAILED, message)
        raise jobs.JobFailed(message) from error

    record["output"] = {"filename": final.name, "size_bytes": final.stat().st_size, "media_type": "video/mp4"}
    record["verification"] = verification
    finish(EXPORT_SUCCEEDED)
    return {
        "export_id": record["export_id"],
        "format": FORMAT_MP4,
        "summary": "Exported %s (%.1f s, %d×%d)."
        % (final.name, verification["duration_seconds"], verification["width"], verification["height"]),
    }


def render_composition(context, directory: Path, document: dict, total_frames: int, record: dict, finish) -> None:
    """Run the Remotion render script on one job document, to its partial file.

    Shared by the captions export and the B-roll export: the same script, the
    same composition, the same progress reporting and cancellation. On
    cancellation or failure `finish` records the outcome and the job is ended;
    on return the partial file exists and still has to be verified.
    """
    job_file = directory / "render-job.json"
    partial = Path(document["output_path"])
    try:
        storage.write_json_atomic(job_file, document)
    except OSError as error:
        finish(EXPORT_FAILED, "The render job could not be written: %s" % error)
        raise jobs.JobFailed(record["error"]) from error

    command = [node_executable() or "node", str(RENDER_SCRIPT), "--job", str(job_file)]
    reader = _EventReader()
    errors: list[dict] = []
    last = [0.0]
    stage_messages = {
        "bundling": "Building the render bundle (once per app update)…",
        "browser": "Starting the headless browser…",
        "composition": "Preparing the composition…",
        "rendering": "Rendering…",
    }

    def on_output(chunk: str) -> None:
        for event in reader.feed(chunk):
            kind = event.get("event")
            if kind == "stage":
                context.progress(stage_messages.get(event.get("stage"), "Rendering…"), percent=0.0)
            elif kind == "progress" and time.monotonic() - last[0] > PROGRESS_INTERVAL_SECONDS:
                last[0] = time.monotonic()
                context.progress(
                    "Rendering — %s of %d frames drawn, %s encoded."
                    % (event.get("rendered"), total_frames, event.get("encoded")),
                    percent=100.0 * float(event.get("progress") or 0),
                )
            elif kind == "error":
                errors.append(event)

    context.progress("Starting the renderer…", percent=0.0)
    try:
        result = processes.run(
            command, cancelled=lambda: context.cancelled, on_output=on_output, cwd=str(FRONTEND_DIRECTORY)
        )
    except processes.ProcessCancelled:
        finish(EXPORT_CANCELLED, "Cancelled at your request. The partial file was deleted.")
        raise jobs.JobCancelled() from None
    except processes.ProcessStartFailed as error:
        finish(EXPORT_FAILED, error.message)
        raise jobs.JobFailed(error.message) from error
    finally:
        job_file.unlink(missing_ok=True)

    record["log_path"] = _write_log(
        directory / "logs" / "render.log",
        "$ %s\n\n%s" % (processes.describe_command(command), result.output),
    )

    if not result.ok or not partial.is_file():
        reason = errors[-1].get("message") if errors else result.last_lines()
        message = "Rendering failed: %s" % reason
        finish(EXPORT_FAILED, message)
        raise jobs.JobFailed(message)


def _job_active(project_id: str, job_id: Any) -> bool:
    if not isinstance(job_id, str):
        return False
    try:
        return jobs.read_job(project_id, job_id)["status"] in jobs.ACTIVE_STATUSES
    except storage.ProjectError:
        return False


def read_export(project_id: str, export_id: Any) -> dict:
    directory = export_directory(project_id, export_id)
    try:
        record = storage.read_json(directory / "export.json")
    except FileNotFoundError as error:
        raise captions.CaptionError("Export not found.", status_code=404) from error
    except (OSError, ValueError) as error:
        raise captions.CaptionError("The export record cannot be read: %s" % error, status_code=422) from error
    if record.get("status") == EXPORT_RUNNING and not _job_active(project_id, record.get("job_id")):
        record = {**record, "status": EXPORT_INTERRUPTED, "error": "The backend stopped while this export was running."}
    record["downloadable"] = record.get("status") == EXPORT_SUCCEEDED and bool(record.get("output"))
    return record


def list_exports(project_id: str) -> list[dict]:
    root = exports_root(project_id)
    if not root.is_dir():
        return []
    records = []
    for directory in root.iterdir():
        try:
            records.append(read_export(project_id, directory.name))
        except storage.ProjectError:
            continue
    records.sort(key=lambda record: record.get("created_at") or "", reverse=True)
    return records


def resolve_export_file(project_id: str, export_id: Any) -> tuple[Path, dict]:
    record = read_export(project_id, export_id)
    if not record["downloadable"]:
        raise captions.CaptionError("This export has no finished file.", status_code=404)
    directory = export_directory(project_id, record["export_id"]).resolve()
    path = (directory / record["output"]["filename"]).resolve()
    if not path.is_relative_to(directory) or not path.is_file():
        raise captions.CaptionError("The exported file is no longer in its folder.", status_code=404)
    return path, record


# --- registration ----------------------------------------------------------------


jobs.register_job_type(TRANSCRIPTION_JOB, "Transcription (Whisper)", run_transcription, build_transcription_input)
jobs.register_job_type(MODEL_DOWNLOAD_JOB, "Whisper model download", run_model_download, build_model_download_input)
jobs.register_job_type(RENDERER_SETUP_JOB, "Prepare caption renderer", run_renderer_setup, build_renderer_setup_input)
jobs.register_job_type(EXPORT_JOB, "Caption export", run_export, build_export_input)
