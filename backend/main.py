from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import api_ai, api_jobs, jobs, projects, tools
from . import job_tasks  # noqa: F401  - importing it registers the job types
from .config import WORKSPACE_ENV_VAR, get_workspace_root, load_env_file

# Backend-only configuration, including provider credentials. Read once, at
# import, and never exposed through any endpoint.
load_env_file()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Any job still marked queued or running belongs to a process that no
    # longer exists: mark it interrupted instead of replaying it silently.
    jobs.start()
    try:
        yield
    finally:
        jobs.stop()


app = FastAPI(title="Video Factory", lifespan=lifespan)

app.include_router(projects.router)
app.include_router(api_jobs.router)
app.include_router(api_ai.router)


@app.get("/health")
def health():
    return {"status": "ok", "message": "Video Factory is running"}


@app.get("/workspace")
def workspace():
    """Where project metadata, intermediates and exports are stored."""
    return {
        "workspace": str(get_workspace_root()),
        "environment_variable": WORKSPACE_ENV_VAR,
    }


@app.get("/tools")
def tools_status():
    return tools.availability_report()


@app.get("/tools/versions")
def tools_versions():
    return tools.version_report()
