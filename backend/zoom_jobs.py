"""The zoom job: transcript → plan → render, automatically, as one queued job.

One job rather than three because the queue runs one job at a time and a
user should not have to approve internal steps. Each stage reuses finished
work when it can:

1. **Transcript** — the newest transcript of exactly these bytes (its latest
   caption revision, so caption corrections count). If there is none, Whisper
   runs here through the captions module's own code path, and the transcript
   is saved where the Captions section will find it.
2. **Plan** — an accepted plan with the same cache key is reused without
   calling the provider. Otherwise one request (bounded retries for rate
   limits) and, if the answer fails validation, one repair request.
3. **Render** — a verified render with the same render key is reused.
   Otherwise FFmpeg runs into a fresh run directory; the partial file is
   verified with FFprobe before it gets its final name.

The job input is an immutable snapshot (input path, fingerprint, metadata,
settings). A retry runs the same snapshot — and, because the plan from the
first attempt was saved, retrying a failed *render* does not call Gemini.
"""

import os
import time
import uuid
from typing import Any

from . import (
    caption_inputs,
    caption_jobs,
    captions,
    cutting,
    jobs,
    media,
    processes,
    storage,
    zoom_planner,
    zooms,
)
from .gemini import GeminiError
from .llm import ProviderError

ZOOM_JOB = "zoom_auto"

PROGRESS_INTERVAL_SECONDS = 0.5


def build_input(project_id: str, raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise zooms.ZoomError("The zoom request is invalid.")
    zooms.validate_input_key(project_id, raw.get("input_key"))
    project = storage.read_project(project_id)
    settings = zooms.read_settings(project)
    preference = raw.get("preference") or settings["preference"]
    if preference not in zoom_planner.PREFERENCES:
        raise zooms.ZoomError("Unknown zoom preference.")

    described = caption_inputs.resolve_for_job(project_id, raw["input_key"])
    if active_job(project_id, described["key"]) is not None:
        raise zooms.ZoomError("Zooms for this video are already being made.", status_code=409)

    plan_id = raw.get("plan_id")
    if plan_id is not None:
        plan = zooms.read_plan(project_id, plan_id)
        if plan["input"]["fingerprint"] != described["fingerprint"]["digest"]:
            raise zooms.ZoomError("That plan was made for a different version of this video.", status_code=409)

    return {
        "input": described,
        "preference": preference,
        "transcription_settings": captions.default_transcription_settings(),
        "cpu_threads": captions.default_cpu_threads(),
        "grouping": captions.read_grouping(project),
        "force_replan": raw.get("force_replan") is True,
        "plan_id": plan_id,
        "clips": zooms.clip_boundaries(project_id, described["key"]),
    }


def active_job(project_id: str, key: str) -> dict | None:
    for record in jobs.list_jobs(project_id):
        if (
            record["type"] == ZOOM_JOB
            and record["status"] in jobs.ACTIVE_STATUSES
            and (record["input"].get("input") or {}).get("key") == key
        ):
            return record
    return None


# --- stages ------------------------------------------------------------------------


def _check_input(context: jobs.JobContext, described: dict) -> None:
    context.progress("Checking the video…")
    path = described["path"]
    if not os.path.isfile(path):
        raise jobs.JobFailed('The video "%s" is no longer where it was.' % described["filename"])
    try:
        current = caption_inputs.fingerprint(path)
    except storage.ProjectError as error:
        raise jobs.JobFailed(error.message) from error
    if current["digest"] != described["fingerprint"]["digest"]:
        raise jobs.JobFailed(
            "The video changed after zooms were requested. Start again so the zooms are "
            "made for the video as it is now."
        )


def _ensure_transcript(context: jobs.JobContext, job_input: dict) -> dict:
    described = job_input["input"]
    digest = described["fingerprint"]["digest"]
    found = zooms.find_transcript(context.project_id, described["key"], digest)
    if found is not None:
        return {**found, "made_now": False}

    if not described["metadata"]["has_audio"]:
        raise jobs.JobFailed("This video has no audio, so there is no speech to plan zooms from.")
    settings = job_input["transcription_settings"]
    if not captions.model_downloaded(settings["model"]):
        raise jobs.JobFailed(
            'This video has no transcript yet, and the Whisper model "%s" is not downloaded. '
            'Press "Download model" in the Captions section once, then try again.'
            % captions.MODELS_BY_ID[settings["model"]]["label"]
        )
    context.progress("No transcript yet — transcribing locally with Whisper…")
    caption_jobs.run_transcription(
        context,
        {
            "input": described,
            "settings": settings,
            "cpu_threads": job_input["cpu_threads"],
            "reuse_key": captions.reuse_key(digest, settings),
            "grouping": job_input["grouping"],
            "force": False,
        },
    )
    found = zooms.find_transcript(context.project_id, described["key"], digest)
    if found is None:
        raise jobs.JobFailed("The transcript could not be found after transcription.")
    return {**found, "made_now": True}


def _plan(context: jobs.JobContext, job_input: dict, transcript: dict) -> tuple[dict, bool]:
    project_id = context.project_id
    described = job_input["input"]
    metadata = described["metadata"]

    if job_input.get("plan_id"):
        return zooms.read_plan(project_id, job_input["plan_id"]), True

    try:
        planner = zoom_planner.get_planner()
    except ProviderError as error:
        raise jobs.JobFailed(error.message) from error
    provider = planner.describe()

    revision = transcript["revision"]
    cache_key = zooms.plan_cache_key(
        input_digest=described["fingerprint"]["digest"],
        transcript_id=transcript["transcript"]["id"],
        revision=revision["revision"],
        provider=provider,
        preference=job_input["preference"],
    )
    if not job_input.get("force_replan"):
        existing = zooms.find_plan(project_id, cache_key)
        if existing is not None:
            context.progress("Reusing the zoom plan already made for this transcript.")
            return existing, True

    normalised, errors = captions.normalise_captions(revision["captions"], metadata["duration_seconds"])
    if errors:
        raise jobs.JobFailed(
            "The latest captions of this video are not valid (%s). Fix them in the Captions "
            "section first." % errors[0]["message"]
        )
    if not normalised:
        raise jobs.JobFailed("No speech was recognised in this video, so there is nothing to plan zooms from.")

    segments = zoom_planner.transcript_view(normalised)
    request = zoom_planner.build_request(
        duration_seconds=metadata["duration_seconds"],
        fps=metadata["fps"],
        width=metadata["display_width"],
        height=metadata["display_height"],
        segments=segments,
        language=transcript["transcript"]["result"].get("language"),
        clips=job_input.get("clips"),
        preference=job_input["preference"],
    )

    try:
        outcome = zoom_planner.request_plan(
            planner,
            request,
            duration_seconds=metadata["duration_seconds"],
            fps=metadata["fps"],
            cancelled=lambda: context.cancelled,
            on_wait=lambda message: context.progress(message),
            on_stage=lambda message: context.progress(message),
        )
    except GeminiError as error:
        if error.kind == "cancelled":
            raise jobs.JobCancelled() from None
        raise jobs.JobFailed(error.message) from None
    except ProviderError as error:
        audit = getattr(error, "audit", None)
        if audit is not None:
            name = zooms.write_rejected(
                project_id,
                {"kind": "video_factory.zoom_plan_rejected", "created_at": zooms.now(), "job_id": context.job_id,
                 "provider": provider, "audit": audit},
            )
            if name:
                error.message += " (The rejected answers are saved as %s.)" % name
        raise jobs.JobFailed(error.message) from None

    plan = outcome["plan"]
    document = {
        "schema_version": zoom_planner.PLAN_SCHEMA_VERSION,
        "kind": zooms.PLAN_DOCUMENT_KIND,
        "plan_id": uuid.uuid4().hex[:12],
        "created_at": zooms.now(),
        "job_id": context.job_id,
        "cache_key": cache_key,
        "input": {
            "key": described["key"],
            "label": described["label"],
            "fingerprint": described["fingerprint"]["digest"],
            "duration_seconds": metadata["duration_seconds"],
            "fps": metadata["fps"],
            "width": metadata["display_width"],
            "height": metadata["display_height"],
        },
        "timebase": zoom_planner.TIMEBASE,
        "transcript": {
            "transcript_id": transcript["transcript"]["id"],
            "revision": revision["revision"],
            "timing_precision": zoom_planner.describe_timing(segments),
            "segment_count": len(segments),
        },
        "clips": job_input.get("clips"),
        "provider": provider,
        "prompt_version": zoom_planner.PROMPT_VERSION,
        "preset_catalog_version": zoom_planner.PRESET_CATALOG_VERSION,
        "presets": zoom_planner.PRESETS,
        "settings": {"preference": job_input["preference"]},
        "summary": plan["summary"],
        "intervals": plan["intervals"],
        "statistics": zoom_planner.plan_statistics(plan["intervals"]),
        "normalisation": plan["normalisation"],
        "audit": outcome["audit"],
        "request_sent": request,
    }
    try:
        zooms.write_plan(project_id, document)
    except storage.ProjectError as error:
        raise jobs.JobFailed(error.message) from error
    return document, False


def _render(context: jobs.JobContext, job_input: dict, plan: dict) -> tuple[dict, bool]:
    project_id = context.project_id
    described = job_input["input"]
    metadata = described["metadata"]
    render_key = zooms.render_cache_key(described["fingerprint"]["digest"], plan["plan_id"])

    existing = zooms.find_reusable_run(project_id, render_key)
    if existing is not None:
        context.progress("Reusing the zoomed video already rendered from this plan.")
        return existing, True

    run_id = zooms.new_run_id()
    directory = zooms.run_directory(project_id, run_id)
    directory.mkdir(parents=True, exist_ok=False)
    partial = directory / zooms.PARTIAL_FILE_NAME
    final = directory / zooms.OUTPUT_FILE_NAME
    filter_file = directory / zooms.FILTER_FILE_NAME

    manifest = {
        "schema_version": zooms.RUN_SCHEMA_VERSION,
        "kind": zooms.RUN_DOCUMENT_KIND,
        "run_id": run_id,
        "job_id": context.job_id,
        "status": zooms.RUN_RUNNING,
        "created_at": zooms.now(),
        "finished_at": None,
        "input": {
            "key": described["key"],
            "kind": described["kind"],
            "label": described["label"],
            "filename": described["filename"],
            "path": described["path"],
            "fingerprint": described["fingerprint"],
            "metadata": metadata,
        },
        "plan_id": plan["plan_id"],
        "plan_summary": plan["summary"],
        "render_key": render_key,
        "renderer": zooms.RENDERER,
        "encoder": zooms.ENCODER,
        "lineage": {
            "timing_parent": {"key": described["key"], "digest": described["fingerprint"]["digest"]},
            "spatial_only": True,
            "note": "Framing only: every frame keeps its timestamp and the audio is the input's.",
        },
        "output": None,
        "verification": None,
        "error": None,
        "log_path": None,
    }
    if len(str(final)) > cutting.WINDOWS_MAX_PATH:
        manifest.update(status=zooms.RUN_FAILED, finished_at=zooms.now(),
                        error="The output path is too long for Windows: %s. Use a shorter workspace path." % final)
        zooms.write_manifest(project_id, manifest)
        raise jobs.JobFailed(manifest["error"])
    zooms.write_manifest(project_id, manifest)

    def finish(status: str, error: str | None = None) -> None:
        partial.unlink(missing_ok=True)
        manifest.update(status=status, error=error, finished_at=zooms.now())
        zooms.write_manifest(project_id, manifest)

    filter_file.write_text(
        zooms.build_filter(plan["intervals"], metadata["display_width"], metadata["display_height"], metadata["fps"]),
        encoding="utf-8",
        newline="\n",
    )
    command = zooms.build_render_command(
        described["path"], str(filter_file), str(partial),
        has_audio=metadata["has_audio"], audio_codec=metadata.get("audio_codec"),
    )
    duration = metadata["duration_seconds"]
    last = [0.0]

    def on_output(chunk: str) -> None:
        fraction = cutting.parse_ffmpeg_progress(chunk, duration)
        if fraction is not None and time.monotonic() - last[0] > PROGRESS_INTERVAL_SECONDS:
            last[0] = time.monotonic()
            context.progress("Rendering the zoomed video — %.0f%%." % (fraction * 100), percent=fraction * 100)

    context.progress("Rendering the zoomed video…", percent=0.0)
    try:
        result = processes.run(command, cancelled=lambda: context.cancelled, on_output=on_output)
    except processes.ProcessCancelled:
        finish(zooms.RUN_CANCELLED, "Cancelled at your request. The partial file was deleted.")
        raise jobs.JobCancelled() from None
    except processes.ProcessStartFailed as error:
        finish(zooms.RUN_FAILED, error.message)
        raise jobs.JobFailed(error.message) from error

    manifest["log_path"] = caption_jobs._write_log(  # noqa: SLF001 - the shared log writer
        directory / "logs" / "ffmpeg.log", "$ %s\n\n%s" % (processes.describe_command(command), result.output)
    )
    if not result.ok or not partial.is_file():
        message = "FFmpeg could not render the zooms:\n%s" % result.last_lines()
        finish(zooms.RUN_FAILED, message)
        raise jobs.JobFailed(message)

    context.progress("Checking the zoomed video with FFprobe…", percent=100.0)
    try:
        verification = zooms.verify_output(
            str(partial), metadata=metadata, input_frames=zooms.count_video_frames(described["path"])
        )
        os.replace(partial, final)
        fingerprint = caption_inputs.fingerprint(str(final))
    except (media.MediaError, OSError, ValueError, storage.ProjectError) as error:
        message = "The zoomed video failed verification and was discarded: %s" % getattr(error, "message", error)
        finish(zooms.RUN_FAILED, message)
        raise jobs.JobFailed(message) from error

    manifest["output"] = {
        "filename": final.name,
        "size_bytes": final.stat().st_size,
        "fingerprint": fingerprint,
    }
    manifest["verification"] = verification
    finish(zooms.RUN_SUCCEEDED)
    return manifest, False


def run_zooms(context: jobs.JobContext, job_input: dict | None = None) -> dict:
    job_input = job_input if job_input is not None else context.input
    described = job_input["input"]
    _check_input(context, described)
    context.raise_if_cancelled()

    plan_reused = False
    transcript = None
    if job_input.get("plan_id"):
        plan, plan_reused = _plan(context, job_input, {})
    else:
        transcript = _ensure_transcript(context, job_input)
        context.raise_if_cancelled()
        plan, plan_reused = _plan(context, job_input, transcript)
    context.raise_if_cancelled()

    manifest, render_reused = _render(context, job_input, plan)
    return {
        "run_id": manifest["run_id"],
        "plan_id": plan["plan_id"],
        "input_key": described["key"],
        "transcribed_now": bool(transcript and transcript["made_now"]),
        "plan_reused": plan_reused,
        "render_reused": render_reused,
        "summary": "%s%s" % (
            plan["summary"],
            " (Reused an existing result; nothing was sent or rendered.)" if plan_reused and render_reused else "",
        ),
    }


jobs.register_job_type(ZOOM_JOB, "Automatic zooms", run_zooms, build_input)
