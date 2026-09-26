"""Focused tests for the job queue: lifecycle, persistence, restart, cancel, retry.

Everything runs against a throwaway workspace, so the developer's real
`workspace/` and any real footage are never touched.
"""

import json
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import jobs  # noqa: E402
from backend.config import WORKSPACE_ENV_VAR  # noqa: E402
from backend.main import app  # noqa: E402

FINISHED = jobs.FINISHED_STATUSES


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    monkeypatch.setenv(WORKSPACE_ENV_VAR, str(root))
    return root


@pytest.fixture
def client():
    with TestClient(app) as running:
        yield running


@pytest.fixture
def project(client):
    response = client.post("/projects", json={"name": "פרויקט משימות"})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def blocking_job_type():
    """A job type that waits until the test releases it.

    Registering one here is how the *running* states — progress, cooperative
    cancellation, interruption — become deterministic instead of racing a
    tool check that finishes in milliseconds.
    """
    started = threading.Event()
    release = threading.Event()

    def handler(context):
        context.progress("Working…", percent=25.0)
        started.set()
        for _ in range(200):
            context.raise_if_cancelled()
            if release.wait(timeout=0.05):
                break
        context.raise_if_cancelled()
        return {"done": True}

    jobs.register_job_type("test_blocking", "Test job", handler, lambda p, raw: raw or {})
    try:
        yield started, release
    finally:
        release.set()
        jobs.JOB_TYPES.pop("test_blocking", None)


def submit(client, project_id, job_type="tool_check", payload=None):
    response = client.post(
        f"/projects/{project_id}/jobs", json={"type": job_type, "input": payload}
    )
    assert response.status_code == 201, response.text
    return response.json()


def wait_for_status(client, project_id, job_id, statuses, timeout=15.0):
    deadline = time.monotonic() + timeout
    record = None

    while time.monotonic() < deadline:
        record = client.get(f"/projects/{project_id}/jobs/{job_id}").json()
        if record["status"] in statuses:
            return record
        time.sleep(0.05)

    raise AssertionError("job stayed in %s" % (record or {}).get("status"))


# --- lifecycle --------------------------------------------------------------


def test_submitting_a_tool_check_returns_immediately_and_then_succeeds(client, project):
    job = submit(client, project["id"])

    # The request returns before the work happens: that is the whole point.
    assert job["status"] in (jobs.QUEUED, jobs.RUNNING)
    assert job["type"] == "tool_check"
    assert job["type_label"]
    assert job["created_at"]

    finished = wait_for_status(client, project["id"], job["id"], {jobs.SUCCEEDED})
    assert finished["started_at"] and finished["finished_at"]
    assert finished["error"] is None
    assert finished["progress_percent"] == 100.0

    result = finished["result"]
    assert result["checked_count"] == 3
    assert {tool["tool"] for tool in result["tools"]} == {
        "ffmpeg",
        "ffprobe",
        "auto_editor",
    }


def test_the_interface_stays_usable_while_a_job_runs(client, project, blocking_job_type):
    started, release = blocking_job_type
    job = submit(client, project["id"], "test_blocking")
    assert started.wait(timeout=5)

    # Other endpoints answer normally while the worker is busy.
    assert client.get("/projects").status_code == 200
    assert client.get(f"/projects/{project['id']}").status_code == 200
    assert client.get("/capabilities").status_code == 200

    running = client.get(f"/projects/{project['id']}/jobs/{job['id']}").json()
    assert running["status"] == jobs.RUNNING
    assert running["progress_message"] == "Working…"
    assert running["progress_percent"] == 25.0

    release.set()
    wait_for_status(client, project["id"], job["id"], {jobs.SUCCEEDED})


def test_jobs_are_persisted_per_project_as_versioned_json(client, project, workspace):
    job = submit(client, project["id"])
    wait_for_status(client, project["id"], job["id"], FINISHED)

    path = workspace / "projects" / project["id"] / "jobs" / f"{job['id']}.json"
    on_disk = json.loads(path.read_text(encoding="utf-8"))

    assert on_disk["schema_version"] == jobs.JOB_SCHEMA_VERSION
    assert on_disk["project_id"] == project["id"]
    assert on_disk["type"] == "tool_check"
    assert on_disk["input"] == {"tools": ["ffmpeg", "ffprobe", "auto_editor"]}


def test_job_results_survive_a_backend_restart(client, project):
    job = submit(client, project["id"])
    finished = wait_for_status(client, project["id"], job["id"], {jobs.SUCCEEDED})

    # A fresh client runs the startup hook again: a restart.
    with TestClient(app) as restarted:
        reopened = restarted.get(f"/projects/{project['id']}/jobs/{job['id']}").json()

    assert reopened["status"] == jobs.SUCCEEDED
    assert reopened["result"] == finished["result"]
    assert reopened["finished_at"] == finished["finished_at"]


def test_listing_is_newest_first_and_scoped_to_the_project(client, project):
    other = client.post("/projects", json={"name": "פרויקט אחר"}).json()

    first = submit(client, project["id"])
    second = submit(client, project["id"])
    elsewhere = submit(client, other["id"])

    for job_id, owner in ((first["id"], project), (second["id"], project)):
        wait_for_status(client, owner["id"], job_id, FINISHED)
    wait_for_status(client, other["id"], elsewhere["id"], FINISHED)

    listing = client.get(f"/projects/{project['id']}/jobs").json()
    ids = [record["id"] for record in listing["jobs"]]

    assert ids == [second["id"], first["id"]]
    assert elsewhere["id"] not in ids


# --- interruption -----------------------------------------------------------


def test_unfinished_jobs_become_interrupted_on_restart(client, project, workspace):
    """A record left `running` by a killed process must not stay running."""
    job = submit(client, project["id"])
    wait_for_status(client, project["id"], job["id"], FINISHED)

    path = workspace / "projects" / project["id"] / "jobs" / f"{job['id']}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update({"status": "running", "finished_at": None, "result": None})
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    with TestClient(app) as restarted:
        recovered = restarted.get(
            f"/projects/{project['id']}/jobs/{job['id']}"
        ).json()

    assert recovered["status"] == jobs.INTERRUPTED
    assert recovered["finished_at"]
    assert "restarted" in recovered["error"]


def test_an_interrupted_job_is_not_replayed(client, project, workspace):
    job = submit(client, project["id"])
    wait_for_status(client, project["id"], job["id"], FINISHED)

    path = workspace / "projects" / project["id"] / "jobs" / f"{job['id']}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(
        {"status": "queued", "started_at": None, "finished_at": None, "result": None}
    )
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    with TestClient(app) as restarted:
        time.sleep(0.3)
        after = restarted.get(f"/projects/{project['id']}/jobs/{job['id']}").json()

    assert after["status"] == jobs.INTERRUPTED
    assert after["result"] is None


# --- cancellation -----------------------------------------------------------


def test_a_running_job_is_cancelled_only_once_it_actually_stops(
    client, project, blocking_job_type
):
    started, release = blocking_job_type
    job = submit(client, project["id"], "test_blocking")
    assert started.wait(timeout=5)

    response = client.post(f"/projects/{project['id']}/jobs/{job['id']}/cancel")
    assert response.status_code == 200

    # Honest reporting: the request was recorded, the job is still running.
    asked = response.json()
    assert asked["status"] == jobs.RUNNING
    assert asked["cancel_requested"] is True
    assert "Cancellation" in asked["progress_message"]

    cancelled = wait_for_status(client, project["id"], job["id"], {jobs.CANCELLED})
    assert cancelled["finished_at"]
    assert cancelled["result"] is None


def test_a_queued_job_is_cancelled_immediately(client, project, blocking_job_type):
    started, release = blocking_job_type

    # The single worker is occupied, so the second job is genuinely queued.
    blocker = submit(client, project["id"], "test_blocking")
    assert started.wait(timeout=5)
    queued = submit(client, project["id"], "test_blocking")

    cancelled = client.post(
        f"/projects/{project['id']}/jobs/{queued['id']}/cancel"
    ).json()
    assert cancelled["status"] == jobs.CANCELLED
    assert cancelled["started_at"] is None

    release.set()
    wait_for_status(client, project["id"], blocker["id"], {jobs.SUCCEEDED})

    # Releasing the worker must not resurrect the cancelled job.
    time.sleep(0.3)
    after = client.get(f"/projects/{project['id']}/jobs/{queued['id']}").json()
    assert after["status"] == jobs.CANCELLED


def test_cancelling_a_finished_job_is_rejected(client, project):
    job = submit(client, project["id"])
    wait_for_status(client, project["id"], job["id"], FINISHED)

    response = client.post(f"/projects/{project['id']}/jobs/{job['id']}/cancel")
    assert response.status_code == 409


# --- failure and retry ------------------------------------------------------


def test_a_failing_handler_is_reported_as_failed_with_a_message(client, project):
    def handler(context):
        raise jobs.JobFailed("Tool not found.")

    jobs.register_job_type("test_failing", "Failing job", handler, lambda p, raw: {})
    try:
        job = submit(client, project["id"], "test_failing")
        failed = wait_for_status(client, project["id"], job["id"], {jobs.FAILED})
    finally:
        jobs.JOB_TYPES.pop("test_failing", None)

    assert failed["error"] == "Tool not found."
    assert failed["result"] is None
    assert failed["finished_at"]


def test_retry_creates_a_new_job_from_the_previous_input_snapshot(client, project):
    original = submit(client, project["id"], payload={"tools": ["ffprobe"]})
    wait_for_status(client, project["id"], original["id"], FINISHED)

    response = client.post(f"/projects/{project['id']}/jobs/{original['id']}/retry")
    assert response.status_code == 201

    retried = response.json()
    assert retried["id"] != original["id"]
    assert retried["retry_of"] == original["id"]
    assert retried["input"] == {"tools": ["ffprobe"]}

    finished = wait_for_status(client, project["id"], retried["id"], {jobs.SUCCEEDED})
    assert finished["result"]["checked_count"] == 1

    # The original record is untouched.
    assert client.get(
        f"/projects/{project['id']}/jobs/{original['id']}"
    ).json()["status"] == jobs.SUCCEEDED


def test_retrying_an_active_job_is_rejected(client, project, blocking_job_type):
    started, _ = blocking_job_type
    job = submit(client, project["id"], "test_blocking")
    assert started.wait(timeout=5)

    response = client.post(f"/projects/{project['id']}/jobs/{job['id']}/retry")
    assert response.status_code == 409


# --- validation -------------------------------------------------------------


def test_unknown_job_type_is_rejected(client, project):
    response = client.post(
        f"/projects/{project['id']}/jobs", json={"type": "render_everything"}
    )
    assert response.status_code == 400
    assert "tool_check" in response.json()["detail"]


def test_invalid_job_input_is_rejected_before_queueing(client, project):
    response = client.post(
        f"/projects/{project['id']}/jobs",
        json={"type": "tool_check", "input": {"tools": ["premiere"]}},
    )
    assert response.status_code == 400
    assert client.get(f"/projects/{project['id']}/jobs").json()["jobs"] == []


def test_jobs_of_an_unknown_project_are_rejected(client):
    assert client.get("/projects/%s/jobs" % ("0" * 32)).status_code == 404
    assert client.get(f"/projects/{'0' * 32}/jobs/{'1' * 32}").status_code == 404


def test_unknown_job_id_returns_404(client, project):
    assert (
        client.get(f"/projects/{project['id']}/jobs/{'a' * 32}").status_code == 404
    )
    assert client.get(f"/projects/{project['id']}/jobs/nope").status_code == 404


def test_existing_tool_endpoints_still_work(client):
    assert set(client.get("/tools").json()) == {"ffmpeg", "ffprobe", "auto_editor"}
    assert set(client.get("/tools/versions").json()) == {
        "ffmpeg",
        "ffprobe",
        "auto_editor",
    }
