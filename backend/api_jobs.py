"""Job endpoints: submit, list, inspect, cancel, retry."""

from fastapi import APIRouter, HTTPException

from . import jobs, storage
from .models import SubmitJobRequest

router = APIRouter(prefix="/projects/{project_id}/jobs", tags=["jobs"])


def _fail(error: storage.ProjectError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.message)


@router.get("")
def list_project_jobs(project_id: str):
    try:
        storage.read_project(project_id)
        records = jobs.list_jobs(project_id)
    except storage.ProjectError as error:
        raise _fail(error) from error

    return {
        "project_id": project_id,
        "job_types": [
            {"type": name, "label": definition["label"]}
            for name, definition in sorted(jobs.JOB_TYPES.items())
        ],
        "jobs": [jobs.describe_job(record) for record in records],
    }


@router.post("", status_code=201)
def submit_job(project_id: str, request: SubmitJobRequest):
    try:
        record = jobs.submit(project_id, request.type, request.input)
    except storage.ProjectError as error:
        raise _fail(error) from error

    return jobs.describe_job(record)


@router.get("/{job_id}")
def get_job(project_id: str, job_id: str):
    try:
        record = jobs.read_job(project_id, job_id)
    except storage.ProjectError as error:
        raise _fail(error) from error

    return jobs.describe_job(record)


@router.post("/{job_id}/cancel")
def cancel_job(project_id: str, job_id: str):
    """Ask for cancellation. A queued job stops now; a running job is asked to."""
    try:
        record = jobs.cancel(project_id, job_id)
    except storage.ProjectError as error:
        raise _fail(error) from error

    return jobs.describe_job(record)


@router.post("/{job_id}/retry", status_code=201)
def retry_job(project_id: str, job_id: str):
    """Run the previous input snapshot again as a new job."""
    try:
        record = jobs.retry(project_id, job_id)
    except storage.ProjectError as error:
        raise _fail(error) from error

    return jobs.describe_job(record)
