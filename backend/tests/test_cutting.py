"""Focused tests for milestone 1A: manual Auto-Editor cutting.

Three layers, deliberately separated:

- **Pure** — settings validation, command construction, progress parsing and
  manifest bookkeeping. Fast, and they run everywhere because no tool is
  involved.
- **Plumbing** — the job path with the subprocess layer stubbed, so failure,
  cancellation and output isolation are tested without waiting on an encoder.
- **Real** — an end-to-end smoke test on short synthetic footage generated with
  FFmpeg, marked so it is skipped when the tools are not installed. It renders
  a few seconds of test pattern, never the user's own footage.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import cut_runner, cutting, jobs, media, processes  # noqa: E402
from backend.config import WORKSPACE_ENV_VAR  # noqa: E402
from backend.main import app  # noqa: E402

TOOLS_PRESENT = all(
    shutil.which(name) for name in ("ffmpeg.exe", "ffprobe.exe", "auto-editor.exe")
)

needs_tools = pytest.mark.skipif(
    not TOOLS_PRESENT, reason="FFmpeg / FFprobe / Auto-Editor are not on PATH"
)


@pytest.fixture(autouse=True)
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv(WORKSPACE_ENV_VAR, str(tmp_path / "workspace"))


@pytest.fixture
def client():
    with TestClient(app) as running:
        yield running


@pytest.fixture
def project(client):
    return client.post("/projects", json={"name": "פרויקט חיתוך"}).json()


def wait_for_job(client, project_id, job_id, timeout=180.0):
    deadline = time.monotonic() + timeout
    record = None

    while time.monotonic() < deadline:
        record = client.get(f"/projects/{project_id}/jobs/{job_id}").json()
        if record["status"] in jobs.FINISHED_STATUSES:
            return record
        time.sleep(0.05)

    raise AssertionError("job did not finish: %s" % (record or {}).get("status"))


# --- synthetic footage -------------------------------------------------------


def make_clip(
    path: Path,
    *,
    seconds: int = 8,
    width: int = 320,
    height: int = 180,
    audio: bool = True,
    tone: int = 1000,
) -> Path:
    """Alternating two seconds of tone and two seconds of silence.

    Small on purpose: a few seconds of 320×180 test pattern renders in well
    under a second, which is what makes a real end-to-end check affordable in
    a test suite.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        "ffmpeg.exe", "-y", "-v", "error",
        "-f", "lavfi",
        "-i", "testsrc2=size=%dx%d:rate=25:duration=%d" % (width, height, seconds),
    ]

    if audio:
        command += [
            "-f", "lavfi",
            "-i", "sine=frequency=%d:sample_rate=48000:duration=%d" % (tone, seconds),
            # Loud for the first two seconds of every four, silent for the rest.
            "-af", "volume='if(lt(mod(t,4),2),1,0)':eval=frame",
            "-c:a", "aac", "-b:a", "128k",
        ]

    command += [
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-shortest", str(path),
    ]

    subprocess.run(command, check=True, capture_output=True)
    return path


def add_source(client, project_id, path: Path) -> str:
    response = client.post(
        f"/projects/{project_id}/sources", json={"path": str(path)}
    )
    assert response.status_code == 201, response.text
    return response.json()["sources"][-1]["id"]


def start_run(client, project_id, source_ids, **overrides):
    body = {
        "source_ids": source_ids,
        "settings": overrides.pop("settings", None),
        "output_mode": overrides.pop("output_mode", cutting.MODE_BOTH),
    }
    return client.post(f"/projects/{project_id}/cutting/runs", json=body)


# =============================================================================
# Parameter validation
# =============================================================================


def test_defaults_match_the_legacy_script():
    """The BAT's settings are the starting point, and must not drift silently."""
    assert cutting.default_settings() == {
        "audio_threshold": 0.04,
        "margin_before_seconds": 0.0,
        "margin_after_seconds": 0.5,
        "min_silence_seconds": 0.1,
        "min_speech_seconds": 0.6,
    }


def test_missing_settings_fall_back_to_defaults():
    assert cutting.validate_settings({}) == cutting.default_settings()
    assert cutting.validate_settings(None) == cutting.default_settings()
    assert cutting.validate_settings({"audio_threshold": None})["audio_threshold"] == 0.04


def test_settings_accept_values_inside_the_declared_range():
    validated = cutting.validate_settings(
        {
            "audio_threshold": 0.12,
            "margin_before_seconds": 0.25,
            "margin_after_seconds": 1.5,
            "min_silence_seconds": 0.4,
            "min_speech_seconds": 2,
        }
    )
    assert validated["audio_threshold"] == 0.12
    assert validated["min_speech_seconds"] == 2.0


@pytest.mark.parametrize(
    "payload",
    [
        {"audio_threshold": 1.5},
        {"audio_threshold": -0.1},
        {"margin_after_seconds": 99},
        {"min_silence_seconds": -1},
        {"min_speech_seconds": 31},
    ],
)
def test_out_of_range_settings_are_refused(payload):
    with pytest.raises(cutting.CuttingError) as caught:
        cutting.validate_settings(payload)
    assert caught.value.message


@pytest.mark.parametrize("value", ["0.04", True, [0.04], {"v": 1}, None])
def test_non_numeric_settings_are_refused(value):
    if value is None:
        # None means "use the default" and is handled above.
        return
    with pytest.raises(cutting.CuttingError):
        cutting.validate_settings({"audio_threshold": value})


def test_unknown_setting_names_are_refused():
    """No pass-through: an unrecognised key is a mistake, not an extension."""
    with pytest.raises(cutting.CuttingError) as caught:
        cutting.validate_settings({"threshold": 0.04})
    assert "threshold" in caught.value.message


def test_forbidden_looking_keys_are_refused_like_any_other_unknown_key():
    for name in ("command", "path", "executable", "args"):
        with pytest.raises(cutting.CuttingError):
            cutting.validate_settings({name: 1})


def test_output_mode_validation():
    assert cutting.validate_output_mode(None) == cutting.DEFAULT_OUTPUT_MODE
    assert cutting.validate_output_mode("clips") == "clips"
    with pytest.raises(cutting.CuttingError):
        cutting.validate_output_mode("everything")
    with pytest.raises(cutting.CuttingError):
        cutting.validate_output_mode(3)


def test_settings_catalog_describes_every_parameter_with_units():
    catalog = cutting.settings_catalog()
    names = {parameter["name"] for parameter in catalog["parameters"]}

    assert names == set(cutting.SETTINGS_SPEC)
    for parameter in catalog["parameters"]:
        assert parameter["unit"]
        assert parameter["description"]
        assert parameter["label"]
        assert parameter["min"] <= parameter["default"] <= parameter["max"]

    assert {mode["id"] for mode in catalog["output_modes"]} == set(cutting.OUTPUT_MODES)


# =============================================================================
# Command construction
# =============================================================================


def test_cut_command_matches_the_verified_auto_editor_flags():
    command = cutting.build_cut_command(
        r"C:\Videos\take 1.mkv",
        r"C:\out\0001_take 1_trimmed.mp4",
        cutting.default_settings(),
    )

    assert command == [
        "auto-editor.exe",
        r"C:\Videos\take 1.mkv",
        "--edit", "audio:threshold=0.04",
        "--margin", "0s,0.5s",
        "--smooth", "0.1s,0.6s",
        "--progress", "machine",
        "--faststart",
        "-o", r"C:\out\0001_take 1_trimmed.mp4",
    ]


def test_cut_command_keeps_paths_as_single_arguments():
    """Spaces, quotes, ampersands and Hebrew are data, never shell syntax."""
    awkward = r"C:\וידאו\take & 1 'final'.mkv"
    command = cutting.build_cut_command(awkward, r"C:\out\פלט.mp4", cutting.default_settings())

    assert awkward in command
    assert r"C:\out\פלט.mp4" in command
    # Nothing was quoted, escaped or merged: the list is handed to the OS as is.
    assert command.count(awkward) == 1


def test_number_formatting_is_stable_and_unit_suffixed():
    assert cutting.format_seconds(0) == "0s"
    assert cutting.format_seconds(0.5) == "0.5s"
    assert cutting.format_seconds(0.10) == "0.1s"
    assert cutting.format_seconds(2) == "2s"
    assert cutting.format_threshold(0.04) == "0.04"
    assert cutting.format_threshold(0) == "0"
    assert cutting.format_threshold(0.125) == "0.125"


def test_settings_reach_the_command_unchanged():
    command = cutting.build_cut_command(
        "in.mkv",
        "out.mp4",
        cutting.validate_settings(
            {
                "audio_threshold": 0.075,
                "margin_before_seconds": 0.2,
                "margin_after_seconds": 1.0,
                "min_silence_seconds": 0.3,
                "min_speech_seconds": 1.25,
            }
        ),
    )

    assert "audio:threshold=0.075" in command
    assert "0.2s,1s" in command
    assert "0.3s,1.25s" in command


def test_concat_list_escaping():
    assert cutting.concat_list_entry(r"C:\a\b.mp4") == "file 'C:\\a\\b.mp4'\n"
    # A single quote in a filename would otherwise close the demuxer's quoting.
    assert cutting.concat_list_entry("C:\\a\\it's.mp4") == "file 'C:\\a\\it'\\''s.mp4'\n"


def test_concat_copy_command_shape():
    command = cutting.build_concat_copy_command("list.txt", "out.mp4")
    assert command[0] == "ffmpeg.exe"
    assert "-f" in command and "concat" in command
    assert command[command.index("-c") + 1] == "copy"
    assert command[-1] == "out.mp4"


def test_concat_filter_command_normalises_every_input():
    command = cutting.build_concat_filter_command(["a.mp4", "b.mp4"], "out.mp4", 25.0)
    graph = command[command.index("-filter_complex") + 1]

    assert command.count("-i") == 2
    assert "[0:v:0]fps=25" in graph
    assert "[1:v:0]fps=25" in graph
    assert "aresample=48000" in graph
    assert "concat=n=2:v=1:a=1[v][a]" in graph
    # Dimensions are never scaled here: mixed dimensions are refused instead.
    assert "scale=" not in graph


def test_progress_parsing():
    assert cutting.parse_machine_progress("(mp4) h264+aac~55.0~178.0~0.24") == pytest.approx(
        55.0 / 178.0
    )
    assert cutting.parse_machine_progress("no progress here") is None
    # The last reading in a chunk wins.
    assert cutting.parse_machine_progress("~1~10~0~5~10~0") == pytest.approx(0.5)

    assert cutting.parse_ffmpeg_progress("out_time_us=5000000", 10.0) == pytest.approx(0.5)
    assert cutting.parse_ffmpeg_progress("out_time_us=5000000", 0) is None
    assert cutting.parse_ffmpeg_progress("frame=12", 10.0) is None


def test_empty_timeline_detection():
    assert cutting.is_empty_timeline("Error! Timeline is empty, nothing to do.")
    assert not cutting.is_empty_timeline("Finished. took 0.81 seconds")


def test_clip_names_are_deterministic_and_ordered():
    assert cutting.clip_file_name(1, "take 1.mkv") == "0001_take 1_trimmed.mp4"
    assert cutting.clip_file_name(12, "צילום ב.mov") == "0012_צילום ב_trimmed.mp4"


def test_run_ids_are_short_enough_to_leave_path_budget():
    run_id = cutting.new_run_id()
    assert len(run_id) == cutting.RUN_ID_LENGTH
    assert cutting.validate_run_id(run_id) == run_id
    # Ids written by an earlier version are still readable.
    assert cutting.validate_run_id("a" * 32) == "a" * 32
    with pytest.raises(cutting.RunNotFound):
        cutting.validate_run_id("short")


@pytest.mark.skipif(os.name != "nt", reason="the 260-character limit is Windows'")
def test_an_over_long_output_path_is_refused_before_anything_renders():
    """Auto-Editor exits zero and writes nothing past MAX_PATH. Say so first."""
    deep = Path("C:/") / ("d" * 230)
    sources = [{"filename": "take 1.mkv"}]

    with pytest.raises(cutting.CuttingError) as caught:
        cutting.check_path_budget(deep, sources)

    assert "VIDEO_FACTORY_WORKSPACE" in caught.value.message
    assert str(cutting.WINDOWS_MAX_PATH) in caught.value.message

    # A sane workspace is not affected.
    cutting.check_path_budget(Path("C:/vf/projects/abc/intermediates/cuts/x"), sources)


def test_safe_stem_keeps_hebrew_and_replaces_only_forbidden_characters():
    assert cutting.safe_stem("צילום ג.mov", "clip") == "צילום ג"
    assert cutting.safe_stem("take 1.mkv", "clip") == "take 1"
    assert cutting.safe_stem('a:b*c?.mp4', "clip") == "a_b_c_"
    assert cutting.safe_stem("...", "clip") == "clip"
    assert len(cutting.safe_stem("x" * 200 + ".mp4", "clip")) <= 60


# =============================================================================
# Input selection and ordering
# =============================================================================


@needs_tools
def test_sources_are_processed_in_the_submitted_order(client, project, tmp_path):
    first = add_source(client, project["id"], make_clip(tmp_path / "a.mp4", seconds=4))
    second = add_source(client, project["id"], make_clip(tmp_path / "b.mp4", seconds=4))

    # Submitted in reverse of the project order.
    job_input = cutting.build_job_input(
        project["id"], {"source_ids": [second, first], "settings": None}
    )

    assert [entry["source_id"] for entry in job_input["sources"]] == [second, first]
    assert [entry["order"] for entry in job_input["sources"]] == [1, 2]
    assert [entry["filename"] for entry in job_input["sources"]] == ["b.mp4", "a.mp4"]


@needs_tools
def test_unknown_duplicate_and_empty_selections_are_refused(client, project, tmp_path):
    source = add_source(client, project["id"], make_clip(tmp_path / "a.mp4", seconds=4))

    for selection in ([], ["nope"], [source, source], "not-a-list"):
        with pytest.raises(cutting.CuttingError):
            cutting.build_job_input(project["id"], {"source_ids": selection})


@needs_tools
def test_a_source_without_audio_is_refused_with_an_actionable_message(
    client, project, tmp_path
):
    """Audio-driven cutting cannot work on silence; say so before rendering."""
    silent = make_clip(tmp_path / "silent.mp4", seconds=4, audio=False)
    source = add_source(client, project["id"], silent)

    response = start_run(client, project["id"], [source])

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "silent.mp4" in detail
    assert "audio track" in detail


@needs_tools
def test_a_missing_file_is_refused_before_the_job_is_queued(client, project, tmp_path):
    clip = make_clip(tmp_path / "gone.mp4", seconds=4)
    source = add_source(client, project["id"], clip)
    os.remove(clip)

    response = start_run(client, project["id"], [source])

    assert response.status_code == 400
    assert "gone.mp4" in response.json()["detail"]
    assert client.get(f"/projects/{project['id']}/jobs").json()["jobs"] == []


def test_an_unreadable_file_is_refused(client, project, tmp_path):
    """A renamed non-video passes the extension check and must fail the probe."""
    fake = tmp_path / "fake.mp4"
    fake.write_bytes(b"this is not a video")
    source = add_source(client, project["id"], fake)

    response = start_run(client, project["id"], [source])

    assert response.status_code == 400
    assert response.json()["detail"]


# =============================================================================
# Settings persistence
# =============================================================================


@needs_tools
def test_settings_are_persisted_with_the_project_and_survive_a_reload(
    client, project, tmp_path
):
    source = add_source(client, project["id"], make_clip(tmp_path / "a.mp4", seconds=4))

    saved = client.put(
        f"/projects/{project['id']}/cutting/settings",
        json={
            "settings": {"audio_threshold": 0.08, "margin_after_seconds": 1.0},
            "output_mode": "clips",
            "source_ids": [source],
        },
    )
    assert saved.status_code == 200, saved.text

    # A fresh client is a fresh read from disk: this is the restart case.
    with TestClient(app) as reopened:
        loaded = reopened.get(f"/projects/{project['id']}/cutting/settings").json()

    assert loaded["settings"]["audio_threshold"] == 0.08
    assert loaded["settings"]["margin_after_seconds"] == 1.0
    # Unspecified parameters come back as their documented defaults.
    assert loaded["settings"]["min_speech_seconds"] == 0.6
    assert loaded["output_mode"] == "clips"
    assert loaded["source_ids"] == [source]
    assert loaded["catalog"]["defaults"] == cutting.default_settings()


def test_invalid_settings_are_refused_and_nothing_is_written(client, project):
    before = client.get(f"/projects/{project['id']}").json()["settings"]

    response = client.put(
        f"/projects/{project['id']}/cutting/settings",
        json={"settings": {"audio_threshold": 9}, "output_mode": "both"},
    )

    assert response.status_code == 400
    assert client.get(f"/projects/{project['id']}").json()["settings"] == before


def test_corrupt_stored_settings_fall_back_to_defaults_without_breaking_the_project():
    project = {"sources": [], "settings": {"cutting": {"settings": {"audio_threshold": 42}}}}
    loaded = cutting.read_settings(project)

    assert loaded["settings"] == cutting.default_settings()
    assert loaded["output_mode"] == cutting.DEFAULT_OUTPUT_MODE


def test_saved_source_ids_that_no_longer_exist_are_dropped_on_read():
    project = {
        "sources": [{"id": "keep"}],
        "settings": {"cutting": {"source_ids": ["keep", "removed"]}},
    }
    assert cutting.read_settings(project)["source_ids"] == ["keep"]


def test_cutting_settings_do_not_invalidate_existing_plans():
    """A plan carries its own parameters; the manual form is not plan input."""
    from backend import resources

    base = {"id": "p", "sources": [], "settings": {}}
    with_cutting = {
        "id": "p",
        "sources": [],
        "settings": {"cutting": {"settings": {"audio_threshold": 0.9}}},
    }

    assert resources.fingerprint(base) == resources.fingerprint(with_cutting)


# =============================================================================
# The job path, with the subprocess layer stubbed
# =============================================================================


class FakeProcess:
    """Stands in for `processes.run` so failures can be provoked on demand."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, cancelled=None, on_output=None, **kwargs):
        self.calls.append(list(argv))
        return self.behaviour(self, argv, cancelled, on_output)


def stub_success(output_bytes=b"stub"):
    def behaviour(fake, argv, cancelled, on_output):
        if on_output is not None:
            on_output("~1~2~0")
        _write_stub_output(argv, output_bytes)
        return processes.ProcessResult(0, "Finished.", False)

    return behaviour


def _write_stub_output(argv, payload: bytes) -> None:
    target = argv[-1] if argv[0].startswith("ffmpeg") else argv[argv.index("-o") + 1]
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


# Every stubbed clip is 6 seconds long, so a join of two must be 12 — the
# length check in the runner is real and has to be satisfied honestly.
STUB_CLIP_SECONDS = 6.0


def stub_verify_output(path, require_audio=True, clips=2):
    combined = cutting.COMBINED_DIRECTORY in str(path).replace("\\", "/")
    return {
        "filename": os.path.basename(path),
        "duration_seconds": STUB_CLIP_SECONDS * (clips if combined else 1),
        "has_video": True,
        "has_audio": True,
        "video": {"codec_name": "h264", "width": 640, "height": 360,
                  "pix_fmt": "yuv420p", "frame_rate": 25.0},
        "audio": {"codec_name": "aac", "sample_rate": 48000, "channels": 2},
    }


@pytest.fixture
def stub_tools(monkeypatch):
    """Replace the tool layer entirely: no encoder runs in these tests."""
    monkeypatch.setattr(
        cutting, "collect_tool_versions", lambda: {"auto_editor": "stub", "ffmpeg": "stub"}
    )
    monkeypatch.setattr(
        media,
        "describe_input",
        lambda path, require_audio=True: {
            "path": path,
            "filename": os.path.basename(path),
            "duration_seconds": 10.0,
            "has_video": True,
            "has_audio": True,
            "video": {"codec_name": "h264", "width": 640, "height": 360,
                      "pix_fmt": "yuv420p", "frame_rate": 25.0},
            "audio": {"codec_name": "aac", "sample_rate": 48000, "channels": 2},
        },
    )
    monkeypatch.setattr(media, "verify_output", stub_verify_output)
    monkeypatch.setattr(
        media,
        "fingerprint",
        lambda path: {"method": "stub", "digest": "sha256:stub",
                      "size_bytes": 1, "modified_ns": 1},
    )


@pytest.fixture
def stub_sources(client, project, tmp_path, stub_tools):
    ids = []
    for name in ("take 1.mkv", "צילום 2.mov"):
        path = tmp_path / name
        path.write_bytes(b"placeholder")
        ids.append(add_source(client, project["id"], path))
    return ids


def test_a_failing_tool_fails_the_run_and_keeps_the_diagnostics(
    client, project, stub_sources, monkeypatch
):
    def behaviour(fake, argv, cancelled, on_output):
        return processes.ProcessResult(1, "Error! something went wrong\n", False)

    monkeypatch.setattr(processes, "run", FakeProcess(behaviour))

    response = start_run(client, project["id"], stub_sources)
    record = wait_for_job(client, project["id"], response.json()["id"])

    assert record["status"] == jobs.FAILED
    assert "something went wrong" in record["error"]

    runs = client.get(f"/projects/{project['id']}/cutting/runs").json()["runs"]
    assert len(runs) == 1
    run = runs[0]

    assert run["status"] == cutting.RUN_FAILED
    assert run["is_complete_result"] is False
    assert run["clips"][0]["status"] == cutting.CLIP_FAILED
    assert run["clips"][0]["exit_code"] == 1
    # The clip that never started is recorded rather than quietly absent.
    assert run["clips"][1]["status"] == cutting.CLIP_SKIPPED
    assert run["combined"] is None


def test_a_missing_executable_is_reported_as_a_readable_error(
    client, project, stub_sources, monkeypatch
):
    def behaviour(fake, argv, cancelled, on_output):
        raise processes.ProcessStartFailed('Tool "auto-editor.exe" was not found on PATH.')

    monkeypatch.setattr(processes, "run", FakeProcess(behaviour))

    response = start_run(client, project["id"], stub_sources)
    record = wait_for_job(client, project["id"], response.json()["id"])

    assert record["status"] == jobs.FAILED
    assert "auto-editor.exe" in record["error"]


def test_an_empty_timeline_names_the_clip_instead_of_dropping_it(
    client, project, stub_sources, monkeypatch
):
    def behaviour(fake, argv, cancelled, on_output):
        if argv[0].startswith("auto-editor") and "take 1.mkv" in " ".join(argv):
            return processes.ProcessResult(
                2, "Error! Timeline is empty, nothing to do.", False
            )
        _write_stub_output(argv, b"stub")
        return processes.ProcessResult(0, "Finished.", False)

    monkeypatch.setattr(processes, "run", FakeProcess(behaviour))
    # Only one clip survives, so the join is one clip long.
    monkeypatch.setattr(
        media,
        "verify_output",
        lambda path, require_audio=True: stub_verify_output(path, clips=1),
    )

    response = start_run(client, project["id"], stub_sources)
    record = wait_for_job(client, project["id"], response.json()["id"])

    assert record["status"] == jobs.SUCCEEDED, record.get("error")

    run = client.get(f"/projects/{project['id']}/cutting/runs").json()["runs"][0]
    empty = run["clips"][0]

    assert empty["status"] == cutting.CLIP_EMPTY
    assert "take 1.mkv" in empty["error"]
    # The combined video knows it is not everything the user selected.
    assert run["combined"]["complete"] is False
    assert run["combined"]["excluded_clips"][0]["source_filename"] == "take 1.mkv"
    assert record["result"]["empty_clip_count"] == 1
    assert "yielded no content" in record["result"]["summary"]


def test_every_clip_empty_fails_the_run(client, project, stub_sources, monkeypatch):
    def behaviour(fake, argv, cancelled, on_output):
        return processes.ProcessResult(2, "Error! Timeline is empty, nothing to do.", False)

    monkeypatch.setattr(processes, "run", FakeProcess(behaviour))

    response = start_run(client, project["id"], stub_sources)
    record = wait_for_job(client, project["id"], response.json()["id"])

    assert record["status"] == jobs.FAILED
    assert "audio threshold" in record["error"]


def test_mixed_dimensions_are_explained_rather_than_stretched(
    client, project, stub_sources, monkeypatch
):
    """FFmpeg would copy these happily and produce a broken picture."""
    sizes = iter([(640, 360), (360, 640)])

    def verify(path, require_audio=True):
        described = stub_verify_output(path)
        width, height = next(sizes)
        described["video"]["width"] = width
        described["video"]["height"] = height
        return described

    monkeypatch.setattr(media, "verify_output", verify)
    monkeypatch.setattr(processes, "run", FakeProcess(stub_success()))

    response = start_run(client, project["id"], stub_sources)
    record = wait_for_job(client, project["id"], response.json()["id"])

    assert record["status"] == jobs.FAILED
    assert "different dimensions" in record["error"]

    run = client.get(f"/projects/{project['id']}/cutting/runs").json()["runs"][0]
    assert run["combined"]["strategy"] == "unsupported_mixed_dimensions"
    # The clips themselves were produced and are still there.
    assert all(clip["status"] == cutting.CLIP_SUCCEEDED for clip in run["clips"])


def test_identical_streams_are_joined_by_copy_and_differing_ones_re_encoded(
    client, project, stub_sources, monkeypatch
):
    fake = FakeProcess(stub_success())
    monkeypatch.setattr(processes, "run", fake)

    response = start_run(client, project["id"], stub_sources)
    wait_for_job(client, project["id"], response.json()["id"])

    run = client.get(f"/projects/{project['id']}/cutting/runs").json()["runs"][0]
    assert run["combined"]["strategy"] == cutting.COMBINE_STREAM_COPY

    join = [call for call in fake.calls if call[0].startswith("ffmpeg")][-1]
    assert "copy" in join

    # Now make the two clips differ in sample rate only.
    rates = iter([48000, 44100])

    def verify(path, require_audio=True):
        described = stub_verify_output(path)
        described["audio"]["sample_rate"] = next(rates, 48000)
        return described

    monkeypatch.setattr(media, "verify_output", verify)
    fake2 = FakeProcess(stub_success())
    monkeypatch.setattr(processes, "run", fake2)

    response = start_run(client, project["id"], stub_sources)
    wait_for_job(client, project["id"], response.json()["id"])

    runs = client.get(f"/projects/{project['id']}/cutting/runs").json()["runs"]
    assert runs[0]["combined"]["strategy"] == cutting.COMBINE_REENCODE

    join = [call for call in fake2.calls if call[0].startswith("ffmpeg")][-1]
    assert "-filter_complex" in join


def test_a_join_of_the_wrong_length_is_not_accepted(
    client, project, stub_sources, monkeypatch
):
    """The exit code says fine; the duration says otherwise. Duration wins."""
    def verify(path, require_audio=True):
        combined = "combined" in path
        return {
            "filename": os.path.basename(path),
            # Each clip is 6s, so the join should be 12s — not 40s.
            "duration_seconds": 40.0 if combined else 6.0,
            "has_video": True,
            "has_audio": True,
            "video": {"codec_name": "h264", "width": 640, "height": 360,
                      "pix_fmt": "yuv420p", "frame_rate": 25.0},
            "audio": {"codec_name": "aac", "sample_rate": 48000, "channels": 2},
        }

    monkeypatch.setattr(media, "verify_output", verify)
    fake = FakeProcess(stub_success())
    monkeypatch.setattr(processes, "run", fake)

    response = start_run(client, project["id"], stub_sources)
    record = wait_for_job(client, project["id"], response.json()["id"])

    assert record["status"] == jobs.FAILED
    assert "does not match" in record["error"]
    # It tried the copy, then fell back to re-encoding before giving up.
    joins = [call for call in fake.calls if call[0].startswith("ffmpeg")]
    assert len(joins) == 2


def test_clips_only_mode_never_runs_ffmpeg(client, project, stub_sources, monkeypatch):
    fake = FakeProcess(stub_success())
    monkeypatch.setattr(processes, "run", fake)

    response = start_run(
        client, project["id"], stub_sources, output_mode=cutting.MODE_CLIPS
    )
    record = wait_for_job(client, project["id"], response.json()["id"])

    assert record["status"] == jobs.SUCCEEDED
    assert all(call[0].startswith("auto-editor") for call in fake.calls)

    run = client.get(f"/projects/{project['id']}/cutting/runs").json()["runs"][0]
    assert run["combined"] is None


# =============================================================================
# Cancellation
# =============================================================================


def test_cancellation_stops_the_remaining_clips_and_the_join(
    client, project, stub_sources, monkeypatch
):
    """Cancel during the first clip: nothing after it may start."""
    state = {"cancelled": False}

    def behaviour(fake, argv, cancelled, on_output):
        if len(fake.calls) == 1:
            # The first clip notices the flag and reports a real kill.
            state["cancelled"] = True
            raise processes.ProcessCancelled()
        _write_stub_output(argv, b"stub")
        return processes.ProcessResult(0, "Finished.", False)

    fake = FakeProcess(behaviour)
    monkeypatch.setattr(processes, "run", fake)

    # Ask for cancellation as soon as the job exists, so the flag is set by the
    # time the handler reaches its first process.
    response = start_run(client, project["id"], stub_sources)
    job_id = response.json()["id"]
    record = wait_for_job(client, project["id"], job_id)

    assert state["cancelled"] is True
    assert record["status"] == jobs.CANCELLED
    assert record["result"] is None

    run = client.get(f"/projects/{project['id']}/cutting/runs").json()["runs"][0]

    assert run["status"] == cutting.RUN_CANCELLED
    assert run["is_complete_result"] is False
    assert run["clips"][0]["status"] == cutting.CLIP_CANCELLED
    assert run["clips"][1]["status"] == cutting.CLIP_SKIPPED
    assert run["combined"] is None
    # Exactly one process was started: the join never began.
    assert len(fake.calls) == 1


def test_a_cancelled_run_contributes_no_project_resources(
    client, project, stub_sources, monkeypatch
):
    def behaviour(fake, argv, cancelled, on_output):
        raise processes.ProcessCancelled()

    monkeypatch.setattr(processes, "run", FakeProcess(behaviour))
    response = start_run(client, project["id"], stub_sources)
    wait_for_job(client, project["id"], response.json()["id"])

    catalog = client.get(f"/projects/{project['id']}/resources").json()
    assert catalog["generated"] == []


@needs_tools
def test_cancelling_a_real_process_actually_stops_it(tmp_path):
    """Not a stub: a real encoder is started, cancelled, and must be gone.

    A long render is asked for and abandoned after a moment. The call has to
    return promptly — if the kill did not work it would block for the full
    render — and the output file must not keep growing afterwards.
    """
    target = tmp_path / "long.mp4"
    command = [
        "ffmpeg.exe", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=600",
        "-c:v", "libx264", "-preset", "veryslow", str(target),
    ]

    stop_at = time.monotonic() + 1.0
    started = time.monotonic()

    with pytest.raises(processes.ProcessCancelled):
        processes.run(command, cancelled=lambda: time.monotonic() > stop_at)

    elapsed = time.monotonic() - started
    assert elapsed < 30, "cancellation did not stop the render (%.1fs)" % elapsed

    # Nothing is still writing to the file.
    first = target.stat().st_size if target.exists() else 0
    time.sleep(1.0)
    second = target.stat().st_size if target.exists() else 0
    assert first == second


@needs_tools
def test_cancelling_a_real_cutting_job_leaves_no_completed_result(
    client, project, tmp_path
):
    """The whole path: cancel a running job and check what is recorded."""
    # Big enough that the first clip is still rendering when cancel arrives.
    clips = [
        make_clip(tmp_path / f"take{index}.mp4", seconds=40, width=1280, height=720)
        for index in range(2)
    ]
    ids = [add_source(client, project["id"], clip) for clip in clips]

    job_id = start_run(client, project["id"], ids).json()["id"]

    # Wait until it is genuinely running, then ask it to stop.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        record = client.get(f"/projects/{project['id']}/jobs/{job_id}").json()
        if record["status"] == jobs.RUNNING:
            break
        time.sleep(0.05)

    client.post(f"/projects/{project['id']}/jobs/{job_id}/cancel")
    record = wait_for_job(client, project["id"], job_id, timeout=90)

    assert record["status"] == jobs.CANCELLED
    assert record["result"] is None

    run = client.get(f"/projects/{project['id']}/cutting/runs").json()["runs"][0]
    assert run["status"] == cutting.RUN_CANCELLED
    assert run["is_complete_result"] is False
    assert run["combined"] is None
    assert not any(clip["status"] == cutting.CLIP_SUCCEEDED for clip in run["clips"][1:])

    # A cancelled run is not offered as a project resource.
    assert client.get(f"/projects/{project['id']}/resources").json()["generated"] == []

    # Retrying is a brand-new run; the cancelled record stays as it is.
    retried = client.post(f"/projects/{project['id']}/jobs/{job_id}/retry").json()
    client.post(f"/projects/{project['id']}/jobs/{retried['id']}/cancel")
    wait_for_job(client, project["id"], retried["id"], timeout=90)

    runs = client.get(f"/projects/{project['id']}/cutting/runs").json()["runs"]
    assert len({run["run_id"] for run in runs}) == len(runs)

    # The originals survived being cancelled mid-render.
    assert all(clip.exists() for clip in clips)


def test_terminate_tree_is_used_for_cancellation(monkeypatch):
    """Cancelling must kill the encoder, not just the launcher."""
    killed = []

    class Fake:
        pid = 4242

        def __init__(self):
            self.polls = 0
            self.stdout = None

        def poll(self):
            self.polls += 1
            return None if self.polls < 3 else 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(processes, "terminate_tree", lambda p: killed.append(p.pid))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Fake())
    monkeypatch.setattr(processes, "POLL_INTERVAL_SECONDS", 0.001)

    with pytest.raises(processes.ProcessCancelled):
        processes.run(["ffmpeg.exe"], cancelled=lambda: True)

    assert killed == [4242]


# =============================================================================
# Output isolation and serving
# =============================================================================


def test_each_run_gets_its_own_directory_and_leaves_the_previous_one_alone(
    client, project, stub_sources, monkeypatch
):
    monkeypatch.setattr(processes, "run", FakeProcess(stub_success(b"first-run")))
    first = wait_for_job(
        client, project["id"], start_run(client, project["id"], stub_sources).json()["id"]
    )

    monkeypatch.setattr(processes, "run", FakeProcess(stub_success(b"second-run")))
    second = wait_for_job(
        client, project["id"], start_run(client, project["id"], stub_sources).json()["id"]
    )

    assert first["status"] == jobs.SUCCEEDED
    assert second["status"] == jobs.SUCCEEDED

    first_run = first["result"]["run_id"]
    second_run = second["result"]["run_id"]
    assert first_run != second_run

    root = cutting.cuts_root(project["id"])
    assert sorted(entry.name for entry in root.iterdir()) == sorted(
        [first_run, second_run]
    )

    # The first run's bytes are exactly as it left them.
    first_clip = next((root / first_run / "clips").iterdir())
    assert first_clip.read_bytes() == b"first-run"

    runs = client.get(f"/projects/{project['id']}/cutting/runs").json()["runs"]
    assert len(runs) == 2
    assert runs[0]["run_id"] == second_run  # newest first


def test_the_run_manifest_records_everything_needed_to_trace_a_result(
    client, project, stub_sources, monkeypatch
):
    monkeypatch.setattr(processes, "run", FakeProcess(stub_success()))
    record = wait_for_job(
        client, project["id"], start_run(client, project["id"], stub_sources).json()["id"]
    )

    run_id = record["result"]["run_id"]
    manifest = json.loads(
        (cutting.run_directory(project["id"], run_id) / "manifest.json").read_text("utf-8")
    )

    assert manifest["run_id"] == run_id
    assert manifest["job_id"] == record["id"]
    assert manifest["project_id"] == project["id"]
    assert manifest["status"] == cutting.RUN_SUCCEEDED
    assert manifest["settings"] == cutting.default_settings()
    assert manifest["tool_versions"]["auto_editor"] == "stub"

    assert [source["order"] for source in manifest["sources"]] == [1, 2]
    for source in manifest["sources"]:
        assert source["source_id"]
        assert source["fingerprint"]["digest"]
        assert source["duration_seconds"] == 10.0

    for clip in manifest["clips"]:
        assert clip["status"] == cutting.CLIP_SUCCEEDED
        assert clip["duration_seconds"] == 6.0
        assert clip["removed_seconds"] == 4.0
        assert clip["command"].startswith("auto-editor.exe")
        assert clip["log_path"]

    assert manifest["combined"]["clip_output_ids"] == ["clip-0001", "clip-0002"]
    assert manifest["combined"]["complete"] is True


def test_the_job_uses_its_snapshot_even_if_the_project_changes(
    client, project, stub_sources, monkeypatch, tmp_path
):
    """A run keeps processing what was submitted, and stays identifiable as such."""
    monkeypatch.setattr(processes, "run", FakeProcess(stub_success()))

    response = start_run(client, project["id"], stub_sources)
    job_id = response.json()["id"]

    # Remove every source from the project while the job is in flight.
    client.put(
        f"/projects/{project['id']}", json={"name": "אחרי שינוי", "source_ids": []}
    )

    record = wait_for_job(client, project["id"], job_id)

    assert record["status"] == jobs.SUCCEEDED
    assert [source["source_id"] for source in record["input"]["sources"]] == stub_sources

    run = client.get(f"/projects/{project['id']}/cutting/runs").json()["runs"][0]
    assert [source["source_id"] for source in run["sources"]] == stub_sources
    assert [clip["source_filename"] for clip in run["clips"]] == [
        "take 1.mkv",
        "צילום 2.mov",
    ]


def test_retrying_creates_a_separate_run(client, project, stub_sources, monkeypatch):
    monkeypatch.setattr(processes, "run", FakeProcess(stub_success()))

    first = wait_for_job(
        client, project["id"], start_run(client, project["id"], stub_sources).json()["id"]
    )
    retried = client.post(
        f"/projects/{project['id']}/jobs/{first['id']}/retry"
    ).json()
    second = wait_for_job(client, project["id"], retried["id"])

    assert second["retry_of"] == first["id"]
    assert second["result"]["run_id"] != first["result"]["run_id"]

    runs = client.get(f"/projects/{project['id']}/cutting/runs").json()["runs"]
    assert len(runs) == 2
    # The original run is untouched by the retry.
    assert runs[1]["job_id"] == first["id"]


def test_successful_outputs_are_registered_as_generated_resources(
    client, project, stub_sources, monkeypatch
):
    monkeypatch.setattr(processes, "run", FakeProcess(stub_success()))
    record = wait_for_job(
        client, project["id"], start_run(client, project["id"], stub_sources).json()["id"]
    )

    catalog = client.get(f"/projects/{project['id']}/resources").json()
    generated = catalog["generated"]

    kinds = sorted(entry["kind"] for entry in generated)
    assert kinds == ["combined_video", "trimmed_clip", "trimmed_clip"]
    assert all(entry["produced_by"] == "edit.cut_silence" for entry in generated)
    assert all(entry["run_id"] == record["result"]["run_id"] for entry in generated)

    # Sources are unchanged: a generated clip is never added to the footage list.
    assert len(catalog["resources"]) == 2
    assert all(resource["id"] in stub_sources for resource in catalog["resources"])

    # And no local path leaked into the catalog.
    assert "C:\\" not in json.dumps(catalog, ensure_ascii=False)


def test_outputs_are_served_by_id_and_support_seeking(
    client, project, stub_sources, monkeypatch
):
    payload = b"0123456789" * 50
    monkeypatch.setattr(processes, "run", FakeProcess(stub_success(payload)))
    record = wait_for_job(
        client, project["id"], start_run(client, project["id"], stub_sources).json()["id"]
    )
    run_id = record["result"]["run_id"]
    base = f"/projects/{project['id']}/cutting/runs/{run_id}/outputs"

    whole = client.get(f"{base}/clip-0001/stream")
    assert whole.status_code == 200
    assert whole.content == payload
    assert whole.headers["accept-ranges"] == "bytes"

    # Seeking: a byte range must come back as 206 with just that slice.
    ranged = client.get(f"{base}/clip-0001/stream", headers={"Range": "bytes=10-19"})
    assert ranged.status_code == 206
    assert ranged.content == payload[10:20]

    download = client.get(f"{base}/clip-0001/download")
    assert download.status_code == 200
    assert "attachment" in download.headers["content-disposition"]


def test_a_hebrew_output_name_survives_the_download_header(
    client, project, stub_sources, monkeypatch
):
    monkeypatch.setattr(processes, "run", FakeProcess(stub_success()))
    record = wait_for_job(
        client, project["id"], start_run(client, project["id"], stub_sources).json()["id"]
    )
    run_id = record["result"]["run_id"]

    response = client.get(
        f"/projects/{project['id']}/cutting/runs/{run_id}/outputs/clip-0002/download"
    )

    assert response.status_code == 200
    # RFC 5987 form carries the Hebrew name; the ASCII fallback is still valid.
    assert "filename*=UTF-8''" in response.headers["content-disposition"]


@pytest.mark.parametrize(
    "output_id",
    ["../../../project", "clip-9999", "..%2Fmanifest.json", "manifest.json", "clip-1"],
)
def test_output_ids_that_are_not_real_outputs_are_refused(
    client, project, stub_sources, monkeypatch, output_id
):
    """There is no endpoint that serves an arbitrary path, and no way to make one."""
    monkeypatch.setattr(processes, "run", FakeProcess(stub_success()))
    record = wait_for_job(
        client, project["id"], start_run(client, project["id"], stub_sources).json()["id"]
    )
    run_id = record["result"]["run_id"]

    response = client.get(
        f"/projects/{project['id']}/cutting/runs/{run_id}/outputs/{output_id}/stream"
    )
    assert response.status_code in (404, 400)


def test_an_output_of_a_failed_clip_is_not_served(
    client, project, stub_sources, monkeypatch
):
    def behaviour(fake, argv, cancelled, on_output):
        return processes.ProcessResult(1, "Error! nope", False)

    monkeypatch.setattr(processes, "run", FakeProcess(behaviour))
    wait_for_job(
        client, project["id"], start_run(client, project["id"], stub_sources).json()["id"]
    )

    run_id = client.get(f"/projects/{project['id']}/cutting/runs").json()["runs"][0][
        "run_id"
    ]
    response = client.get(
        f"/projects/{project['id']}/cutting/runs/{run_id}/outputs/clip-0001/stream"
    )
    assert response.status_code == 404


def test_a_run_of_another_project_is_not_reachable(client, project, stub_sources, monkeypatch):
    monkeypatch.setattr(processes, "run", FakeProcess(stub_success()))
    record = wait_for_job(
        client, project["id"], start_run(client, project["id"], stub_sources).json()["id"]
    )
    run_id = record["result"]["run_id"]

    other = client.post("/projects", json={"name": "פרויקט אחר"}).json()
    response = client.get(f"/projects/{other['id']}/cutting/runs/{run_id}")

    assert response.status_code == 404


# =============================================================================
# The real thing
# =============================================================================


@needs_tools
def test_end_to_end_cut_and_join_on_synthetic_footage(client, project, tmp_path):
    """The whole path, with the real tools, on footage generated for this test.

    Twelve seconds of alternating tone and silence per take. With the default
    settings the silence must actually go, the clips must be playable MP4s of
    the source's dimensions, and the joined file must be the sum of its parts.
    """
    first = make_clip(tmp_path / "take 1.mp4", seconds=12, tone=1000)
    # A space and Hebrew in the name, and a different container, on purpose.
    second = make_clip(tmp_path / "צילום ב.mkv", seconds=8, tone=600)

    ids = [
        add_source(client, project["id"], first),
        add_source(client, project["id"], second),
    ]

    response = start_run(client, project["id"], ids, output_mode=cutting.MODE_BOTH)
    assert response.status_code == 201, response.text

    record = wait_for_job(client, project["id"], response.json()["id"])
    assert record["status"] == jobs.SUCCEEDED, record.get("error")

    run = client.get(
        f"/projects/{project['id']}/cutting/runs/{record['result']['run_id']}"
    ).json()

    assert run["status"] == cutting.RUN_SUCCEEDED
    assert run["is_complete_result"] is True
    assert len(run["clips"]) == 2

    for clip, source_seconds in zip(run["clips"], (12.0, 8.0)):
        assert clip["status"] == cutting.CLIP_SUCCEEDED
        assert clip["filename"].endswith("_trimmed.mp4")
        # Silence was genuinely removed, and content genuinely survived.
        assert 0 < clip["duration_seconds"] < source_seconds
        assert clip["removed_seconds"] > 1.0
        # Source dimensions preserved, browser-playable codecs.
        assert (clip["video"]["width"], clip["video"]["height"]) == (320, 180)
        assert clip["video"]["codec_name"] == "h264"
        assert clip["audio"]["codec_name"] == "aac"

    assert run["removed_duration_seconds"] > 2.0

    combined = run["combined"]
    assert combined["status"] == cutting.CLIP_SUCCEEDED
    assert combined["complete"] is True
    assert combined["strategy"] in (cutting.COMBINE_STREAM_COPY, cutting.COMBINE_REENCODE)
    expected = sum(clip["duration_seconds"] for clip in run["clips"])
    assert combined["duration_seconds"] == pytest.approx(expected, abs=1.0)

    # The produced files really are playable media, read back independently.
    for output_id in ("clip-0001", "clip-0002", "combined"):
        path, _entry = cutting.resolve_output(
            project["id"], run["run_id"], output_id
        )
        described = media.probe(str(path))
        assert described["has_video"] and described["has_audio"]

        served = client.get(
            f"/projects/{project['id']}/cutting/runs/{run['run_id']}"
            f"/outputs/{output_id}/stream",
            headers={"Range": "bytes=0-1023"},
        )
        assert served.status_code == 206
        assert len(served.content) == 1024

    # The originals are exactly as they were.
    assert first.exists() and second.exists()


@needs_tools
def test_end_to_end_mixed_dimensions_reports_the_limitation(client, project, tmp_path):
    """The normalisation path this milestone does not implement, stated clearly."""
    landscape = make_clip(tmp_path / "landscape.mp4", seconds=8, width=320, height=180)
    portrait = make_clip(tmp_path / "portrait.mp4", seconds=8, width=180, height=320)

    ids = [
        add_source(client, project["id"], landscape),
        add_source(client, project["id"], portrait),
    ]

    record = wait_for_job(
        client,
        project["id"],
        start_run(client, project["id"], ids, output_mode=cutting.MODE_BOTH)
        .json()["id"],
    )

    assert record["status"] == jobs.FAILED
    assert "different dimensions" in record["error"]
    assert "320×180" in record["error"] and "180×320" in record["error"]

    # Clips-only is the documented way forward, and it works.
    record = wait_for_job(
        client,
        project["id"],
        start_run(client, project["id"], ids, output_mode=cutting.MODE_CLIPS)
        .json()["id"],
    )
    assert record["status"] == jobs.SUCCEEDED


@needs_tools
def test_end_to_end_settings_change_the_result(client, project, tmp_path):
    """Proof that the five parameters reach the tool and matter."""
    clip = make_clip(tmp_path / "take.mp4", seconds=12)
    source = add_source(client, project["id"], clip)

    def duration_with(settings):
        record = wait_for_job(
            client,
            project["id"],
            start_run(
                client,
                project["id"],
                [source],
                settings=settings,
                output_mode=cutting.MODE_CLIPS,
            ).json()["id"],
        )
        assert record["status"] == jobs.SUCCEEDED, record.get("error")
        run = client.get(
            f"/projects/{project['id']}/cutting/runs/{record['result']['run_id']}"
        ).json()
        return run["clips"][0]["duration_seconds"]

    tight = duration_with({"margin_after_seconds": 0.0})
    generous = duration_with({"margin_after_seconds": 1.0})

    # A bigger trailing margin keeps more of the take. Nothing else changed.
    assert generous > tight


@needs_tools
def test_end_to_end_everything_cut_away_is_reported_per_clip(client, project, tmp_path):
    """Settings that discard the whole take: named and explained, not silent."""
    clip = make_clip(tmp_path / "take.mp4", seconds=8)
    source = add_source(client, project["id"], clip)

    record = wait_for_job(
        client,
        project["id"],
        start_run(
            client,
            project["id"],
            [source],
            # Every speech segment is 2s, so requiring 20s discards them all.
            settings={"min_speech_seconds": 20.0},
            output_mode=cutting.MODE_CLIPS,
        ).json()["id"],
    )

    assert record["status"] == jobs.FAILED
    assert "silence" in record["error"]

    run = client.get(f"/projects/{project['id']}/cutting/runs").json()["runs"][0]
    assert run["clips"][0]["status"] == cutting.CLIP_EMPTY
    assert "take.mp4" in run["clips"][0]["error"]


@needs_tools
def test_media_probe_rejects_what_it_cannot_use(tmp_path):
    silent = make_clip(tmp_path / "silent.mp4", seconds=3, audio=False)

    with pytest.raises(media.MediaError) as caught:
        media.describe_input(str(silent), require_audio=True)
    assert "audio track" in caught.value.message

    # The same file is fine when audio is not required.
    assert media.describe_input(str(silent), require_audio=False)["has_video"]

    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"nonsense")
    with pytest.raises(media.MediaError):
        media.describe_input(str(broken))

    with pytest.raises(media.MediaError):
        media.describe_input(str(tmp_path / "missing.mp4"))


@needs_tools
def test_fingerprints_distinguish_recordings(tmp_path):
    first = make_clip(tmp_path / "a.mp4", seconds=3, tone=440)
    second = make_clip(tmp_path / "b.mp4", seconds=3, tone=880)
    copied = tmp_path / "copy.mp4"
    shutil.copyfile(first, copied)

    assert media.fingerprint(str(first))["digest"] == media.fingerprint(str(copied))["digest"]
    assert media.fingerprint(str(first))["digest"] != media.fingerprint(str(second))["digest"]
