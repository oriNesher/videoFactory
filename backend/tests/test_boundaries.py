"""Focused tests for milestone 1B: boundary-only trimming, previews, AI advice.

Layered like the 1A tests:

- **Pure** — detection on synthetic envelopes, padding and clamping, manual
  overrides, window expansion, settings migration, staleness keys, cut maps and
  proposal validation. No tool runs.
- **Plumbing** — cancellation of each job with the tool layer stubbed.
- **Real** — FFmpeg on a few seconds of generated test pattern: leading silence,
  two audible sections separated by a pause, trailing silence. These verify the
  mechanism. They say nothing about how a real voice sounds at the cut; that
  is the manual listening checklist in the README.
"""

import json
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import (  # noqa: E402
    boundaries,
    capabilities,
    cut_advice,
    cut_map,
    cut_samples,
    cutting,
    jobs,
    llm,
    media,
    processes,
)
from backend.config import (  # noqa: E402
    API_KEY_ENV_VAR,
    PROVIDER_ENV_VAR,
    WORKSPACE_ENV_VAR,
)
from backend.main import app  # noqa: E402

TOOLS_PRESENT = all(shutil.which(name) for name in ("ffmpeg.exe", "ffprobe.exe"))
AUTO_EDITOR_PRESENT = bool(shutil.which("auto-editor.exe"))

needs_tools = pytest.mark.skipif(not TOOLS_PRESENT, reason="FFmpeg / FFprobe are not on PATH")
needs_auto_editor = pytest.mark.skipif(
    not (TOOLS_PRESENT and AUTO_EDITOR_PRESENT), reason="Auto-Editor is not on PATH"
)

FRAME = boundaries.FRAME_SECONDS
DEFAULTS = boundaries.default_settings()

# The fixture every end-to-end test uses: two seconds of silence, sound, a
# one-and-a-half-second pause, sound again, two and a half seconds of silence.
AUDIBLE = [(2.0, 4.0), (5.5, 7.5)]
FIXTURE_SECONDS = 10


@pytest.fixture(autouse=True)
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv(WORKSPACE_ENV_VAR, str(tmp_path / "workspace"))
    monkeypatch.setenv(PROVIDER_ENV_VAR, "mock")
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)


@pytest.fixture
def client():
    with TestClient(app) as running:
        yield running


@pytest.fixture
def project(client):
    return client.post("/projects", json={"name": "חיתוך קצוות"}).json()


def wait_for_job(client, project_id, job_id, timeout=180.0):
    deadline = time.monotonic() + timeout
    record = None
    while time.monotonic() < deadline:
        record = client.get(f"/projects/{project_id}/jobs/{job_id}").json()
        if record["status"] in jobs.FINISHED_STATUSES:
            return record
        time.sleep(0.05)
    raise AssertionError("job did not finish: %s" % (record or {}).get("status"))


def wait_until_running(client, project_id, job_id, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = client.get(f"/projects/{project_id}/jobs/{job_id}").json()
        if record["status"] != jobs.QUEUED:
            return record
        time.sleep(0.02)
    raise AssertionError("job never started")


def submit(client, project_id, endpoint, body):
    response = client.post(f"/projects/{project_id}/cutting/{endpoint}", json=body)
    assert response.status_code == 201, response.text
    return wait_for_job(client, project_id, response.json()["id"])


def state(client, project_id, body):
    response = client.post(f"/projects/{project_id}/cutting/state", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def add_source(client, project_id, path: Path) -> str:
    response = client.post(f"/projects/{project_id}/sources", json={"path": str(path)})
    assert response.status_code == 201, response.text
    return response.json()["sources"][-1]["id"]


# --- synthetic material -------------------------------------------------------


def make_envelope(seconds: float, audible: list[tuple], level=0.2, floor=0.0) -> list[float]:
    frames = int(round(seconds / FRAME))
    envelope = [floor] * frames
    for start, end in audible:
        for index in range(int(round(start / FRAME)), min(frames, int(round(end / FRAME)))):
            envelope[index] = level
    return envelope


class FakeDecoder:
    """Serves slices of a synthetic envelope and records what was asked for."""

    def __init__(self, envelope: list[float]):
        self.envelope = envelope
        self.requests: list[tuple] = []

    def __call__(self, path, start, length, cancelled):
        self.requests.append((round(start, 3), round(length, 3)))
        first = int(round(start / FRAME))
        return self.envelope[first : first + int(round(length / FRAME))]

    @property
    def decoded(self) -> float:
        return sum(length for _start, length in self.requests)


def detect(envelope: list[float], settings=None, **options) -> tuple[dict, FakeDecoder]:
    decoder = FakeDecoder(envelope)
    detection = boundaries.detect(
        "unused", len(envelope) * FRAME, True, settings or DEFAULTS,
        decoder=decoder, **options,
    )
    return detection, decoder


def make_take(
    path: Path,
    *,
    seconds: int = FIXTURE_SECONDS,
    audible=AUDIBLE,
    audio: bool = True,
    floor: float = 0.0,
    tone: int = 1000,
    width: int = 320,
    height: int = 180,
) -> Path:
    """A test-pattern clip whose tone is audible only in the given intervals.

    `floor` is the level outside them, as a share of the tone: 0 is digital
    silence, 0.2 is a loud room.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg.exe", "-y", "-v", "error",
        "-f", "lavfi",
        "-i", "testsrc2=size=%dx%d:rate=25:duration=%d" % (width, height, seconds),
    ]
    if audio:
        gate = "+".join("between(t,%s,%s)" % interval for interval in audible) or "0"
        command += [
            "-f", "lavfi",
            "-i", "sine=frequency=%d:sample_rate=48000:duration=%d" % (tone, seconds),
            "-af", "volume='if(%s,1,%s)':eval=frame" % (gate, floor),
            "-c:a", "aac", "-b:a", "128k",
        ]
    command += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-shortest", str(path)]
    subprocess.run(command, check=True, capture_output=True)
    return path


# =============================================================================
# Detection: one continuous interval
# =============================================================================


def test_leading_and_trailing_silence_go_and_the_internal_pause_stays():
    detection, _ = detect(make_envelope(10, AUDIBLE))

    assert detection["status"] == boundaries.STATUS_DETECTED
    assert detection["activity_start_seconds"] == pytest.approx(2.0, abs=FRAME)
    # The end of the *last* section: the pause between the two is inside.
    assert detection["activity_end_seconds"] == pytest.approx(7.5, abs=FRAME)

    resolved = boundaries.resolve(detection, DEFAULTS, None, 25.0)
    assert resolved["start_seconds"] == pytest.approx(2.0 - 0.15, abs=0.04)
    assert resolved["end_seconds"] == pytest.approx(7.5 + 0.35, abs=0.04)
    # One interval. Everything between start and end is kept, pause included.
    assert resolved["retained_seconds"] == pytest.approx(
        resolved["end_seconds"] - resolved["start_seconds"]
    )
    assert resolved["retained_seconds"] > (7.5 - 2.0)
    assert not resolved["needs_review"]


def test_short_dips_inside_a_word_do_not_split_the_activity():
    # 100 ms of silence inside otherwise continuous sound: a consonant closure.
    runs = boundaries.find_runs(make_envelope(3, [(1.0, 1.4), (1.5, 2.0)]), 0.04)
    assert len(runs) == 1
    assert runs[0] == pytest.approx((1.0, 2.0), abs=FRAME)

    # A real pause does split it.
    assert len(boundaries.find_runs(make_envelope(3, [(1.0, 1.4), (2.0, 2.5)]), 0.04)) == 2


def test_a_short_click_cannot_set_a_boundary():
    """Pressing record and pressing stop both make a noise."""
    envelope = make_envelope(12, [(0.3, 0.35), (3.0, 8.0), (11.0, 11.06)])
    detection, _ = detect(envelope)

    assert detection["activity_start_seconds"] == pytest.approx(3.0, abs=FRAME)
    assert detection["activity_end_seconds"] == pytest.approx(8.0, abs=FRAME)

    # With noise rejection switched off the clicks do count.
    loose, _ = detect(envelope, {**DEFAULTS, "min_activity_seconds": 0.0})
    assert loose["activity_start_seconds"] == pytest.approx(0.3, abs=FRAME)
    assert loose["activity_end_seconds"] == pytest.approx(11.06, abs=FRAME)


def test_the_threshold_decides_what_counts_as_activity():
    envelope = make_envelope(10, [(2.0, 8.0)], level=0.03)
    quiet, _ = detect(envelope)
    assert quiet["status"] == boundaries.STATUS_NO_ACTIVITY

    found, _ = detect(envelope, {**DEFAULTS, "detection_threshold": 0.02})
    assert found["status"] == boundaries.STATUS_DETECTED


# =============================================================================
# Padding and limits
# =============================================================================


def test_padding_is_applied_on_both_sides():
    detection, _ = detect(make_envelope(10, [(3.0, 6.0)]))
    settings = {**DEFAULTS, "leading_padding_seconds": 0.5, "trailing_padding_seconds": 1.0}
    resolved = boundaries.resolve(detection, settings, None, None)

    assert resolved["start_seconds"] == pytest.approx(2.5, abs=FRAME)
    assert resolved["end_seconds"] == pytest.approx(7.0, abs=FRAME)
    assert resolved["removed_leading_seconds"] == pytest.approx(2.5, abs=FRAME)
    assert resolved["removed_trailing_seconds"] == pytest.approx(3.0, abs=FRAME)


def test_padding_is_clamped_to_the_source():
    detection, _ = detect(make_envelope(10, [(0.3, 9.8)]))
    settings = {**DEFAULTS, "leading_padding_seconds": 2.0, "trailing_padding_seconds": 2.0}
    resolved = boundaries.resolve(detection, settings, None, 25.0)

    assert resolved["start_seconds"] == 0.0
    assert resolved["end_seconds"] == 10.0
    codes = {warning["code"] for warning in resolved["warnings"]}
    assert {"leading_padding_clamped", "trailing_padding_clamped"} <= codes
    # Clamping is information, not doubt about the boundary.
    assert not resolved["needs_review"]


def test_boundaries_are_snapped_outward_to_whole_frames():
    detection, _ = detect(make_envelope(10, [(3.0, 6.0)]))
    resolved = boundaries.resolve(detection, DEFAULTS, None, 25.0)

    for value in (resolved["start_seconds"], resolved["end_seconds"]):
        assert value * 25 == pytest.approx(round(value * 25), abs=1e-6)
    # Outward: never less than the padding asked for.
    assert resolved["start_seconds"] <= 3.0 - 0.15 + 1e-9
    assert resolved["end_seconds"] >= 6.0 + 0.35 - 1e-9


def test_sound_at_the_very_first_and_last_frame_is_kept():
    detection, _ = detect(make_envelope(8, [(0.0, 8.0)]))
    assert detection["starts_immediately"] and detection["runs_to_end"]

    resolved = boundaries.resolve(detection, DEFAULTS, None, 25.0)
    assert resolved["start_seconds"] == 0.0
    assert resolved["end_seconds"] == 8.0
    assert resolved["retained_seconds"] == 8.0

    codes = {warning["code"] for warning in resolved["warnings"]}
    assert {"starts_immediately", "runs_to_end"} <= codes
    assert not resolved["needs_review"]


def test_long_silence_on_either_side_is_removed_whole():
    detection, _ = detect(make_envelope(120, [(50.0, 60.0)]))
    resolved = boundaries.resolve(detection, DEFAULTS, None, None)

    assert resolved["removed_leading_seconds"] == pytest.approx(49.85, abs=0.02)
    assert resolved["removed_trailing_seconds"] == pytest.approx(59.65, abs=0.02)


# =============================================================================
# Inputs with nothing confident to find
# =============================================================================


def test_a_silent_clip_is_kept_whole_and_flagged():
    detection, _ = detect(make_envelope(20, []))
    assert detection["status"] == boundaries.STATUS_NO_ACTIVITY

    resolved = boundaries.resolve(detection, DEFAULTS, None, 25.0)
    assert (resolved["start_seconds"], resolved["end_seconds"]) == (0.0, 20.0)
    assert resolved["start_origin"] == boundaries.ORIGIN_SOURCE_LIMIT
    assert resolved["needs_review"]
    assert resolved["warnings"][0]["code"] == "no_activity"
    assert "whole clip is kept" in resolved["warnings"][0]["message"]


def test_a_clip_without_audio_is_kept_whole_and_flagged():
    decoder = FakeDecoder([])
    detection = boundaries.detect("unused", 6.0, False, DEFAULTS, decoder=decoder)

    assert detection["status"] == boundaries.STATUS_NO_AUDIO
    assert decoder.requests == []  # nothing to decode, so nothing was decoded

    resolved = boundaries.resolve(detection, DEFAULTS, None, 25.0)
    assert (resolved["start_seconds"], resolved["end_seconds"]) == (0.0, 6.0)
    assert resolved["needs_review"]
    assert resolved["warnings"][0]["code"] == "no_audio"


def test_background_noise_near_the_threshold_is_reported_as_uncertain():
    detection, _ = detect(make_envelope(10, [(2.0, 8.0)], level=0.2, floor=0.03))
    resolved = boundaries.resolve(detection, DEFAULTS, None, 25.0)

    assert resolved["status"] == boundaries.STATUS_DETECTED
    assert "background_near_threshold" in {w["code"] for w in resolved["warnings"]}
    assert resolved["needs_review"]


def test_low_volume_activity_just_over_the_threshold_is_reported_as_uncertain():
    detection, _ = detect(make_envelope(10, [(2.0, 8.0)], level=0.045))
    resolved = boundaries.resolve(detection, DEFAULTS, None, 25.0)

    assert "activity_near_threshold" in {w["code"] for w in resolved["warnings"]}
    assert resolved["needs_review"]


def test_a_very_short_clip_is_handled_in_one_decode():
    detection, decoder = detect(make_envelope(0.6, [(0.1, 0.5)]))
    assert detection["full_scan"] and len(decoder.requests) == 1

    resolved = boundaries.resolve(detection, DEFAULTS, None, 25.0)
    assert 0 < resolved["retained_seconds"] <= 0.6
    assert "very_short_clip" in {w["code"] for w in resolved["warnings"]}


def test_nothing_to_keep_is_an_error_not_an_empty_output():
    detection, _ = detect(make_envelope(10, [(3.0, 6.0)]))
    with pytest.raises(boundaries.BoundaryError) as caught:
        boundaries.resolve(detection, DEFAULTS, {"start_seconds": 9.0}, 25.0)
    assert "less than" in caught.value.message


# =============================================================================
# Analysing only the ends
# =============================================================================


def full_scan(envelope, settings=None):
    return detect(envelope, settings, initial_window=10 ** 9)[0]


def same_boundaries(first: dict, second: dict) -> bool:
    return (
        first["status"] == second["status"]
        and first["activity_start_seconds"] == second["activity_start_seconds"]
        and first["activity_end_seconds"] == second["activity_end_seconds"]
    )


def test_a_typical_take_is_found_from_the_two_ends_alone():
    envelope = make_envelope(300, [(3.0, 295.0)])
    detection, decoder = detect(envelope)

    assert same_boundaries(detection, full_scan(envelope))
    assert not detection["full_scan"]
    assert detection["expansions"] == 0
    # Two twelve-second windows, not five minutes.
    assert decoder.decoded == pytest.approx(24.0)


def test_the_window_grows_until_it_finds_the_start():
    """Speech that begins after the first window must not be cut at its edge."""
    envelope = make_envelope(300, [(40.0, 290.0)])
    detection, decoder = detect(envelope)

    assert detection["activity_start_seconds"] == pytest.approx(40.0, abs=FRAME)
    assert detection["activity_start_seconds"] not in (12.0, 24.0, 48.0)
    assert same_boundaries(detection, full_scan(envelope))
    assert detection["expansions"] >= 1
    assert decoder.decoded < 300


def test_the_window_grows_until_it_finds_the_end():
    envelope = make_envelope(300, [(5.0, 200.0)])
    detection, _ = detect(envelope)

    assert detection["activity_end_seconds"] == pytest.approx(200.0, abs=FRAME)
    assert same_boundaries(detection, full_scan(envelope))


def test_activity_only_in_the_middle_falls_back_to_a_full_scan():
    envelope = make_envelope(100, [(48.0, 52.0)])
    detection, decoder = detect(envelope)

    assert detection["full_scan"]
    assert same_boundaries(detection, full_scan(envelope))
    # Each stretch of audio was decoded once, not once per expansion.
    assert decoder.decoded == pytest.approx(100.0, abs=0.5)


def test_a_run_cut_off_by_the_window_edge_is_not_mistaken_for_a_short_noise():
    # The sound starts 0.1 s before the first window ends. Seen through that
    # window alone it is shorter than the minimum; in truth it runs for ages.
    envelope = make_envelope(200, [(11.9, 150.0)])
    detection, _ = detect(envelope)

    assert detection["activity_start_seconds"] == pytest.approx(11.9, abs=FRAME)
    assert same_boundaries(detection, full_scan(envelope))

    # And the mirror image at the tail.
    envelope = make_envelope(200, [(20.0, 188.1)])
    detection, _ = detect(envelope)
    assert detection["activity_end_seconds"] == pytest.approx(188.1, abs=FRAME)
    assert same_boundaries(detection, full_scan(envelope))


def test_windowed_analysis_always_agrees_with_a_full_scan():
    """The property the whole optimisation rests on, over many random clips."""
    generator = random.Random(20260930)

    for _ in range(150):
        seconds = generator.uniform(1, 240)
        intervals = []
        for _ in range(generator.randint(0, 6)):
            start = generator.uniform(0, seconds)
            intervals.append((start, start + generator.choice([0.03, 0.1, 0.5, 3, 40])))
        envelope = make_envelope(seconds, intervals)
        settings = {**DEFAULTS, "min_activity_seconds": generator.choice([0.0, 0.2, 1.0])}

        windowed, _ = detect(envelope, settings, initial_window=generator.choice([2, 12]))
        assert same_boundaries(windowed, full_scan(envelope, settings)), intervals


def test_a_silent_clip_costs_one_pass_over_its_audio():
    detection, decoder = detect(make_envelope(100, []))
    assert detection["status"] == boundaries.STATUS_NO_ACTIVITY
    assert decoder.decoded == pytest.approx(100.0, abs=0.5)


# =============================================================================
# Manual overrides
# =============================================================================


def test_a_manual_start_or_end_replaces_only_that_side():
    detection, _ = detect(make_envelope(10, [(3.0, 6.0)]))

    start_only = boundaries.resolve(detection, DEFAULTS, {"start_seconds": 1.0}, 25.0)
    assert start_only["start_seconds"] == 1.0
    assert start_only["start_origin"] == boundaries.ORIGIN_MANUAL
    assert start_only["end_origin"] == boundaries.ORIGIN_DETECTED

    end_only = boundaries.resolve(detection, DEFAULTS, {"end_seconds": 9.0}, 25.0)
    assert end_only["end_seconds"] == 9.0
    assert end_only["start_origin"] == boundaries.ORIGIN_DETECTED

    # Padding belongs to detection: a manual boundary is used as given.
    both = boundaries.resolve(
        detection, DEFAULTS, {"start_seconds": 2.0, "end_seconds": 7.0}, 25.0
    )
    assert (both["start_seconds"], both["end_seconds"]) == (2.0, 7.0)


def test_keeping_the_whole_clip_overrides_detection():
    detection, _ = detect(make_envelope(10, [(3.0, 6.0)]))
    resolved = boundaries.resolve(detection, DEFAULTS, {"keep_whole": True}, 25.0)

    assert (resolved["start_seconds"], resolved["end_seconds"]) == (0.0, 10.0)
    assert resolved["start_origin"] == boundaries.ORIGIN_KEPT


def test_a_manual_boundary_settles_a_clip_with_no_detected_one():
    detection, _ = detect(make_envelope(10, []))
    resolved = boundaries.resolve(
        detection, DEFAULTS, {"start_seconds": 1.0, "end_seconds": 4.0}, 25.0
    )

    assert (resolved["start_seconds"], resolved["end_seconds"]) == (1.0, 4.0)
    # The user decided both sides; there is nothing left to be unsure about.
    assert not resolved["needs_review"]


@pytest.mark.parametrize(
    "override",
    [
        {"start_seconds": -1},
        {"start_seconds": "2"},
        {"end_seconds": True},
        {"start_seconds": 5, "end_seconds": 5},
        {"start_seconds": 5, "end_seconds": 2},
        {"keep_whole": "yes"},
        {"start": 1},
        "whole",
    ],
)
def test_invalid_manual_boundaries_are_refused(override):
    with pytest.raises(boundaries.BoundaryError):
        boundaries.validate_override(override)


def test_manual_boundaries_must_lie_inside_the_clip():
    boundaries.check_override_fits({"start_seconds": 1, "end_seconds": 9.99}, 10.0, "a.mp4")
    boundaries.check_override_fits({"keep_whole": True}, 10.0, "a.mp4")
    # The duration is shown rounded; typing what is shown must work.
    boundaries.check_override_fits({"end_seconds": 10.03}, 10.0, "a.mp4")

    with pytest.raises(boundaries.BoundaryError) as caught:
        boundaries.check_override_fits({"end_seconds": 12.0}, 10.0, "take 1.mp4")
    assert "take 1.mp4" in caught.value.message and "10.00" in caught.value.message

    with pytest.raises(boundaries.BoundaryError):
        boundaries.check_override_fits({"start_seconds": 10.0}, 10.0, "a.mp4")


def test_empty_overrides_vanish_and_unknown_sources_are_dropped():
    cleaned = boundaries.validate_overrides(
        {"a": {"start_seconds": 1.23456}, "b": {}, "c": {"keep_whole": False}, "gone": {"keep_whole": True}},
        {"a", "b", "c"},
    )
    assert cleaned == {"a": {"start_seconds": 1.235}}


# =============================================================================
# Settings: the new default, and what was saved before it existed
# =============================================================================


def test_boundary_only_is_the_default_for_a_configuration_never_saved():
    loaded = cutting.read_settings({"sources": [], "settings": {}})

    assert loaded["mode"] == cutting.CUT_MODE_BOUNDARY
    assert loaded["mode_is_explicit"] is False
    assert loaded["boundary_settings"] == boundaries.default_settings()
    assert loaded["settings"] == cutting.default_settings()


def test_settings_saved_before_modes_existed_stay_full_clip():
    """A 1A project's explicit settings are not reinterpreted."""
    project = {
        "sources": [],
        "settings": {"cutting": {"settings": {"audio_threshold": 0.08}, "output_mode": "both"}},
    }
    loaded = cutting.read_settings(project)

    assert loaded["mode"] == cutting.CUT_MODE_FULL
    assert loaded["mode_is_explicit"] is True
    assert loaded["settings"]["audio_threshold"] == 0.08


def test_a_run_or_job_without_a_mode_is_a_full_clip_run():
    assert cutting.job_mode({}) == cutting.CUT_MODE_FULL
    assert cutting.job_mode({"mode": "boundary"}) == cutting.CUT_MODE_BOUNDARY

    described = cutting.describe_run(
        {"status": "succeeded", "settings": cutting.default_settings(), "clips": [], "sources": []}
    )
    assert described["mode"] == cutting.CUT_MODE_FULL
    assert described["cut_map"]["available"] is False


def test_boundary_settings_are_validated_like_the_others():
    assert boundaries.validate_settings(None) == boundaries.default_settings()
    assert boundaries.validate_settings({"detection_threshold": 0.1})["detection_threshold"] == 0.1

    for bad in (
        {"detection_threshold": 0},
        {"leading_padding_seconds": -0.1},
        {"trailing_padding_seconds": 99},
        {"min_activity_seconds": "0.2"},
        # The full-clip names are not accepted here: nothing is repurposed.
        {"audio_threshold": 0.04},
        {"margin_after_seconds": 0.5},
    ):
        with pytest.raises(boundaries.BoundaryError):
            boundaries.validate_settings(bad)

    # And the boundary names are not accepted by the full-clip validator.
    with pytest.raises(cutting.CuttingError):
        cutting.validate_settings({"leading_padding_seconds": 0.2})


def test_the_catalog_describes_both_modes_and_every_boundary_parameter():
    catalog = cutting.settings_catalog()

    assert catalog["default_mode"] == cutting.CUT_MODE_BOUNDARY
    assert [mode["id"] for mode in catalog["modes"]] == ["boundary", "full_clip"]
    assert "pauses" in catalog["modes"][0]["description"]

    for parameter in catalog["boundary"]["parameters"]:
        assert parameter["unit"] and parameter["label"] and parameter["description"]
        assert parameter["min"] <= parameter["default"] <= parameter["max"]

    # Detection is described as loudness, never as speech recognition.
    threshold = boundaries.SETTINGS_SPEC["detection_threshold"]["description"]
    assert "loudness" in threshold and "cannot tell speech" in threshold


def test_saving_bumps_the_revision_only_when_a_cut_would_move(client, project, tmp_path):
    path = tmp_path / "take.mp4"
    path.write_bytes(b"placeholder")
    source = add_source(client, project["id"], path)
    url = f"/projects/{project['id']}/cutting/settings"

    first = client.put(url, json={"source_ids": [source]}).json()
    assert (first["mode"], first["revision"]) == (cutting.CUT_MODE_BOUNDARY, 1)

    # The preview length and the selection do not move a cut.
    same = client.put(url, json={"source_ids": [], "sample_seconds": 6}).json()
    assert same["revision"] == 1 and same["sample_seconds"] == 6

    padded = client.put(
        url, json={"boundary_settings": {"trailing_padding_seconds": 0.6}}
    ).json()
    assert padded["revision"] == 2

    overridden = client.put(
        url,
        json={
            "boundary_settings": {"trailing_padding_seconds": 0.6},
            "overrides": {source: {"start_seconds": 1.5}},
        },
    ).json()
    assert overridden["revision"] == 3
    assert overridden["overrides"] == {source: {"start_seconds": 1.5}}

    # Survives a fresh read from disk.
    with TestClient(app) as reopened:
        loaded = reopened.get(url).json()
    assert loaded["mode_is_explicit"] and loaded["revision"] == 3
    assert loaded["boundary_settings"]["trailing_padding_seconds"] == 0.6


# =============================================================================
# What makes a result stale
# =============================================================================


def configuration(**changes) -> dict:
    base = {
        "mode": cutting.CUT_MODE_BOUNDARY,
        "settings": cutting.default_settings(),
        "boundary_settings": boundaries.default_settings(),
        "overrides": {},
    }
    base.update(changes)
    return base


def test_the_basis_key_follows_exactly_what_moves_a_cut():
    entries = [("a", "sha256:1"), ("b", "sha256:2")]
    key = cutting.basis_key(configuration(), entries)

    assert key == cutting.basis_key(configuration(), entries)

    # A boundary setting, an override, the file's bytes, the order: all count.
    padded = {**boundaries.default_settings(), "leading_padding_seconds": 0.3}
    assert key != cutting.basis_key(configuration(boundary_settings=padded), entries)
    assert key != cutting.basis_key(
        configuration(overrides={"a": {"start_seconds": 1.0}}), entries
    )
    assert key != cutting.basis_key(configuration(), [("a", "sha256:9"), ("b", "sha256:2")])
    assert key != cutting.basis_key(configuration(), list(reversed(entries)))
    assert key != cutting.basis_key(configuration(mode=cutting.CUT_MODE_FULL), entries)

    # The other mode's settings do not: tuning one never invalidates the other.
    tuned = {**cutting.default_settings(), "audio_threshold": 0.2}
    assert key == cutting.basis_key(configuration(settings=tuned), entries)

    # An override on another clip does not touch this clip's samples.
    single = cutting.basis_key(configuration(), [("a", "sha256:1")])
    assert single == cutting.basis_key(
        configuration(overrides={"b": {"keep_whole": True}}), [("a", "sha256:1")]
    )


# =============================================================================
# Commands
# =============================================================================


def test_the_trim_command_seeks_accurately_and_re_encodes():
    command = cutting.build_trim_command(r"C:\וידאו\take 1.mkv", r"C:\out\clip.mp4", 1.84, 7.88)

    assert command[0] == "ffmpeg.exe"
    # The seek and the length come before the input: a frame-accurate decode.
    assert command.index("-ss") < command.index("-i")
    assert command[command.index("-ss") + 1] == "1.840000"
    assert command[command.index("-t") + 1] == "6.040000"
    assert r"C:\וידאו\take 1.mkv" in command
    # Never a stream copy: that could only cut on a keyframe.
    assert "copy" not in command
    assert "libx264" in command and "aac" in command
    assert command[-1] == r"C:\out\clip.mp4"


def test_a_source_without_audio_gets_a_silent_track():
    command = cutting.build_trim_command("in.mp4", "out.mp4", 0.0, 4.0, has_audio=False)

    assert any(argument.startswith("anullsrc") for argument in command)
    assert "1:a:0" in command and "0:a:0" not in command


def test_the_join_preview_uses_the_same_normalisation_as_the_real_join():
    command = cutting.build_join_sample_command(
        [
            {"path": "a.mp4", "start_seconds": 3.0, "end_seconds": 7.0, "has_audio": True},
            {"path": "b.mp4", "start_seconds": 1.0, "end_seconds": 5.0, "has_audio": True},
        ],
        "out.mp4",
        25.0,
    )
    graph = command[command.index("-filter_complex") + 1]

    assert command.count("-i") == 2
    assert "concat=n=2:v=1:a=1[v][a]" in graph
    assert "fps=25" in graph and "aresample=48000" in graph


def test_the_timeline_export_reuses_the_render_flags():
    settings = cutting.default_settings()
    render = cutting.build_cut_command("in.mkv", "out.mp4", settings)
    export = cutting.build_timeline_export_command("in.mkv", "clip.v3", settings)

    for flag in ("--edit", "--margin", "--smooth"):
        assert export[export.index(flag) + 1] == render[render.index(flag) + 1]
    assert export[export.index("--export") + 1] == "v3"
    assert export[-1] == "clip.v3"


# =============================================================================
# Cut maps
# =============================================================================

VIDEO = {"frame_rate": 25.0}
AUDIO = {"sample_rate": 48000}


def test_a_boundary_map_is_the_interval_that_was_rendered():
    detection, _ = detect(make_envelope(10, AUDIBLE))
    resolved = boundaries.resolve(detection, DEFAULTS, None, 25.0)
    entry = cut_map.boundary_entry(resolved, resolved["retained_seconds"] + 0.02, VIDEO, AUDIO)

    assert entry["available"] is True
    assert entry["retained"] == [
        {
            "source_start": resolved["start_seconds"],
            "source_end": resolved["end_seconds"],
            "output_start": 0.0,
            "output_end": resolved["retained_seconds"],
        }
    ]
    assert [piece["position"] for piece in entry["removed"]] == ["leading", "trailing"]
    assert entry["removed"][0]["source_end"] == resolved["start_seconds"]
    assert entry["removed"][1]["source_start"] == resolved["end_seconds"]


def test_a_map_that_disagrees_with_the_rendered_file_is_not_offered():
    detection, _ = detect(make_envelope(10, AUDIBLE))
    resolved = boundaries.resolve(detection, DEFAULTS, None, 25.0)
    entry = cut_map.boundary_entry(resolved, resolved["retained_seconds"] - 1.0, VIDEO, AUDIO)

    assert entry["available"] is False
    assert "tolerance" in entry["reason"]


V3_TIMELINE = {
    "version": "3",
    "timebase": "25/1",
    "v": [[
        {"src": "a.mp4", "start": 0, "dur": 63, "offset": 50, "stream": 0},
        {"src": "a.mp4", "start": 63, "dur": 63, "offset": 137, "stream": 0},
    ]],
}


def test_a_full_clip_map_is_read_from_auto_editors_own_timeline():
    entry = cut_map.full_clip_entry(V3_TIMELINE, 10.0, 5.04, VIDEO, AUDIO)

    assert entry["available"] is True
    assert entry["origin"] == cut_map.SOURCE_AUTO_EDITOR
    assert entry["retained"] == [
        {"source_start": 2.0, "source_end": 4.52, "output_start": 0.0, "output_end": 2.52},
        {"source_start": 5.48, "source_end": 8.0, "output_start": 2.52, "output_end": 5.04},
    ]
    assert [(piece["position"]) for piece in entry["removed"]] == [
        "leading", "internal", "trailing",
    ]
    assert entry["nominal_output_seconds"] == 5.04


@pytest.mark.parametrize(
    "timeline",
    [
        None,
        "stub",
        {"timebase": "25/1", "v": []},
        {"timebase": "nonsense", "v": [[]]},
        {"timebase": "25/1", "v": [[{"start": 0, "dur": 10, "offset": 0, "speed": 2}]]},
        {"timebase": "25/1", "v": [[{"start": 5, "dur": 10, "offset": 0}]]},
        {"timebase": "25/1", "v": [[]]},
    ],
)
def test_an_unusable_timeline_means_no_map_rather_than_a_guess(timeline):
    entry = cut_map.full_clip_entry(timeline, 10.0, 5.0, VIDEO, AUDIO)
    assert entry["available"] is False and entry["reason"]
    assert "retained" not in entry


def test_sequence_positions_follow_the_joined_files_segments():
    def clip(output_id, order, duration, source_id):
        return {
            "output_id": output_id, "order": order, "status": "succeeded",
            "source_id": source_id, "source_filename": "%s.mp4" % source_id,
            "source_duration_seconds": 10.0, "duration_seconds": duration,
            "filename": "%s.mp4" % output_id, "video": VIDEO, "audio": AUDIO,
            "cut_map": {"available": True, "retained": []},
        }

    manifest = {
        "project_id": "p", "run_id": "r", "status": "succeeded", "mode": "boundary",
        "boundary_settings": DEFAULTS,
        "sources": [{"source_id": "a", "fingerprint": {"digest": "sha256:1"}},
                    {"source_id": "b", "fingerprint": {"digest": "sha256:2"}}],
        "clips": [clip("clip-0001", 1, 6.04, "a"), clip("clip-0002", 2, 5.04, "b")],
        "combined": {
            "status": "succeeded", "output_id": "combined", "strategy": "re_encode",
            "duration_seconds": 11.0,
            # The join dropped 80 ms of black from the end of the first clip.
            "segments": [
                {"output_id": "clip-0001", "start_seconds": 0.0, "duration_seconds": 5.96},
                {"output_id": "clip-0002", "start_seconds": 5.96, "duration_seconds": 5.04},
            ],
        },
    }
    document = cut_map.build(manifest)

    assert document["schema_version"] == cut_map.CUT_MAP_SCHEMA_VERSION
    assert document["timebase"]["intervals"].startswith("half-open")
    first, second = document["clips"]
    assert (first["sequence_start_seconds"], first["sequence_end_seconds"]) == (0.0, 5.96)
    assert (second["sequence_start_seconds"], second["sequence_end_seconds"]) == (5.96, 11.0)
    assert (first["sequence_index"], second["sequence_index"]) == (0, 1)
    assert first["source_fingerprint"] == {"digest": "sha256:1"}
    assert document["sequence"]["rendered"] and document["sequence"]["within_tolerance"]

    # Without a joined file the positions are nominal, and say so.
    manifest["combined"] = None
    nominal = cut_map.build(manifest)
    assert nominal["sequence"]["rendered"] is False
    assert nominal["clips"][1]["sequence_start_seconds"] == 6.04
    assert "nominal" in nominal["sequence"]["positions"]


# =============================================================================
# AI proposals: validated, never trusted
# =============================================================================


def test_a_proposal_is_checked_against_the_mode_it_names():
    current = boundaries.default_settings()
    proposal = cut_advice.validate_proposal(
        {
            "mode": "boundary",
            "settings": {"trailing_padding_seconds": 0.5},
            "explanation": "A little more room after each take.",
            "limitations": ["Based on statistics only.", 7, ""],
        },
        cutting.CUT_MODE_BOUNDARY,
        current,
    )

    # What it named changed; what it did not name kept its current value.
    assert proposal["settings"] == {**current, "trailing_padding_seconds": 0.5}
    assert proposal["mode_changed"] is False
    assert proposal["limitations"] == ["Based on statistics only."]


@pytest.mark.parametrize(
    "answer",
    [
        {"mode": "semantic", "settings": {}, "explanation": "x"},
        {"mode": "boundary", "settings": {"trailing_padding_seconds": 60}, "explanation": "x"},
        {"mode": "boundary", "settings": {"margin_after_seconds": 0.5}, "explanation": "x"},
        {"mode": "boundary", "settings": {"command": "rm"}, "explanation": "x"},
        {"mode": "boundary", "settings": [], "explanation": "x"},
        {"mode": "boundary", "settings": {}, "explanation": ""},
        "just text",
    ],
)
def test_an_invalid_proposal_is_rejected(answer):
    with pytest.raises(Exception) as caught:
        cut_advice.validate_proposal(answer, cutting.CUT_MODE_BOUNDARY, DEFAULTS)
    assert getattr(caught.value, "message", "")


def test_a_proposal_for_another_mode_is_flagged_not_merged():
    proposal = cut_advice.validate_proposal(
        {"mode": "full_clip", "settings": {"min_silence_seconds": 0.4}, "explanation": "x"},
        cutting.CUT_MODE_BOUNDARY,
        DEFAULTS,
    )
    assert proposal["mode_changed"] is True
    # The other mode's own defaults, not boundary values under new names.
    assert proposal["settings"] == {**cutting.default_settings(), "min_silence_seconds": 0.4}


def test_the_request_carries_measurements_and_no_media_names_or_paths():
    job_input = {
        "mode": "boundary", "request": "tighter joins", "feedback": None,
        "current_settings": DEFAULTS,
    }
    clips = [{"clip": 1, "resource_id": "abc", "duration_seconds": 10.0,
              "first_sound_at_seconds": 2.0}]
    request = cut_advice.build_request(job_input, clips, [])
    text = json.dumps(request, ensure_ascii=False)

    assert "have not heard" in request["evidence"]
    assert {c["id"] for c in request["capabilities"]} == {
        capabilities.TRIM_BOUNDARIES, capabilities.CUT_SILENCE,
    }
    # Real parameter ranges, straight from the catalog.
    trim = next(c for c in request["capabilities"] if c["id"] == capabilities.TRIM_BOUNDARIES)
    assert trim["parameters"]["trailing_padding_seconds"]["max"] == 5.0
    assert "filename" not in text and ":\\\\" not in text and ".mp4" not in text


def test_the_mock_never_changes_the_mode():
    request = cut_advice.build_request(
        {"mode": "boundary", "request": "remove every pause, make it much tighter",
         "feedback": None, "current_settings": DEFAULTS},
        [], [],
    )
    answer = llm.MockProvider().recommend_cut(request)

    assert answer["mode"] == "boundary"
    assert answer["settings"]["trailing_padding_seconds"] < DEFAULTS["trailing_padding_seconds"]
    assert "not an AI model" in answer["explanation"]


# =============================================================================
# Cancellation, with the tool layer stubbed
# =============================================================================


@pytest.fixture
def stub_sources(client, project, tmp_path, monkeypatch):
    """Two placeholder sources that probe as ten-second clips."""
    monkeypatch.setattr(
        cutting, "collect_tool_versions", lambda: {"ffmpeg": "stub", "ffprobe": "stub"}
    )
    monkeypatch.setattr(
        media,
        "describe_input",
        lambda path, require_audio=True: {
            "path": path, "filename": Path(path).name, "duration_seconds": 10.0,
            "has_video": True, "has_audio": True,
            "video": {"codec_name": "h264", "width": 640, "height": 360,
                      "pix_fmt": "yuv420p", "frame_rate": 25.0},
            "audio": {"codec_name": "aac", "sample_rate": 48000, "channels": 2},
        },
    )
    ids = []
    for name in ("take 1.mkv", "צילום 2.mov"):
        path = tmp_path / name
        path.write_bytes(name.encode("utf-8"))
        ids.append(add_source(client, project["id"], path))
    return ids


def blocking_until_cancelled(started: list):
    def behaviour(argv, *, cancelled=None, on_output=None, **kwargs):
        started.append(list(argv))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if cancelled is not None and cancelled():
                raise processes.ProcessCancelled()
            time.sleep(0.01)
        raise AssertionError("the job was never cancelled")

    return behaviour


def synthetic_decode(path, start, length, cancelled=None, channels=None):
    envelope = make_envelope(10, AUDIBLE)
    first = int(round(start / FRAME))
    return envelope[first : first + int(round(length / FRAME))]


def cancel_when_running(client, project_id, job_id, started: list):
    wait_until_running(client, project_id, job_id)
    deadline = time.monotonic() + 20
    while not started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started, "the job never reached its external tool"
    client.post(f"/projects/{project_id}/jobs/{job_id}/cancel")
    return wait_for_job(client, project_id, job_id, timeout=30)


def test_cancelling_analysis_stops_the_decoder_and_caches_nothing(
    client, project, stub_sources, monkeypatch
):
    started: list = []

    def decode(path, start, length, cancelled=None, channels=None):
        started.append(path)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if cancelled is not None and cancelled():
                raise processes.ProcessCancelled()
            time.sleep(0.01)
        raise AssertionError("the job was never cancelled")

    monkeypatch.setattr(boundaries, "decode_envelope", decode)

    job = client.post(
        f"/projects/{project['id']}/cutting/analysis", json={"source_ids": stub_sources}
    ).json()
    record = cancel_when_running(client, project["id"], job["id"], started)

    assert record["status"] == jobs.CANCELLED
    assert record["result"] is None
    # Only the first clip was ever touched, and nothing was written for it.
    assert len(started) == 1
    assert not boundaries.analysis_directory(project["id"]).exists()
    assert state(client, project["id"], {"source_ids": stub_sources})["analysis_needed"]


def test_cancelling_a_preview_render_leaves_no_half_made_preview(
    client, project, stub_sources, monkeypatch
):
    monkeypatch.setattr(boundaries, "decode_envelope", synthetic_decode)
    started: list = []
    monkeypatch.setattr(processes, "run", blocking_until_cancelled(started))

    job = client.post(
        f"/projects/{project['id']}/cutting/samples", json={"source_ids": stub_sources}
    ).json()
    record = cancel_when_running(client, project["id"], job["id"], started)

    assert record["status"] == jobs.CANCELLED
    assert len(started) == 1  # no second render began
    assert cut_samples.list_samples(project["id"]) == []
    root = cut_samples.samples_root(project["id"])
    assert not root.exists() or list(root.iterdir()) == []


def test_cancelling_the_final_render_stops_the_remaining_clips_and_the_join(
    client, project, stub_sources, monkeypatch
):
    monkeypatch.setattr(boundaries, "decode_envelope", synthetic_decode)
    started: list = []
    monkeypatch.setattr(processes, "run", blocking_until_cancelled(started))

    job = client.post(
        f"/projects/{project['id']}/cutting/runs",
        json={"source_ids": stub_sources, "output_mode": "both"},
    ).json()
    record = cancel_when_running(client, project["id"], job["id"], started)

    assert record["status"] == jobs.CANCELLED
    assert len(started) == 1

    run = client.get(f"/projects/{project['id']}/cutting/runs").json()["runs"][0]
    assert run["mode"] == cutting.CUT_MODE_BOUNDARY
    assert run["status"] == cutting.RUN_CANCELLED
    assert run["clips"][0]["status"] == cutting.CLIP_CANCELLED
    assert run["clips"][1]["status"] == cutting.CLIP_SKIPPED
    assert run["combined"] is None
    assert run["cut_map"]["available"] is False
    assert client.get(f"/projects/{project['id']}/resources").json()["generated"] == []


@needs_tools
def test_cancelling_a_real_decode_raises_instead_of_returning_a_partial_envelope(tmp_path):
    take = make_take(tmp_path / "take.mp4")
    with pytest.raises(processes.ProcessCancelled):
        boundaries.decode_envelope(str(take), 0.0, 10.0, cancelled=lambda: True)


def test_a_cancelled_detection_during_the_final_run_is_recorded_as_cancelled(
    client, project, stub_sources, monkeypatch
):
    def decode(path, start, length, cancelled=None, channels=None):
        raise processes.ProcessCancelled()

    monkeypatch.setattr(boundaries, "decode_envelope", decode)
    monkeypatch.setattr(
        processes, "run", lambda *a, **k: pytest.fail("nothing should be rendered")
    )

    job = client.post(
        f"/projects/{project['id']}/cutting/runs", json={"source_ids": stub_sources}
    ).json()
    record = wait_for_job(client, project["id"], job["id"])

    assert record["status"] == jobs.CANCELLED
    run = client.get(f"/projects/{project['id']}/cutting/runs").json()["runs"][0]
    assert run["status"] == cutting.RUN_CANCELLED
    assert "analysed" in run["clips"][0]["error"]


# =============================================================================
# The real thing, on generated footage
# =============================================================================


@needs_tools
def test_a_real_decode_finds_the_fixtures_boundaries(tmp_path):
    take = make_take(tmp_path / "צילום א.mp4")
    described = media.describe_input(str(take), require_audio=False)
    detection = boundaries.detect(
        str(take), described["duration_seconds"], True, DEFAULTS,
        channels=described["audio"]["channels"],
    )

    assert detection["status"] == boundaries.STATUS_DETECTED
    assert detection["activity_start_seconds"] == pytest.approx(2.0, abs=0.05)
    assert detection["activity_end_seconds"] == pytest.approx(7.5, abs=0.05)
    # The level is measured on the channels as recorded: FFmpeg's sine source
    # is an eighth of full scale, and that is what must come back.
    assert detection["stats"]["peak"] == pytest.approx(0.125, abs=0.02)
    assert detection["stats"]["background_level"] == pytest.approx(0.0, abs=0.005)


@needs_tools
def test_real_windowed_analysis_matches_a_real_full_scan(tmp_path):
    """The window logic against a real decoder, not just a synthetic envelope."""
    take = make_take(tmp_path / "long.mp4", seconds=40, audible=[(17.0, 21.0), (25.0, 33.3)])

    windowed = boundaries.detect(str(take), 40.0, True, DEFAULTS, initial_window=4.0, channels=1)
    full = boundaries.detect(str(take), 40.0, True, DEFAULTS, initial_window=10 ** 9, channels=1)

    assert windowed["expansions"] >= 2
    assert windowed["activity_start_seconds"] == pytest.approx(full["activity_start_seconds"], abs=0.02)
    assert windowed["activity_end_seconds"] == pytest.approx(full["activity_end_seconds"], abs=0.02)
    assert full["activity_start_seconds"] == pytest.approx(17.0, abs=0.05)
    assert full["activity_end_seconds"] == pytest.approx(33.3, abs=0.05)


def silent_stretches(path: Path, threshold: float = 0.04) -> list[tuple]:
    """Inactive stretches of a rendered file, to prove what was kept."""
    described = media.probe(str(path))
    envelope = boundaries.decode_envelope(
        str(path), 0.0, described["duration_seconds"], channels=described["audio"]["channels"]
    )
    runs = boundaries.find_runs(envelope, threshold)
    return [(first[1], second[0]) for first, second in zip(runs, runs[1:])]


@needs_tools
def test_end_to_end_boundary_trimming_keeps_the_pause_and_maps_the_cut(
    client, project, tmp_path
):
    """The whole path on the fixture: two takes, trimmed, joined and mapped."""
    first = make_take(tmp_path / "take 1.mp4")
    # A space and Hebrew in the name, and a different shape of take.
    second = make_take(tmp_path / "צילום ב.mkv", seconds=9, audible=[(1.5, 6.0)], tone=600)
    ids = [add_source(client, project["id"], path) for path in (first, second)]

    body = {"source_ids": ids, "output_mode": "both"}
    record = submit(client, project["id"], "runs", body)
    assert record["status"] == jobs.SUCCEEDED, record.get("error")
    assert record["result"]["mode"] == cutting.CUT_MODE_BOUNDARY
    assert record["result"]["cut_map_available"] is True

    run = client.get(
        f"/projects/{project['id']}/cutting/runs/{record['result']['run_id']}"
    ).json()
    assert run["mode"] == cutting.CUT_MODE_BOUNDARY
    assert run["boundary_settings"] == boundaries.default_settings()
    assert run["settings"] is None  # the full-clip numbers were not used

    clip = run["clips"][0]
    boundary = clip["boundary"]
    assert boundary["start_seconds"] == pytest.approx(2.0 - 0.15, abs=0.05)
    assert boundary["end_seconds"] == pytest.approx(7.5 + 0.35, abs=0.05)
    assert clip["command"].startswith("ffmpeg.exe")
    assert (clip["video"]["width"], clip["video"]["height"]) == (320, 180)
    assert (clip["video"]["codec_name"], clip["audio"]["codec_name"]) == ("h264", "aac")

    # The rendered clip is as long as the interval that was asked for…
    tolerance = clip["cut_map"]["tolerance_seconds"]
    assert clip["duration_seconds"] == pytest.approx(boundary["retained_seconds"], abs=tolerance)
    # …and the pause between the two audible sections is still inside it.
    path, _entry = cutting.resolve_output(project["id"], run["run_id"], "clip-0001")
    pauses = silent_stretches(path)
    assert len(pauses) == 1
    assert pauses[0][1] - pauses[0][0] == pytest.approx(1.5, abs=0.1)

    # The map on disk says the same thing as the manifest and the file.
    document = client.get(
        f"/projects/{project['id']}/cutting/runs/{run['run_id']}/cut-map"
    ).json()
    assert document["available"] is True
    assert document["mode"] == cutting.CUT_MODE_BOUNDARY
    assert document["settings"] == boundaries.default_settings()
    assert document["config_fingerprint"] == run["config_fingerprint"]

    mapped = document["clips"][0]
    assert mapped["source_id"] == ids[0]
    assert mapped["source_fingerprint"]["digest"] == run["sources"][0]["fingerprint"]["digest"]
    assert mapped["source_duration_seconds"] == pytest.approx(10.0, abs=0.05)
    assert len(mapped["retained"]) == 1
    assert mapped["retained"][0]["source_start"] == boundary["start_seconds"]
    assert mapped["retained"][0]["source_end"] == boundary["end_seconds"]
    assert [piece["position"] for piece in mapped["removed"]] == ["leading", "trailing"]
    assert mapped["removed"][0] == {
        "source_start": 0.0, "source_end": boundary["start_seconds"], "position": "leading",
    }
    assert abs(mapped["difference_seconds"]) <= mapped["tolerance_seconds"]

    # Concatenated offsets: each clip starts where the previous one ended, and
    # the sum is the joined file's measured length.
    clips = document["clips"]
    assert clips[0]["sequence_start_seconds"] == 0.0
    assert clips[1]["sequence_start_seconds"] == clips[0]["sequence_end_seconds"]
    assert clips[0]["sequence_end_seconds"] == pytest.approx(
        run["clips"][0]["duration_seconds"], abs=0.01
    )
    sequence = document["sequence"]
    assert sequence["rendered"] and sequence["within_tolerance"]
    combined = media.probe(
        str(cutting.resolve_output(project["id"], run["run_id"], "combined")[0])
    )
    assert combined["duration_seconds"] == pytest.approx(
        clips[1]["sequence_end_seconds"], abs=sequence["tolerance_seconds"]
    )

    # A source time maps to where the sound really is in the joined file: the
    # second take's first sound, 1.5 s into its source.
    second_clip = clips[1]
    interval = second_clip["retained"][0]
    expected = second_clip["sequence_start_seconds"] + (1.5 - interval["source_start"])
    sounds = boundaries.find_runs(
        boundaries.decode_envelope(
            str(cutting.resolve_output(project["id"], run["run_id"], "combined")[0]),
            0.0, combined["duration_seconds"], channels=combined["audio"]["channels"],
        ),
        0.04,
    )
    assert any(start == pytest.approx(expected, abs=0.08) for start, _end in sounds)

    # The originals are untouched, and the outputs are registered as products.
    assert first.exists() and second.exists()
    generated = client.get(f"/projects/{project['id']}/resources").json()["generated"]
    assert {entry["produced_by"] for entry in generated} == {capabilities.TRIM_BOUNDARIES}


@needs_tools
def test_previews_and_the_final_run_are_cut_at_the_same_boundaries(
    client, project, tmp_path
):
    ids = [
        add_source(client, project["id"], make_take(tmp_path / "a.mp4")),
        add_source(
            client, project["id"],
            make_take(tmp_path / "b.mp4", seconds=9, audible=[(1.5, 6.0)]),
        ),
    ]
    body = {"source_ids": ids, "sample_seconds": 2.0, "output_mode": "clips"}

    sampled = submit(client, project["id"], "samples", body)
    assert sampled["status"] == jobs.SUCCEEDED, sampled.get("error")
    # Every preview the selection allows: two openings, two endings, one join.
    assert sampled["result"]["sample_count"] == 5
    assert "analysis_seconds" in sampled["result"] and "render_seconds" in sampled["result"]

    current = state(client, project["id"], body)
    samples = {cut_samples.slot(sample): sample for sample in current["samples"]}
    assert not any(sample["stale"] for sample in samples.values())

    ran = submit(client, project["id"], "runs", body)
    run = client.get(
        f"/projects/{project['id']}/cutting/runs/{ran['result']['run_id']}"
    ).json()
    final = {
        clip["source_id"]: (clip["boundary"]["start_seconds"], clip["boundary"]["end_seconds"])
        for clip in run["clips"]
    }

    for sample in samples.values():
        for source in sample["sources"]:
            used = (source["boundary"]["start_seconds"], source["boundary"]["end_seconds"])
            assert used == final[source["source_id"]]
        assert sample["basis"]["boundary_settings"] == run["boundary_settings"]

    start, end = final[ids[0]]

    opening = samples["opening:%s" % ids[0]]
    assert opening["edited"]["segments"] == [
        {"source_id": ids[0], "source_start_seconds": start, "source_end_seconds": start + 2.0}
    ]
    assert opening["edited"]["duration_seconds"] == pytest.approx(2.0, abs=0.09)
    # The source comparison begins before the cut and marks where it falls.
    context = opening["source_context"]
    assert context["source_start_seconds"] == 0.0
    assert context["cut_at_seconds"] == pytest.approx(start, abs=0.001)
    assert context["duration_seconds"] == pytest.approx(start + 2.0, abs=0.09)

    ending = samples["ending:%s" % ids[0]]
    assert ending["edited"]["segments"][0]["source_end_seconds"] == end
    assert ending["source_context"]["source_end_seconds"] == pytest.approx(10.0, abs=0.05)
    assert ending["source_context"]["cut_at_seconds"] == pytest.approx(2.0, abs=0.001)

    join = samples["join:%s:%s" % tuple(ids)]
    assert [segment["source_id"] for segment in join["edited"]["segments"]] == ids
    assert join["edited"]["segments"][0]["source_end_seconds"] == end
    assert join["edited"]["segments"][1]["source_start_seconds"] == final[ids[1]][0]
    assert join["edited"]["join_at_seconds"] == 2.0
    assert join["edited"]["duration_seconds"] == pytest.approx(4.0, abs=0.12)
    assert join["source_context"] is None

    # Served by id, with seeking, and only the files a preview really has.
    base = f"/projects/{project['id']}/cutting/samples"
    served = client.get(
        f"{base}/{opening['sample_id']}/edited/stream", headers={"Range": "bytes=0-99"}
    )
    assert served.status_code == 206 and len(served.content) == 100
    assert client.get(f"{base}/{opening['sample_id']}/source/stream").status_code == 200
    assert client.get(f"{base}/{join['sample_id']}/source/stream").status_code == 404
    assert client.get(f"{base}/{opening['sample_id']}/sample.json/stream").status_code == 404
    assert client.get(f"{base}/000000000000/edited/stream").status_code == 404


@needs_tools
def test_a_preview_of_a_clip_shorter_than_the_preview_length_shows_all_of_it(
    client, project, tmp_path
):
    take = make_take(tmp_path / "short.mp4", seconds=3, audible=[(0.5, 2.0)])
    source = add_source(client, project["id"], take)
    body = {
        "source_ids": [source], "sample_seconds": 10,
        "samples": [{"kind": "opening", "source_id": source}],
    }

    record = submit(client, project["id"], "samples", body)
    assert record["status"] == jobs.SUCCEEDED, record.get("error")

    sample = state(client, project["id"], body)["samples"][0]
    retained = sample["sources"][0]["boundary"]["retained_seconds"]
    assert retained < 10
    assert sample["edited"]["duration_seconds"] == pytest.approx(retained, abs=0.09)
    assert any("shorter than" in note for note in sample["notes"])


@needs_tools
def test_changing_settings_or_a_boundary_makes_previews_stale(client, project, tmp_path):
    ids = [
        add_source(client, project["id"], make_take(tmp_path / "a.mp4")),
        add_source(client, project["id"], make_take(tmp_path / "b.mp4", tone=500)),
    ]
    body = {"source_ids": ids, "sample_seconds": 1.0}
    assert submit(client, project["id"], "samples", body)["status"] == jobs.SUCCEEDED

    def stale(changes) -> dict:
        current = state(client, project["id"], {**body, **changes})
        return {cut_samples.slot(sample): sample["stale"] for sample in current["samples"]}

    assert not any(stale({}).values())
    # The preview length is not a boundary: nothing goes stale.
    assert not any(stale({"sample_seconds": 3.0}).values())
    # Tuning the mode that is not selected changes nothing either.
    assert not any(stale({"settings": {"audio_threshold": 0.2}}).values())

    # A boundary setting invalidates every preview.
    assert all(stale({"boundary_settings": {"trailing_padding_seconds": 0.8}}).values())

    # A manual boundary on the second clip invalidates that clip's previews
    # and the join it takes part in — and leaves the first clip's alone.
    moved = stale({"overrides": {ids[1]: {"start_seconds": 1.0}}})
    assert moved["opening:%s" % ids[1]] and moved["ending:%s" % ids[1]]
    assert moved["join:%s:%s" % tuple(ids)]
    assert not moved["opening:%s" % ids[0]] and not moved["ending:%s" % ids[0]]

    # Reordering breaks the join, deselecting drops the clip, and switching
    # mode leaves nothing current.
    reordered = stale({"source_ids": list(reversed(ids))})
    assert reordered["join:%s:%s" % tuple(ids)]
    assert not reordered["opening:%s" % ids[0]]
    assert stale({"source_ids": [ids[0]]})["ending:%s" % ids[1]]
    assert all(stale({"mode": "full_clip"}).values())

    # The reason is stated, and a fresh render is current again.
    changed = {**body, "boundary_settings": {"trailing_padding_seconds": 0.8}}
    assert "changed" in state(client, project["id"], changed)["samples"][0]["stale_reason"]
    assert submit(client, project["id"], "samples", changed)["status"] == jobs.SUCCEEDED
    assert not any(
        sample["stale"] for sample in state(client, project["id"], changed)["samples"]
    )
    # The earlier previews were kept, not overwritten.
    assert len(cut_samples.list_samples(project["id"])) == 10


@needs_tools
def test_a_re_recorded_take_makes_its_previews_and_analysis_stale(client, project, tmp_path):
    take = make_take(tmp_path / "take.mp4")
    source = add_source(client, project["id"], take)
    body = {"source_ids": [source], "sample_seconds": 1.0,
            "samples": [{"kind": "opening", "source_id": source}]}
    assert submit(client, project["id"], "samples", body)["status"] == jobs.SUCCEEDED
    assert not state(client, project["id"], body)["samples"][0]["stale"]

    # The same file name, different bytes.
    make_take(take, audible=[(4.0, 9.0)], tone=300)

    current = state(client, project["id"], body)
    assert current["samples"][0]["stale"]
    assert current["analysis_needed"]


@needs_tools
def test_the_state_reports_boundaries_durations_and_what_needs_a_look(
    client, project, tmp_path
):
    ids = [
        add_source(client, project["id"], make_take(tmp_path / "good.mp4")),
        add_source(client, project["id"], make_take(tmp_path / "silent.mp4", audible=[])),
        add_source(client, project["id"], make_take(tmp_path / "mute.mp4", audio=False)),
        add_source(client, project["id"], make_take(tmp_path / "noisy.mp4", floor=0.25)),
    ]
    body = {"source_ids": ids}

    before = state(client, project["id"], body)
    assert before["mode"] == cutting.CUT_MODE_BOUNDARY
    assert before["analysis_needed"] and before["retained_seconds"] is None
    assert [clip["analysed"] for clip in before["clips"]] == [False] * 4

    analysed = submit(client, project["id"], "analysis", body)
    assert analysed["status"] == jobs.SUCCEEDED, analysed.get("error")
    assert analysed["result"]["clip_count"] == 4
    assert analysed["result"]["needs_review_count"] == 3

    after = state(client, project["id"], body)
    assert not after["analysis_needed"]
    good, silent, mute, noisy = after["clips"]

    assert good["boundary"]["status"] == boundaries.STATUS_DETECTED
    assert not good["boundary"]["needs_review"]
    assert good["boundary"]["retained_seconds"] == pytest.approx(6.0, abs=0.1)

    # None of these is dropped or emptied: each is kept whole and flagged.
    for clip, code in ((silent, "no_activity"), (mute, "no_audio")):
        assert clip["boundary"]["retained_seconds"] == pytest.approx(10.0, abs=0.05)
        assert clip["boundary"]["needs_review"]
        assert clip["boundary"]["warnings"][0]["code"] == code

    assert noisy["boundary"]["status"] == boundaries.STATUS_DETECTED
    assert "background_near_threshold" in {w["code"] for w in noisy["boundary"]["warnings"]}

    assert after["needs_review_count"] == 3
    assert after["original_seconds"] == pytest.approx(40.0, abs=0.2)
    assert after["retained_seconds"] == pytest.approx(
        sum(clip["boundary"]["retained_seconds"] for clip in after["clips"])
    )

    # A second analysis decodes nothing: the cache answers.
    again = submit(client, project["id"], "analysis", body)
    assert again["result"]["reused_count"] == 4
    assert again["result"]["decoded_seconds"] == 0

    # An invalid manual boundary is reported on its own clip, with a reason.
    broken = state(client, project["id"], {**body, "overrides": {ids[0]: {"end_seconds": 99}}})
    assert "past the end" in broken["clips"][0]["error"]
    assert broken["invalid_count"] == 1
    assert broken["clips"][1]["boundary"] is not None

    # And it is refused before any job is queued.
    refused = client.post(
        f"/projects/{project['id']}/cutting/runs",
        json={**body, "overrides": {ids[0]: {"end_seconds": 99}}},
    )
    assert refused.status_code == 400 and "past the end" in refused.json()["detail"]


@needs_tools
def test_end_to_end_uncertain_clips_are_rendered_whole_and_flagged(
    client, project, tmp_path
):
    """Silent, no audio track, noisy: deliberate behaviour for each, none lost."""
    ids = [
        add_source(client, project["id"], make_take(tmp_path / "silent.mp4", seconds=4, audible=[])),
        add_source(client, project["id"], make_take(tmp_path / "mute.mp4", seconds=4, audio=False)),
        add_source(client, project["id"], make_take(tmp_path / "good.mp4")),
    ]

    record = submit(client, project["id"], "runs", {"source_ids": ids, "output_mode": "both"})
    assert record["status"] == jobs.SUCCEEDED, record.get("error")
    assert record["result"]["clip_count"] == 3
    assert record["result"]["needs_review_count"] == 2
    assert "need a look" in record["result"]["summary"]

    run = client.get(
        f"/projects/{project['id']}/cutting/runs/{record['result']['run_id']}"
    ).json()
    silent, mute, good = run["clips"]

    for clip in (silent, mute):
        assert clip["status"] == cutting.CLIP_SUCCEEDED
        assert clip["duration_seconds"] == pytest.approx(4.0, abs=0.09)
        assert clip["boundary"]["needs_review"]
        assert clip["cut_map"]["available"] and clip["cut_map"]["removed"] == []

    # The clip with no audio got a silent track, so the three can be joined.
    assert mute["audio_synthesised"] is True and mute["audio"]["codec_name"] == "aac"
    assert run["combined"]["status"] == cutting.CLIP_SUCCEEDED
    assert run["combined"]["complete"] is True
    assert len(run["notes"]) == 2


@needs_tools
def test_end_to_end_manual_boundaries_are_what_gets_rendered(client, project, tmp_path):
    source = add_source(client, project["id"], make_take(tmp_path / "take.mp4"))
    body = {
        "source_ids": [source],
        "overrides": {source: {"start_seconds": 3.0, "end_seconds": 9.0}},
        "output_mode": "clips",
    }

    record = submit(client, project["id"], "runs", body)
    assert record["status"] == jobs.SUCCEEDED, record.get("error")

    clip = client.get(
        f"/projects/{project['id']}/cutting/runs/{record['result']['run_id']}"
    ).json()["clips"][0]

    assert (clip["boundary"]["start_seconds"], clip["boundary"]["end_seconds"]) == (3.0, 9.0)
    assert clip["boundary"]["start_origin"] == boundaries.ORIGIN_MANUAL
    assert clip["duration_seconds"] == pytest.approx(6.0, abs=0.09)
    assert clip["cut_map"]["retained"][0]["source_start"] == 3.0

    # Keeping the original clip is always available.
    whole = submit(
        client, project["id"], "runs",
        {**body, "overrides": {source: {"keep_whole": True}}},
    )
    kept = client.get(
        f"/projects/{project['id']}/cutting/runs/{whole['result']['run_id']}"
    ).json()["clips"][0]
    assert kept["duration_seconds"] == pytest.approx(10.0, abs=0.09)
    assert kept["boundary"]["start_origin"] == boundaries.ORIGIN_KEPT


@needs_auto_editor
def test_end_to_end_full_clip_mode_still_cuts_inside_and_maps_every_interval(
    client, project, tmp_path
):
    """The explicit alternative: the internal pause goes, and the map says where."""
    source = add_source(client, project["id"], make_take(tmp_path / "take.mp4"))
    body = {"source_ids": [source], "mode": "full_clip", "output_mode": "clips"}

    record = submit(client, project["id"], "runs", body)
    assert record["status"] == jobs.SUCCEEDED, record.get("error")
    assert record["result"]["mode"] == cutting.CUT_MODE_FULL

    run = client.get(
        f"/projects/{project['id']}/cutting/runs/{record['result']['run_id']}"
    ).json()
    clip = run["clips"][0]

    assert run["settings"] == cutting.default_settings()
    assert run["boundary_settings"] is None and "boundary" not in clip
    assert clip["command"].startswith("auto-editor.exe")

    mapped = clip["cut_map"]
    assert mapped["available"] is True
    assert mapped["origin"] == cut_map.SOURCE_AUTO_EDITOR
    # Two retained islands and the pause between them removed.
    assert len(mapped["retained"]) == 2
    assert [piece["position"] for piece in mapped["removed"]] == [
        "leading", "internal", "trailing",
    ]
    assert mapped["retained"][0]["source_start"] == pytest.approx(2.0, abs=0.1)
    assert mapped["retained"][1]["source_start"] == pytest.approx(5.5, abs=0.1)
    assert abs(mapped["difference_seconds"]) <= mapped["tolerance_seconds"]
    assert mapped["retained"][1]["output_end"] == pytest.approx(clip["duration_seconds"], abs=0.09)

    # No preview samples in this mode, and the reason is given.
    refused = client.post(f"/projects/{project['id']}/cutting/samples", json=body)
    assert refused.status_code == 400
    assert "boundary-only" in refused.json()["detail"]


# =============================================================================
# The recommendation flow
# =============================================================================


class ScriptedProvider:
    """Stands in for a real provider. Nothing leaves the machine."""

    id = "scripted"
    label = "Scripted provider"
    is_mock = False
    model = "test-model"

    def __init__(self, answer):
        self.answer = answer
        self.requests: list[dict] = []

    def describe(self):
        return {"id": self.id, "label": self.label, "model": self.model, "is_mock": False}

    def recommend_cut(self, request):
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


REQUEST = "Make the joins slightly tighter without clipping the first or last word."


@needs_tools
def test_a_recommendation_is_an_editable_plan_that_must_be_approved_to_apply(
    client, project, tmp_path
):
    source = add_source(client, project["id"], make_take(tmp_path / "take.mp4"))
    body = {"source_ids": [source], "output_mode": "both"}

    record = submit(client, project["id"], "recommendations", {**body, "request": REQUEST})
    assert record["status"] == jobs.SUCCEEDED, record.get("error")
    assert record["result"]["mode"] == cutting.CUT_MODE_BOUNDARY
    assert record["result"]["provider"]["is_mock"] is True

    # It is an ordinary plan: listed, versioned, not yet approved.
    plan = client.get(
        f"/projects/{project['id']}/plans/{record['result']['plan_id']}"
    ).json()
    assert plan["origin"] == "ai" and plan["approved"] is False
    assert plan["actions"][0]["capability_id"] == capabilities.TRIM_BOUNDARIES
    assert plan["actions"][0]["resource_ids"] == [source]
    assert plan["context"]["kind"] == cut_advice.CONTEXT_KIND
    assert plan["context"]["evidence"].startswith("measurements only")

    recommendation = state(client, project["id"], body)["recommendation"]
    assert recommendation["stale"] is False and recommendation["applied"] is False
    assert recommendation["mode_changed"] is False
    assert recommendation["limitations"]
    proposed = recommendation["settings"]
    assert proposed["trailing_padding_seconds"] < DEFAULTS["trailing_padding_seconds"]

    # Nothing has changed yet: the form still holds what the user set.
    settings_url = f"/projects/{project['id']}/cutting/settings"
    assert client.get(settings_url).json()["mode_is_explicit"] is False

    # The user edits the proposal: a new revision, which needs approval again.
    edited = dict(plan["actions"][0])
    edited["parameters"] = {**edited["parameters"], "trailing_padding_seconds": 0.3}
    revised = client.post(
        f"/projects/{project['id']}/plans/{plan['plan_id']}/revisions",
        json={"summary": plan["summary"], "actions": [edited]},
    ).json()
    assert revised["revision"] == 2 and revised["approved"] is False
    assert revised["context"]["based_on_fingerprint"] == plan["context"]["based_on_fingerprint"]

    # The superseded revision cannot be applied.
    apply_url = f"/projects/{project['id']}/cutting/recommendations/{plan['plan_id']}/revisions"
    assert client.post(f"{apply_url}/1/apply", json=body).status_code == 409

    applied = client.post(f"{apply_url}/2/apply", json=body)
    assert applied.status_code == 200, applied.text
    saved = applied.json()
    assert saved["mode"] == cutting.CUT_MODE_BOUNDARY
    assert saved["boundary_settings"]["trailing_padding_seconds"] == 0.3
    assert saved["applied_plan"] == {"plan_id": plan["plan_id"], "revision": 2}

    # Applying approved it through the existing approval record…
    assert client.get(
        f"/projects/{project['id']}/plans/{plan['plan_id']}"
    ).json()["approved"] is True
    # …and rendered nothing: that stays an explicit action.
    assert client.get(f"/projects/{project['id']}/cutting/runs").json()["runs"] == []

    # With the applied settings in the form, the recommendation reads as
    # applied; after a further manual edit the label is dropped.
    form = {**body, "boundary_settings": saved["boundary_settings"],
            "applied_plan": saved["applied_plan"]}
    current = state(client, project["id"], form)
    assert current["recommendation"]["applied"] and current["applied_plan"]

    nudged = {**form, "boundary_settings": {**saved["boundary_settings"],
                                           "leading_padding_seconds": 0.4}}
    assert state(client, project["id"], nudged)["applied_plan"] is None

    # A run from the applied form says where its settings came from.
    ran = submit(client, project["id"], "runs", form)
    run = client.get(
        f"/projects/{project['id']}/cutting/runs/{ran['result']['run_id']}"
    ).json()
    assert run["applied_plan"] == saved["applied_plan"]
    assert run["settings_revision"] == saved["revision"]
    assert run["boundary_settings"]["trailing_padding_seconds"] == 0.3


@needs_tools
def test_a_stale_recommendation_is_detected_and_cannot_be_applied(client, project, tmp_path):
    source = add_source(client, project["id"], make_take(tmp_path / "take.mp4"))
    body = {"source_ids": [source]}
    record = submit(client, project["id"], "recommendations", {**body, "request": REQUEST})
    plan_id, revision = record["result"]["plan_id"], record["result"]["revision"]
    url = (
        f"/projects/{project['id']}/cutting/recommendations/{plan_id}"
        f"/revisions/{revision}/apply"
    )

    for changed in (
        {**body, "boundary_settings": {"detection_threshold": 0.1}},
        {**body, "overrides": {source: {"start_seconds": 1.0}}},
    ):
        recommendation = state(client, project["id"], changed)["recommendation"]
        assert recommendation["stale"] is True
        assert "changed" in recommendation["stale_reason"]

        refused = client.post(url, json=changed)
        assert refused.status_code == 409
        assert "no longer describes" in refused.json()["detail"]

    # Nothing was approved or saved by the refused attempts.
    assert client.get(f"/projects/{project['id']}/plans/{plan_id}").json()["approved"] is False
    assert client.get(
        f"/projects/{project['id']}/cutting/settings"
    ).json()["mode_is_explicit"] is False


@needs_tools
def test_a_request_cannot_silently_switch_to_internal_silence_removal(
    client, project, tmp_path, monkeypatch
):
    provider = ScriptedProvider(
        {
            "supported": True,
            "mode": "full_clip",
            "settings": {"min_silence_seconds": 0.4},
            "explanation": "Removing the pauses inside each take makes it tighter.",
            "limitations": ["This changes the pacing of the delivery."],
        }
    )
    monkeypatch.setattr(llm, "get_provider", lambda: provider)

    source = add_source(client, project["id"], make_take(tmp_path / "take.mp4"))
    body = {"source_ids": [source]}
    record = submit(client, project["id"], "recommendations", {**body, "request": "tighter"})

    assert record["status"] == jobs.SUCCEEDED, record.get("error")
    assert record["result"]["mode_changed"] is True
    assert record["result"]["summary"].startswith("This proposal changes the cutting mode")

    # What the provider was sent: statistics of the clip, and nothing of it.
    sent = provider.requests[0]
    assert sent["current"]["mode"] == cutting.CUT_MODE_BOUNDARY
    assert sent["clips"][0]["first_sound_at_seconds"] == pytest.approx(2.0, abs=0.05)
    assert sent["clips"][0]["peak_level"] is not None
    assert "take.mp4" not in json.dumps(sent, ensure_ascii=False)
    assert str(tmp_path) not in json.dumps(sent, ensure_ascii=False)

    recommendation = state(client, project["id"], body)["recommendation"]
    assert recommendation["mode_changed"] and recommendation["mode"] == cutting.CUT_MODE_FULL

    url = (
        f"/projects/{project['id']}/cutting/recommendations/{record['result']['plan_id']}"
        f"/revisions/{record['result']['revision']}/apply"
    )
    refused = client.post(url, json=body)
    assert refused.status_code == 409
    assert "Confirm the mode change" in refused.json()["detail"]
    assert client.get(
        f"/projects/{project['id']}/cutting/settings"
    ).json()["mode"] == cutting.CUT_MODE_BOUNDARY

    # Only an explicit confirmation switches the mode.
    confirmed = client.post(url, json={**body, "confirm_mode_change": True})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["mode"] == cutting.CUT_MODE_FULL
    assert confirmed.json()["settings"]["min_silence_seconds"] == 0.4
    # The boundary settings were left as they were.
    assert confirmed.json()["boundary_settings"] == boundaries.default_settings()


@needs_tools
@pytest.mark.parametrize(
    "answer, expected",
    [
        ({"supported": True, "mode": "boundary", "explanation": "x",
          "settings": {"trailing_padding_seconds": 99}}, "rejected in validation"),
        ({"supported": True, "mode": "boundary", "explanation": "x",
          "settings": {"script": "del *"}}, "rejected in validation"),
        (llm.ProviderError("The AI provider call exceeded its time budget."), "time budget"),
    ],
)
def test_a_bad_answer_fails_the_job_and_creates_no_plan(
    client, project, tmp_path, monkeypatch, answer, expected
):
    monkeypatch.setattr(llm, "get_provider", lambda: ScriptedProvider(answer))
    source = add_source(client, project["id"], make_take(tmp_path / "take.mp4"))

    record = submit(
        client, project["id"], "recommendations", {"source_ids": [source], "request": "x"}
    )

    assert record["status"] == jobs.FAILED
    assert expected in record["error"]
    assert client.get(f"/projects/{project['id']}/plans").json()["plans"] == []
    assert state(client, project["id"], {"source_ids": [source]})["recommendation"] is None


@needs_tools
def test_a_request_the_settings_cannot_meet_is_declined_without_a_plan(
    client, project, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        llm, "get_provider",
        lambda: ScriptedProvider(
            {"supported": False, "explanation": "Choosing the best take is not a cutting setting."}
        ),
    )
    source = add_source(client, project["id"], make_take(tmp_path / "take.mp4"))
    record = submit(
        client, project["id"], "recommendations",
        {"source_ids": [source], "request": "keep only my best take"},
    )

    assert record["status"] == jobs.SUCCEEDED
    assert record["result"]["supported"] is False
    assert client.get(f"/projects/{project['id']}/plans").json()["plans"] == []


@needs_tools
def test_feedback_revises_the_same_plan_once(client, project, tmp_path):
    source = add_source(client, project["id"], make_take(tmp_path / "take.mp4"))
    body = {"source_ids": [source]}
    first = submit(client, project["id"], "recommendations", {**body, "request": REQUEST})
    plan_id = first["result"]["plan_id"]

    # A revision has to say what to change.
    refused = client.post(
        f"/projects/{project['id']}/cutting/recommendations",
        json={**body, "request": REQUEST, "revise_plan_id": plan_id},
    )
    assert refused.status_code == 400 and "feedback" in refused.json()["detail"]

    second = submit(
        client, project["id"], "recommendations",
        {**body, "request": REQUEST, "revise_plan_id": plan_id,
         "feedback": "Leave a little more breathing room after each section."},
    )
    assert second["status"] == jobs.SUCCEEDED, second.get("error")
    assert (second["result"]["plan_id"], second["result"]["revision"]) == (plan_id, 2)

    recommendation = state(client, project["id"], body)["recommendation"]
    assert recommendation["revision"] == 2
    assert recommendation["feedback"].startswith("Leave a little more")
    # One request produced one proposal: no job was queued behind it.
    queued = client.get(f"/projects/{project['id']}/jobs").json()["jobs"]
    assert len(queued) == 2 and all(job["type"] == "cut_recommendation" for job in queued)


def test_manual_cutting_needs_no_provider_and_no_approval(
    client, project, stub_sources, monkeypatch
):
    """The AI is an optional route: a broken provider must not block a cut."""
    monkeypatch.setenv(PROVIDER_ENV_VAR, "anthropic")  # configured, with no key
    monkeypatch.setattr(boundaries, "decode_envelope", synthetic_decode)
    monkeypatch.setattr(
        media, "verify_output",
        lambda path, require_audio=True: {
            "filename": Path(path).name, "duration_seconds": 6.04,
            "has_video": True, "has_audio": True,
            "video": {"codec_name": "h264", "width": 640, "height": 360,
                      "pix_fmt": "yuv420p", "frame_rate": 25.0},
            "audio": {"codec_name": "aac", "sample_rate": 48000, "channels": 2},
        },
    )

    def render(argv, *, cancelled=None, on_output=None, **kwargs):
        Path(argv[-1]).parent.mkdir(parents=True, exist_ok=True)
        Path(argv[-1]).write_bytes(b"stub")
        return processes.ProcessResult(0, "out_time_us=6040000", False)

    monkeypatch.setattr(processes, "run", render)

    record = submit(
        client, project["id"], "runs", {"source_ids": stub_sources, "output_mode": "clips"}
    )
    assert record["status"] == jobs.SUCCEEDED, record.get("error")
    assert client.get(f"/projects/{project['id']}/plans").json()["plans"] == []

    # The recommendation, by contrast, fails with a readable reason.
    advice = submit(
        client, project["id"], "recommendations",
        {"source_ids": stub_sources, "request": "tighter"},
    )
    assert advice["status"] == jobs.FAILED
    assert API_KEY_ENV_VAR in advice["error"]


@needs_tools
def test_an_approved_trim_plan_runs_through_the_existing_plan_execution(
    client, project, tmp_path
):
    """The same capability from the plan panel, with saved manual boundaries."""
    source = add_source(client, project["id"], make_take(tmp_path / "take.mp4"))
    body = {"source_ids": [source], "output_mode": "clips",
            "overrides": {source: {"start_seconds": 1.0}}}
    client.put(f"/projects/{project['id']}/cutting/settings", json=body)

    advice = submit(client, project["id"], "recommendations", {**body, "request": REQUEST})
    plan_id, revision = advice["result"]["plan_id"], advice["result"]["revision"]
    base = f"/projects/{project['id']}/plans/{plan_id}/revisions/{revision}"

    # Unapproved, it cannot run.
    assert client.post(f"{base}/execute").status_code == 400

    assert client.post(f"{base}/approve").status_code == 200
    executed = client.post(f"{base}/execute")
    assert executed.status_code == 201, executed.text
    record = wait_for_job(client, project["id"], executed.json()["id"])
    assert record["status"] == jobs.SUCCEEDED, record.get("error")

    run = client.get(f"/projects/{project['id']}/cutting/runs").json()["runs"][0]
    assert run["mode"] == cutting.CUT_MODE_BOUNDARY
    assert run["plan"] == {"plan_id": plan_id, "revision": revision}
    assert run["boundary_settings"] == advice["result"]["settings"]
    # The manual start saved in the cutting screen was respected.
    assert run["clips"][0]["boundary"]["start_seconds"] == 1.0
    assert run["clips"][0]["boundary"]["start_origin"] == boundaries.ORIGIN_MANUAL
