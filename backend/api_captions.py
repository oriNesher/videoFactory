"""Captions endpoints: inputs, transcription, caption revisions, style, exports.

The same rules as the cutting router: the browser names ids and keys, never
paths; anything slow is a queued job; files are served only after resolving
an id against a record this backend wrote.
"""

import urllib.parse

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from . import caption_inputs, caption_jobs, captions, jobs, storage, whisper_service
from .models import (
    CaptionExportRequest,
    CaptionImportRequest,
    CaptionOperationRequest,
    CaptionRegroupRequest,
    CaptionRevisionRequest,
    CaptionSelectionRequest,
    CaptionStyleRequest,
    CaptionTranscriptionRequest,
    CaptionValidateRequest,
    ModelDownloadRequest,
)

router = APIRouter(prefix="/projects/{project_id}/captions", tags=["captions"])


def _fail(error: storage.ProjectError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.message)


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except storage.ProjectError as error:
        raise _fail(error) from error


@router.get("/catalog")
def get_catalog(project_id: str):
    """Models, languages, devices, grouping and style controls, and readiness."""
    _call(storage.read_project, project_id)
    return {
        **captions.catalog(),
        "renderer": caption_jobs.renderer_status(),
        "whisper": whisper_service.service.status(),
    }


def _selected(project: dict) -> str | None:
    block = project.get("settings", {}).get(caption_inputs.SETTINGS_KEY)
    selected = block.get("selected_input") if isinstance(block, dict) else None
    return selected if isinstance(selected, str) else None


@router.get("/inputs")
def list_inputs(project_id: str):
    project = _call(storage.read_project, project_id)
    return {
        "inputs": _call(caption_inputs.list_inputs, project_id),
        "selected": _selected(project),
        **captions.read_style(project),
        "grouping": captions.read_grouping(project),
    }


@router.post("/imports", status_code=201)
def import_video(project_id: str, request: CaptionImportRequest):
    """Add a finished video for captioning only. No path opens the file dialog."""
    return _call(caption_inputs.add_import, project_id, request.path)


@router.put("/selection")
def select_input(project_id: str, request: CaptionSelectionRequest):
    """Remember which video the captions screen is working on.

    Switching deletes nothing: every transcript, revision and export stays on
    disk under its own input key.
    """
    key = request.input_key
    if key is not None:
        _call(caption_inputs.resolve, project_id, key)
    _call(caption_inputs.update_captions_block, project_id, {"selected_input": key})
    return {"selected": key}


def _caption_jobs(project_id: str, input_key: str | None) -> dict:
    active, recent = [], {}
    for record in jobs.list_jobs(project_id):
        if record["type"] not in caption_jobs.CAPTION_JOB_TYPES:
            continue
        record_key = (record["input"].get("input") or {}).get("key")
        if input_key is not None and record_key not in (None, input_key):
            continue
        summary = {
            "id": record["id"],
            "type": record["type"],
            "type_label": jobs.job_type_label(record["type"]),
            "status": record["status"],
            "progress_message": record["progress_message"],
            "progress_percent": record["progress_percent"],
            "cancel_requested": record["cancel_requested"],
            "created_at": record["created_at"],
            "finished_at": record["finished_at"],
            "error": record["error"],
            "result": record["result"],
            "format": record["input"].get("format"),
        }
        if record["status"] in jobs.ACTIVE_STATUSES:
            active.append(summary)
        else:
            recent.setdefault(record["type"], summary)
    return {"active_jobs": active, "recent_jobs": recent}


@router.get("/inputs/{input_key}")
def describe_input(project_id: str, input_key: str):
    """One video: metadata, current fingerprint, its transcripts and exports.

    Transcripts are listed for this key with a stale flag: one made from a
    different fingerprint is kept and shown, but marked as not matching the
    video as it is now.
    """
    described = _call(caption_inputs.describe, project_id, input_key)
    current = described["fingerprint"]
    transcripts = [
        captions.summarise_transcript(project_id, transcript, current)
        for transcript in _call(captions.list_transcripts, project_id)
        if transcript["input"]["key"] == input_key
    ]
    exports = [
        record for record in _call(caption_jobs.list_exports, project_id)
        if record.get("input_key") == input_key
    ]
    for record in exports:
        record["stale"] = current is None or record.get("input_fingerprint") != current["digest"]
    return {
        "input": described,
        "transcripts": transcripts,
        "exports": exports,
        **_caption_jobs(project_id, input_key),
    }


@router.get("/inputs/{input_key}/stream")
def stream_input(project_id: str, input_key: str):
    """The video itself, for the preview, with Range support. Key, not path."""
    path = _call(caption_inputs.resolve_stream_path, project_id, input_key)
    return FileResponse(path, media_type="video/mp4", headers={"Accept-Ranges": "bytes", "Cache-Control": "no-cache"})


@router.post("/transcriptions", status_code=201)
def start_transcription(project_id: str, request: CaptionTranscriptionRequest):
    """Queue Whisper — or, unless `force`, reuse a matching transcript.

    A transcript is reused when the video's fingerprint and every setting that
    changes Whisper's output are the same. `force` always makes a new
    transcript; the earlier one and its revisions stay.
    """
    raw = {"input_key": request.input_key, "settings": request.settings, "force": request.force is True}
    if request.force is not True:
        settings = _call(captions.validate_transcription_settings, request.settings)
        described = _call(caption_inputs.resolve_for_job, project_id, request.input_key)
        key = captions.reuse_key(described["fingerprint"]["digest"], settings)
        existing = _call(captions.find_reusable, project_id, key)
        if existing is not None:
            return {
                "reused": captions.summarise_transcript(project_id, existing, described["fingerprint"]),
                "job": None,
            }
    record = _call(jobs.submit, project_id, caption_jobs.TRANSCRIPTION_JOB, raw)
    return {"reused": None, "job": jobs.describe_job(record)}


@router.post("/model-downloads", status_code=201)
def download_model(project_id: str, request: ModelDownloadRequest):
    record = _call(jobs.submit, project_id, caption_jobs.MODEL_DOWNLOAD_JOB, {"model": request.model})
    return jobs.describe_job(record)


@router.post("/renderer-setup", status_code=201)
def prepare_renderer(project_id: str):
    record = _call(jobs.submit, project_id, caption_jobs.RENDERER_SETUP_JOB, {})
    return jobs.describe_job(record)


@router.get("/transcripts/{transcript_id}")
def get_transcript(project_id: str, transcript_id: str):
    transcript = _call(captions.read_transcript, project_id, transcript_id)
    current = None
    try:
        current = caption_inputs.describe(project_id, transcript["input"]["key"])["fingerprint"]
    except storage.ProjectError:
        pass
    return {
        **captions.summarise_transcript(project_id, transcript, current),
        "text": transcript["result"].get("text", ""),
        "duration_seconds": transcript["input"]["metadata"]["duration_seconds"],
        "whisper": transcript.get("whisper"),
    }


@router.get("/transcripts/{transcript_id}/revisions/{revision}")
def get_revision(project_id: str, transcript_id: str, revision: int):
    _call(storage.read_project, project_id)
    return _call(captions.read_revision, project_id, transcript_id, revision)


@router.post("/transcripts/{transcript_id}/validate")
def validate_captions(project_id: str, transcript_id: str, request: CaptionValidateRequest):
    """Check an unsaved draft. Writes nothing; used for inline errors."""
    transcript = _call(captions.read_transcript, project_id, transcript_id)
    normalised, errors = captions.normalise_captions(
        request.captions, transcript["input"]["metadata"]["duration_seconds"]
    )
    return {"captions": normalised, "errors": errors, "valid": not errors}


@router.post("/transcripts/{transcript_id}/operations")
def caption_operation(project_id: str, transcript_id: str, request: CaptionOperationRequest):
    """Split, merge or fix overlaps on an unsaved draft. Writes nothing.

    One implementation, here, for what the editor's buttons do; the result
    comes back with the same validation the save will apply.
    """
    transcript = _call(captions.read_transcript, project_id, transcript_id)
    draft = request.captions
    if not isinstance(draft, list) or not all(isinstance(item, dict) for item in draft):
        raise HTTPException(status_code=400, detail="The captions must be a list.")
    operation = request.operation
    if operation == "split":
        result = _call(captions.split_caption, draft, request.caption_id, request.word_index, request.at_seconds)
    elif operation == "merge":
        result = _call(captions.merge_with_next, draft, request.caption_id)
    elif operation == "fix_overlaps":
        timed = [item for item in draft if captions._finite(item.get("start")) and captions._finite(item.get("end"))]  # noqa: SLF001
        if len(timed) != len(draft):
            raise HTTPException(status_code=400, detail="Every caption needs a numeric start and end first.")
        result = captions.fix_overlaps(draft)
    else:
        raise HTTPException(status_code=400, detail="Unknown caption operation.")
    normalised, errors = captions.normalise_captions(result, transcript["input"]["metadata"]["duration_seconds"])
    return {"captions": result if errors else normalised, "errors": errors, "valid": not errors}


@router.post("/transcripts/{transcript_id}/revisions", status_code=201)
def save_revision(project_id: str, transcript_id: str, request: CaptionRevisionRequest):
    _call(storage.read_project, project_id)
    return _call(
        captions.save_edited_revision,
        project_id,
        transcript_id,
        base_revision=request.base_revision,
        captions=request.captions,
    )


@router.post("/transcripts/{transcript_id}/regroup", status_code=201)
def regroup(project_id: str, transcript_id: str, request: CaptionRegroupRequest):
    _call(storage.read_project, project_id)
    return _call(
        captions.regroup,
        project_id,
        transcript_id,
        base_revision=request.base_revision,
        grouping=request.grouping,
        confirm_discard_edits=request.confirm_discard_edits is True,
    )


@router.get("/style")
def get_style(project_id: str):
    return captions.read_style(_call(storage.read_project, project_id))


@router.put("/style")
def save_style(project_id: str, request: CaptionStyleRequest):
    """Style only. Never re-transcribes and never changes a caption revision."""
    return _call(captions.save_style, project_id, request.style)


@router.post("/exports", status_code=201)
def start_export(project_id: str, request: CaptionExportRequest):
    record = _call(
        jobs.submit,
        project_id,
        caption_jobs.EXPORT_JOB,
        {"transcript_id": request.transcript_id, "revision": request.revision, "format": request.format},
    )
    return jobs.describe_job(record)


@router.get("/exports")
def list_exports(project_id: str):
    _call(storage.read_project, project_id)
    return {"exports": _call(caption_jobs.list_exports, project_id)}


@router.get("/exports/{export_id}/file")
def export_file(project_id: str, export_id: str, download: bool = False):
    _call(storage.read_project, project_id)
    path, record = _call(caption_jobs.resolve_export_file, project_id, export_id)
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
    if download:
        filename = record["output"]["filename"]
        headers["Content-Disposition"] = "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
            filename.encode("ascii", "replace").decode("ascii"),
            urllib.parse.quote(filename, safe=""),
        )
    return FileResponse(path, media_type=record["output"]["media_type"], headers=headers)
