"""B-roll endpoints (stage 3A).

Two routers. `/broll/...` is the workspace-wide asset library, shared by
every project. `/projects/{id}/broll/...` is one project's plans, review and
export. As everywhere: the browser names ids and keys, never paths; reading
state renders nothing; anything slow is a queued job.
"""

import urllib.parse
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

from . import (
    broll,
    broll_jobs,
    broll_library,
    broll_planner,
    broll_planner_jobs,
    broll_templates,
    caption_inputs,
    caption_jobs,
    cutting,
    jobs,
    storage,
)
from .models import (
    BrollAddPlacementRequest,
    BrollDecisionRequest,
    BrollImportRequest,
    BrollPlanRequest,
    BrollPlanStartRequest,
    BrollRevisionRequest,
    BrollSettingsRequest,
    BrollUpdatePlacementRequest,
)

library_router = APIRouter(prefix="/broll", tags=["broll"])
router = APIRouter(prefix="/projects/{project_id}/broll", tags=["broll"])

STORAGE_NOTE = (
    "Imported files are referenced where they are, never copied or modified. Moving, renaming "
    "or editing a file afterwards shows it as missing or changed here."
)


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except storage.ProjectError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error


def _attachment(filename: str) -> str:
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
        filename.encode("ascii", "replace").decode("ascii"),
        urllib.parse.quote(filename, safe=""),
    )


# --- the library ---------------------------------------------------------------------


@library_router.get("/library")
def get_library():
    return {"assets": _call(broll_library.list_public), "storage": "referenced", "storage_note": STORAGE_NOTE}


@library_router.post("/library/imports", status_code=201)
def import_asset(request: BrollImportRequest):
    """Add an image or video. No path opens the machine's file dialog."""
    if request.path is None:
        return _call(broll_library.choose_and_import)
    return _call(broll_library.import_asset, request.path)


@library_router.patch("/library/{asset_id}")
def update_asset(asset_id: str, changes: dict[str, Any] = Body(...)):
    return _call(broll_library.update_details, asset_id, changes)


@library_router.post("/library/{asset_id}/remeasure")
def remeasure_asset(asset_id: str):
    return _call(broll_library.remeasure, asset_id)


@library_router.delete("/library/{asset_id}")
def remove_asset(asset_id: str):
    """Removes the library entry only; the file on disk is never touched."""
    return _call(broll_library.remove, asset_id)


@library_router.get("/library/{asset_id}/media")
def asset_media(asset_id: str):
    path, content_type = _call(broll_library.media_file, asset_id)
    return FileResponse(path, media_type=content_type, headers={"Accept-Ranges": "bytes", "Cache-Control": "no-cache"})


@library_router.get("/library/{asset_id}/thumbnail")
def asset_thumbnail(asset_id: str):
    path = _call(broll_library.thumbnail_file, asset_id)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@library_router.get("/catalog")
def get_catalog():
    """Templates, constraints and assets as a planner will see them: no paths, no filenames."""
    return _call(broll.catalog)


# --- one project ------------------------------------------------------------------


@router.get("/status")
def status(project_id: str):
    project = _call(storage.read_project, project_id)
    settings = broll.read_settings(project)
    inputs = _call(broll.list_inputs, project_id)
    selected = settings["selected_input"]
    if selected not in {entry["key"] for entry in inputs}:
        selected = _call(broll.default_input, project_id)
    return {
        "settings": settings,
        "inputs": inputs,
        "selected_input": selected,
        **broll_templates.catalog(),
        "constraints": broll.constraints(),
        "renderer": caption_jobs.renderer_status(),
        "provider": broll_planner.provider_status(),
        "preferences": [{"id": key, "description": value} for key, value in broll.PLANNER_PREFERENCES.items()],
    }


@router.put("/settings")
def save_settings(project_id: str, request: BrollSettingsRequest):
    return _call(
        broll.save_settings, project_id,
        enabled=request.enabled, selected_input=request.selected_input, preference=request.preference,
    )


@router.get("/catalog")
def project_catalog(project_id: str, input_key: str | None = None):
    _call(storage.read_project, project_id)
    return _call(broll.catalog, project_id if input_key else None, input_key)


def _job_summary(record: dict | None) -> dict | None:
    if record is None:
        return None
    return {key: record[key] for key in (
        "id", "type", "status", "progress_message", "progress_percent", "cancel_requested",
        "created_at", "finished_at", "error", "result",
    )}


def _continuation(project_id: str, key: str, decision: dict | None) -> dict | None:
    """What followed the latest valid decision: an export, or the base as final."""
    if decision is None:
        return None
    # An approved plan with no placements continues exactly like a skip: the
    # unchanged picture goes on to the captions export (or is final).
    if decision["decision"] == broll.DECISION_APPROVED and decision.get("placement_count"):
        for record in broll_jobs.list_exports(project_id):
            if record.get("composition_digest") == decision["composition_digest"] and record.get("status") in (
                caption_jobs.EXPORT_RUNNING, caption_jobs.EXPORT_SUCCEEDED,
            ):
                return {"kind": "broll_export", "export": record, "job": None}
        active = broll_jobs.active_job(project_id, key)
        if active is not None and active["input"].get("composition_digest") == decision["composition_digest"]:
            return {"kind": "broll_export", "export": None, "job": _job_summary(active)}
        failed = next(
            (record for record in broll_jobs.list_exports(project_id)
             if record.get("composition_digest") == decision["composition_digest"]),
            None,
        )
        return {"kind": "broll_export", "export": failed, "job": None} if failed else None

    reference = decision.get("captions")
    if reference is None:
        return {
            "kind": "final",
            "message": (
                "Approved with no B-roll. The video itself is the finished file."
                if decision["decision"] == broll.DECISION_APPROVED
                else "B-roll skipped. The video itself is the finished file."
            ),
        }
    for record in caption_jobs.list_exports(project_id):
        if (
            record.get("input_key") == key
            and record.get("format") == caption_jobs.FORMAT_MP4
            and record.get("transcript_id") == reference["transcript_id"]
            and record.get("revision") == reference["revision"]
            and record.get("style_revision") == reference["style_revision"]
        ):
            return {"kind": "caption_export", "export": record, "job": None}
    for record in jobs.list_jobs(project_id):
        if (
            record["type"] == caption_jobs.EXPORT_JOB
            and record["status"] in jobs.ACTIVE_STATUSES
            and (record["input"].get("input") or {}).get("key") == key
        ):
            return {"kind": "caption_export", "export": None, "job": _job_summary(record)}
    return None


def _planner_state(project_id: str, key: str, plan: dict | None) -> dict:
    """Automatic planning as the panel shows it. Reading this never calls a provider."""
    run = broll_planner_jobs.current_run(project_id, plan)
    active = broll_planner_jobs.active_job(project_id, key)
    last = broll_planner_jobs.last_job(project_id, key)
    return {
        "implemented": True,
        "provider": broll_planner.provider_status(),
        "catalog": _call(broll_planner_jobs.catalog_readiness),
        "active_job": _job_summary(active),
        "last_job": _job_summary(last),
        # The run the current plan revision comes from, with its explanations.
        "run": broll_planner_jobs.public_run(run),
        "edited_since": bool(run) and (plan or {}).get("origin") != broll.ORIGIN_PLANNER,
    }


@router.get("/inputs/{input_key}")
def describe_input(project_id: str, input_key: str):
    """The base video, its plan, the composed props and the review state.

    The props are exactly what the export would render, minus the media URLs
    the browser adds; the digest names them for approval.
    """
    _call(storage.read_project, project_id)
    described = _call(caption_inputs.describe, project_id, input_key)
    exports = [record for record in broll_jobs.list_exports(project_id) if record.get("input_key") == input_key]
    active = broll_jobs.active_job(project_id, input_key)
    last = next(
        (record for record in jobs.list_jobs(project_id)
         if record["type"] == broll_jobs.EXPORT_JOB
         and (record["input"].get("input") or {}).get("key") == input_key
         and record["status"] in jobs.FINISHED_STATUSES),
        None,
    )
    state: dict = {
        "input": described,
        "available": False,
        "unavailable_reason": described.get("error"),
        "plan": None,
        "stale_plans": [],
        "issues": [],
        "assets": {},
        "props": None,
        "composition_digest": None,
        "captions": None,
        "review": None,
        "continuation": None,
        "exports": exports,
        "active_job": _job_summary(active),
        "last_job": _job_summary(last),
        "planner": _planner_state(project_id, input_key, None),
        "revisions": [],
    }
    if not described["available"] or described["fingerprint"] is None:
        return state

    composed = _call(broll.compose, project_id, input_key)
    review = broll.review_state(project_id, composed)
    digest = composed["base"]["digest"]
    state.update(
        planner=_planner_state(project_id, input_key, composed["plan"]),
        revisions=broll.list_revisions(project_id, composed["plan"]["plan_id"]) if composed["plan"] else [],
    )
    state.update(
        available=True,
        unavailable_reason=None,
        plan=composed["plan"],
        stale_plans=[
            {"plan_id": plan["plan_id"], "revision": plan["revision"], "created_at": plan["created_at"],
             "placement_count": len(plan["placements"])}
            for plan in broll.list_plans(project_id)
            if plan["base"]["key"] == input_key and plan["base"]["digest"] != digest
        ],
        issues=composed["issues"],
        props=composed["props"],
        composition_digest=composed["digest"],
        captions=(
            {**composed["captions_reference"], "caption_count": len(composed["overlay"]["captions"])}
            if composed["overlay"] else None
        ),
        review=review,
        continuation=_continuation(
            project_id, input_key, review["decision"] if (review["approved"] or review["skipped"]) else None
        ),
        assets={asset_id: broll_library.public_entry(entry) for asset_id, entry in composed["assets"].items()},
    )
    return state


@router.post("/inputs/{input_key}/placements", status_code=201)
def add_placement(project_id: str, input_key: str, request: BrollAddPlacementRequest):
    """Place an asset at the playhead with the default template and length."""
    return _call(
        broll.add_placement, project_id, input_key,
        asset_id=request.asset_id, at_seconds=request.at_seconds,
        template_id=request.template_id, base_revision=request.base_revision,
    )


@router.patch("/inputs/{input_key}/placements/{placement_id}")
def update_placement(project_id: str, input_key: str, placement_id: str, request: BrollUpdatePlacementRequest):
    return _call(broll.update_placement, project_id, input_key, placement_id, request.changes, base_revision=request.base_revision)


@router.delete("/inputs/{input_key}/placements/{placement_id}")
def remove_placement(project_id: str, input_key: str, placement_id: str, base_revision: int):
    return _call(broll.remove_placement, project_id, input_key, placement_id, base_revision=base_revision)


@router.put("/inputs/{input_key}/plan", status_code=201)
def save_plan(project_id: str, input_key: str, request: BrollPlanRequest):
    """Store a whole plan as a new revision — the path a stage 3B planner will use.

    Validated exactly like the "Add B-roll" action.
    """
    origin = request.origin if request.origin is not None else broll.ORIGIN_MANUAL
    return _call(broll.save_plan, project_id, input_key, request.placements, base_revision=request.base_revision, origin=origin)


@router.post("/inputs/{input_key}/validate")
def validate_plan(project_id: str, input_key: str, request: BrollPlanRequest):
    """Check placements against the video and the library. Writes nothing."""
    described = _call(broll.describe_base, project_id, input_key)
    placements, errors = broll.validate_placements(
        request.placements, broll.base_reference(described), broll.library_index()
    )
    return {"placements": placements, "errors": errors, "valid": not errors}


@router.post("/inputs/{input_key}/approve")
def approve(project_id: str, input_key: str, request: BrollDecisionRequest):
    """Approve exactly the reviewed composition, then export it.

    A reviewed plan with no placements is approved as "no B-roll" and
    continues like a skip, to the existing captions export.
    """
    _call(storage.read_project, project_id)
    decision, _composed = _call(
        broll.decide, project_id, input_key, decision=broll.DECISION_APPROVED, composition_digest=request.composition_digest
    )
    if not decision["placement_count"]:
        return _continue_without_broll(project_id, input_key, decision)
    existing = _continuation(project_id, input_key, decision)
    if existing is not None and (existing.get("job") or (existing.get("export") or {}).get("status") in (
        caption_jobs.EXPORT_RUNNING, caption_jobs.EXPORT_SUCCEEDED,
    )):
        return {"decision": decision, "next": existing}
    try:
        record = jobs.submit(
            project_id, broll_jobs.EXPORT_JOB,
            {"input_key": input_key, "composition_digest": decision["composition_digest"]},
        )
    except storage.ProjectError as error:
        return {"decision": decision, "next": {"kind": "blocked", "message": "Approved, but the export could not start: %s" % error.message}}
    return {"decision": decision, "next": {"kind": "broll_export", "export": None, "job": _job_summary(record)}}


@router.post("/inputs/{input_key}/skip")
def skip(project_id: str, input_key: str, request: BrollDecisionRequest):
    """No B-roll for this video: continue to the existing export flow."""
    _call(storage.read_project, project_id)
    decision, _composed = _call(
        broll.decide, project_id, input_key, decision=broll.DECISION_SKIPPED, composition_digest=request.composition_digest
    )
    return _continue_without_broll(project_id, input_key, decision)


def _continue_without_broll(project_id: str, input_key: str, decision: dict) -> dict:
    """The unchanged picture continues: the captions export, or the file itself."""
    existing = _continuation(project_id, input_key, decision)
    if existing is not None and existing["kind"] == "final":
        return {"decision": decision, "next": existing}
    if existing is not None and (existing.get("job") or (existing.get("export") or {}).get("status") in (
        caption_jobs.EXPORT_RUNNING, caption_jobs.EXPORT_SUCCEEDED,
    )):
        return {"decision": decision, "next": existing}
    reference = decision["captions"]
    try:
        record = jobs.submit(
            project_id, caption_jobs.EXPORT_JOB,
            {"transcript_id": reference["transcript_id"], "revision": reference["revision"],
             "format": caption_jobs.FORMAT_MP4, "input_key": input_key},
        )
    except storage.ProjectError as error:
        return {"decision": decision, "next": {"kind": "blocked", "message": "Decided, but the captioned export could not start: %s" % error.message}}
    return {"decision": decision, "next": {"kind": "caption_export", "export": None, "job": _job_summary(record)}}


# --- automatic planning (stage 3B) -------------------------------------------------


@router.post("/inputs/{input_key}/planner/runs", status_code=201)
def start_planning(project_id: str, input_key: str, request: BrollPlanStartRequest):
    """Plan B-roll automatically. Idempotent: a running job for this video is
    returned, and an unedited plan for exactly today's inputs is returned
    without asking the provider again (unless `force_replan`)."""
    _call(storage.read_project, project_id)
    active = broll_planner_jobs.active_job(project_id, input_key)
    if active is not None:
        return {"job": _job_summary(active), "reused": None}
    if request.force_replan is not True:
        reused = broll_planner_jobs.reusable_result(project_id, input_key)
        if reused is not None:
            return {"job": None, "reused": broll_planner_jobs.public_run(reused)}
    record = _call(
        jobs.submit, project_id, broll_planner_jobs.PLAN_JOB,
        {"input_key": input_key, "force_replan": request.force_replan is True, "replace_current": request.replace_current is True},
    )
    return {"job": _job_summary(record), "reused": None}


@router.get("/planner/runs/{run_id}")
def get_planning_run(project_id: str, run_id: str):
    """A stored planning run, including exactly what was sent to the provider."""
    _call(storage.read_project, project_id)
    return _call(broll_planner_jobs.read_run, project_id, run_id)


@router.post("/inputs/{input_key}/planner/runs/{run_id}/apply", status_code=201)
def apply_planning_run(project_id: str, input_key: str, run_id: str, request: BrollRevisionRequest):
    """Use a planning run that was kept but not applied (or an earlier one) as a new revision."""
    _call(storage.read_project, project_id)
    run = _call(broll_planner_jobs.read_run, project_id, run_id)
    if run["base"]["key"] != input_key:
        raise HTTPException(status_code=404, detail="That plan belongs to another video.")
    return _call(broll_planner_jobs.apply_run, project_id, run, base_revision=request.base_revision)


@router.post("/inputs/{input_key}/plan/restore/{revision}", status_code=201)
def restore_revision(project_id: str, input_key: str, revision: int, request: BrollRevisionRequest):
    """Bring an earlier revision back as the newest one. Nothing is deleted."""
    _call(storage.read_project, project_id)
    return _call(broll.restore_revision, project_id, input_key, revision, base_revision=request.base_revision)


@router.get("/inputs/{input_key}/video")
def base_video(project_id: str, input_key: str, download: bool = False):
    """The base video itself — the finished file when B-roll is skipped and there are no captions."""
    path = _call(caption_inputs.resolve_stream_path, project_id, input_key)
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
    if download:
        resolved = _call(caption_inputs.resolve, project_id, input_key)
        headers["Content-Disposition"] = _attachment("%s.mp4" % cutting.safe_stem(resolved["filename"], "video"))
    return FileResponse(path, media_type="video/mp4", headers=headers)


@router.get("/exports")
def list_exports(project_id: str):
    _call(storage.read_project, project_id)
    return {"exports": _call(broll_jobs.list_exports, project_id)}


@router.get("/exports/{export_id}/file")
def export_file(project_id: str, export_id: str, download: bool = False):
    _call(storage.read_project, project_id)
    path, record = _call(broll_jobs.resolve_export_file, project_id, export_id)
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
    if download:
        headers["Content-Disposition"] = _attachment(record["output"]["filename"])
    return FileResponse(path, media_type="video/mp4", headers=headers)
