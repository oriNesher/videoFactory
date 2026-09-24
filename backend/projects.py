"""Project management endpoints."""

from fastapi import APIRouter, HTTPException

from . import storage
from .config import get_workspace_root
from .models import AddSourceRequest, CreateProjectRequest, SaveProjectRequest

router = APIRouter(prefix="/projects", tags=["projects"])


def _fail(error: storage.ProjectError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.message)


@router.get("")
def list_projects():
    return {
        "workspace": str(get_workspace_root()),
        "projects": storage.list_projects(),
    }


@router.post("", status_code=201)
def create_project(request: CreateProjectRequest):
    try:
        project = storage.create_project(request.name)
    except storage.ProjectError as error:
        raise _fail(error) from error

    return storage.describe_project(project)


@router.get("/{project_id}")
def get_project(project_id: str):
    try:
        project = storage.read_project(project_id)
    except storage.ProjectError as error:
        raise _fail(error) from error

    return storage.describe_project(project)


@router.put("/{project_id}")
def save_project(project_id: str, request: SaveProjectRequest):
    try:
        project = storage.save_project(project_id, request.name, request.source_ids)
    except storage.ProjectError as error:
        raise _fail(error) from error

    return storage.describe_project(project)


@router.post("/{project_id}/sources", status_code=201)
def add_source(project_id: str, request: AddSourceRequest):
    try:
        project = storage.add_source(project_id, request.path)
    except storage.ProjectError as error:
        raise _fail(error) from error

    return storage.describe_project(project)
