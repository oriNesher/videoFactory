"""Focused tests for project persistence, validation and missing sources.

Every test runs against a temporary workspace so the developer's real
`workspace/` directory and any real footage are never touched.
"""

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import storage  # noqa: E402
from backend.config import WORKSPACE_ENV_VAR  # noqa: E402
from backend.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """Point the workspace at a throwaway directory for every test.

    The workspace location is read from the environment on every call, so this
    needs no module reloading.
    """
    root = tmp_path / "workspace"
    monkeypatch.setenv(WORKSPACE_ENV_VAR, str(root))
    return root


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def video(tmp_path):
    """A stand-in source file. Content does not matter: nothing decodes it."""
    path = tmp_path / "take 1.mp4"
    path.write_bytes(b"not a real video")
    return path


def create(client, name="סרטון ראשון"):
    response = client.post("/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def add_source(client, project_id, path):
    return client.post(f"/projects/{project_id}/sources", json={"path": str(path)})


# --- persistence ------------------------------------------------------------


def test_create_project_writes_versioned_file_and_directories(client, workspace):
    project = create(client, "פרויקט בדיקה")

    directory = workspace / "projects" / project["id"]
    assert (directory / "intermediates").is_dir()
    assert (directory / "exports").is_dir()

    on_disk = json.loads((directory / "project.json").read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == storage.SCHEMA_VERSION
    assert on_disk["id"] == project["id"]
    assert on_disk["name"] == "פרויקט בדיקה"
    assert on_disk["sources"] == []
    assert on_disk["settings"] == {}
    assert on_disk["created_at"] and on_disk["updated_at"]


def test_storage_path_uses_generated_id_not_the_name(client, workspace):
    project = create(client, "שם עם רווחים / תווים : לא חוקיים")

    directories = [d.name for d in (workspace / "projects").iterdir()]
    assert directories == [project["id"]]
    assert len(project["id"]) == 32


def test_project_survives_a_restart(client, video):
    project = create(client, "לפני הפעלה מחדש")
    source_id = add_source(client, project["id"], video).json()["sources"][0]["id"]
    client.put(
        f"/projects/{project['id']}",
        json={"name": "אחרי שינוי שם", "source_ids": [source_id]},
    )

    # A fresh client stands in for a restart: nothing is cached in memory, so
    # everything below is read back from disk.
    with TestClient(app) as restarted:
        reopened = restarted.get(f"/projects/{project['id']}").json()

    assert reopened["name"] == "אחרי שינוי שם"
    assert [s["path"] for s in reopened["sources"]] == [os.path.normpath(str(video))]


def test_rename_and_reorder_are_saved(client, tmp_path):
    first = tmp_path / "a.mp4"
    second = tmp_path / "ב עם רווח.mp4"
    third = tmp_path / "c.mov"
    for path in (first, second, third):
        path.write_bytes(b"x")

    project = create(client)
    for path in (first, second, third):
        assert add_source(client, project["id"], path).status_code == 201

    sources = client.get(f"/projects/{project['id']}").json()["sources"]
    assert [s["filename"] for s in sources] == ["a.mp4", "ב עם רווח.mp4", "c.mov"]

    reordered = [sources[2]["id"], sources[0]["id"], sources[1]["id"]]
    response = client.put(
        f"/projects/{project['id']}",
        json={"name": "שם חדש", "source_ids": reordered},
    )
    assert response.status_code == 200

    saved = client.get(f"/projects/{project['id']}").json()
    assert saved["name"] == "שם חדש"
    assert [s["filename"] for s in saved["sources"]] == [
        "c.mov",
        "a.mp4",
        "ב עם רווח.mp4",
    ]


def test_removing_a_source_keeps_the_file_on_disk(client, video):
    project = create(client)
    add_source(client, project["id"], video)

    response = client.put(
        f"/projects/{project['id']}", json={"name": "ללא מקורות", "source_ids": []}
    )
    assert response.status_code == 200
    assert response.json()["sources"] == []
    assert video.exists(), "removing a reference must not delete the source file"


def test_saving_does_not_touch_other_projects(client, video):
    first = create(client, "פרויקט א")
    second = create(client, "פרויקט ב")
    add_source(client, second["id"], video)

    client.put(f"/projects/{first['id']}", json={"name": "עודכן", "source_ids": []})

    unchanged = client.get(f"/projects/{second['id']}").json()
    assert unchanged["name"] == "פרויקט ב"
    assert len(unchanged["sources"]) == 1


def test_save_is_atomic_and_leaves_no_temporary_files(client, video, workspace):
    project = create(client)
    add_source(client, project["id"], video)
    client.put(
        f"/projects/{project['id']}", json={"name": "שמור", "source_ids": []}
    )

    directory = workspace / "projects" / project["id"]
    leftovers = [p.name for p in directory.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_listing_reports_counts_and_is_newest_first(client, video):
    older = create(client, "ישן")
    newer = create(client, "חדש")
    add_source(client, newer["id"], video)

    listing = client.get("/projects").json()
    ids = [entry["id"] for entry in listing["projects"]]
    assert set(ids) == {older["id"], newer["id"]}

    entry = next(e for e in listing["projects"] if e["id"] == newer["id"])
    assert entry["source_count"] == 1
    assert entry["missing_source_count"] == 0
    assert entry["error"] is None


# --- validation -------------------------------------------------------------


@pytest.mark.parametrize("name", ["", "   ", None, 7, "x" * 101, "שם\nעם\nשורות"])
def test_invalid_project_names_are_rejected(client, name):
    response = client.post("/projects", json={"name": name})
    assert response.status_code == 400
    assert isinstance(response.json()["detail"], str)


def test_project_name_is_trimmed(client):
    assert create(client, "  פרויקט  ")["name"] == "פרויקט"


def test_relative_path_is_rejected(client):
    project = create(client)
    response = add_source(client, project["id"], "videos\\take1.mp4")
    assert response.status_code == 400
    assert "C:" in response.json()["detail"]


def test_non_video_extension_is_rejected(client, tmp_path):
    document = tmp_path / "notes.txt"
    document.write_text("hello", encoding="utf-8")

    project = create(client)
    response = add_source(client, project["id"], document)
    assert response.status_code == 400


def test_nonexistent_file_is_rejected_when_added(client, tmp_path):
    project = create(client)
    response = add_source(client, project["id"], tmp_path / "nope.mp4")
    assert response.status_code == 400


def test_directory_path_is_rejected(client, tmp_path):
    directory = tmp_path / "clips.mp4"
    directory.mkdir()

    project = create(client)
    response = add_source(client, project["id"], directory)
    assert response.status_code == 400


def test_quoted_windows_path_is_accepted(client, video):
    """Explorer's "Copy as path" wraps the path in double quotes."""
    project = create(client)
    response = add_source(client, project["id"], f'"{video}"')
    assert response.status_code == 201
    assert response.json()["sources"][0]["path"] == os.path.normpath(str(video))


def test_duplicate_source_is_rejected(client, video):
    project = create(client)
    assert add_source(client, project["id"], video).status_code == 201

    response = add_source(client, project["id"], video)
    assert response.status_code == 409


def test_unknown_project_returns_404(client):
    assert client.get("/projects/%s" % ("0" * 32)).status_code == 404
    assert client.get("/projects/not-an-id").status_code == 404


def test_saving_an_unknown_source_id_is_rejected(client, video):
    project = create(client)
    add_source(client, project["id"], video)

    response = client.put(
        f"/projects/{project['id']}",
        json={"name": "שם", "source_ids": ["deadbeef"]},
    )
    assert response.status_code == 400


def test_saving_duplicate_source_ids_is_rejected(client, video):
    project = create(client)
    source_id = add_source(client, project["id"], video).json()["sources"][0]["id"]

    response = client.put(
        f"/projects/{project['id']}",
        json={"name": "שם", "source_ids": [source_id, source_id]},
    )
    assert response.status_code == 400


def test_failed_save_leaves_the_previous_version_intact(client, video, workspace):
    project = create(client)
    add_source(client, project["id"], video)

    before = (workspace / "projects" / project["id"] / "project.json").read_text(
        encoding="utf-8"
    )

    response = client.put(
        f"/projects/{project['id']}", json={"name": "", "source_ids": []}
    )
    assert response.status_code == 400

    after = (workspace / "projects" / project["id"] / "project.json").read_text(
        encoding="utf-8"
    )
    assert after == before


# --- missing sources and malformed files ------------------------------------


def test_missing_source_is_flagged_without_breaking_the_project(client, tmp_path):
    present = tmp_path / "present.mp4"
    disappearing = tmp_path / "disappearing.mp4"
    for path in (present, disappearing):
        path.write_bytes(b"x")

    project = create(client, "עם קובץ חסר")
    add_source(client, project["id"], present)
    add_source(client, project["id"], disappearing)

    disappearing.unlink()

    reopened = client.get(f"/projects/{project['id']}")
    assert reopened.status_code == 200

    sources = reopened.json()["sources"]
    assert [s["exists"] for s in sources] == [True, False]
    assert sources[1]["filename"] == "disappearing.mp4"
    assert sources[1]["size_bytes"] is None

    entry = next(
        e for e in client.get("/projects").json()["projects"] if e["id"] == project["id"]
    )
    assert entry["missing_source_count"] == 1


def test_a_project_with_a_missing_source_can_still_be_saved(client, tmp_path):
    missing = tmp_path / "gone.mp4"
    missing.write_bytes(b"x")

    project = create(client)
    source_id = add_source(client, project["id"], missing).json()["sources"][0]["id"]
    missing.unlink()

    response = client.put(
        f"/projects/{project['id']}",
        json={"name": "עדיין נשמר", "source_ids": [source_id]},
    )
    assert response.status_code == 200
    assert response.json()["sources"][0]["exists"] is False


def test_malformed_project_file_reports_a_clear_error(client, workspace):
    project = create(client)
    path = workspace / "projects" / project["id"] / "project.json"
    path.write_text("{ this is not json", encoding="utf-8")

    response = client.get(f"/projects/{project['id']}")
    assert response.status_code == 422
    assert "JSON" in response.json()["detail"]


def test_project_from_a_newer_schema_is_rejected(client, workspace):
    project = create(client)
    path = workspace / "projects" / project["id"] / "project.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = storage.SCHEMA_VERSION + 99
    path.write_text(json.dumps(data), encoding="utf-8")

    response = client.get(f"/projects/{project['id']}")
    assert response.status_code == 422


def test_malformed_project_does_not_break_the_listing(client, workspace):
    healthy = create(client, "תקין")
    broken = create(client, "פגום")
    (workspace / "projects" / broken["id"] / "project.json").write_text(
        "{]", encoding="utf-8"
    )

    listing = client.get("/projects")
    assert listing.status_code == 200

    entries = {entry["id"]: entry for entry in listing.json()["projects"]}
    assert entries[healthy["id"]]["error"] is None
    assert entries[broken["id"]]["error"]


# --- existing functionality --------------------------------------------------


def test_health_and_tools_endpoints_still_respond(client):
    assert client.get("/health").json()["status"] == "ok"
    assert set(client.get("/tools").json()) == {"ffmpeg", "ffprobe", "auto_editor"}


# --- adding a whole folder ---------------------------------------------------


def add_directory(client, project_id, path):
    return client.post(
        f"/projects/{project_id}/sources/directory", json={"path": str(path)}
    )


@pytest.fixture
def folder(tmp_path):
    """A folder of takes, deliberately not created in name order."""
    directory = tmp_path / "shoot 01"
    directory.mkdir()

    for name in ("take10.mkv", "take2.MP4", "Take1.mov"):
        (directory / name).write_bytes(b"not a real video")

    # Things that live beside footage and must be ignored, not refused.
    (directory / "notes.txt").write_text("shot list", encoding="utf-8")
    (directory / "thumb.png").write_bytes(b"png")
    (directory / "proxies").mkdir()

    return directory


def test_a_folder_loads_its_videos_in_name_order_and_ignores_the_rest(
    client, folder
):
    project = create(client)

    response = add_directory(client, project["id"], folder)
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["cancelled"] is False
    assert body["added"] == ["Take1.mov", "take2.MP4", "take10.mkv"]
    assert sorted(body["ignored"]) == ["notes.txt", "thumb.png"]
    assert body["failed"] == []

    # The project's own order is the order the clips will be cut in.
    assert [source["filename"] for source in body["project"]["sources"]] == [
        "Take1.mov",
        "take2.MP4",
        "take10.mkv",
    ]


def test_a_folder_is_added_in_one_write(client, folder, workspace):
    project = create(client)
    add_directory(client, project["id"], folder)

    on_disk = json.loads(
        (workspace / "projects" / project["id"] / "project.json").read_text(
            encoding="utf-8"
        )
    )
    # One write means one timestamp: three takes must not look like three edits.
    assert len({source["added_at"] for source in on_disk["sources"]}) == 1


def test_re_adding_a_folder_reports_duplicates_instead_of_failing(client, folder):
    project = create(client)
    add_directory(client, project["id"], folder)

    second = add_directory(client, project["id"], folder)
    assert second.status_code == 201

    body = second.json()
    assert body["added"] == []
    assert sorted(body["duplicates"]) == ["Take1.mov", "take10.mkv", "take2.MP4"]
    assert len(body["project"]["sources"]) == 3


def test_a_folder_gaining_a_clip_adds_only_the_new_one(client, folder):
    project = create(client)
    add_directory(client, project["id"], folder)

    (folder / "take3.mp4").write_bytes(b"not a real video")
    body = add_directory(client, project["id"], folder).json()

    assert body["added"] == ["take3.mp4"]
    assert len(body["duplicates"]) == 3
    assert [s["filename"] for s in body["project"]["sources"]][-1] == "take3.mp4"


def test_a_folder_without_video_is_refused_with_a_readable_message(
    client, tmp_path
):
    empty = tmp_path / "documents"
    empty.mkdir()
    (empty / "script.docx").write_bytes(b"doc")

    response = add_directory(client, create(client)["id"], empty)
    assert response.status_code == 400
    assert "No video files" in response.json()["detail"]


def test_a_missing_or_non_folder_path_is_refused(client, tmp_path, video):
    project = create(client)

    missing = add_directory(client, project["id"], tmp_path / "nope")
    assert missing.status_code == 400
    assert "Folder not found" in missing.json()["detail"]

    not_a_folder = add_directory(client, project["id"], video)
    assert not_a_folder.status_code == 400
    assert "not a folder" in not_a_folder.json()["detail"]


def test_a_relative_path_is_refused(client):
    response = add_directory(client, create(client)["id"], "videos")
    assert response.status_code == 400
    assert "full path" in response.json()["detail"]


def test_omitting_the_path_opens_the_machines_folder_dialog(
    client, folder, monkeypatch
):
    """No path means: ask the person, through the OS dialog, not the browser."""
    from backend import folders

    monkeypatch.setattr(folders, "_ask_for_directory", lambda: str(folder))

    response = client.post(
        f"/projects/{create(client)['id']}/sources/directory", json={}
    )
    assert response.status_code == 201
    assert response.json()["added"] == ["Take1.mov", "take2.MP4", "take10.mkv"]


def test_dismissing_the_dialog_changes_nothing(client, monkeypatch):
    from backend import folders

    monkeypatch.setattr(folders, "_ask_for_directory", lambda: None)

    project = create(client)
    response = client.post(
        f"/projects/{project['id']}/sources/directory", json={}
    )

    assert response.status_code == 201
    assert response.json() == {"cancelled": True}
    assert client.get(f"/projects/{project['id']}").json()["sources"] == []


def test_a_dialog_that_cannot_open_says_so_instead_of_hanging(
    client, monkeypatch
):
    from backend import folders

    def unavailable():
        raise ImportError("no module named tkinter")

    monkeypatch.setattr(folders, "_ask_for_directory", unavailable)

    response = client.post(
        f"/projects/{create(client)['id']}/sources/directory", json={}
    )
    assert response.status_code == 503
    assert "Paste the folder path instead" in response.json()["detail"]
