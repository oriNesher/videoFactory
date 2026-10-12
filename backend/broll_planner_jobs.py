"""The automatic B-roll planning job (stage 3B) and its records.

`broll_plan` turns an edited base video into a reviewable B-roll plan
revision without any choice from the user:

1. **Snapshot** (when requested): the base video's bytes and timeline, its
   timing identity (a zoomed video plans on its clean parent's transcript),
   the transcript id + caption revision to use (revisions are immutable),
   every *described*, available library asset as it is now, the editorial
   preference, the caption band, the zooms and take joins in the base, and
   the plan revision being replaced. Editing captions, descriptions or the
   plan while the job runs changes nothing it does.
2. **Transcript.** The snapshot's revision; only when the video has none yet
   does Whisper run here, through the zoom job's own code path.
3. **Plan.** An accepted planning run with the same cache key is reused
   without calling the provider. Otherwise one request (bounded retries) and
   at most one repair request (`broll_planner.request_plan`).
4. **Apply.** The accepted plan becomes a new immutable revision of the base
   video's B-roll plan (`origin: "planner"`). If the plan was edited while
   the job ran, or an asset changed, nothing is overwritten: the run is kept
   and can be applied explicitly.

    intermediates/broll/planner/
      runs/<run-id>.json          an accepted plan with full provenance (immutable)
      rejected-<id>.json          answers that failed validation (audit)
"""

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from . import (
    broll,
    broll_library,
    broll_planner,
    broll_templates,
    caption_inputs,
    captions,
    jobs,
    storage,
    zoom_jobs,
    zooms,
)
from .gemini import GeminiError
from .llm import ProviderError

PLAN_JOB = "broll_plan"
RUN_SCHEMA_VERSION = 1
RUN_KIND = "video_factory.broll_planner_run"
MAX_ZOOM_INTERVALS = 300


def _hash(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- locations and records -----------------------------------------------------------


def planner_root(project_id: str) -> Path:
    return storage.project_directory(project_id) / "intermediates" / "broll" / "planner"


def runs_directory(project_id: str) -> Path:
    return planner_root(project_id) / "runs"


def read_run(project_id: str, run_id: Any) -> dict:
    if not isinstance(run_id, str) or len(run_id) != 12 or not all(c in "0123456789abcdef" for c in run_id):
        raise broll.BrollError("Planning run not found.", status_code=404)
    try:
        document = storage.read_json(runs_directory(project_id) / ("%s.json" % run_id))
    except FileNotFoundError as error:
        raise broll.BrollError("Planning run not found.", status_code=404) from error
    except (OSError, ValueError) as error:
        raise broll.BrollError("The planning run cannot be read: %s" % error, status_code=422) from error
    if not isinstance(document, dict) or document.get("kind") != RUN_KIND:
        raise broll.BrollError("The file is not a B-roll planning run.", status_code=422)
    if (document.get("schema_version") or 0) > RUN_SCHEMA_VERSION:
        raise broll.BrollError("The planning run was written by a newer version.", status_code=422)
    return document


def list_runs(project_id: str) -> list[dict]:
    directory = runs_directory(project_id)
    if not directory.is_dir():
        return []
    documents = []
    for path in directory.glob("*.json"):
        try:
            documents.append(read_run(project_id, path.stem))
        except storage.ProjectError:
            continue
    documents.sort(key=lambda document: document.get("created_at") or "", reverse=True)
    return documents


def _write_run(project_id: str, document: dict) -> None:
    try:
        storage.write_json_exclusive(runs_directory(project_id) / ("%s.json" % document["run_id"]), document)
    except OSError as error:
        raise jobs.JobFailed("Saving the B-roll plan failed: %s" % error) from error


def _write_rejected(project_id: str, document: dict) -> str | None:
    path = planner_root(project_id) / ("rejected-%s.json" % uuid.uuid4().hex[:12])
    try:
        storage.write_json_exclusive(path, document)
    except OSError:
        return None
    return path.name


def public_run(run: dict | None) -> dict | None:
    """A run for the interface: everything but the raw request and answers."""
    if run is None:
        return None
    audit = run.get("audit") or {}
    return {
        key: run.get(key) for key in (
            "run_id", "created_at", "base", "transcript", "catalog", "template_catalog_version",
            "provider", "prompt_version", "preferences", "summary", "placements", "explanations",
            "suggestions", "statistics", "normalisation",
        )
    } | {"audit": {"repair_used": audit.get("repair_used", False), "calls": audit.get("calls", [])}}


# --- jobs --------------------------------------------------------------------------------


def _for_key(record: dict, key: str) -> bool:
    return record["type"] == PLAN_JOB and record["input"].get("input_key") == key


def active_job(project_id: str, key: str) -> dict | None:
    return next(
        (record for record in jobs.list_jobs(project_id) if _for_key(record, key) and record["status"] in jobs.ACTIVE_STATUSES),
        None,
    )


def last_job(project_id: str, key: str) -> dict | None:
    return next(
        (record for record in jobs.list_jobs(project_id) if _for_key(record, key) and record["status"] in jobs.FINISHED_STATUSES),
        None,
    )


# --- the snapshot ------------------------------------------------------------------------


def catalog_readiness() -> dict:
    """Which assets automatic planning may use, and the one message to show if any."""
    found = broll_planner.readiness(broll_library.read_entries())
    return {key: value for key, value in found.items() if key != "candidates"}


def caption_band(project_id: str) -> str:
    """Where captions sit (from the caption style), so B-roll can avoid them."""
    style = captions.read_style(storage.read_project(project_id))["style"]
    return "bottom" if float(style.get("vertical_position_percent", 80)) >= 50 else "top"


def zoom_intervals(project_id: str, key: str) -> list[dict] | None:
    kind, parts = caption_inputs.parse_key(key)
    if kind != caption_inputs.KIND_ZOOM:
        return None
    try:
        plan = zooms.read_plan(project_id, zooms.read_run(project_id, parts[0])["plan_id"])
    except storage.ProjectError:
        return None
    return [
        {"start": interval["start"], "end": interval["end"], "preset": interval["preset"]}
        for interval in plan.get("intervals") or [] if interval.get("preset") != "NORMAL"
    ][:MAX_ZOOM_INTERVALS]


def snapshot(project_id: str, raw: Any) -> dict:
    """Freeze everything a planning run depends on. Raises `BrollError` with
    an actionable message when planning cannot start."""
    if not isinstance(raw, dict):
        raise broll.BrollError("The planning request is invalid.")
    key = raw.get("input_key")
    described = broll.describe_base(project_id, key)
    base = broll.base_reference(described)
    resolved = caption_inputs.resolve(project_id, key)

    identity = caption_inputs.timing_identity(project_id, key)
    if identity["key"] is None or identity["fingerprint"] is None:
        raise broll.BrollError(
            "The clean video this zoomed video was made from has changed or is gone, so its transcript "
            "cannot be used. Plan B-roll on a current video.", status_code=409,
        )
    digest = identity["fingerprint"]["digest"]
    found = zooms.find_transcript(project_id, identity["key"], digest)
    timing_input = None
    settings = captions.default_transcription_settings()
    if found is None:
        timing_input = caption_inputs.resolve_for_job(project_id, identity["key"])
        if not timing_input["metadata"]["has_audio"]:
            raise broll.BrollError("This video has no audio, so there is no speech to plan B-roll from.", status_code=409)
        if not captions.model_downloaded(settings["model"]):
            raise broll.BrollError(
                'This video has no transcript yet, and the Whisper model "%s" is not downloaded. Press '
                '"Download model" in the Captions section once, then plan again.'
                % captions.MODELS_BY_ID[settings["model"]]["label"], status_code=409,
            )

    ready = broll_planner.readiness(broll_library.read_entries())
    if not ready["ready"]:
        raise broll.BrollError(ready["message"], status_code=409)

    project = storage.read_project(project_id)
    preference = raw.get("preference") or broll.read_settings(project)["preference"]
    if preference not in broll.PLANNER_PREFERENCES:
        raise broll.BrollError("Unknown B-roll preference.")
    current = broll.find_plan(project_id, base["key"], base["digest"])
    return {
        "request_id": uuid.uuid4().hex[:12],
        "requested_at": broll.now(),
        "input_key": base["key"],
        "base": base,
        "base_path": resolved["path"],
        "timing_identity": {"key": identity["key"], "digest": digest, "via_zoom": identity["via_zoom"]},
        "transcript": (
            {"transcript_id": found["transcript"]["id"], "revision": found["revision"]["revision"]} if found else None
        ),
        "timing_input": timing_input,
        "transcription_settings": settings,
        "cpu_threads": captions.default_cpu_threads(),
        "grouping": captions.read_grouping(project),
        "candidates": ready["candidates"],
        "catalog_counts": {
            "described": ready["described_count"],
            "undescribed": ready["undescribed_count"],
            "unavailable": ready["unavailable_count"],
        },
        "preference": preference,
        "caption_band": caption_band(project_id),
        "zoom_intervals": zoom_intervals(project_id, base["key"]),
        "clips": zooms.clip_boundaries(project_id, identity["key"]),
        "base_revision": current["revision"] if current else 0,
        "force_replan": raw.get("force_replan") is True,
        "auto": raw.get("auto") is True,
    }


def build_input(project_id: str, raw: Any) -> dict:
    snap = snapshot(project_id, raw)
    if active_job(project_id, snap["input_key"]) is not None:
        raise broll.BrollError("B-roll for this video is already being planned.", status_code=409)
    try:
        broll_planner.get_planner()
    except ProviderError as error:
        raise broll.BrollError(error.message, status_code=409) from error
    if snap["base_revision"] and raw.get("replace_current") is not True:
        raise broll.BrollError(
            "This video already has a B-roll plan (revision %d). Making a new plan adds a new revision "
            "and keeps this one; confirm to continue." % snap["base_revision"], status_code=409,
        )
    return snap


# --- planning ------------------------------------------------------------------------------


def _provider_identity() -> dict:
    status = broll_planner.provider_status()
    return {"id": status["provider"], "label": status["label"], "model": status["model"], "is_mock": status["is_mock"]}


def prepare(snap: dict, revision: dict, language: str | None, duration: float) -> dict:
    """Transcript → offered catalog, request and cache key. Pure given its inputs."""
    normalised, errors = captions.normalise_captions(revision["captions"], duration)
    if errors:
        raise jobs.JobFailed(
            "The latest captions of this video are not valid (%s). Fix them in the Captions section first."
            % errors[0]["message"]
        )
    if not normalised:
        raise jobs.JobFailed("No speech was recognised in this video, so there is nothing to plan B-roll from.")
    segments = broll_planner.transcript_segments(normalised)
    offered, shortlist = broll_planner.shortlist(snap["candidates"], " ".join(segment["text"] for segment in segments))
    request = broll_planner.build_request(
        base=snap["base"], segments=segments, language=language, clips=snap["clips"],
        caption_band=snap["caption_band"], zoom_intervals=snap["zoom_intervals"],
        offered=offered, preference=snap["preference"],
    )
    catalog_fingerprint = _hash([[candidate["view"], candidate["digest"]] for candidate in offered])
    provider = _provider_identity()
    cache_key = _hash(
        {
            "prompt": broll_planner.PROMPT_VERSION,
            "rules": broll_planner.RULES_VERSION,
            "plan_schema": broll.PLAN_SCHEMA_VERSION,
            "base": snap["base"]["digest"],
            "transcript": [revision["transcript_id"], revision["revision"]],
            "catalog": catalog_fingerprint,
            "templates": broll_templates.TEMPLATE_CATALOG_VERSION,
            "provider": provider["id"],
            "model": provider["model"],
            "preference": snap["preference"],
            "caption_band": snap["caption_band"],
        }
    )
    return {
        "segments": segments,
        "offered": offered,
        "shortlist": shortlist,
        "request": request,
        "catalog_fingerprint": catalog_fingerprint,
        "provider": provider,
        "cache_key": cache_key,
    }


def find_reusable_run(project_id: str, cache_key: str, base: dict, request_id: str | None = None) -> dict | None:
    """An accepted run for the same inputs that still validates today.

    With `request_id`, only a run made by an earlier attempt of the same
    request (a retried job) — so "Make a new plan" asks once, even if retried.
    """
    library = broll.library_index()
    for run in list_runs(project_id):
        if request_id is not None and run.get("request_id") != request_id:
            continue
        if run.get("cache_key") != cache_key or (run.get("base") or {}).get("digest") != base["digest"]:
            continue
        _placements, problems = broll.validate_placements(run.get("placements") or [], base, library)
        if not problems:
            return run
    return None


def _load_transcript(context: jobs.JobContext, snap: dict) -> tuple[dict, dict]:
    if snap["transcript"] is not None:
        transcript = captions.read_transcript(context.project_id, snap["transcript"]["transcript_id"])
        return transcript, captions.read_revision(context.project_id, transcript["id"], snap["transcript"]["revision"])
    found = zoom_jobs._ensure_transcript(  # noqa: SLF001 - the shared transcript step
        context,
        {
            "input": snap["timing_input"],
            "transcription_settings": snap["transcription_settings"],
            "cpu_threads": snap["cpu_threads"],
            "grouping": snap["grouping"],
        },
    )
    return found["transcript"], found["revision"]


def _check_base(context: jobs.JobContext, snap: dict) -> None:
    context.progress("Checking the video…")
    path = snap["base_path"]
    if not os.path.isfile(path):
        raise jobs.JobFailed("The video is no longer where it was.")
    try:
        current = caption_inputs.fingerprint(path)
    except storage.ProjectError as error:
        raise jobs.JobFailed(error.message) from error
    if current["digest"] != snap["base"]["digest"]:
        raise jobs.JobFailed("The video changed after planning was requested. Plan again for the video as it is now.")


def _plan(context: jobs.JobContext, snap: dict, prepared: dict, transcript: dict, revision: dict) -> tuple[dict, bool]:
    project_id = context.project_id
    if not snap["force_replan"]:
        existing = find_reusable_run(project_id, prepared["cache_key"], snap["base"])
        if existing is not None:
            context.progress("Reusing the B-roll plan already made for this transcript and library.")
            return existing, True
    else:
        retried = find_reusable_run(project_id, prepared["cache_key"], snap["base"], request_id=snap["request_id"])
        if retried is not None:
            context.progress("Reusing the plan this request already received.")
            return retried, True

    try:
        broll_planner.check_budget(prepared["request"])
        planner = broll_planner.get_planner()
    except ProviderError as error:
        raise jobs.JobFailed(error.message) from error
    offered = {candidate["asset_id"]: candidate for candidate in prepared["offered"]}
    try:
        outcome = broll_planner.request_plan(
            planner, prepared["request"], base=snap["base"], offered=offered, library=broll.library_index(),
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
            name = _write_rejected(project_id, {
                "kind": "video_factory.broll_plan_rejected", "created_at": broll.now(), "job_id": context.job_id,
                "provider": planner.describe(), "prompt_version": broll_planner.PROMPT_VERSION, "audit": audit,
            })
            if name:
                error.message += " (The rejected answers are saved as %s.)" % name
        raise jobs.JobFailed(error.message) from None

    plan = outcome["plan"]
    run = {
        "schema_version": RUN_SCHEMA_VERSION,
        "kind": RUN_KIND,
        "run_id": broll_planner.new_run_id(),
        "created_at": broll.now(),
        "job_id": context.job_id,
        "request_id": snap["request_id"],
        "cache_key": prepared["cache_key"],
        "base": {**snap["base"], "timeline": broll.TIMELINE_NOTE},
        "timing_identity": snap["timing_identity"],
        "transcript": {
            "transcript_id": transcript["id"],
            "revision": revision["revision"],
            "language": transcript["result"].get("language"),
            "segment_count": len(prepared["segments"]),
        },
        "catalog": {
            "fingerprint": prepared["catalog_fingerprint"],
            "asset_digests": {candidate["asset_id"]: candidate["digest"] for candidate in prepared["offered"]},
            "counts": snap["catalog_counts"],
            "shortlist": prepared["shortlist"],
        },
        "template_catalog_version": broll_templates.TEMPLATE_CATALOG_VERSION,
        "rules_version": broll_planner.RULES_VERSION,
        "provider": planner.describe(),
        "prompt_version": broll_planner.PROMPT_VERSION,
        "preferences": {"preference": snap["preference"], "caption_band": snap["caption_band"]},
        "summary": plan["summary"],
        "placements": plan["placements"],
        "explanations": plan["explanations"],
        "suggestions": plan["suggestions"],
        "statistics": plan["statistics"],
        "normalisation": plan["normalisation"],
        "audit": outcome["audit"],
        "request_sent": prepared["request"],
    }
    _write_run(project_id, run)
    return run, False


def apply_run(project_id: str, run: dict, *, base_revision: int) -> dict:
    """Make an accepted run the newest plan revision. Refuses rather than overwrites."""
    key = run["base"]["key"]
    described = broll.describe_base(project_id, key)
    if described["fingerprint"]["digest"] != run["base"]["digest"]:
        raise broll.BrollError("This plan was made for an earlier version of the video.", status_code=409)
    library = broll.library_index()
    for placement in run["placements"]:
        entry = library.get(placement["asset_id"])
        expected = run["catalog"]["asset_digests"].get(placement["asset_id"])
        if entry is None or entry["fingerprint"].get("digest") != expected:
            raise broll.BrollError(
                "An asset this plan uses was removed or changed since it was planned. Plan again.", status_code=409
            )
    return broll.save_plan(
        project_id, key, run["placements"], base_revision=base_revision, origin=broll.ORIGIN_PLANNER,
        note=run["summary"][:200], planner={"run_id": run["run_id"]},
    )


def run_plan(context: jobs.JobContext) -> dict:
    snap = context.input
    project_id = context.project_id
    _check_base(context, snap)
    context.raise_if_cancelled()
    transcript, revision = _load_transcript(context, snap)
    context.raise_if_cancelled()
    prepared = prepare(snap, revision, transcript["result"].get("language"), transcript["input"]["metadata"]["duration_seconds"])
    run, reused = _plan(context, snap, prepared, transcript, revision)
    context.raise_if_cancelled()

    context.progress("Preparing the composed preview…")
    current = broll.find_plan(project_id, snap["base"]["key"], snap["base"]["digest"])
    current_revision = current["revision"] if current else 0
    applied, reason = None, None
    if current is not None and (current.get("planner") or {}).get("run_id") == run["run_id"] and current.get("origin") == broll.ORIGIN_PLANNER:
        applied = current["revision"]  # a retry of a job that already applied it
    elif current_revision != snap["base_revision"]:
        reason = (
            "The B-roll plan was changed while this plan was being made, so it was not applied and your "
            "changes were kept. Apply the new plan if you want it."
        )
    else:
        try:
            applied = apply_run(project_id, run, base_revision=current_revision)["revision"]
        except broll.BrollError as error:
            reason = "The new plan was kept but not applied: %s" % error.message

    count = len(run["placements"])
    return {
        "run_id": run["run_id"],
        "input_key": snap["input_key"],
        "plan_reused": reused,
        "applied": applied is not None,
        "applied_revision": applied,
        "not_applied_reason": reason,
        "placement_count": count,
        "summary": (
            "%s%s" % (run["summary"], " (Reused an existing plan; nothing was sent.)" if reused else "")
            if applied is not None else reason
        ),
    }


# --- starting --------------------------------------------------------------------------


def current_run(project_id: str, plan: dict | None) -> dict | None:
    run_id = ((plan or {}).get("planner") or {}).get("run_id")
    if not run_id:
        return None
    try:
        return read_run(project_id, run_id)
    except storage.ProjectError:
        return None


def reusable_result(project_id: str, key: str, preference: str | None = None) -> dict | None:
    """The current plan, when it is the unedited result of a planning run for
    exactly today's inputs — then pressing "Plan" again does nothing at all."""
    try:
        snap = snapshot(project_id, {"input_key": key, "preference": preference})
    except storage.ProjectError:
        return None
    current = broll.find_plan(project_id, snap["base"]["key"], snap["base"]["digest"])
    run = current_run(project_id, current)
    if run is None or current.get("origin") != broll.ORIGIN_PLANNER or snap["transcript"] is None:
        return None
    try:
        revision = captions.read_revision(project_id, snap["transcript"]["transcript_id"], snap["transcript"]["revision"])
        transcript = captions.read_transcript(project_id, snap["transcript"]["transcript_id"])
        prepared = prepare(snap, revision, transcript["result"].get("language"), transcript["input"]["metadata"]["duration_seconds"])
    except (storage.ProjectError, jobs.JobFailed):
        return None
    return run if run.get("cache_key") == prepared["cache_key"] else None


def auto_start(project_id: str, key: str) -> dict:
    """Continue the factory into B-roll planning after an approved upstream step.

    Never replaces an existing plan, never duplicates a running job, and
    reports — rather than hides — why it could not start.
    """
    active = active_job(project_id, key)
    if active is not None:
        return {"status": "running", "job_id": active["id"], "message": "B-roll is being planned automatically."}
    try:
        described = broll.describe_base(project_id, key)
        if broll.find_plan(project_id, key, described["fingerprint"]["digest"]) is not None:
            return {"status": "existing", "job_id": None, "message": "This video already has a B-roll plan; review it in B-roll."}
        record = jobs.submit(project_id, PLAN_JOB, {"input_key": key, "auto": True})
    except storage.ProjectError as error:
        return {"status": "blocked", "job_id": None, "message": error.message}
    return {"status": "started", "job_id": record["id"], "message": "Planning B-roll automatically."}


jobs.register_job_type(PLAN_JOB, "Automatic B-roll planning", run_plan, build_input)
