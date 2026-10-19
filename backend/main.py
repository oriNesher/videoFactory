from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import api_ai, api_broll, api_captions, api_cutting, api_jobs, api_music, api_pipeline, api_subtitles, api_zooms, jobs, projects, tools
from . import whisper_service
from . import broll_jobs, broll_planner_jobs, caption_jobs, job_tasks, music_jobs, zoom_jobs  # noqa: F401  - importing them registers the job types
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
        # The Whisper worker keeps a model loaded between jobs; never leave it
        # running after the server.
        whisper_service.service.stop()


app = FastAPI(title="Video Factory", lifespan=lifespan)

app.include_router(projects.router)
app.include_router(api_jobs.router)
app.include_router(api_cutting.router)
app.include_router(api_subtitles.router)
app.include_router(api_captions.router)
app.include_router(api_zooms.router)
app.include_router(api_broll.router)
app.include_router(api_broll.library_router)
app.include_router(api_music.router)
app.include_router(api_music.library_router)
app.include_router(api_ai.router)
app.include_router(api_pipeline.router)


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
