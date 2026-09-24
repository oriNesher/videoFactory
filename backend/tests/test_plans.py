"""Focused tests for editing plans.

Covers plan validation, immutable revisions, explicit approval, outdated-input
detection, provider failures (mocked — no request ever leaves this machine) and
the one execution path this milestone supports.
"""

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import capabilities, jobs, llm  # noqa: E402
from backend.config import (  # noqa: E402
    API_KEY_ENV_VAR,
    MODEL_ENV_VAR,
    PROVIDER_ENV_VAR,
    WORKSPACE_ENV_VAR,
)
from backend.main import app  # noqa: E402

TOOL_CHECK = capabilities.TOOL_CHECK


@pytest.fixture(autouse=True)
def environment(tmp_path, monkeypatch):
    """A throwaway workspace and mock mode, whatever the developer's .env says."""
    monkeypatch.setenv(WORKSPACE_ENV_VAR, str(tmp_path / "workspace"))
    monkeypatch.setenv(PROVIDER_ENV_VAR, "mock")
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    monkeypatch.delenv(MODEL_ENV_VAR, raising=False)


@pytest.fixture
def client():
    with TestClient(app) as running:
        yield running


@pytest.fixture
def project(client):
    return client.post("/projects", json={"name": "פרויקט תוכניות"}).json()


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "take 1.mp4"
    path.write_bytes(b"not a real video")
    return path


def wait_for_job(client, project_id, job_id, timeout=15.0):
    deadline = time.monotonic() + timeout
    record = None

    while time.monotonic() < deadline:
        record = client.get(f"/projects/{project_id}/jobs/{job_id}").json()
        if record["status"] in jobs.FINISHED_STATUSES:
            return record
        time.sleep(0.05)

    raise AssertionError("job did not finish: %s" % (record or {}).get("status"))


def generate(client, project_id, instruction="בדוק אילו כלי עיבוד מותקנים"):
    response = client.post(
        f"/projects/{project_id}/plans/generate", json={"instruction": instruction}
    )
    assert response.status_code == 201, response.text
    return wait_for_job(client, project_id, response.json()["id"])


def generated_plan(client, project):
    job = generate(client, project["id"])
    assert job["status"] == jobs.SUCCEEDED, job.get("error")
    assert job["result"]["supported"] is True
    return client.get(
        f"/projects/{project['id']}/plans/{job['result']['plan_id']}"
    ).json()


def save_revision(client, project_id, plan_id, summary, actions):
    return client.post(
        f"/projects/{project_id}/plans/{plan_id}/revisions",
        json={"summary": summary, "actions": actions},
    )


def approve(client, project_id, plan_id, revision):
    return client.post(
        f"/projects/{project_id}/plans/{plan_id}/revisions/{revision}/approve"
    )


def execute(client, project_id, plan_id, revision):
    return client.post(
        f"/projects/{project_id}/plans/{plan_id}/revisions/{revision}/execute"
    )


# --- catalogs ---------------------------------------------------------------


def test_capability_catalog_only_lists_implemented_capabilities(client):
    catalog = client.get("/capabilities").json()
    ids = [capability["id"] for capability in catalog["capabilities"]]

    assert ids == [TOOL_CHECK]
    assert catalog["capabilities"][0]["kind"] == capabilities.KIND_DIAGNOSTIC
    assert catalog["capabilities"][0]["executable"] is True
    # Editing areas are described as unsupported text, not as capabilities.
    assert catalog["not_yet_supported"]


def test_resource_catalog_exposes_ids_not_local_paths(client, project, video):
    client.post(f"/projects/{project['id']}/sources", json={"path": str(video)})

    catalog = client.get(f"/projects/{project['id']}/resources").json()
    resource = catalog["resources"][0]

    assert resource["filename"] == "take 1.mp4"
    assert resource["media_type"] == "video"
    assert resource["available"] is True
    assert "path" not in resource
    assert str(video.parent) not in json.dumps(catalog, ensure_ascii=False)
    assert catalog["note"]


# --- mock mode --------------------------------------------------------------


def test_mock_mode_is_labelled_and_needs_no_api_key(client):
    status = client.get("/llm").json()

    assert status["provider"] == "mock"
    assert status["is_mock"] is True
    assert status["ready"] is True
    assert status["data_sent"] == []


def test_generating_a_plan_in_mock_mode_creates_a_proposal(client, project):
    plan = generated_plan(client, project)

    assert plan["revision"] == 1
    assert plan["status"] == "proposed"
    assert plan["approved"] is False
    assert plan["outdated"] is False
    assert plan["executable"] is False  # not approved yet
    assert plan["provider"]["is_mock"] is True
    assert plan["origin"] == "ai"
    assert plan["capability_catalog_version"] == capabilities.CATALOG_VERSION
    assert plan["input_fingerprint"].startswith("sha256:")

    action = plan["actions"][0]
    assert action["capability_id"] == TOOL_CHECK
    assert action["parameters"]["tools"] == ["ffmpeg", "ffprobe", "auto_editor"]
    assert action["resource_ids"] == []


def test_an_unsupported_request_is_explained_not_invented(client, project):
    job = generate(client, project["id"], "תחתוך את כל השתיקות ותוסיף כתוביות")

    assert job["status"] == jobs.SUCCEEDED
    assert job["result"]["supported"] is False
    assert job["result"]["explanation"]
    # No plan is fabricated to look helpful.
    assert client.get(f"/projects/{project['id']}/plans").json()["plans"] == []


def test_an_empty_instruction_is_rejected(client, project):
    response = client.post(
        f"/projects/{project['id']}/plans/generate", json={"instruction": "   "}
    )
    assert response.status_code == 400


# --- validation -------------------------------------------------------------


@pytest.mark.parametrize(
    "actions, fragment",
    [
        ([{"id": "a1", "capability_id": "video.cut_silence", "parameters": {}}], "אינה קיימת"),
        ([{"id": "a1", "capability_id": TOOL_CHECK, "parameters": {"tools": ["premiere"]}}], "אינו נתמך"),
        ([{"id": "a1", "capability_id": TOOL_CHECK, "parameters": {"speed": 2}}], "אינו מוכר"),
        ([{"id": "a1", "capability_id": TOOL_CHECK, "parameters": {"tools": "ffmpeg"}}], "רשימה"),
        ([{"id": "a1", "capability_id": TOOL_CHECK, "parameters": {}, "resource_ids": ["nope"]}], "אינה פועלת על חומרי גלם"),
        ([{"capability_id": TOOL_CHECK, "parameters": {}}], "מזהה"),
        ([], "לפחות פעולה אחת"),
    ],
)
def test_invalid_actions_are_rejected_with_a_clear_message(
    client, project, actions, fragment
):
    plan = generated_plan(client, project)

    response = save_revision(client, project["id"], plan["plan_id"], "תקציר", actions)
    assert response.status_code == 400
    assert fragment in response.json()["detail"]


def test_duplicate_action_ids_are_rejected(client, project):
    plan = generated_plan(client, project)
    action = dict(plan["actions"][0])

    response = save_revision(
        client, project["id"], plan["plan_id"], "תקציר", [action, dict(action)]
    )
    assert response.status_code == 400
    assert "כבר בשימוש" in response.json()["detail"]


def test_a_plan_can_never_carry_a_command_or_code(client, project):
    plan = generated_plan(client, project)

    for forbidden in ("command", "script", "code", "python"):
        response = save_revision(
            client,
            project["id"],
            plan["plan_id"],
            "תקציר",
            [
                {
                    "id": "a1",
                    "capability_id": TOOL_CHECK,
                    "parameters": {forbidden: "ffmpeg -i in.mp4 out.mp4"},
                }
            ],
        )
        assert response.status_code == 400
        assert "פקודות" in response.json()["detail"]


def test_an_unknown_resource_reference_is_rejected(client, project, video, monkeypatch):
    """A capability that *does* take resources must still reject unknown ids."""
    monkeypatch.setitem(
        capabilities.CAPABILITIES[TOOL_CHECK], "max_resources", 1
    )
    monkeypatch.setitem(
        capabilities.CAPABILITIES[TOOL_CHECK], "resource_types", ["video"]
    )
    plan = generated_plan(client, project)

    response = save_revision(
        client,
        project["id"],
        plan["plan_id"],
        "תקציר",
        [
            {
                "id": "a1",
                "capability_id": TOOL_CHECK,
                "parameters": {},
                "resource_ids": ["0" * 32],
            }
        ],
    )
    assert response.status_code == 400
    assert "אינו קיים בפרויקט" in response.json()["detail"]


def test_a_plan_belongs_to_one_project(client, project):
    plan = generated_plan(client, project)
    other = client.post("/projects", json={"name": "פרויקט אחר"}).json()

    response = client.get(f"/projects/{other['id']}/plans/{plan['plan_id']}")
    assert response.status_code == 404


# --- revisions and approval -------------------------------------------------


def test_editing_creates_a_new_revision_and_never_rewrites_the_old_one(client, project):
    plan = generated_plan(client, project)
    plan_id = plan["plan_id"]

    directory = Path(
        client.get(f"/projects/{project['id']}").json()["directory"]
    ) / "plans" / plan_id
    before = (directory / "rev-0001.json").read_text(encoding="utf-8")

    edited = dict(plan["actions"][0])
    edited["parameters"] = {"tools": ["ffmpeg"]}
    edited["note"] = "רק FFmpeg מספיק"

    response = save_revision(
        client, project["id"], plan_id, "תקציר מעודכן", [edited]
    )
    assert response.status_code == 201

    revision = response.json()
    assert revision["revision"] == 2
    assert revision["origin"] == "user"
    assert revision["summary"] == "תקציר מעודכן"
    assert revision["actions"][0]["parameters"]["tools"] == ["ffmpeg"]
    assert revision["revisions"] == [1, 2]

    # Revision 1 is still on disk, byte for byte.
    assert (directory / "rev-0001.json").read_text(encoding="utf-8") == before
    old = client.get(
        f"/projects/{project['id']}/plans/{plan_id}/revisions/1"
    ).json()
    assert old["actions"][0]["parameters"]["tools"] == [
        "ffmpeg",
        "ffprobe",
        "auto_editor",
    ]
    assert old["is_latest"] is False


def test_approval_is_explicit_and_per_revision(client, project):
    plan = generated_plan(client, project)
    plan_id = plan["plan_id"]

    approved = approve(client, project["id"], plan_id, 1)
    assert approved.status_code == 200

    body = approved.json()
    assert body["approved"] is True
    assert body["status"] == "approved"
    assert body["approved_at"]
    assert body["executable"] is True


def test_editing_an_approved_plan_requires_approval_again(client, project):
    plan = generated_plan(client, project)
    plan_id = plan["plan_id"]
    approve(client, project["id"], plan_id, 1)

    edited = dict(plan["actions"][0])
    edited["parameters"] = {"tools": ["ffprobe"]}
    revision = save_revision(
        client, project["id"], plan_id, "תקציר חדש", [edited]
    ).json()

    assert revision["revision"] == 2
    assert revision["approved"] is False
    assert revision["status"] == "proposed"
    assert revision["executable"] is False

    # And the approval of revision 1 is still recorded, on revision 1 only.
    first = client.get(
        f"/projects/{project['id']}/plans/{plan_id}/revisions/1"
    ).json()
    assert first["approved"] is True
    assert first["is_latest"] is False
    assert first["executable"] is False  # superseded


def test_only_the_latest_revision_can_be_approved(client, project):
    plan = generated_plan(client, project)
    plan_id = plan["plan_id"]
    save_revision(client, project["id"], plan_id, "תקציר חדש", plan["actions"])

    response = approve(client, project["id"], plan_id, 1)
    assert response.status_code == 400
    assert "הגרסה האחרונה" in response.json()["detail"]


# --- outdated inputs --------------------------------------------------------


def test_changing_project_sources_makes_a_plan_outdated(client, project, video):
    plan = generated_plan(client, project)
    plan_id = plan["plan_id"]
    approve(client, project["id"], plan_id, 1)

    # A relevant project change: a new source.
    assert (
        client.post(
            f"/projects/{project['id']}/sources", json={"path": str(video)}
        ).status_code
        == 201
    )

    reread = client.get(f"/projects/{project['id']}/plans/{plan_id}").json()
    assert reread["outdated"] is True
    assert reread["outdated_reason"]
    assert reread["executable"] is False

    assert execute(client, project["id"], plan_id, 1).status_code == 400
    assert approve(client, project["id"], plan_id, 1).status_code == 400


def test_renaming_the_project_does_not_outdate_a_plan(client, project):
    plan = generated_plan(client, project)

    client.put(
        f"/projects/{project['id']}", json={"name": "שם חדש", "source_ids": []}
    )

    reread = client.get(f"/projects/{project['id']}/plans/{plan['plan_id']}").json()
    assert reread["outdated"] is False


def test_a_regenerated_plan_is_current_again(client, project, video):
    plan = generated_plan(client, project)
    client.post(f"/projects/{project['id']}/sources", json={"path": str(video)})
    assert client.get(
        f"/projects/{project['id']}/plans/{plan['plan_id']}"
    ).json()["outdated"] is True

    fresh = generated_plan(client, project)
    assert fresh["plan_id"] != plan["plan_id"]
    assert fresh["outdated"] is False


# --- execution --------------------------------------------------------------


def test_an_approved_diagnostic_plan_can_be_executed(client, project):
    plan = generated_plan(client, project)
    plan_id = plan["plan_id"]
    approve(client, project["id"], plan_id, 1)

    response = execute(client, project["id"], plan_id, 1)
    assert response.status_code == 201

    finished = wait_for_job(client, project["id"], response.json()["id"])
    assert finished["status"] == jobs.SUCCEEDED

    result = finished["result"]
    assert result["plan_id"] == plan_id
    assert len(result["action_results"]) == 1
    assert result["action_results"][0]["capability_id"] == TOOL_CHECK
    assert result["action_results"][0]["output"]["checked_count"] == 3


def test_an_unapproved_plan_cannot_be_executed(client, project):
    plan = generated_plan(client, project)

    response = execute(client, project["id"], plan["plan_id"], 1)
    assert response.status_code == 400
    assert "אושר" in response.json()["detail"]


def test_generation_does_not_approve_or_execute_anything(client, project):
    generated_plan(client, project)

    types = [
        record["type"]
        for record in client.get(f"/projects/{project['id']}/jobs").json()["jobs"]
    ]
    assert types == ["plan_generation"]


# --- provider failures ------------------------------------------------------


class _BrokenProvider:
    id = "anthropic"
    label = "Anthropic Claude"
    model = "claude-opus-5"
    is_mock = False

    def __init__(self, error):
        self._error = error

    def describe(self):
        return {"id": self.id, "label": self.label, "model": self.model, "is_mock": False}

    def propose(self, request):
        raise self._error


class _ScriptedProvider(_BrokenProvider):
    def __init__(self, payload):
        self._payload = payload

    def propose(self, request):
        return self._payload


def test_a_provider_timeout_fails_the_job_with_a_readable_message(
    client, project, monkeypatch
):
    monkeypatch.setattr(
        llm,
        "get_provider",
        lambda: _BrokenProvider(llm.ProviderError("הפנייה לספק ה־AI חרגה מהזמן המוקצב.")),
    )

    job = generate(client, project["id"])
    assert job["status"] == jobs.FAILED
    assert "חרגה מהזמן" in job["error"]
    assert client.get(f"/projects/{project['id']}/plans").json()["plans"] == []


def test_a_missing_api_key_fails_the_job_clearly(client, project, monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV_VAR, "anthropic")
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)

    status = client.get("/llm").json()
    assert status["ready"] is False
    assert status["is_mock"] is False
    assert "ANTHROPIC_API_KEY" in status["message"]

    job = generate(client, project["id"])
    assert job["status"] == jobs.FAILED
    assert "ANTHROPIC_API_KEY" in job["error"]


def test_provider_output_that_fails_validation_is_rejected(client, project, monkeypatch):
    monkeypatch.setattr(
        llm,
        "get_provider",
        lambda: _ScriptedProvider(
            {
                "supported": True,
                "summary": "תוכנית שגויה",
                "actions": [
                    {
                        "id": "a1",
                        "capability_id": "video.add_captions",
                        "parameters": {"language": "he"},
                    }
                ],
            }
        ),
    )

    job = generate(client, project["id"])
    assert job["status"] == jobs.FAILED
    assert "נדחתה באימות" in job["error"]
    assert client.get(f"/projects/{project['id']}/plans").json()["plans"] == []


def test_provider_output_that_is_not_json_is_rejected(client, project, monkeypatch):
    monkeypatch.setattr(
        llm, "get_provider", lambda: _ScriptedProvider("not a dict at all")
    )

    job = generate(client, project["id"])
    assert job["status"] == jobs.FAILED
    assert job["error"]


def test_extract_json_tolerates_surrounding_text_but_not_garbage():
    assert llm._extract_json('here you go:\n{"supported": false}\nthanks') == {
        "supported": False
    }

    with pytest.raises(llm.ProviderError):
        llm._extract_json("no json here")
