"""Tests for stage 3A: the B-roll library, plans, composition and export.

- **Library** — import and measured metadata, references not copies, the
  user's words kept apart from measurements, missing / changed /
  unsupported files, the model-facing catalog without paths.
- **Plans** — validation (bounds, source ranges, overlaps, templates),
  the minimal "add at playhead" flow and its clamping, immutable revisions,
  conflicts, reopening, a changed base video.
- **Review** — props as composed for preview and export, approval naming
  exactly what was composed, skipping, the hand-over from zooms.
- **Rendering** — one real Remotion export of a zoomed video with Hebrew
  captions, a full-frame image and a picture-in-picture video, inspected
  frame by frame and by its audio; cancellation and immutable snapshots.

Small generated fixtures only. No test calls Gemini or loads Whisper.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import broll, broll_jobs, broll_library, caption_inputs, caption_jobs, captions, zoom_planner, zooms  # noqa: E402
from backend.config import GEMINI_API_KEY_ENV_VAR, WORKSPACE_ENV_VAR, ZOOM_PROVIDER_ENV_VAR  # noqa: E402
from backend.main import app  # noqa: E402
from backend.tests.test_captions import FAKE_WORDS, FakeWhisper  # noqa: E402
from backend.tests.test_zooms import FakePlanner, ffprobe_json, frame_rgb, make_grid_video, plan_of, psnr  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg.exe") and shutil.which("ffprobe.exe")), reason="FFmpeg / FFprobe are not on PATH"
)
needs_renderer = pytest.mark.skipif(
    not caption_jobs.renderer_status()["ready"], reason="The Remotion renderer is not prepared"
)

pytestmark = needs_ffmpeg


# --- fixtures ------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv(WORKSPACE_ENV_VAR, str(tmp_path / "workspace"))
    monkeypatch.setenv(GEMINI_API_KEY_ENV_VAR, "AIzaTESTKEY_0123456789abcdefghijklmnop")
    monkeypatch.delenv(ZOOM_PROVIDER_ENV_VAR, raising=False)
    monkeypatch.setattr(captions, "model_downloaded", lambda model_id: True)
    caption_inputs._fingerprint_cache.clear()
    caption_inputs._metadata_cache.clear()


@pytest.fixture
def client():
    with TestClient(app) as running:
        yield running


@pytest.fixture
def project(client):
    created = client.post("/projects", json={"name": "בי-רול"}).json()
    # These tests cover B-roll's own export, which is what approving does with
    # the music stage switched off. The default hand-over to music (stage 4)
    # is tested in test_music.py.
    response = client.put(f"/projects/{created['id']}/music/settings", json={"enabled": False})
    assert response.status_code == 200, response.text
    return created


@pytest.fixture
def fake_whisper(monkeypatch):
    from backend import whisper_service

    fake = FakeWhisper()
    monkeypatch.setattr(whisper_service.service, "transcribe", fake)
    return fake


@pytest.fixture
def planner(monkeypatch):
    fake = FakePlanner(plan_of((0, 1.0, "NORMAL"), (1.0, 4.0, "PUNCH_STRONG")))
    monkeypatch.setattr(zoom_planner, "get_planner", lambda: fake)
    return fake


def ffmpeg(*arguments):
    subprocess.run(["ffmpeg.exe", "-hide_banner", "-loglevel", "error", "-y", *map(str, arguments)], check=True)


@pytest.fixture
def media_dir(tmp_path):
    folder = tmp_path / "בי רול עם רווח"
    folder.mkdir()
    return folder


def make_image(path: Path, colour="red", size="360x640") -> Path:
    ffmpeg("-f", "lavfi", "-i", "color=c=%s:size=%s" % (colour, size), "-frames:v", "1", path)
    return path


def make_asset_video(path: Path, codec="libx264") -> Path:
    """3 s at 25 fps: blue for 0.5 s, then lime; a loud 300 Hz tone throughout."""
    ffmpeg(
        "-f", "lavfi", "-i", "color=c=blue:size=320x240:rate=25:duration=0.5",
        "-f", "lavfi", "-i", "color=c=lime:size=320x240:rate=25:duration=2.5",
        "-f", "lavfi", "-i", "sine=frequency=300:duration=3",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-map", "2:a", "-c:v", codec, "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path,
    )
    return path


@pytest.fixture
def image(media_dir):
    return make_image(media_dir / "תמונה אדומה.png")


@pytest.fixture
def clip(media_dir):
    return make_asset_video(media_dir / "הקלטת מסך.mp4")


@pytest.fixture
def base_file(tmp_path):
    folder = tmp_path / "צילומים"
    folder.mkdir()
    return make_grid_video(folder / "טייק 1.mp4", seconds=4, size="360x640", rate=25)


def add_source(client, project_id, path) -> str:
    response = client.post(f"/projects/{project_id}/sources", json={"path": str(path)})
    assert response.status_code == 201, response.text
    return "source.%s" % response.json()["sources"][-1]["id"]


def import_asset(client, path) -> dict:
    response = client.post("/broll/library/imports", json={"path": str(path)})
    assert response.status_code == 201, response.text
    return response.json()["asset"]


def state_of(client, project_id, key) -> dict:
    response = client.get(f"/projects/{project_id}/broll/inputs/{key}")
    assert response.status_code == 200, response.text
    return response.json()


def add(client, project_id, key, asset_id, at, template_id=None, revision=None):
    if revision is None:
        plan = state_of(client, project_id, key)["plan"]
        revision = plan["revision"] if plan else 0
    return client.post(
        f"/projects/{project_id}/broll/inputs/{key}/placements",
        json={"asset_id": asset_id, "at_seconds": at, "template_id": template_id, "base_revision": revision},
    )


def wait_for_job(client, project_id, job_id, timeout=600.0):
    from backend import jobs

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = client.get(f"/projects/{project_id}/jobs/{job_id}").json()
        if record["status"] in jobs.FINISHED_STATUSES:
            return record
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def placement(asset_id, start, end, template_id="fullframe", source_start=None, settings=None, pid="p00000001"):
    return {"id": pid, "asset_id": asset_id, "template_id": template_id, "start": start, "end": end,
            "source_start": source_start, "settings": settings or {}}


# --- the library ---------------------------------------------------------------------


def test_importing_measures_the_file_and_references_it_in_place(client, image, clip):
    before = {path: path.read_bytes() for path in (image, clip)}
    picture = import_asset(client, image)
    video = import_asset(client, clip)

    assert picture["media_type"] == "image" and picture["status"] == "available"
    assert picture["measured"]["width"] == 360 and picture["measured"]["height"] == 640
    assert picture["measured"]["aspect_ratio"] == pytest.approx(9 / 16, abs=1e-4)
    assert picture["measured"]["duration_seconds"] is None and picture["measured"]["has_audio"] is False
    assert picture["storage"] == "referenced" and picture["display_name_source"] == "filename"
    assert picture["description"] is None and picture["tags"] == []

    assert video["media_type"] == "video" and video["measured"]["codec"] == "h264"
    assert (video["measured"]["width"], video["measured"]["height"]) == (320, 240)
    assert video["measured"]["duration_seconds"] == pytest.approx(3.0, abs=0.1)
    assert video["measured"]["has_audio"] is True

    listing = client.get("/broll/library").json()
    assert [asset["id"] for asset in listing["assets"]] == [video["id"], picture["id"]]
    assert str(image.parent) not in json.dumps(listing, ensure_ascii=False)  # no paths in the API

    # Referenced, not copied: the library folder holds only its file and thumbnails,
    # and the originals are byte-identical.
    root = broll_library.library_root()
    stored = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
    assert stored[0] == "library.json" and len(stored) == 3
    assert all(name.startswith("thumbnails/") and name.endswith(".jpg") for name in stored[1:])
    assert all(path.read_bytes() == content for path, content in before.items())

    thumbnail = client.get(f"/broll/library/{picture['id']}/thumbnail")
    assert thumbnail.status_code == 200 and thumbnail.headers["content-type"] == "image/jpeg"
    ranged = client.get(f"/broll/library/{video['id']}/media", headers={"Range": "bytes=0-99"})
    assert ranged.status_code == 206 and len(ranged.content) == 100

    again = client.post("/broll/library/imports", json={"path": str(image)}).json()
    assert again["duplicate"] is True and again["asset"]["id"] == picture["id"]


def test_descriptions_are_the_users_own_and_the_catalog_has_no_paths_or_filenames(client, image):
    asset = import_asset(client, image)
    catalog = client.get("/broll/catalog").json()
    entry = catalog["assets"][0]
    assert entry["semantic_description"] == "none" and entry["user_description"] is None and entry["user_label"] is None
    text = json.dumps(catalog, ensure_ascii=False)
    assert "תמונה אדומה" not in text and "C:" not in text and ".png" not in text
    assert {template["id"] for template in catalog["templates"]} == {"fullframe", "pip"}
    assert catalog["constraints"]["overlap_policy"] == "reject"

    response = client.patch(f"/broll/library/{asset['id']}", json={"description": "גרף מכירות רבעוני", "tags": ["גרף", "גרף", "נתונים"]})
    assert response.status_code == 200 and response.json()["tags"] == ["גרף", "נתונים"]
    entry = client.get("/broll/catalog").json()["assets"][0]
    assert entry["semantic_description"] == "user_provided" and entry["user_description"] == "גרף מכירות רבעוני"
    # Measured facts cannot be edited.
    assert client.patch(f"/broll/library/{asset['id']}", json={"measured": {"width": 1}}).status_code == 400

    # Removing an entry never touches the file.
    assert client.delete(f"/broll/library/{asset['id']}").status_code == 200
    assert image.is_file() and client.get("/broll/library").json()["assets"] == []


def test_an_undecodable_codec_is_listed_as_unsupported_and_cannot_be_placed(client, project, base_file, media_dir):
    old = make_asset_video(media_dir / "old codec.mp4", codec="mpeg4")
    asset = import_asset(client, old)
    assert asset["status"] == "unsupported" and "H.264" in asset["status_reason"]
    key = add_source(client, project["id"], base_file)
    response = add(client, project["id"], key, asset["id"], 0.5)
    assert response.status_code == 409 and "cannot be used" in response.json()["detail"]


def test_unknown_file_types_are_refused_with_the_reason(client, media_dir):
    text = media_dir / "notes.txt"
    text.write_text("not media", encoding="utf-8")
    response = client.post("/broll/library/imports", json={"path": str(text)})
    assert response.status_code == 400 and "Unsupported file type" in response.json()["detail"]
    assert client.post("/broll/library/imports", json={"path": "relative.png"}).status_code == 400


# --- plans and validation ----------------------------------------------------------


@pytest.fixture
def ready(client, project, base_file, image, clip):
    key = add_source(client, project["id"], base_file)
    return {
        "project": project["id"],
        "key": key,
        "image": import_asset(client, image)["id"],
        "clip": import_asset(client, clip)["id"],
    }


def validate(client, ready, placements):
    response = client.post(f"/projects/{ready['project']}/broll/inputs/{ready['key']}/validate", json={"placements": placements})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    "build, fragment",
    [
        (lambda s: [placement("0123456789ab", 0.5, 1.5)], "not in the B-roll library"),
        (lambda s: [placement(s["image"], 0.5, 1.5, template_id="kenburns")], "Unknown template"),
        (lambda s: [placement(s["image"], 0.5, 1.5, settings={"fit": "stretch"})], "Fit must be one of"),
        (lambda s: [placement(s["image"], 0.5, 1.5, settings={"opacity": 0.5})], "Unknown setting"),
        (lambda s: [placement(s["image"], 0.5, 1.5, template_id="pip", settings={"size_percent": 95})], "between 10 and 90"),
        (lambda s: [placement(s["image"], 0.5, 1.5, template_id="pip", settings={"rotation_degrees": 60})], "between -45 and 45"),
        (lambda s: [placement(s["image"], "0.5", 1.5)], "finite"),
        (lambda s: [placement(s["image"], -0.1, 1.5)], "before the beginning"),
        (lambda s: [placement(s["image"], 3.0, 4.5)], "after the end"),
        (lambda s: [placement(s["image"], 1.0, 1.1)], "at least 0.2 s"),
        (lambda s: [placement(s["image"], 1.0, 0.5)], "at least 0.2 s"),
        (lambda s: [placement(s["clip"], 0.0, 3.5, source_start=0.0)], "never looped or frozen"),
        (lambda s: [placement(s["clip"], 0.0, 2.0, source_start=1.5)], "never looped or frozen"),
        (lambda s: [placement(s["clip"], 0.0, 1.0, source_start=-1)], "in-point"),
        (lambda s: [placement(s["image"], 0.0, 1.0, source_start=0.5)], "image has no in-point"),
        (lambda s: [{**placement(s["image"], 0.0, 1.0), "command": "del"}], "unknown field"),
        (lambda s: [placement(s["image"], 0.0, 1.0), placement(s["clip"], 2.0, 3.0, pid="p00000001")], "repeats the id"),
        (lambda s: [placement(s["image"], 0.5, 2.0), placement(s["clip"], 1.5, 3.0, template_id="pip", pid="p00000002")], "overlap"),
    ],
)
def test_invalid_placements_are_refused_with_a_reason(client, ready, build, fragment):
    result = validate(client, ready, build(ready))
    assert not result["valid"]
    assert any(fragment in error["message"] for error in result["errors"]), result["errors"]


def test_non_finite_times_are_refused(ready):
    base = {"duration_seconds": 4.0}
    for bad in (float("nan"), float("inf"), True, None):
        _placements, errors = broll.validate_placements([placement(ready["image"], bad, 1.5)], base, broll.library_index(ready["project"]))
        assert errors and "finite" in errors[0]["message"], bad


def test_valid_placements_back_to_back_and_defaults_filled(client, ready):
    result = validate(client, ready, [
        placement(ready["clip"], 1.0, 3.4, template_id="pip", source_start=0.5, pid="p00000002"),
        placement(ready["image"], 0.0, 1.0),
        placement(ready["image"], 3.5, 4.0004, pid="p00000003"),  # within a millisecond of the end
    ])
    assert result["valid"], result["errors"]
    first, second, third = result["placements"]
    assert first["settings"] == {
        "fit": "contain", "caption_scrim": True, "muted": True, "entrance": "none", "entrance_seconds": 0.4,
    }
    assert second["settings"] == {
        "position": "auto", "size_percent": 55.0, "margin_percent": 4.0,
        "x_percent": 50.0, "y_percent": 30.0, "rotation_degrees": 0.0, "muted": True,
        "entrance": "none", "entrance_seconds": 0.4,
    }
    assert first["end"] == second["start"] and third["end"] == 4.0 and third["start"] == 3.5


def test_add_at_playhead_clamps_to_the_asset_the_video_and_the_next_placement(client, ready):
    pid, key = ready["project"], ready["key"]
    plan = add(client, pid, key, ready["clip"], 0.0).json()
    # A 3 s video: default 4 s is clamped to the asset, never looped.
    assert plan["placements"][0]["end"] == pytest.approx(3.0, abs=0.05) and plan["placements"][0]["source_start"] == 0.0
    plan = add(client, pid, key, ready["image"], 3.5).json()
    assert plan["placements"][-1]["end"] == 4.0  # clamped to the end of the video

    inside = add(client, pid, key, ready["image"], 1.0)
    assert inside.status_code == 409 and "already B-roll" in inside.json()["detail"]
    clip_placement = plan["placements"][0]
    plan = client.delete(f"/projects/{pid}/broll/inputs/{key}/placements/{clip_placement['id']}?base_revision={plan['revision']}").json()
    plan = add(client, pid, key, ready["image"], 1.0).json()
    assert [(p["start"], p["end"]) for p in plan["placements"]] == [(1.0, 3.5), (3.5, 4.0)]  # clamped to the next one
    plan = add(client, pid, key, ready["image"], 0.0).json()
    assert plan["placements"][0]["end"] == 1.0  # back-to-back, not overlapping
    full = add(client, pid, key, ready["image"], 3.95)
    assert full.status_code == 409


def test_revisions_are_immutable_conflicts_are_refused_and_plans_survive_a_restart(client, ready):
    pid, key = ready["project"], ready["key"]
    first = add(client, pid, key, ready["image"], 0.0).json()
    path = broll.plans_root(pid) / first["plan_id"] / "rev-0001.json"
    content = path.read_bytes()
    second = add(client, pid, key, ready["clip"], 3.0, template_id="pip").json()
    assert second["revision"] == 2 and second["parent_revision"] == 1 and path.read_bytes() == content

    stale = add(client, pid, key, ready["image"], 3.5, revision=1)
    assert stale.status_code == 409 and "changed since" in stale.json()["detail"]
    update = client.patch(f"/projects/{pid}/broll/inputs/{key}/placements/{first['placements'][0]['id']}",
                          json={"changes": {"template_id": "pip"}, "base_revision": 1})
    assert update.status_code == 409

    with TestClient(app) as restarted:
        state = state_of(restarted, pid, key)
    assert state["plan"]["revision"] == 2 and len(state["plan"]["placements"]) == 2


def test_the_planner_path_is_validated_exactly_like_manual_edits(client, ready):
    pid, key = ready["project"], ready["key"]
    response = client.put(f"/projects/{pid}/broll/inputs/{key}/plan", json={
        "placements": [placement(ready["image"], 0.5, 2.0), placement(ready["clip"], 1.0, 2.0, template_id="pip", pid="p00000002")],
        "base_revision": 0, "origin": "planner",
    })
    assert response.status_code == 400 and "overlap" in response.json()["detail"]
    response = client.put(f"/projects/{pid}/broll/inputs/{key}/plan", json={
        "placements": [placement(ready["image"], 0.5, 2.0)], "base_revision": 0, "origin": "planner",
    })
    assert response.status_code == 201 and response.json()["origin"] == "planner"


# --- composition and review ---------------------------------------------------------


def test_props_are_resolved_once_for_preview_and_export(client, ready):
    pid, key = ready["project"], ready["key"]
    add(client, pid, key, ready["image"], 0.0)
    add(client, pid, key, ready["clip"], 3.0, template_id="pip")
    state = state_of(client, pid, key)
    props = state["props"]
    assert props["video"] == {"width": 360, "height": 640, "fps": 25.0, "duration_in_frames": 100}
    assert props["captions"] == [] and props["broll"]["caption_band"] is None
    full, pip = props["broll"]["placements"]
    assert full["settings"]["fit"] == "contain" and full["asset"]["media_type"] == "image"
    # "auto" is resolved on the backend: centred just above the captions (real-world trial).
    assert pip["settings"]["position"] == "below_captions" and pip["source_start"] == 0.0
    assert "src" not in pip["asset"] and "path" not in json.dumps(props)

    digest = state["composition_digest"]
    client.patch(f"/projects/{pid}/broll/inputs/{key}/placements/{pip['id']}",
                 json={"changes": {"settings": {"position": "bottom_left"}}, "base_revision": state["plan"]["revision"]})
    changed = state_of(client, pid, key)
    assert changed["composition_digest"] != digest
    assert changed["props"]["broll"]["placements"][1]["settings"]["position"] == "bottom_left"


def test_a_changed_base_video_leaves_its_plan_stale(client, ready, base_file):
    pid, key = ready["project"], ready["key"]
    add(client, pid, key, ready["image"], 0.5)
    make_grid_video(base_file, seconds=4, size="360x640", rate=25, colour="red")  # re-recorded under the same name
    caption_inputs._fingerprint_cache.clear()
    caption_inputs._metadata_cache.clear()
    state = state_of(client, pid, key)
    assert state["plan"] is None and len(state["stale_plans"]) == 1 and state["props"]["broll"]["placements"] == []


def test_missing_and_changed_assets_block_use_until_fixed(client, ready, image, monkeypatch):
    monkeypatch.setattr(caption_jobs, "renderer_status", lambda: {"ready": False, "problems": ["not prepared"]})
    pid, key = ready["project"], ready["key"]
    add(client, pid, key, ready["image"], 0.5)
    state = state_of(client, pid, key)
    approved = client.post(f"/projects/{pid}/broll/inputs/{key}/approve", json={"composition_digest": state["composition_digest"]})
    assert approved.status_code == 200 and approved.json()["next"]["kind"] == "blocked"  # no renderer, but approved
    assert state_of(client, pid, key)["review"]["approved"]

    moved = image.with_name("moved.png")
    image.rename(moved)
    state = state_of(client, pid, key)
    assert client.get("/broll/library").json()["assets"][-1]["status"] == "missing"
    assert state["props"]["broll"]["placements"] == [] and "cannot be used" in state["issues"][0]["message"]
    assert not state["review"]["approved"] and not state["review"]["can_approve"]
    refused = client.post(f"/projects/{pid}/broll/inputs/{key}/approve", json={"composition_digest": state["composition_digest"]})
    assert refused.status_code == 409

    moved.rename(image)
    assert state_of(client, pid, key)["review"]["approved"]  # back as it was: the approval holds again

    make_image(image, colour="yellow")  # edited in place
    caption_inputs._fingerprint_cache.clear()
    asset = next(a for a in client.get("/broll/library").json()["assets"] if a["id"] == ready["image"])
    assert asset["status"] == "changed"
    assert client.post(f"/broll/library/{ready['image']}/remeasure").json()["status"] == "available"
    state = state_of(client, pid, key)
    assert not state["review"]["approved"] and "asset changed" in state["review"]["outdated_reason"]


def test_skip_and_approve_name_exactly_what_was_reviewed(client, ready, monkeypatch):
    monkeypatch.setattr(caption_jobs, "renderer_status", lambda: {"ready": False, "problems": ["not prepared"]})
    pid, key = ready["project"], ready["key"]
    state = state_of(client, pid, key)
    assert state["plan"] is None and state["planner"]["implemented"] is True
    nothing = client.post(f"/projects/{pid}/broll/inputs/{key}/approve", json={"composition_digest": state["composition_digest"]})
    assert nothing.status_code == 409 and "skip" in nothing.json()["detail"]

    skipped = client.post(f"/projects/{pid}/broll/inputs/{key}/skip", json={"composition_digest": state["composition_digest"]}).json()
    assert skipped["next"]["kind"] == "final"  # no captions: the video itself is the result
    state = state_of(client, pid, key)
    assert state["review"]["skipped"] and state["continuation"]["kind"] == "final"
    assert client.get(f"/projects/{pid}/broll/inputs/{key}/video?download=true").status_code == 200

    add(client, pid, key, ready["image"], 0.5)
    state = state_of(client, pid, key)
    assert not state["review"]["skipped"] and "plan changed" in state["review"]["outdated_reason"]
    wrong = client.post(f"/projects/{pid}/broll/inputs/{key}/approve", json={"composition_digest": "sha256:old"})
    assert wrong.status_code == 409


def test_an_approved_zoom_hands_over_to_broll_without_exporting(client, project, base_file, fake_whisper, planner):
    pid = project["id"]
    key = add_source(client, pid, base_file)
    job = client.post(f"/projects/{pid}/zooms/runs", json={"input_key": key}).json()["job"]
    assert wait_for_job(client, pid, job["id"])["status"] == "succeeded"
    run_id = zooms.list_runs(pid)[0]["run_id"]
    run = client.get(f"/projects/{pid}/zooms/runs/{run_id}").json()
    response = client.post(f"/projects/{pid}/zooms/runs/{run_id}/approve",
                           json={"output_digest": run["output"]["fingerprint"]["digest"], "captions": run["review"]["overlay_reference"]})
    assert response.status_code == 200
    following = response.json()["next"]
    assert following["kind"] == "broll" and following["input_key"] == "zoom.%s" % run_id
    # Stage 3B continues into planning on its own; with an empty library it cannot,
    # and says why instead of queueing a job that would fail.
    assert following["planning"]["status"] == "blocked" and "library is empty" in following["planning"]["message"]
    assert "could not be planned automatically" in following["message"]
    assert client.get(f"/projects/{pid}/broll/status").json()["selected_input"] == "zoom.%s" % run_id
    assert caption_jobs.list_exports(pid) == []  # nothing exported yet

    # The zoomed base carries its parent's captions; skipping continues to the
    # captioned export exactly as approving zooms used to.
    state = state_of(client, pid, "zoom.%s" % run_id)
    assert state["captions"] is not None and state["props"]["broll"]["caption_band"] == "bottom"


# --- rendering ----------------------------------------------------------------------


def _pcm(path) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg.exe", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", "16000",
         "-f", "s16le", "-"], capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0


def _rms(samples, start, end, rate=16000):
    window = samples[int(start * rate):int(end * rate)]
    return float(np.sqrt(np.mean(window ** 2))) if len(window) else 0.0


def _frame(path, index):
    return frame_rgb(path, index).reshape(640, 360, 3)


@needs_renderer
def test_a_real_export_composes_zooms_broll_and_hebrew_captions_correctly(client, project, base_file, image, clip, fake_whisper, planner, tmp_path):
    """One composition with both templates, over a zoomed video, with Hebrew captions.

    Base: a 360×640 grid, 25 fps, 4 s, a tone at 1.0–1.5 s; zoomed to 120 %
    from 1.0 s. B-roll: a red 16:9 image full-frame at 0.6–1.4 s (frames
    15–34), and a 4:3 video picture-in-picture at 2.0–3.2 s (frames 50–79)
    from its 0.5 s in-point — blue before 0.5 s, lime after — with a loud
    tone of its own that must stay muted. The reference is the existing
    captions export of the same zoomed video.
    """
    pid = project["id"]
    key = add_source(client, pid, base_file)
    job = client.post(f"/projects/{pid}/zooms/runs", json={"input_key": key}).json()["job"]
    assert wait_for_job(client, pid, job["id"])["status"] == "succeeded"
    run_id = zooms.list_runs(pid)[0]["run_id"]
    zoom_key = "zoom.%s" % run_id
    zoomed_file = zooms.output_path(pid, zooms.read_run(pid, run_id))
    whisper_calls, zoom_runs = len(fake_whisper.calls), len(zooms.list_runs(pid))
    transcripts = len(captions.list_transcripts(pid))

    picture, video = import_asset(client, image), import_asset(client, clip)
    saved = client.put(f"/projects/{pid}/broll/inputs/{zoom_key}/plan", json={"base_revision": 0, "placements": [
        placement(picture["id"], 0.6, 1.4, pid="p0000000a"),
        # Pinned to the top-right corner at 40 % (the pixel checks below); the
        # default above-captions position is covered by tests/broll.test.ts.
        placement(video["id"], 2.0, 3.2, template_id="pip", source_start=0.5, pid="p0000000b",
                  settings={"position": "top_right", "size_percent": 40}),
    ]})
    assert saved.status_code == 201, saved.text
    state = state_of(client, pid, zoom_key)
    assert state["issues"] == [] and state["captions"]["caption_count"] > 0

    response = client.post(f"/projects/{pid}/broll/inputs/{zoom_key}/approve", json={"composition_digest": state["composition_digest"]})
    assert response.status_code == 200 and response.json()["next"]["kind"] == "broll_export"
    record = wait_for_job(client, pid, response.json()["next"]["job"]["id"])
    assert record["status"] == "succeeded", record["error"]
    (export,) = broll_jobs.list_exports(pid)
    exported, _ = broll_jobs.resolve_export_file(pid, export["export_id"])

    # Preview and export are one: the snapshot rendered the props the review player was given.
    snapshot = json.loads((broll_jobs.export_directory(pid, export["export_id"]) / "snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["props"] == state["props"] and snapshot["composition_digest"] == state["composition_digest"]
    assert set(snapshot["assets"][0]) == {"id", "path", "filename", "media_type", "digest"}

    reference_job = client.post(f"/projects/{pid}/captions/exports", json={
        "transcript_id": state["captions"]["transcript_id"], "revision": state["captions"]["revision"],
        "format": "mp4", "input_key": zoom_key,
    }).json()
    assert wait_for_job(client, pid, reference_job["id"])["status"] == "succeeded"
    reference = next(r for r in caption_jobs.list_exports(pid) if r["input_key"] == zoom_key)
    reference_file, _ = caption_jobs.resolve_export_file(pid, reference["export_id"])

    # Streams, size, length, frame count.
    probe = ffprobe_json(exported, "-count_packets", "-show_entries", "stream=codec_type,codec_name,width,height,nb_read_packets:format=duration")
    streams = probe["streams"]
    assert [(s["codec_type"], s["codec_name"]) for s in streams] == [("video", "h264"), ("audio", "aac")]
    assert (streams[0]["width"], streams[0]["height"]) == (360, 640) and int(streams[0]["nb_read_packets"]) == 100
    assert float(probe["format"]["duration"]) == pytest.approx(4.0, abs=0.04 + 0.05)

    top = (slice(0, 200), slice(None))
    # Before the cutaway: exactly the zoomed, captioned reference.
    assert psnr(_frame(exported, 14)[top], _frame(reference_file, 14)[top]) > 35
    # Full-frame at frame 15 (= ceil(0.6 × 25)) through 34: the speaker is hidden and the
    # video-shaped (9:16) image fills the frame, NOT zoomed. Only the top 300 rows are
    # checked: the caption scrim darkens the lower part.
    for index in (15, 25, 34):
        frame = _frame(exported, index)
        red_rows = [y for y in range(300) if frame[y, 180, 0] > 200 and frame[y, 180, 1] < 60 and frame[y, 180, 2] < 60]
        assert len(red_rows) >= 297, (index, len(red_rows))
        assert frame[:180, :, 0].min() > 200, index  # red edge to edge, no grid showing through
    # Frame 35 (= ceil(1.4 × 25)): the speaker is back, at the zoomed framing of that moment.
    assert psnr(_frame(exported, 35)[top], _frame(reference_file, 35)[top]) > 35
    # (Remotion shifts fully saturated test colours a little against the FFmpeg file —
    # the captions export does the same — so this is relative: far closer to the
    # zoomed picture than to the un-zoomed source.)
    assert psnr(_frame(exported, 35)[top], _frame(zoomed_file, 35)[top]) > psnr(_frame(exported, 35)[top], _frame(base_file, 35)[top]) + 10

    # Picture-in-picture, frames 50–79: a 144×108 box (40 % of 360, the asset's 4:3), top right,
    # showing the asset from its in-point (lime, not the blue before 0.5 s).
    def lime(pixel):
        return pixel[1] > 200 and pixel[0] < 80 and pixel[2] < 80

    def run(frame, y, x, dy, dx):
        length = 0
        while 0 <= y < 640 and 0 <= x < 360 and lime(frame[y, x]):
            length, y, x = length + 1, y + dy, x + dx
        return length

    box = (slice(20, 116), slice(208, 340))
    for index in (50, 65, 79):
        frame = _frame(exported, index)
        assert lime(frame[68, 274]), index
        width = run(frame, 68, 274, 0, 1) + run(frame, 68, 273, 0, -1)
        height = run(frame, 68, 274, 1, 0) + run(frame, 67, 274, -1, 0)
        assert abs(width - 144) <= 3 and abs(height - 108) <= 3, (index, width, height)
    for index in (49, 80):  # one frame before and after: the speaker, exactly as in the reference
        assert psnr(_frame(exported, index)[box], _frame(reference_file, index)[box]) > 35, index
    outside = (slice(150, 420), slice(0, 180))
    assert psnr(_frame(exported, 60)[outside], _frame(reference_file, 60)[outside]) > 35  # speaker untouched beside it

    # Captions: identical to the reference under picture-in-picture, and in the same place
    # (same white text pixels) over the full-frame image.
    band = (slice(320, 440), slice(None))  # the caption line at the default 60 %
    assert psnr(_frame(exported, 60)[band], _frame(reference_file, 60)[band]) > 35
    over_image = _frame(exported, 20)[band]
    text = (over_image > 230).all(axis=2)
    assert text.sum() > 50, "no caption visible over the full-frame image"
    assert ((_frame(reference_file, 20)[band] > 200).all(axis=2)[text]).mean() > 0.85

    # Audio: the speaker's track once, at its own level and time; the B-roll's tone muted.
    ours, source = _pcm(exported), _pcm(base_file)
    assert 0.89 < _rms(ours, 1.05, 1.45) / _rms(source, 1.05, 1.45) < 1.12  # not doubled, not lost
    onset = lambda samples: np.argmax(np.abs(samples) > 0.05) / 16000  # noqa: E731
    assert abs(onset(ours) - onset(source)) < 0.03
    assert _rms(ours, 2.05, 3.15) < 0.003  # the asset's loud 300 Hz tone is not there

    # Nothing upstream was redone for B-roll.
    assert len(fake_whisper.calls) == whisper_calls and len(zooms.list_runs(pid)) == zoom_runs
    assert len(captions.list_transcripts(pid)) == transcripts

    for index in (14, 20, 35, 60):  # kept for a person to look at
        subprocess.run(["ffmpeg.exe", "-hide_banner", "-loglevel", "error", "-y", "-i", str(exported), "-vf",
                        "select=eq(n\\,%d)" % index, "-frames:v", "1", str(tmp_path / ("frame-%03d.png" % index))], check=True)


@needs_renderer
def test_an_export_renders_its_snapshot_and_can_be_cancelled(client, project, tmp_path, image):
    long_video = make_grid_video(tmp_path / "ארוך.mp4", seconds=30, size="1280x720", rate=30)
    pid = project["id"]
    key = add_source(client, pid, long_video)
    asset = import_asset(client, image)
    add(client, pid, key, asset["id"], 1.0)
    state = state_of(client, pid, key)
    response = client.post(f"/projects/{pid}/broll/inputs/{key}/approve", json={"composition_digest": state["composition_digest"]})
    job_id = response.json()["next"]["job"]["id"]

    # Editing the plan while the export waits or runs changes nothing it renders.
    placement_id = state["plan"]["placements"][0]["id"]
    client.delete(f"/projects/{pid}/broll/inputs/{key}/placements/{placement_id}?base_revision=1")
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        record = client.get(f"/projects/{pid}/jobs/{job_id}").json()
        if "drawn" in (record["progress_message"] or ""):
            break
        assert record["status"] in ("queued", "running"), record
        time.sleep(0.1)
    (running,) = broll_jobs.list_exports(pid)
    snapshot = json.loads((broll_jobs.export_directory(pid, running["export_id"]) / "snapshot.json").read_text(encoding="utf-8"))
    assert len(snapshot["props"]["broll"]["placements"]) == 1

    client.post(f"/projects/{pid}/jobs/{job_id}/cancel")
    record = wait_for_job(client, pid, job_id)
    assert record["status"] == "cancelled"
    (cancelled,) = broll_jobs.list_exports(pid)
    directory = broll_jobs.export_directory(pid, cancelled["export_id"])
    assert cancelled["status"] == "cancelled" and not cancelled["downloadable"]
    assert not any(path.suffix == ".mp4" for path in directory.iterdir())
    assert os.path.isfile(long_video)


# --- per-video libraries -------------------------------------------------------------


def import_to_project(client, project_id, path) -> dict:
    response = client.post(f"/projects/{project_id}/broll/library/imports", json={"path": str(path)})
    assert response.status_code == 201, response.text
    return response.json()


def ids_seen(client, project_id=None, own_only=False) -> set[str]:
    url = f"/projects/{project_id}/broll/library" if project_id else "/broll/library"
    response = client.get(url, params={"own_only": own_only} if project_id else None)
    assert response.status_code == 200, response.text
    return {asset["id"] for asset in response.json()["assets"]}


def test_a_videos_own_assets_are_visible_to_that_video_only(client, ready, media_dir):
    other = client.post("/projects", json={"name": "אחר"}).json()["id"]
    own = import_to_project(client, ready["project"], make_image(media_dir / "פרטי.png", colour="green"))
    assert own["duplicate"] is False and own["asset"]["library"] == "project"
    own_id = own["asset"]["id"]

    # Stored in the video's own folder, with its thumbnail beside it.
    root = broll_library.library_root(ready["project"])
    assert root.parent.name == ready["project"]
    assert any(path.name.startswith(own_id) for path in (root / "thumbnails").iterdir())

    general = {ready["image"], ready["clip"]}
    assert ids_seen(client) == general
    assert ids_seen(client, ready["project"]) == general | {own_id}
    assert ids_seen(client, ready["project"], own_only=True) == {own_id}
    assert ids_seen(client, other) == general

    # Usable by its own video, unreachable from anywhere else.
    assert validate(client, ready, [placement(own_id, 0.5, 1.5)])["valid"]
    assert client.get(f"/projects/{ready['project']}/broll/library/{own_id}/thumbnail").status_code == 200
    assert client.get(f"/projects/{ready['project']}/broll/library/{own_id}/media").status_code == 200
    for url in (f"/broll/library/{own_id}/media", f"/projects/{other}/broll/library/{own_id}/media",
                f"/projects/{other}/broll/library/{own_id}/thumbnail"):
        assert client.get(url).status_code == 404, url
    assert client.patch(f"/projects/{other}/broll/library/{own_id}", json={"description": "x"}).status_code == 404
    assert client.delete(f"/projects/{other}/broll/library/{own_id}").status_code == 404
    catalog = client.get(f"/projects/{other}/broll/catalog").json()
    assert own_id not in {asset["asset_id"] for asset in catalog["assets"]}

    # A video edits general assets through its own view, too.
    edited = client.patch(f"/projects/{ready['project']}/broll/library/{ready['image']}", json={"description": "אדום"})
    assert edited.status_code == 200 and edited.json()["library"] == "general"


def test_a_file_the_video_already_sees_is_not_imported_again(client, ready, image):
    again = import_to_project(client, ready["project"], image)
    assert again["duplicate"] is True and again["asset"]["id"] == ready["image"]
    assert ids_seen(client, ready["project"], own_only=True) == set()


def test_moving_an_asset_between_a_video_and_the_general_library_keeps_its_id(client, ready, media_dir):
    other = client.post("/projects", json={"name": "אחר"}).json()["id"]
    own_id = import_to_project(client, ready["project"], make_image(media_dir / "משותף.png", colour="yellow"))["asset"]["id"]
    move = f"/projects/{ready['project']}/broll/library/{own_id}/move"

    shared = client.post(move, json={"to": "general"})
    assert shared.status_code == 200 and shared.json()["library"] == "general" and shared.json()["id"] == own_id
    assert own_id in ids_seen(client, other) and own_id in ids_seen(client)
    assert client.get(f"/projects/{other}/broll/library/{own_id}/thumbnail").status_code == 200
    assert validate(client, ready, [placement(own_id, 0.5, 1.5)])["valid"]

    # A general asset made private to the other video.
    private = client.post(f"/projects/{other}/broll/library/{own_id}/move", json={"to": "project"})
    assert private.status_code == 200 and private.json()["library"] == "project"
    assert own_id not in ids_seen(client, ready["project"]) and own_id in ids_seen(client, other, own_only=True)
    assert not validate(client, ready, [placement(own_id, 0.5, 1.5)])["valid"]

    assert client.post(move, json={"to": "general"}).status_code == 404  # not visible to this video any more
    assert client.post(f"/projects/{other}/broll/library/{own_id}/move", json={"to": "elsewhere"}).status_code == 400
