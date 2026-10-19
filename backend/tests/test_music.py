"""Tests for stage 4: the music library, the music stage and the final export.

- **Library** — import and measured metadata, references not copies,
  duplicates, unsupported files, a changed file noticed and re-measured,
  removal leaving the file alone.
- **Settings** — bounds and unknown keys refused, a new track starting at
  its beginning.
- **Hand-over** — B-roll's approval or skip continues to the music stage
  (no export yet), and the music stage waits for that decision.
- **Review** — props as composed for preview and export, a decision naming
  exactly what was composed and outdated by any change to it, continuing
  without anything to add making the video itself the result.
- **Rendering** — one real Remotion export with looping, faded music under
  the speaker, inspected by its audio.

Small generated fixtures only. No test calls Gemini or loads Whisper.
"""

import json
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import audio_library, caption_inputs, caption_jobs, jobs, music, music_jobs  # noqa: E402
from backend.tests.test_broll import (  # noqa: E402, F401 - fixtures
    _pcm, _rms, add, add_source, base_file, client, environment, ffmpeg, image, import_asset, media_dir,
    needs_renderer, state_of, wait_for_job,
)

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg.exe") and shutil.which("ffprobe.exe")), reason="FFmpeg / FFprobe are not on PATH"
)
pytestmark = needs_ffmpeg


# --- fixtures ------------------------------------------------------------------------


@pytest.fixture
def project(client):
    # The music stage is on by default: no settings needed.
    return client.post("/projects", json={"name": "מוזיקה"}).json()


def make_track(path: Path, seconds=2.5, frequency=300) -> Path:
    """A steady sine tone, full lavfi level (amplitude 1/8)."""
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=%d:duration=%s" % (frequency, seconds), "-c:a", "libmp3lame", "-b:a", "128k", path)
    return path


@pytest.fixture
def track_file(media_dir):
    return make_track(media_dir / "מנגינה רגועה.mp3")


def import_track(client, path) -> dict:
    response = client.post("/audio/library/imports", json={"path": str(path), "kind": "music"})
    assert response.status_code == 201, response.text
    return response.json()


def music_state(client, project_id) -> dict:
    response = client.get(f"/projects/{project_id}/music")
    assert response.status_code == 200, response.text
    return response.json()


def save(client, project_id, **changes):
    return client.put(f"/projects/{project_id}/music/settings", json=changes)


def skip_broll(client, project_id, key) -> dict:
    state = state_of(client, project_id, key)
    response = client.post(f"/projects/{project_id}/broll/inputs/{key}/skip", json={"composition_digest": state["composition_digest"]})
    assert response.status_code == 200, response.text
    return response.json()


def decide(client, project_id, kind, digest=None):
    if digest is None:
        digest = music_state(client, project_id)["composition_digest"]
    return client.post(f"/projects/{project_id}/music/{kind}", json={"composition_digest": digest})


@pytest.fixture
def no_renderer(monkeypatch):
    monkeypatch.setattr(caption_jobs, "renderer_status", lambda: {"ready": False, "problems": ["not prepared"]})


# --- the library ---------------------------------------------------------------------


def test_importing_measures_the_track_and_references_it_in_place(client, track_file, tmp_path):
    before = track_file.read_bytes()
    result = import_track(client, track_file)
    track = result["asset"]
    assert result["duplicate"] is False and track["kind"] == "music" and track["status"] == "available"
    assert track["measured"]["duration_seconds"] == pytest.approx(2.5, abs=0.1)
    assert track["measured"]["codec"] == "mp3" and track["storage"] == "referenced"
    assert track["display_name"] == track_file.name and "path" not in track
    assert track_file.read_bytes() == before
    stored = json.loads(audio_library.library_file().read_text(encoding="utf-8"))
    assert stored["assets"][0]["path"] == str(track_file)

    again = import_track(client, track_file)
    assert again["duplicate"] is True and again["asset"]["id"] == track["id"]

    text = tmp_path / "notes.txt"
    text.write_text("not music", encoding="utf-8")
    refused = client.post("/audio/library/imports", json={"path": str(text)})
    assert refused.status_code == 400 and "Unsupported" in refused.json()["detail"]
    fake = tmp_path / "fake.mp3"
    fake.write_bytes(b"\x00" * 2048)
    assert client.post("/audio/library/imports", json={"path": str(fake)}).status_code == 400

    renamed = client.patch(f"/audio/library/{track['id']}", json={"display_name": "  רגוע   ושקט "}).json()
    assert renamed["display_name"] == "רגוע ושקט"
    media = client.get(f"/audio/library/{track['id']}/media")
    assert media.status_code == 200 and media.headers["content-type"] == "audio/mpeg"


def test_a_changed_track_is_noticed_and_removing_it_leaves_the_file(client, track_file):
    track = import_track(client, track_file)["asset"]
    make_track(track_file, seconds=3.5)  # re-recorded under the same name
    caption_inputs._fingerprint_cache.clear()
    (listed,) = client.get("/audio/library?kind=music").json()["assets"]
    assert listed["status"] == "changed" and "Re-measure" in listed["status_reason"]
    remeasured = client.post(f"/audio/library/{track['id']}/remeasure").json()
    assert remeasured["status"] == "available" and remeasured["measured"]["duration_seconds"] == pytest.approx(3.5, abs=0.1)

    assert client.delete(f"/audio/library/{track['id']}").json() == {"removed": track["id"]}
    assert client.get("/audio/library").json()["assets"] == [] and track_file.is_file()


# --- settings --------------------------------------------------------------------------


def test_settings_are_validated_and_a_new_track_starts_at_its_beginning(client, project, track_file):
    pid = project["id"]
    assert music_state(client, pid)["settings"] == {"enabled": True, "track_id": None, **music.DEFAULTS}
    assert save(client, pid, volume_percent=150).status_code == 400
    assert save(client, pid, fade_in_seconds="2").status_code == 400
    assert save(client, pid, loop="yes").status_code == 400
    assert save(client, pid, command="rm").status_code == 400
    assert save(client, pid, track_id="0123456789ab").status_code == 404

    track = import_track(client, track_file)["asset"]
    assert save(client, pid, start_seconds=1.5, volume_percent=40).status_code == 200
    saved = save(client, pid, track_id=track["id"]).json()
    assert saved["track_id"] == track["id"] and saved["start_seconds"] == 0.0 and saved["volume_percent"] == 40.0


# --- hand-over and review ----------------------------------------------------------------


def test_b_roll_decisions_continue_to_music_without_exporting(client, project, base_file, image):
    pid = project["id"]
    key = add_source(client, pid, base_file)
    state = music_state(client, pid)
    assert state["input_key"] == key and state["broll_state"] == "pending"
    assert "Approve or skip B-roll first" in state["review"]["blocked_reason"]
    assert decide(client, pid, "skip").status_code == 409

    skipped = skip_broll(client, pid, key)
    assert skipped["next"]["kind"] == "music"
    assert state_of(client, pid, key)["continuation"]["kind"] == "music"
    assert music_state(client, pid)["broll_state"] == "skipped"

    # Approving B-roll with a placement goes to music too, and exports nothing.
    asset = import_asset(client, image)
    assert add(client, pid, key, asset["id"], 0.5).status_code == 201
    broll_state = state_of(client, pid, key)
    approved = client.post(f"/projects/{pid}/broll/inputs/{key}/approve", json={"composition_digest": broll_state["composition_digest"]})
    assert approved.json()["next"]["kind"] == "music"
    assert not any(record["type"].endswith("_export") for record in jobs.list_jobs(pid))
    state = music_state(client, pid)
    assert state["broll_state"] == "approved" and len(state["props"]["broll"]["placements"]) == 1


def test_the_composed_music_is_what_gets_approved_and_any_change_outdates_it(client, project, base_file, track_file, no_renderer):
    pid = project["id"]
    key = add_source(client, pid, base_file)
    skip_broll(client, pid, key)
    assert decide(client, pid, "approve").status_code == 409  # no track yet

    track = import_track(client, track_file)["asset"]
    save(client, pid, track_id=track["id"], volume_percent=30, fade_in_seconds=0.5, fade_out_seconds=1.0, loop=False)
    state = music_state(client, pid)
    assert state["issues"] == [] and state["track"]["id"] == track["id"]
    assert state["props"]["music"] == {
        "asset": {"id": track["id"], "duration_seconds": track["measured"]["duration_seconds"]},
        "volume": 0.3, "start_seconds": 0.0, "fade_in_seconds": 0.5, "fade_out_seconds": 1.0, "loop": False,
    }
    assert decide(client, pid, "approve", "sha256:old").status_code == 409

    approved = decide(client, pid, "approve")
    assert approved.status_code == 200 and approved.json()["next"]["kind"] == "blocked"  # renderer missing here
    assert music_state(client, pid)["review"]["approved"]

    save(client, pid, volume_percent=35)
    review = music_state(client, pid)["review"]
    assert not review["approved"] and "settings changed" in review["outdated_reason"]
    decide(client, pid, "approve")
    assert music_state(client, pid)["review"]["approved"]

    make_track(track_file, seconds=4)  # the track re-recorded under the same name
    caption_inputs._fingerprint_cache.clear()
    state = music_state(client, pid)
    assert state["props"]["music"] is None and "changed" in state["issues"][0]
    assert not state["review"]["approved"] and not state["review"]["can_approve"]
    client.post(f"/audio/library/{track['id']}/remeasure")
    review = music_state(client, pid)["review"]
    assert not review["approved"] and "track changed" in review["outdated_reason"]

    save(client, pid, start_seconds=3.9)
    assert "past the end of the track" in music_state(client, pid)["issues"][0]


def test_a_new_b_roll_decision_outdates_the_music_decision(client, project, base_file, image, track_file, no_renderer):
    pid = project["id"]
    key = add_source(client, pid, base_file)
    skip_broll(client, pid, key)
    save(client, pid, track_id=import_track(client, track_file)["asset"]["id"])
    decide(client, pid, "approve")
    assert music_state(client, pid)["review"]["approved"]

    add(client, pid, key, import_asset(client, image)["id"], 0.5)
    state = music_state(client, pid)
    assert state["broll_state"] == "pending" and not state["review"]["approved"]
    broll_state = state_of(client, pid, key)
    client.post(f"/projects/{pid}/broll/inputs/{key}/approve", json={"composition_digest": broll_state["composition_digest"]})
    review = music_state(client, pid)["review"]
    assert not review["approved"] and "B-roll decision changed" in review["outdated_reason"]


def test_continuing_with_nothing_to_add_makes_the_video_itself_the_result(client, project, base_file):
    pid = project["id"]
    key = add_source(client, pid, base_file)
    skip_broll(client, pid, key)
    response = decide(client, pid, "skip")
    assert response.status_code == 200 and response.json()["next"]["kind"] == "final"
    state = music_state(client, pid)
    assert state["review"]["skipped"] and state["continuation"]["kind"] == "final"
    assert music_jobs.list_exports(pid) == [] and not jobs.list_jobs(pid)


def test_switching_the_stage_off_restores_b_rolls_own_export(client, project, base_file, no_renderer):
    pid = project["id"]
    key = add_source(client, pid, base_file)
    save(client, pid, enabled=False)
    assert skip_broll(client, pid, key)["next"]["kind"] == "final"  # no captions: the video itself, as before


# --- rendering ----------------------------------------------------------------------


def _band(samples, start, end, frequency, rate=16000):
    """Spectral magnitude around `frequency` in [start, end) seconds."""
    window = samples[int(start * rate):int(end * rate)]
    spectrum = np.abs(np.fft.rfft(window * np.hanning(len(window))))
    frequencies = np.fft.rfftfreq(len(window), 1 / rate)
    return float(spectrum[(frequencies > frequency - 25) & (frequencies < frequency + 25)].max())


@needs_renderer
def test_a_real_export_mixes_looping_faded_music_under_the_speaker(client, project, base_file, track_file):
    """Base: 4 s, 25 fps, an 880 Hz tone at 1.0–1.5 s. Music: a 2.5 s, 300 Hz
    tone at 60 %, faded in over 1 s and out over 0.5 s, looping."""
    pid = project["id"]
    key = add_source(client, pid, base_file)
    skip_broll(client, pid, key)
    track = import_track(client, track_file)["asset"]
    save(client, pid, track_id=track["id"], volume_percent=60, fade_in_seconds=1.0, fade_out_seconds=0.5, loop=True)

    response = decide(client, pid, "approve")
    assert response.status_code == 200 and response.json()["next"]["kind"] == "final_export", response.text
    record = wait_for_job(client, pid, response.json()["next"]["job"]["id"])
    assert record["status"] == "succeeded", record["error"]
    (export,) = music_jobs.list_exports(pid)
    exported, _ = music_jobs.resolve_export_file(pid, export["export_id"])
    assert exported.name.endswith(".final.mp4") and export["music"]["id"] == track["id"]
    assert music_state(client, pid)["continuation"]["export"]["export_id"] == export["export_id"]
    snapshot = json.loads((music_jobs.broll_jobs.export_directory(pid, export["export_id"], "final") / "snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["music"]["path"] == str(track_file) and snapshot["props"]["music"]["volume"] == 0.6

    ours, source = _pcm(exported), _pcm(base_file)
    full = _rms(ours, 1.6, 2.4)
    assert full > 0.02, "the music is there"
    assert _rms(ours, 0.05, 0.25) < 0.4 * full, "and fades in"
    assert _rms(ours, 2.7, 3.3) > 0.6 * full, "and loops past the end of the 2.5 s track"
    assert _rms(ours, 3.85, 3.98) < 0.5 * full, "and fades out at the end of the video"
    # The speaker's tone is unchanged under it: same level at 880 Hz as in the source.
    assert 0.8 < _band(ours, 1.1, 1.4, 880) / _band(source, 1.1, 1.4, 880) < 1.25
    assert _band(ours, 1.6, 2.4, 300) > 10 * _band(source, 1.6, 2.4, 300)
    assert os.path.isfile(track_file)


def test_a_finished_export_opens_in_vlc_and_explorer_by_id(client, project, monkeypatch):
    from backend import player

    pid = project["id"]
    directory = music_jobs.broll_jobs.export_directory(pid, "0123456789ab", "final")
    directory.mkdir(parents=True)
    (directory / "טייק 1.final.mp4").write_bytes(b"\x00" * 16)
    (directory / "export.json").write_text(json.dumps({
        "export_id": "0123456789ab", "status": caption_jobs.EXPORT_SUCCEEDED, "created_at": "2026-10-19T00:00:00+00:00",
        "output": {"filename": "טייק 1.final.mp4", "size_bytes": 16, "media_type": "video/mp4"},
    }), encoding="utf-8")
    launched = []
    monkeypatch.setattr(player, "find_vlc", lambda: "C:\vlc.exe")
    monkeypatch.setattr(player, "launch", lambda command: launched.append(command))
    monkeypatch.setattr(player.subprocess, "Popen", lambda command, **kwargs: launched.append(command))

    assert client.post(f"/projects/{pid}/music/exports/0123456789ab/open").json() == {"opened": True}
    assert client.post(f"/projects/{pid}/music/exports/0123456789ab/reveal").json() == {"opened": True}
    target = str(directory / "טייק 1.final.mp4")
    assert launched == [["C:\vlc.exe", target], 'explorer /select,"%s"' % os.path.normpath(target)]
    # Only finished exports of this video, by id.
    assert client.post(f"/projects/{pid}/music/exports/../../x/open").status_code == 404
    assert client.post(f"/projects/{pid}/music/exports/aaaaaaaaaaaa/reveal").status_code == 404
