"""The editor's stage bar and live preview: one read, plus "Accept" for the
stages that have no approval of their own (footage, cutting, captions)."""

from fastapi import APIRouter, HTTPException

from . import pipeline, storage
from .models import PipelineAcceptRequest

router = APIRouter(prefix="/projects/{project_id}/pipeline", tags=["pipeline"])


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except storage.ProjectError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error


@router.get("")
def describe(project_id: str):
    return _call(pipeline.state, project_id)


@router.post("/{stage_id}/accept")
def accept(project_id: str, stage_id: str, request: PipelineAcceptRequest):
    return _call(pipeline.accept, project_id, stage_id, request.token)
