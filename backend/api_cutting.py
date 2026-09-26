"""Cutting endpoints: settings, runs, and serving the files a run produced.

Two rules govern this router.

**The client names ids, never paths.** A cut request carries project source
ids; a playback request carries a run id and an output id. Both are resolved
against files the backend itself wrote, inside that run's own directory. There
is deliberately no endpoint that takes a filesystem path, so there is nothing
to point at `C:\\Windows\\...`, and the resolved path is checked to be inside
the run directory even if a manifest were edited by hand.

**Running is a job, not a request.** Submitting a run goes through the existing
queue in `backend/jobs.py`, so cutting is cancellable, retryable and survives a
restart exactly like every other job. Nothing here renders video inline.
"""

import urllib.parse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from . import cutting, jobs, job_tasks, storage
from .models import SaveCuttingSettingsRequest, StartCuttingRunRequest

router = APIRouter(prefix="/projects/{project_id}/cutting", tags=["cutting"])

# Serving a file for playback in a <video> element is a read of something this
# application produced; the browser needs the media type to decide it can play
# it at all.
VIDEO_MEDIA_TYPE = "video/mp4"


def _fail(error: storage.ProjectError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.message)


@router.get("/settings")
def get_cutting_settings(project_id: str):
    """The saved form, plus everything needed to render it."""
    try:
        project = storage.read_project(project_id)
    except storage.ProjectError as error:
        raise _fail(error) from error

    return {
        "project_id": project["id"],
        "catalog": cutting.settings_catalog(),
        **cutting.read_settings(project),
    }


@router.put("/settings")
def save_cutting_settings(project_id: str, request: SaveCuttingSettingsRequest):
    try:
        saved = cutting.save_settings(
            project_id,
            {
                "settings": request.settings,
                "output_mode": request.output_mode,
                "source_ids": request.source_ids,
            },
        )
    except storage.ProjectError as error:
        raise _fail(error) from error

    return {
        "project_id": project_id,
        "catalog": cutting.settings_catalog(),
        **saved,
    }


@router.post("/runs", status_code=201)
def start_cutting_run(project_id: str, request: StartCuttingRunRequest):
    """Queue a cutting job. Returns the job, not a result: it runs in the queue.

    The submitted selection and settings are snapshotted by the validator, so
    editing the project afterwards cannot change what this run processes.
    """
    try:
        record = jobs.submit(
            project_id,
            job_tasks.CUT_MEDIA_JOB,
            {
                "source_ids": request.source_ids,
                "settings": request.settings,
                "output_mode": request.output_mode,
            },
        )
    except storage.ProjectError as error:
        raise _fail(error) from error

    return jobs.describe_job(record)


@router.get("/runs")
def list_cutting_runs(project_id: str):
    try:
        storage.read_project(project_id)
        runs = cutting.list_runs(project_id)
    except storage.ProjectError as error:
        raise _fail(error) from error

    return {
        "project_id": project_id,
        "runs": [cutting.describe_run(run) for run in runs],
    }


@router.get("/runs/{run_id}")
def get_cutting_run(project_id: str, run_id: str):
    try:
        storage.read_project(project_id)
        manifest = cutting.read_manifest(project_id, run_id)
    except storage.ProjectError as error:
        raise _fail(error) from error

    return cutting.describe_run(manifest)


def _resolve(project_id: str, run_id: str, output_id: str):
    try:
        storage.read_project(project_id)
        return cutting.resolve_output(project_id, run_id, output_id)
    except storage.ProjectError as error:
        raise _fail(error) from error


@router.get("/runs/{run_id}/outputs/{output_id}/stream")
def stream_output(project_id: str, run_id: str, output_id: str, request: Request):
    """Play a produced file in the browser, with seeking.

    `FileResponse` honours the `Range` header, which is what lets a `<video>`
    element seek instead of having to download the whole clip first. The
    request object is unused beyond documenting that this is a normal GET.
    """
    path, _entry = _resolve(project_id, run_id, output_id)

    return FileResponse(
        path,
        media_type=VIDEO_MEDIA_TYPE,
        # Nothing behind this URL is cached across runs: a run id never repeats,
        # so the browser may cache freely within one.
        headers={"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"},
    )


@router.get("/runs/{run_id}/outputs/{output_id}/download")
def download_output(project_id: str, run_id: str, output_id: str):
    """The same file, offered as a download under its own name."""
    path, entry = _resolve(project_id, run_id, output_id)

    filename = entry.get("filename") or path.name
    # A Hebrew filename cannot go in a bare `filename=`; RFC 5987's starred
    # form carries it correctly, with an ASCII fallback for anything that
    # cannot read it.
    quoted = urllib.parse.quote(filename, safe="")
    disposition = "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
        path.name.encode("ascii", "replace").decode("ascii"),
        quoted,
    )

    return FileResponse(
        path,
        media_type=VIDEO_MEDIA_TYPE,
        headers={"Content-Disposition": disposition},
    )
