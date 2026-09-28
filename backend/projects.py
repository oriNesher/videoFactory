"""Project management endpoints."""

from fastapi import APIRouter, HTTPException

from . import folders, player, storage
from .config import get_workspace_root
from .models import (
    AddSourceRequest,
    AddSourceDirectoryRequest,
    CreateProjectRequest,
    SaveProjectRequest,
)

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


@router.post("/{project_id}/sources/directory", status_code=201)
def add_source_directory(project_id: str, request: AddSourceDirectoryRequest):
    """Add every video file in a folder, ordered by file name.

    With no `path`, the backend opens the machine's own folder dialog first —
    the browser cannot tell a web page where a folder really lives on disk.
    """
    try:
        path = request.path
        if path is None:
            chosen = folders.choose_directory()
            if chosen["cancelled"]:
                return {"cancelled": True}
            path = chosen["path"]

        return {"cancelled": False, **folders.add_directory(project_id, path)}
    except storage.ProjectError as error:
        raise _fail(error) from error


@router.post("/{project_id}/sources/{source_id}/open")
def open_source(project_id: str, source_id: str):
    """Open one of the project's files in VLC on this machine."""
    try:
        player.open_source(project_id, source_id)
    except storage.ProjectError as error:
        raise _fail(error) from error

    return {"opened": True}
