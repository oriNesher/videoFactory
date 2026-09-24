"""Catalog, provider-status and editing-plan endpoints.

Plan *generation* and plan *execution* are not endpoints that do the work: both
submit a job and return it, so neither ties up an HTTP request and both are
visible in the jobs panel.
"""

from typing import Any

from fastapi import APIRouter, HTTPException

from . import capabilities, job_tasks, jobs, llm, plans, resources, storage
from .models import GeneratePlanRequest, SavePlanRevisionRequest

router = APIRouter(tags=["planning"])


def _fail(error: storage.ProjectError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.message)


def _project(project_id: str) -> dict:
    return storage.read_project(project_id)


def _revision_number(raw: str) -> int:
    try:
        return plans.validate_revision_number(int(raw))
    except (TypeError, ValueError):
        raise plans.PlanNotFound("מספר גרסה לא חוקי.") from None


# --- catalogs and provider --------------------------------------------------


@router.get("/capabilities")
def get_capabilities():
    """What this application can be asked to do. Owned by the backend."""
    return capabilities.catalog()


@router.get("/llm")
def get_llm_status():
    """Which provider is configured. Never returns the API key."""
    return llm.provider_status()


@router.get("/projects/{project_id}/resources")
def get_resources(project_id: str):
    try:
        project = _project(project_id)
    except storage.ProjectError as error:
        raise _fail(error) from error

    catalog = resources.build_catalog(project)
    catalog["input_fingerprint"] = resources.fingerprint(project)
    return catalog


# --- plans ------------------------------------------------------------------


@router.get("/projects/{project_id}/plans")
def list_plans(project_id: str):
    try:
        project = _project(project_id)
        entries = plans.list_plans(project)
    except storage.ProjectError as error:
        raise _fail(error) from error

    return {"project_id": project_id, "plans": entries}


@router.post("/projects/{project_id}/plans/generate", status_code=201)
def generate_plan(project_id: str, request: GeneratePlanRequest):
    """Queue a plan-generation job. The model's output is a proposal only."""
    try:
        record = jobs.submit(
            project_id,
            job_tasks.PLAN_GENERATION_JOB,
            {"instruction": request.instruction},
        )
    except storage.ProjectError as error:
        raise _fail(error) from error

    return jobs.describe_job(record)


@router.get("/projects/{project_id}/plans/{plan_id}")
def get_plan(project_id: str, plan_id: str):
    try:
        project = _project(project_id)
        return plans.read_latest(project, plan_id)
    except storage.ProjectError as error:
        raise _fail(error) from error


@router.get("/projects/{project_id}/plans/{plan_id}/revisions/{revision}")
def get_plan_revision(project_id: str, plan_id: str, revision: str):
    try:
        project = _project(project_id)
        return plans.read_revision(project, plan_id, _revision_number(revision))
    except storage.ProjectError as error:
        raise _fail(error) from error


@router.post("/projects/{project_id}/plans/{plan_id}/revisions", status_code=201)
def save_plan_revision(
    project_id: str, plan_id: str, request: SavePlanRevisionRequest
):
    """Save an edit as a new revision. Earlier revisions are never rewritten."""
    try:
        project = _project(project_id)
        latest = plans.read_latest(project, plan_id)
        return plans.create_revision(
            project,
            summary=request.summary,
            actions=request.actions,
            origin=plans.ORIGIN_USER,
            provider=latest["provider"],
            instruction=latest["instruction"],
            plan_id=plan_id,
        )
    except storage.ProjectError as error:
        raise _fail(error) from error


@router.post("/projects/{project_id}/plans/{plan_id}/revisions/{revision}/approve")
def approve_plan_revision(project_id: str, plan_id: str, revision: str):
    """The explicit human step between a proposal and anything running."""
    try:
        project = _project(project_id)
        return plans.approve_revision(project, plan_id, _revision_number(revision))
    except storage.ProjectError as error:
        raise _fail(error) from error


@router.post(
    "/projects/{project_id}/plans/{plan_id}/revisions/{revision}/execute",
    status_code=201,
)
def execute_plan_revision(project_id: str, plan_id: str, revision: str):
    """Queue execution of an approved plan. Diagnostics only in this milestone."""
    try:
        payload: dict[str, Any] = {
            "plan_id": plan_id,
            "revision": _revision_number(revision),
        }
        record = jobs.submit(project_id, job_tasks.PLAN_EXECUTION_JOB, payload)
    except storage.ProjectError as error:
        raise _fail(error) from error

    return jobs.describe_job(record)
