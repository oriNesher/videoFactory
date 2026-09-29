"""Tests for subtitles with Whisper, and merging a run's clips into one video.

- **Pure** — the legacy line-splitting rules, SRT and WebVTT rendering, and
  settings validation.
- **Plumbing** — the whole job path, with Whisper replaced by a tiny stand-in
  script that speaks the worker's protocol. No model is loaded and nothing is
  downloaded, so these run anywhere in well under a second each.
"""

import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import cutting, jobs, media, subtitle_runner, subtitles  # noqa: E402
from backend.config import WORKSPACE_ENV_VAR  # noqa: E402
from backend.main import app  # noqa: E402

DEFAULTS = subtitles.default_settings()

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg.exe") and shutil.which("ffprobe.exe")),
    reason="FFmpeg / FFprobe are not on PATH",
)


def word(start, end, text):
    return {"start": start, "end": end, "word": text}


# --- pure: splitting ---------------------------------------------------------


def test_splits_at_the_word_limit():
    segment = {
        "start": 0.0,
        "end": 3.5,
        "text": "",
        "words": [word(i * 0.5, i * 0.5 + 0.4, "w%d" % i) for i in range(7)],
    }
    lines = subtitles.split_segment(segment, {**DEFAULTS, "max_words": 3})
    assert [line.text for line in lines] == ["w0 w1 w2", "w3 w4 w5", "w6"]
    assert lines[0].start == 0.0 and lines[0].end == pytest.approx(1.4)


def test_splits_on_a_pause():
    segment = {
        "start": 0.0,
        "end": 3.0,
        "text": "",
        "words": [word(0.0, 0.3, "שלום"), word(0.35, 0.6, "לכם"), word(2.0, 2.3, "היום")],
    }
    lines = subtitles.split_segment(segment, DEFAULTS)
    assert [line.text for line in lines] == ["שלום לכם", "היום"]


def test_splits_at_punctuation_only_after_min_words():
    segment = {
        "start": 0.0,
        "end": 2.0,
        "text": "",
        "words": [
            word(0.0, 0.2, "כן,"),
            word(0.2, 0.4, "אני"),
            word(0.4, 0.6, "יודע."),
            word(0.6, 0.8, "אבל"),
        ],
    }
    lines = subtitles.split_segment(segment, {**DEFAULTS, "min_words": 2})
    assert [line.text for line in lines] == ["כן, אני יודע.", "אבל"]


def test_character_limit_closes_the_line_before_overflowing():
    segment = {
        "start": 0.0,
        "end": 2.0,
        "text": "",
        "words": [word(0.0, 0.2, "aaaaaaaa"), word(0.2, 0.4, "bbbbbbbb"), word(0.4, 0.6, "cc")],
    }
    lines = subtitles.split_segment(segment, {**DEFAULTS, "max_characters": 12})
    assert [line.text for line in lines] == ["aaaaaaaa", "bbbbbbbb cc"]


def test_segment_without_words_falls_back_to_its_text():
    segment = {"start": 1.0, "end": 2.0, "text": "  hello   there ", "words": []}
    lines = subtitles.split_segment(segment, DEFAULTS)
    assert [(line.start, line.end, line.text) for line in lines] == [(1.0, 2.0, "hello there")]


def test_render_srt_and_vtt():
    lines = [subtitles.Line(0.0, 1.5, "שלום"), subtitles.Line(3661.25, 3662.0, "עולם")]
    srt = subtitles.render_srt(lines)
    m = "‏"  # every line is wrapped in right-to-left marks
    assert srt == (
        f"1\n00:00:00,000 --> 00:00:01,500\n{m}שלום{m}\n\n"
        f"2\n01:01:01,250 --> 01:01:02,000\n{m}עולם{m}\n"
    )
    vtt = subtitles.srt_to_vtt("﻿" + srt)
    assert vtt.startswith(f"WEBVTT\n\n1\n00:00:00.000 --> 00:00:01.500\n{m}שלום{m}\n")


# --- pure: settings ----------------------------------------------------------


def test_settings_validation():
    assert subtitles.validate_settings(None) == DEFAULTS
    assert subtitles.validate_settings({"max_words": 3.0})["max_words"] == 3

    for bad in ({"max_words": 0}, {"max_words": 2.5}, {"pause_seconds": "1"}, {"nope": 1}):
        with pytest.raises(subtitles.SubtitleError):
            subtitles.validate_settings(bad)



# --- plumbing ----------------------------------------------------------------

FAKE_WORKER = textwrap.dedent(
    """
    import argparse, json, sys, time
    parser = argparse.ArgumentParser()
    for name in ("--input", "--output", "--model", "--language"):
        parser.add_argument(name)
    args = parser.parse_args()
    if "fail" in args.input:
        print("boom: the model exploded", flush=True)
        sys.exit(3)
    print("@@loading", flush=True)
    print("@@info he 0.9900 4.000", flush=True)
    if "slow" in args.input:
        time.sleep(30)
    print("@@progress 4.000", flush=True)
    words = [
        {"start": 0.0, "end": 0.4, "word": " שלום"},
        {"start": 0.5, "end": 0.9, "word": " לכולם."},
        {"start": 2.0, "end": 2.5, "word": " ברוכים"},
        {"start": 2.6, "end": 3.0, "word": " הבאים"},
    ]
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump({"language": "he", "language_probability": 0.99,
                   "duration_seconds": 4.0, "model": args.model,
                   "segments": [{"start": 0.0, "end": 3.0, "text": "", "words": words}]},
                  handle, ensure_ascii=False)
    """
)


@pytest.fixture(autouse=True)
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv(WORKSPACE_ENV_VAR, str(tmp_path / "workspace"))
    worker = tmp_path / "fake_worker.py"
    worker.write_text(FAKE_WORKER, encoding="utf-8")
    monkeypatch.setattr(subtitle_runner, "WORKER_SCRIPT", worker)
    monkeypatch.setattr(subtitles, "whisper_installed", lambda: True)


@pytest.fixture
def client():
    with TestClient(app) as running:
        yield running


@pytest.fixture
def project(client):
    return client.post("/projects", json={"name": "כתוביות"}).json()


def make_run(project_id: str, clip_names: list[str]) -> str:
    """A finished cut run on disk, as the cutting module would have left it."""
    run_id = cutting.new_run_id()
    directory = cutting.run_directory(project_id, run_id)
    (directory / cutting.CLIPS_DIRECTORY).mkdir(parents=True)

    clips = []
    for index, name in enumerate(clip_names, start=1):
        filename = "%04d_%s_trimmed.mp4" % (index, name)
        (directory / cutting.CLIPS_DIRECTORY / filename).write_bytes(b"not really a video")
        clips.append(
            {
                "output_id": "clip-%04d" % index,
                "order": index,
                "source_filename": name + ".mkv",
                "filename": filename,
                "relative_path": "clips/" + filename,
                "status": cutting.CLIP_SUCCEEDED,
                "duration_seconds": 4.0,
            }
        )

    cutting.write_manifest(
        {
            "schema_version": cutting.RUN_SCHEMA_VERSION,
            "run_id": run_id,
            "project_id": project_id,
            "job_id": "0" * 32,
            "created_at": cutting.now(),
            "finished_at": cutting.now(),
            "status": cutting.RUN_SUCCEEDED,
            "settings": cutting.default_settings(),
            "output_mode": cutting.MODE_CLIPS,
            "tool_versions": {},
            "sources": [],
            "clips": clips,
            "combined": None,
            "error": None,
            "notes": [],
        }
    )
    return run_id


def wait_for_job(client, project_id, job_id, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = client.get(f"/projects/{project_id}/jobs/{job_id}").json()
        if record["status"] in jobs.FINISHED_STATUSES:
            return record
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def start(client, project_id, run_id, output_ids, **extra):
    return client.post(
        f"/projects/{project_id}/subtitles/jobs",
        json={"run_id": run_id, "output_ids": output_ids, **extra},
    )


def test_settings_round_trip(client, project):
    loaded = client.get(f"/projects/{project['id']}/subtitles/settings").json()
    assert loaded["catalog"]["model"]["id"] == subtitles.MODEL
    assert loaded["catalog"]["language"] == "Hebrew"
    assert loaded["settings"] == DEFAULTS

    saved = client.put(
        f"/projects/{project['id']}/subtitles/settings",
        json={"settings": {"max_words": 4}},
    ).json()
    assert saved["settings"]["max_words"] == 4

    again = client.get(f"/projects/{project['id']}/subtitles/settings").json()
    assert again["settings"]["max_words"] == 4


def test_model_and_language_are_fixed(client, project):
    run_id = make_run(project["id"], ["take1"])
    # Extra fields from an older client are ignored, not obeyed.
    response = start(client, project["id"], run_id, ["clip-0001"], model="small", language="en")
    assert response.status_code == 201
    assert response.json()["input"]["model"] == subtitles.MODEL
    assert response.json()["input"]["language"] == "he"
    wait_for_job(client, project["id"], response.json()["id"])


def test_job_writes_srt_and_shows_it_on_the_run(client, project):
    run_id = make_run(project["id"], ["take1", "take2"])

    response = start(client, project["id"], run_id, ["clip-0001", "clip-0002"])
    assert response.status_code == 201, response.text
    job = wait_for_job(client, project["id"], response.json()["id"])
    assert job["status"] == "succeeded", job["error"]
    assert "2 videos" in job["result"]["summary"]

    srt = client.get(
        f"/projects/{project['id']}/subtitles/runs/{run_id}/outputs/clip-0001/srt"
    )
    assert srt.status_code == 200
    assert "0001_take1_trimmed.srt" in srt.headers["content-disposition"]
    text = srt.content.decode("utf-8-sig").replace("‏", "")
    # A pause after "לכולם." and the punctuation both end the first line.
    assert text == (
        "1\n00:00:00,000 --> 00:00:00,900\nשלום לכולם.\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nברוכים הבאים\n"
    )
    assert srt.content.startswith(b"\xef\xbb\xbf")  # BOM, for Premiere

    vtt = client.get(
        f"/projects/{project['id']}/subtitles/runs/{run_id}/outputs/clip-0001/vtt"
    )
    assert vtt.text.startswith("WEBVTT") and "00:00:00.900" in vtt.text

    run = client.get(f"/projects/{project['id']}/cutting/runs/{run_id}").json()
    entry = run["subtitles"]["clip-0002"]
    assert entry["available"] is True
    assert entry["current"]["line_count"] == 2
    assert entry["attempt"]["status"] == "succeeded"

    directory = subtitles.subtitles_directory(project["id"], run_id)
    assert (directory / "clip-0001.transcript.json").is_file()
    assert not list(directory.glob(".*"))  # no temporary files left behind


def test_failed_attempt_keeps_the_previous_srt(client, project):
    run_id = make_run(project["id"], ["fail"])
    directory = subtitles.subtitles_directory(project["id"], run_id)
    directory.mkdir(parents=True)
    previous = "1\n00:00:00,000 --> 00:00:01,000\nקודם\n"
    subtitles.srt_path(project["id"], run_id, "clip-0001").write_text(previous, encoding="utf-8")
    subtitles.write_record(
        project["id"],
        run_id,
        {"output_id": "clip-0001", "current": {"created_at": "x", "line_count": 1}, "attempt": None},
    )

    job = wait_for_job(
        client, project["id"], start(client, project["id"], run_id, ["clip-0001"]).json()["id"]
    )
    assert job["status"] == "failed"
    assert "model exploded" in job["error"]

    entry = client.get(f"/projects/{project['id']}/cutting/runs/{run_id}").json()["subtitles"][
        "clip-0001"
    ]
    assert entry["attempt"]["status"] == "failed"
    assert entry["available"] is True  # the earlier subtitles are still there
    assert subtitles.srt_path(project["id"], run_id, "clip-0001").read_text(encoding="utf-8") == previous


def test_cancel_kills_whisper_and_records_it(client, project):
    run_id = make_run(project["id"], ["slow"])
    job_id = start(client, project["id"], run_id, ["clip-0001"]).json()["id"]

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        record = client.get(f"/projects/{project['id']}/jobs/{job_id}").json()
        if record["status"] == "running":
            break
        time.sleep(0.05)
    time.sleep(0.5)

    started = time.monotonic()
    client.post(f"/projects/{project['id']}/jobs/{job_id}/cancel")
    job = wait_for_job(client, project["id"], job_id)
    assert job["status"] == "cancelled"
    assert time.monotonic() - started < 10  # far less than the stand-in's 30 s sleep

    entry = client.get(f"/projects/{project['id']}/cutting/runs/{run_id}").json()["subtitles"][
        "clip-0001"
    ]
    assert entry["attempt"]["status"] == "cancelled"
    assert entry["available"] is False


def test_requests_are_refused_up_front(client, project):
    run_id = make_run(project["id"], ["take1"])

    assert start(client, project["id"], run_id, []).status_code == 400
    assert start(client, project["id"], run_id, ["clip-0009"]).status_code == 404
    assert start(client, project["id"], run_id, ["../../etc"]).status_code == 404
    assert start(client, project["id"], "not-a-run", ["clip-0001"]).status_code == 404
    assert (
        client.get(
            f"/projects/{project['id']}/subtitles/runs/{run_id}/outputs/clip-0001/srt"
        ).status_code
        == 404
    )


def make_real_clip(path: Path, seconds: int = 2) -> None:
    """A couple of seconds of 320×180 test pattern with a tone. Not footage."""
    subprocess.run(
        [
            "ffmpeg.exe", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=%d" % seconds,
            "-f", "lavfi", "-i", "sine=frequency=800:sample_rate=48000:duration=%d" % seconds,
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
        capture_output=True,
    )


@needs_ffmpeg
def test_run_subtitles_merge_the_clips_first_in_the_background(client, project):
    run_id = make_run(project["id"], ["take1", "take2"])
    directory = cutting.run_directory(project["id"], run_id)

    # Replace the placeholder bytes with real clips, described as a cut run would.
    manifest = cutting.read_manifest(project["id"], run_id)
    for clip in manifest["clips"]:
        path = directory / clip["relative_path"]
        make_real_clip(path)
        described = media.describe_input(str(path))
        clip.update(
            video=described["video"],
            audio=described["audio"],
            duration_seconds=described["duration_seconds"],
        )
    cutting.write_manifest(manifest)

    # No clips named: the run's merged video, which this run does not have yet.
    response = start(client, project["id"], run_id, None)
    assert response.status_code == 201, response.text
    job = wait_for_job(client, project["id"], response.json()["id"], timeout=60)
    assert job["status"] == "succeeded", job["error"]

    run = client.get(f"/projects/{project['id']}/cutting/runs/{run_id}").json()
    assert run["status"] == "succeeded"  # the cut's own outcome is untouched
    assert run["output_mode"] == "both"
    assert run["combined"]["playable"] is True
    assert run["combined"]["duration_seconds"] == pytest.approx(4.0, abs=0.3)
    assert run["active_jobs"] == []
    assert run["subtitles"]["combined"]["available"] is True

    srt = client.get(f"/projects/{project['id']}/subtitles/runs/{run_id}/outputs/combined/srt")
    assert srt.status_code == 200
    assert "combined.srt" in srt.headers["content-disposition"]

    # Again: the merged video is reused, not rebuilt or overwritten.
    size = (directory / run["combined"]["relative_path"]).stat().st_mtime_ns
    job = wait_for_job(
        client, project["id"], start(client, project["id"], run_id, None).json()["id"]
    )
    assert job["status"] == "succeeded", job["error"]
    assert (directory / run["combined"]["relative_path"]).stat().st_mtime_ns == size


def test_run_subtitles_refused_when_there_is_nothing_to_merge(client, project):
    run_id = make_run(project["id"], ["take1"])
    manifest = cutting.read_manifest(project["id"], run_id)
    manifest["clips"][0]["status"] = cutting.CLIP_EMPTY
    cutting.write_manifest(manifest)

    response = start(client, project["id"], run_id, None)
    assert response.status_code == 400
    assert "no clips" in response.json()["detail"]


def test_one_subtitle_job_per_run_at_a_time(client, project):
    run_id = make_run(project["id"], ["slow"])
    first = start(client, project["id"], run_id, ["clip-0001"]).json()["id"]

    second = start(client, project["id"], run_id, ["clip-0001"])
    assert second.status_code == 409

    run = client.get(f"/projects/{project['id']}/cutting/runs/{run_id}").json()
    assert [job["id"] for job in run["active_jobs"]] == [first]

    client.post(f"/projects/{project['id']}/jobs/{first}/cancel")
    wait_for_job(client, project["id"], first)


def make_black_tail_clip(path: Path) -> None:
    """Two seconds whose last two frames are black, as Auto-Editor leaves them.

    Encoded with B-frames (the libx264 default), like Auto-Editor's output —
    that is what makes the black impossible to drop without re-encoding.
    """
    subprocess.run(
        [
            "ffmpeg.exe", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=800:sample_rate=48000:duration=2",
            "-vf", "drawbox=c=black:t=fill:enable='gte(n,58)'",
            "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
        capture_output=True,
    )


def black_runs(path: Path) -> list[str]:
    output = subprocess.run(
        ["ffmpeg.exe", "-hide_banner", "-nostats", "-i", str(path), "-an",
         "-vf", "blackdetect=d=0:pix_th=0.10", "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr
    return [line for line in output.splitlines() if "black_start" in line]


@needs_ffmpeg
def test_black_frames_at_the_end_are_found(tmp_path):
    clip = tmp_path / "tail.mp4"
    make_black_tail_clip(clip)
    duration = media.verify_output(str(clip))["duration_seconds"]
    assert media.trailing_black_start(str(clip), duration) == pytest.approx(58 / 30, abs=0.01)


@needs_ffmpeg
def test_merged_video_has_no_black_blips_between_clips(client, project):
    """The reported bug: a black flash at every join of a merged video."""
    run_id = make_run(project["id"], ["take1", "take2"])
    directory = cutting.run_directory(project["id"], run_id)
    manifest = cutting.read_manifest(project["id"], run_id)
    for clip in manifest["clips"]:
        path = directory / clip["relative_path"]
        make_black_tail_clip(path)
        described = media.describe_input(str(path))
        clip.update(
            video=described["video"],
            audio=described["audio"],
            duration_seconds=described["duration_seconds"],
        )
    cutting.write_manifest(manifest)

    job = wait_for_job(
        client, project["id"], start(client, project["id"], run_id, None).json()["id"], 60
    )
    assert job["status"] == "succeeded", job["error"]

    run = client.get(f"/projects/{project['id']}/cutting/runs/{run_id}").json()
    assert run["combined"]["strategy"] == cutting.COMBINE_REENCODE
    assert run["combined"]["trimmed_black_seconds"] == pytest.approx(4 / 30, abs=0.02)
    assert run["combined"]["duration_seconds"] == pytest.approx(2 * 58 / 30, abs=0.1)
    assert black_runs(directory / run["combined"]["relative_path"]) == []


@needs_ffmpeg
def test_a_clip_without_a_black_tail_is_left_alone(tmp_path):
    clip = tmp_path / "clean.mp4"
    make_real_clip(clip)
    duration = media.verify_output(str(clip))["duration_seconds"]
    assert media.trailing_black_start(str(clip), duration) is None


def test_missing_whisper_is_explained(client, project, monkeypatch):
    monkeypatch.setattr(subtitles, "whisper_installed", lambda: False)
    run_id = make_run(project["id"], ["take1"])
    response = start(client, project["id"], run_id, ["clip-0001"])
    assert response.status_code == 503
    assert "pip install" in response.json()["detail"]
