"""Tests for the editor's stage pipeline: acceptance per stage and the live preview.

- **Acceptance** — footage, cutting and captions are accepted here, each
  naming exactly what was shown (a token); a stale token, a skipped stage or
  nothing to accept is refused.
- **Outdating** — a change to the clips, the merged video or the captions
  reopens that stage and everything after it, without deleting anything.
- **Preview** — always the furthest video reached (first clip, merged cut,
  zoomed video), composed with the current captions like the B-roll review,
  and from the music stage on with the chosen music.

The cut is real FFmpeg-free plumbing: the tool runner is stubbed to copy a
small generated clip, so the merged video is a real, probe-able file.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import cutting, processes, zooms  # noqa: E402
from backend.tests.test_captions import transcribe  # noqa: E402
from backend.tests.test_zooms import (  # noqa: E402
    add_source, approve, client, environment, fake_whisper, make_grid_video, planner, run_to_end, wait_for_job,
)

__all__ = ["client", "environment", "fake_whisper", "planner"]  # fixtures, re-used

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg.exe") and shutil.which("ffprobe.exe")), reason="FFmpeg / FFprobe are not on PATH"
)
pytestmark = needs_ffmpeg


@pytest.fixture
def project(client):
    return client.post("/projects", json={"name": "שלבים"}).json()


@pytest.fixture
def grid(tmp_path):
    folder = tmp_path / "צילומים"
    folder.mkdir()
    return make_grid_video(folder / "טייק 1.mp4")


@pytest.fixture
def copying_tools(monkeypatch, grid):
    """Every cut and join "renders" by copying the generated clip into place."""

    def run(argv, *, cancelled=None, on_output=None, **kwargs):
        target = Path(argv[-1] if argv[0].startswith("ffmpeg") else argv[argv.index("-o") + 1])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(grid, target)
        return processes.ProcessResult(0, "Finished.", False)

    monkeypatch.setattr(processes, "run", run)
    monkeypatch.setattr(cutting, "collect_tool_versions", lambda: {"auto_editor": "stub", "ffmpeg": "stub"})


def pipeline(client, project_id) -> dict:
    response = client.get(f"/projects/{project_id}/pipeline")
    assert response.status_code == 200, response.text
    return response.json()


def stage(state, stage_id) -> dict:
    return next(entry for entry in state["stages"] if entry["id"] == stage_id)


def accept(client, project_id, stage_id, token=None):
    if token is None:
        token = stage(pipeline(client, project_id), stage_id)["candidate"]["token"]
    return client.post(f"/projects/{project_id}/pipeline/{stage_id}/accept", json={"token": token})


def cut(client, project_id) -> str:
    source_ids = [source["id"] for source in client.get(f"/projects/{project_id}").json()["sources"]]
    response = client.post(f"/projects/{project_id}/cutting/runs", json={
        "source_ids": source_ids, "output_mode": cutting.MODE_BOTH, "mode": cutting.CUT_MODE_FULL,
    })
    assert response.status_code in (200, 201), response.text
    record = wait_for_job(client, project_id, response.json()["id"])
    assert record["status"] == "succeeded", record["error"]
    return "cut.%s.%s" % (record["result"]["run_id"], cutting.COMBINED_OUTPUT_ID)


def test_an_empty_video_starts_at_footage_with_nothing_to_accept_or_preview(client, project):
    state = pipeline(client, project["id"])
    assert [entry["id"] for entry in state["stages"]] == ["footage", "cutting", "captions", "zooms", "broll", "music"]
    assert state["current_stage"] == "footage" and state["stages_left"] == 6
    assert stage(state, "footage")["candidate"] is None
    assert "Add the clips" in stage(state, "footage")["blocked_reason"]
    assert state["preview"] is None
    assert accept(client, project["id"], "footage", "sha256:x").status_code == 409


def test_stages_are_accepted_in_order_and_the_preview_follows_the_furthest_result(
    client, project, grid, copying_tools, fake_whisper,
):
    pid = project["id"]
    source_key = add_source(client, pid, grid)

    state = pipeline(client, pid)
    assert state["preview"]["key"] == source_key and state["preview"]["stage"] == "footage"
    assert state["preview"]["props"]["video"]["duration_in_frames"] == 100
    assert state["preview"]["props"]["captions"] == []

    # Out of order, or not what was shown: refused.
    assert accept(client, pid, "cutting", "sha256:x").status_code == 409
    assert accept(client, pid, "footage", "sha256:stale").status_code == 409
    assert accept(client, pid, "zooms", "sha256:x").status_code == 404

    response = accept(client, pid, "footage")
    assert response.status_code == 200, response.text
    state = response.json()
    assert stage(state, "footage")["done"] and state["current_stage"] == "cutting"
    assert "Render the clips" in stage(state, "cutting")["blocked_reason"]

    cut_key = cut(client, pid)
    state = pipeline(client, pid)
    assert stage(state, "cutting")["candidate"]["key"] == cut_key
    # Not accepted yet, but already the furthest video: the preview shows it.
    assert state["preview"]["key"] == cut_key and state["preview"]["stage"] == "cutting"

    state = accept(client, pid, "cutting").json()
    assert stage(state, "cutting")["done"] and state["current_stage"] == "captions"
    assert "Transcribe" in stage(state, "captions")["blocked_reason"]

    transcribe(client, pid, cut_key)
    state = pipeline(client, pid)
    assert state["preview"]["props"]["captions"], "the preview draws the current captions"
    candidate = stage(state, "captions")["candidate"]
    assert candidate["key"] == cut_key and candidate["revision"] == 1

    state = accept(client, pid, "captions").json()
    assert stage(state, "captions")["done"] and state["current_stage"] == "zooms"
    assert state["stages_left"] == 3
    accepted = client.get(f"/projects/{pid}").json()["settings"]["pipeline"]
    assert set(accepted) == {"footage", "cutting", "captions"}
    assert accepted["captions"]["transcript_id"] == candidate["transcript_id"]


def test_a_change_upstream_reopens_that_stage_and_everything_after_it(
    client, project, grid, copying_tools, fake_whisper, tmp_path,
):
    pid = project["id"]
    add_source(client, pid, grid)
    accept(client, pid, "footage")
    cut_key = cut(client, pid)
    accept(client, pid, "cutting")
    transcribe(client, pid, cut_key)
    accept(client, pid, "captions")

    # A new caption style outdates the captions acceptance only.
    style = client.get(f"/projects/{pid}/captions/style").json()["style"]
    response = client.put(f"/projects/{pid}/captions/style", json={"style": {**style, "vertical_position_percent": 75.0}})
    assert response.status_code == 200, response.text
    state = pipeline(client, pid)
    assert stage(state, "cutting")["done"] and not stage(state, "captions")["done"]
    assert "style changed" in stage(state, "captions")["outdated_reason"]
    assert state["current_stage"] == "captions"
    accept(client, pid, "captions")

    # Another clip: footage reopens, and with it every later stage.
    second = tmp_path / "טייק 2.mp4"
    shutil.copyfile(grid, second)
    add_source(client, pid, second)
    state = pipeline(client, pid)
    assert state["current_stage"] == "footage" and state["stages_left"] == 6
    assert "changed after they were accepted" in stage(state, "footage")["outdated_reason"]
    assert "not accepted" in stage(state, "cutting")["outdated_reason"]
    # Nothing was thrown away: re-accepting the footage restores the rest.
    state = accept(client, pid, "footage").json()
    assert stage(state, "captions")["done"] and state["current_stage"] == "zooms"


def test_an_approved_zoom_is_the_preview_and_hands_over_to_b_roll(
    client, project, grid, copying_tools, fake_whisper, planner, tmp_path,
):
    pid = project["id"]
    add_source(client, pid, grid)
    accept(client, pid, "footage")
    cut_key = cut(client, pid)
    accept(client, pid, "cutting")
    transcribe(client, pid, cut_key)
    accept(client, pid, "captions")

    outcome = run_to_end(client, pid, cut_key)
    run_id = outcome["job"]["result"]["run_id"]
    state = pipeline(client, pid)
    assert state["preview"]["key"] == "zoom.%s" % run_id and state["preview"]["stage"] == "zooms"
    assert state["preview"]["props"]["captions"], "the zoomed video shares the cut's captions"
    assert not stage(state, "zooms")["done"]

    approve(client, pid, client.get(f"/projects/{pid}/zooms/runs/{run_id}").json())
    state = pipeline(client, pid)
    assert stage(state, "zooms")["done"] and state["current_stage"] == "broll"
    assert state["preview"]["stage"] == "broll"
    assert zooms.read_approval(pid, run_id) is not None

    # B-roll switched off: approving the zoom continues to the music stage,
    # and with that off too it finishes the video.
    client.put(f"/projects/{pid}/broll/settings", json={"enabled": False})
    state = pipeline(client, pid)
    assert stage(state, "broll")["done"] and state["current_stage"] == "music"
    assert state["preview"]["stage"] == "music" and state["preview"]["props"]["music"] is None

    # A chosen track is in the preview at once, the way the export will mix it.
    track_file = tmp_path / "מנגינה.mp3"
    subprocess.run(["ffmpeg.exe", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=duration=3",
                    str(track_file)], check=True)
    track = client.post("/audio/library/imports", json={"path": str(track_file)}).json()["asset"]
    client.put(f"/projects/{pid}/music/settings", json={"track_id": track["id"], "volume_percent": 20})
    preview = pipeline(client, pid)["preview"]
    assert preview["props"]["music"]["asset"]["id"] == track["id"] and preview["props"]["music"]["volume"] == 0.2
    assert preview["music"] == {"id": track["id"], "digest": track["fingerprint"]["digest"]}
    assert not stage(pipeline(client, pid), "music")["done"]

    client.put(f"/projects/{pid}/music/settings", json={"enabled": False})
    state = pipeline(client, pid)
    assert state["current_stage"] is None and state["stages_left"] == 0
