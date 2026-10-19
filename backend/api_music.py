"""Background music endpoints (stage 4).

Two routers. `/audio/...` is the shared audio library (music tracks today,
sound effects later). `/projects/{id}/music/...` is one video's music
settings, review and final export. As everywhere: the browser names ids and
keys, never paths; reading state renders nothing; anything slow is a job.
"""

from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

from . import audio_library, caption_inputs, caption_jobs, jobs, music, music_jobs, player, storage
from .api_broll import _attachment, _job_summary
from .models import AudioImportRequest, AudioRenameRequest, MusicDecisionRequest

library_router = APIRouter(prefix="/audio", tags=["music"])
router = APIRouter(prefix="/projects/{project_id}/music", tags=["music"])

STORAGE_NOTE = (
    "Imported tracks are referenced where they are, never copied or modified. Moving, renaming "
    "or editing a file afterwards shows it as missing or changed here."
)
RIGHTS_NOTE = "Use music you have the rights to publish with your video."


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except storage.ProjectError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error


# --- the library ---------------------------------------------------------------------


@library_router.get("/library")
def get_library(kind: str | None = None):
    if kind is not None:
        _call(audio_library.validate_kind, kind)
    return {"assets": _call(audio_library.list_public, kind), "storage_note": STORAGE_NOTE, "rights_note": RIGHTS_NOTE}


@library_router.post("/library/imports", status_code=201)
def import_track(request: AudioImportRequest):
    """Add a track. No path opens the machine's file dialog."""
    if request.path is None:
        return _call(audio_library.choose_and_import, request.kind)
    return _call(audio_library.import_asset, request.path, request.kind)


@library_router.patch("/library/{asset_id}")
def rename_track(asset_id: str, request: AudioRenameRequest):
    return _call(audio_library.rename, asset_id, request.display_name)


@library_router.post("/library/{asset_id}/remeasure")
def remeasure_track(asset_id: str):
    return _call(audio_library.remeasure, asset_id)


@library_router.delete("/library/{asset_id}")
def remove_track(asset_id: str):
    """Removes the library entry only; the file on disk is never touched."""
    return _call(audio_library.remove, asset_id)


@library_router.get("/library/{asset_id}/media")
def track_media(asset_id: str):
    path, content_type = _call(audio_library.media_file, asset_id)
    return FileResponse(path, media_type=content_type, headers={"Accept-Ranges": "bytes", "Cache-Control": "no-cache"})


# --- one project ------------------------------------------------------------------


def _matches(record: dict, decision: dict) -> bool:
    """An export (or export job input) made from exactly this decision."""
    return record.get("composition_digest") == decision["composition_digest"] and record.get("decision") == decision["decision"]


def _continuation(project_id: str, key: str, decision: dict | None) -> dict | None:
    """What followed the latest valid decision: the final export, or the base as final."""
    if decision is None:
        return None
    if not decision.get("has_overlays"):
        return {"kind": "final", "message": "Nothing is added over this video: the video itself is the finished file."}
    exports = [record for record in music_jobs.list_exports(project_id) if _matches(record, decision)]
    current = next(
        (record for record in exports if record.get("status") in (caption_jobs.EXPORT_RUNNING, caption_jobs.EXPORT_SUCCEEDED)),
        None,
    )
    if current is not None:
        return {"kind": "final_export", "export": current, "job": None}
    active = music_jobs.active_job(project_id, key)
    if active is not None and _matches(active["input"], decision):
        return {"kind": "final_export", "export": None, "job": _job_summary(active)}
    return {"kind": "final_export", "export": exports[0], "job": None} if exports else None


@router.get("")
def describe(project_id: str):
    """The video music goes on, the settings, the composed props and the review.

    The props are exactly what the export would render, minus the media URLs
    the browser adds; the digest names them for approval.
    """
    project = _call(storage.read_project, project_id)
    key = _call(music.input_key, project_id)
    state: dict = {
        "settings": music.read_settings(project),
        "input_key": key,
        "input": None,
        "available": False,
        "unavailable_reason": "There is no video yet. Finish the stages before this one first." if key is None else None,
        "broll_state": None,
        "track": None,
        "issues": [],
        "props": None,
        "composition_digest": None,
        "review": None,
        "continuation": None,
        "renderer": caption_jobs.renderer_status(),
        "rights_note": RIGHTS_NOTE,
    }
    if key is None:
        return state
    described = _call(caption_inputs.describe, project_id, key)
    state.update(input=described, unavailable_reason=described.get("error"))
    if not described["available"] or described["fingerprint"] is None:
        return state

    composed = _call(music.compose, project_id, key)
    review = music.review_state(project_id, composed)
    entry = audio_library.find_entry(composed["settings"]["track_id"]) if composed["settings"]["track_id"] else None
    state.update(
        available=True,
        unavailable_reason=None,
        broll_state=composed["broll_state"],
        track=audio_library.public_entry(entry) if entry else None,
        issues=composed["issues"],
        props=composed["props"],
        composition_digest=composed["digest"],
        review=review,
        continuation=_continuation(project_id, key, review["decision"] if (review["approved"] or review["skipped"]) else None),
    )
    return state


@router.put("/settings")
def save_settings(project_id: str, changes: dict[str, Any] = Body(...)):
    return _call(music.save_settings, project_id, changes)


def _decide(project_id: str, decision_kind: str, digest) -> dict:
    _call(storage.read_project, project_id)
    key = _call(music.input_key, project_id)
    if key is None:
        raise HTTPException(status_code=409, detail="There is no video to add music to yet.")
    decision, _composed = _call(music.decide, project_id, key, decision=decision_kind, composition_digest=digest)
    existing = _continuation(project_id, key, decision)
    if existing is not None and (
        existing["kind"] == "final"
        or existing.get("job")
        or (existing.get("export") or {}).get("status") in (caption_jobs.EXPORT_RUNNING, caption_jobs.EXPORT_SUCCEEDED)
    ):
        return {"decision": decision, "next": existing}
    try:
        record = jobs.submit(
            project_id, music_jobs.EXPORT_JOB, {"input_key": key, "composition_digest": decision["composition_digest"]}
        )
    except storage.ProjectError as error:
        return {"decision": decision, "next": {"kind": "blocked", "message": "Decided, but the export could not start: %s" % error.message}}
    return {"decision": decision, "next": {"kind": "final_export", "export": None, "job": _job_summary(record)}}


@router.post("/approve")
def approve(project_id: str, request: MusicDecisionRequest):
    """Approve exactly the reviewed music, then export the finished video."""
    return _decide(project_id, music.DECISION_APPROVED, request.composition_digest)


@router.post("/skip")
def skip(project_id: str, request: MusicDecisionRequest):
    """No music for this video: export the decided result without it."""
    return _decide(project_id, music.DECISION_SKIPPED, request.composition_digest)


@router.get("/exports")
def list_exports(project_id: str):
    _call(storage.read_project, project_id)
    return {"exports": _call(music_jobs.list_exports, project_id)}


@router.post("/exports/{export_id}/open")
def open_export(project_id: str, export_id: str):
    """Play the finished video in VLC on this machine. Export id, not path."""
    _call(storage.read_project, project_id)
    path, _record = _call(music_jobs.resolve_export_file, project_id, export_id)
    _call(player.open_file, str(path))
    return {"opened": True}


@router.post("/exports/{export_id}/reveal")
def reveal_export(project_id: str, export_id: str):
    """Show the finished video in Explorer, selected in its folder."""
    _call(storage.read_project, project_id)
    path, _record = _call(music_jobs.resolve_export_file, project_id, export_id)
    _call(player.reveal_file, str(path))
    return {"opened": True}


@router.get("/exports/{export_id}/file")
def export_file(project_id: str, export_id: str, download: bool = False):
    _call(storage.read_project, project_id)
    path, record = _call(music_jobs.resolve_export_file, project_id, export_id)
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
    if download:
        headers["Content-Disposition"] = _attachment(record["output"]["filename"])
    return FileResponse(path, media_type="video/mp4", headers=headers)
