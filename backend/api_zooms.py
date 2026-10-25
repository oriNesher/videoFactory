"""Zoom endpoints (stage 2B).

As elsewhere: the browser names keys and ids, never paths; anything slow is a
queued job; reading state never calls a provider or renders anything, so
polling is free. Starting is idempotent: a request for a video that is already
being zoomed returns that job, and a request whose transcript, plan and render
all already exist returns the finished result without queueing anything.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from . import broll, broll_planner_jobs, caption_inputs, caption_jobs, cutting, jobs, music, storage, zoom_jobs, zoom_planner, zooms
from .models import ExternalPlanImportRequest, ZoomApproveRequest, ZoomSettingsRequest, ZoomStartRequest

router = APIRouter(prefix="/projects/{project_id}/zooms", tags=["zooms"])


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except storage.ProjectError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error


def _public_run(manifest: dict) -> dict:
    """A run without filesystem paths."""
    public = {key: value for key, value in manifest.items() if key not in ("log_path",)}
    public["input"] = {key: value for key, value in manifest["input"].items() if key != "path"}
    return public


def _public_plan(plan: dict) -> dict:
    return {key: value for key, value in plan.items() if key not in ("request_sent", "audit")} | {
        "audit": {
            "repair_used": (plan.get("audit") or {}).get("repair_used", False),
            "calls": (plan.get("audit") or {}).get("calls", []),
        }
    }


def _job_summary(record: dict | None) -> dict | None:
    if record is None:
        return None
    return {
        "id": record["id"],
        "type": record["type"],
        "status": record["status"],
        "progress_message": record["progress_message"],
        "progress_percent": record["progress_percent"],
        "cancel_requested": record["cancel_requested"],
        "created_at": record["created_at"],
        "finished_at": record["finished_at"],
        "error": record["error"],
        "result": record["result"],
    }


@router.get("/status")
def status(project_id: str):
    project = _call(storage.read_project, project_id)
    settings = zooms.read_settings(project)
    inputs = _call(zooms.list_inputs, project_id)
    selected = _call(zooms.current_input, project_id)
    return {
        "provider": zoom_planner.provider_status(),
        "presets": zoom_planner.PRESETS,
        "preferences": [{"id": key, "description": value} for key, value in zoom_planner.PREFERENCES.items()],
        "renderer": zooms.RENDERER,
        "caption_renderer": caption_jobs.renderer_status(),
        "settings": settings,
        "inputs": inputs,
        "selected_input": selected,
        # Whether approving continues to the B-roll step (stage 3A) or exports.
        "broll_enabled": broll.read_settings(project)["enabled"],
        # With B-roll off: whether approving continues to the music stage (stage 4).
        "music_enabled": music.read_settings(project)["enabled"],
    }


@router.put("/settings")
def save_settings(project_id: str, request: ZoomSettingsRequest):
    return _call(zooms.save_settings, project_id, preference=request.preference, selected_input=request.selected_input)


def _continuation(project_id: str, run_id: str, approval: dict | None) -> dict | None:
    """The export that followed an approval, if any (newest first)."""
    if approval is None or approval.get("captions") is None:
        return None
    reference = approval["captions"]
    for record in caption_jobs.list_exports(project_id):
        if (
            record.get("input_key") == "zoom.%s" % run_id
            and record.get("format") == caption_jobs.FORMAT_MP4
            and record.get("transcript_id") == reference["transcript_id"]
            and record.get("revision") == reference["revision"]
            and record.get("style_revision") == reference["style_revision"]
        ):
            return record
    for record in jobs.list_jobs(project_id):
        snapshot = record["input"]
        if (
            record["type"] == caption_jobs.EXPORT_JOB
            and record["status"] in jobs.ACTIVE_STATUSES
            and (snapshot.get("input") or {}).get("key") == "zoom.%s" % run_id
        ):
            return {"status": "queued", "job_id": record["id"], "progress_message": record["progress_message"],
                    "progress_percent": record["progress_percent"]}
    return None


def _describe_run(project_id: str, manifest: dict) -> dict:
    described = _public_run(manifest)
    try:
        plan = zooms.read_plan(project_id, manifest["plan_id"])
        described["plan"] = _public_plan(plan)
    except storage.ProjectError:
        described["plan"] = None
    if manifest.get("status") == zooms.RUN_SUCCEEDED:
        review = zooms.review_state(project_id, manifest)
        described["review"] = review
        described["continuation"] = _continuation(
            project_id, manifest["run_id"], review["approval"] if review["approved"] else None
        )
        described["output_key"] = "zoom.%s" % manifest["run_id"]
    return described


@router.get("/inputs/{input_key}")
def describe_input(project_id: str, input_key: str):
    _call(zooms.validate_input_key, project_id, input_key)
    described = _call(caption_inputs.describe, project_id, input_key)
    digest = (described.get("fingerprint") or {}).get("digest")

    transcript = None
    if digest:
        found = _call(zooms.find_transcript, project_id, input_key, digest)
        if found is not None:
            transcript = {
                "transcript_id": found["transcript"]["id"],
                "revision": found["revision"]["revision"],
                "caption_count": len(found["revision"]["captions"]),
            }

    runs = [manifest for manifest in _call(zooms.list_runs, project_id) if manifest["input"]["key"] == input_key]
    current = next(
        (
            manifest
            for manifest in runs
            if manifest.get("status") == zooms.RUN_SUCCEEDED and manifest["input"]["fingerprint"]["digest"] == digest
        ),
        None,
    )
    records = [
        record for record in jobs.list_jobs(project_id)
        if record["type"] == zoom_jobs.ZOOM_JOB and (record["input"].get("input") or {}).get("key") == input_key
    ]
    active = next((record for record in records if record["status"] in jobs.ACTIVE_STATUSES), None)
    last = next((record for record in records if record["status"] in jobs.FINISHED_STATUSES), None)

    return {
        "input": described,
        "transcript": transcript,
        "active_job": _job_summary(active),
        "last_job": _job_summary(last),
        "current_run": _describe_run(project_id, current) if current else None,
        "runs": [
            {
                "run_id": manifest["run_id"],
                "status": manifest["status"],
                "created_at": manifest["created_at"],
                "finished_at": manifest.get("finished_at"),
                "plan_id": manifest.get("plan_id"),
                "error": manifest.get("error"),
                "stale": manifest["input"]["fingerprint"]["digest"] != digest,
            }
            for manifest in runs
        ],
    }


def _reusable_result(project_id: str, input_key: str, preference: str) -> dict | None:
    """Everything already exists for the current settings? Then no job at all."""
    described = caption_inputs.resolve_for_job(project_id, input_key)
    digest = described["fingerprint"]["digest"]
    found = zooms.find_transcript(project_id, input_key, digest)
    status = zoom_planner.provider_status()
    if found is None:
        return None
    cache_key = zooms.plan_cache_key(
        input_digest=digest,
        transcript_id=found["transcript"]["id"],
        revision=found["revision"]["revision"],
        provider={"id": status["provider"], "model": status["model"]},
        preference=preference,
    )
    plan = zooms.find_plan(project_id, cache_key)
    if plan is None:
        return None
    return zooms.find_reusable_run(project_id, zooms.render_cache_key(digest, plan["plan_id"]))


@router.post("/runs", status_code=201)
def start(project_id: str, request: ZoomStartRequest):
    """Make zooms for a video automatically: transcript, plan, render.

    Repeated clicks are harmless: an active job for the same video is
    returned rather than duplicated, and a finished result for the same
    transcript, settings and model is returned without asking Gemini again.
    """
    _call(zooms.validate_input_key, project_id, request.input_key)
    active = zoom_jobs.active_job(project_id, request.input_key)
    if active is not None:
        return {"job": jobs.describe_job(active), "reused": None}

    project = _call(storage.read_project, project_id)
    preference = request.preference or zooms.read_settings(project)["preference"]
    if preference not in zoom_planner.PREFERENCES:
        raise HTTPException(status_code=400, detail="Unknown zoom preference.")
    if request.force_replan is not True and request.plan_id is None:
        reused = _call(_reusable_result, project_id, request.input_key, preference)
        if reused is not None:
            return {"job": None, "reused": _describe_run(project_id, reused)}

    record = _call(
        jobs.submit,
        project_id,
        zoom_jobs.ZOOM_JOB,
        {
            "input_key": request.input_key,
            "preference": preference,
            "force_replan": request.force_replan is True,
            "plan_id": request.plan_id,
        },
    )
    return {"job": jobs.describe_job(record), "reused": None}


@router.get("/inputs/{input_key}/external-prompt")
def external_prompt(project_id: str, input_key: str):
    """The exact instructions Gemini would get, to paste into another AI. Sends nothing."""
    return _call(zoom_jobs.external_prompt, project_id, input_key)


@router.post("/inputs/{input_key}/external-plan", status_code=201)
def import_external_plan(project_id: str, input_key: str, request: ExternalPlanImportRequest):
    """Use another AI's answer: validated like Gemini's, then rendered as a queued job."""
    return _call(zoom_jobs.import_plan, project_id, input_key, request.answer)


@router.get("/runs/{run_id}")
def get_run(project_id: str, run_id: str):
    _call(storage.read_project, project_id)
    return _describe_run(project_id, _call(zooms.read_run, project_id, run_id))


@router.get("/runs/{run_id}/video")
def run_video(project_id: str, run_id: str, download: bool = False):
    """The actual FFmpeg output, for review and download."""
    _call(storage.read_project, project_id)
    manifest = _call(zooms.read_run, project_id, run_id)
    path = _call(zooms.output_path, project_id, manifest)
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
    if download:
        stem = cutting.safe_stem(manifest["input"]["filename"], "video")
        headers["Content-Disposition"] = 'attachment; filename="%s.zoomed.mp4"' % stem.encode("ascii", "replace").decode("ascii")
    return FileResponse(path, media_type="video/mp4", headers=headers)


@router.get("/plans/{plan_id}")
def get_plan(project_id: str, plan_id: str):
    """The stored plan, including exactly what was sent to the provider."""
    _call(storage.read_project, project_id)
    return _call(zooms.read_plan, project_id, plan_id)


@router.post("/runs/{run_id}/approve")
def approve(project_id: str, run_id: str, request: ZoomApproveRequest):
    """Approve the reviewed result, then continue to the next step.

    The next step is the B-roll module when it is enabled for the project
    (the default): the zoomed video becomes its base video, and B-roll's own
    approval or skip leads to the export. With B-roll disabled it is the
    existing export: with captions, the Captions section's MP4 export draws
    the approved caption revision over the zoomed video with Remotion; without
    captions, the zoomed video itself is the finished file.
    """
    project = _call(storage.read_project, project_id)
    approval = _call(
        zooms.approve, project_id, run_id, output_digest=request.output_digest, captions_reference=request.captions
    )
    if broll.read_settings(project)["enabled"]:
        key = "zoom.%s" % run_id
        _call(broll.save_settings, project_id, selected_input=key)
        # Continue without another Start: B-roll is planned in the background
        # (unless the video already has a plan, or planning cannot start —
        # then the reason is shown and B-roll can be placed by hand or skipped).
        planning = broll_planner_jobs.auto_start(project_id, key)
        message = {
            "started": "Approved. Planning B-roll automatically — the result appears in B-roll below.",
            "running": "Approved. B-roll is being planned — the result appears in B-roll below.",
            "existing": "Approved. This video already has a B-roll plan; review it in B-roll below.",
        }.get(planning["status"], "Approved. B-roll could not be planned automatically: %s" % planning["message"])
        return {
            "approval": approval,
            "next": {"kind": "broll", "input_key": key, "message": message, "planning": planning},
        }
    if music.read_settings(project)["enabled"]:
        return {
            "approval": approval,
            "next": {
                "kind": "music",
                "message": "Approved. Next step: background music. Choose a track there, or continue without music, to export.",
            },
        }
    if approval["captions"] is None:
        return {
            "approval": approval,
            "next": {"kind": "final", "message": "Approved. The zoomed video is the finished file; download it below."},
        }

    existing = _continuation(project_id, run_id, approval)
    if existing is not None and existing.get("status") in ("queued", caption_jobs.EXPORT_RUNNING, caption_jobs.EXPORT_SUCCEEDED):
        return {"approval": approval, "next": {"kind": "caption_export", "export": existing, "job": None}}

    reference = approval["captions"]
    try:
        record = jobs.submit(
            project_id,
            caption_jobs.EXPORT_JOB,
            {
                "transcript_id": reference["transcript_id"],
                "revision": reference["revision"],
                "format": caption_jobs.FORMAT_MP4,
                "input_key": "zoom.%s" % run_id,
            },
        )
    except storage.ProjectError as error:
        return {
            "approval": approval,
            "next": {"kind": "blocked", "message": "Approved, but the captioned export could not start: %s" % error.message},
        }
    return {"approval": approval, "next": {"kind": "caption_export", "export": None, "job": jobs.describe_job(record)}}

