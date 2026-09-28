"""Subtitle endpoints: settings, starting a Whisper job, and serving the files.

The same two rules as the cutting router: the client names ids, never paths,
and the work runs as a queued job rather than inside a request.
"""

import urllib.parse

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from . import jobs, job_tasks, storage, subtitles
from .models import SaveSubtitleSettingsRequest, StartSubtitlesRequest

router = APIRouter(prefix="/projects/{project_id}/subtitles", tags=["subtitles"])


def _fail(error: storage.ProjectError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.message)


@router.get("/settings")
def get_subtitle_settings(project_id: str):
    try:
        project = storage.read_project(project_id)
    except storage.ProjectError as error:
        raise _fail(error) from error

    return {
        "project_id": project["id"],
        "catalog": subtitles.settings_catalog(),
        **subtitles.read_settings(project),
    }


@router.put("/settings")
def save_subtitle_settings(project_id: str, request: SaveSubtitleSettingsRequest):
    try:
        saved = subtitles.save_settings(project_id, {"settings": request.settings})
    except storage.ProjectError as error:
        raise _fail(error) from error

    return {"project_id": project_id, "catalog": subtitles.settings_catalog(), **saved}


@router.post("/jobs", status_code=201)
def start_subtitles(project_id: str, request: StartSubtitlesRequest):
    """Queue Whisper for some clips of one cutting run. Returns the job."""
    try:
        record = jobs.submit(
            project_id,
            job_tasks.SUBTITLES_JOB,
            {
                "run_id": request.run_id,
                "output_ids": request.output_ids,
                "settings": request.settings,
            },
        )
    except storage.ProjectError as error:
        raise _fail(error) from error

    return jobs.describe_job(record)


def _resolve(project_id: str, run_id: str, output_id: str):
    try:
        storage.read_project(project_id)
        return subtitles.resolve_srt(project_id, run_id, output_id)
    except storage.ProjectError as error:
        raise _fail(error) from error


@router.get("/runs/{run_id}/outputs/{output_id}/vtt")
def subtitle_track(project_id: str, run_id: str, output_id: str):
    """The subtitles as WebVTT, for a <track> on the clip's player."""
    path = _resolve(project_id, run_id, output_id)
    srt = path.read_text(encoding="utf-8-sig", errors="replace")
    return Response(
        subtitles.srt_to_vtt(srt),
        media_type="text/vtt; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/runs/{run_id}/outputs/{output_id}/srt")
def download_srt(project_id: str, run_id: str, output_id: str):
    """The SRT itself, named after the clip, ready for Premiere."""
    path = _resolve(project_id, run_id, output_id)
    record = subtitles.read_record(project_id, run_id, output_id)
    filename = (record.get("current") or {}).get("filename") or path.name

    quoted = urllib.parse.quote(filename, safe="")
    disposition = "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
        filename.encode("ascii", "replace").decode("ascii"),
        quoted,
    )
    return FileResponse(
        path,
        media_type="application/x-subrip",
        headers={"Content-Disposition": disposition, "Cache-Control": "no-store"},
    )
