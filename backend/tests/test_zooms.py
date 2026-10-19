"""Tests for stage 2B: automatic zoom planning (Gemini) and rendering.

- **Pure** — the preset catalog, plan validation and normalisation, crop
  geometry, the filter graph and command, the request sent to the model.
- **Gemini adapter** — request shape against the real SDK types (no network),
  error classification, bounded retries, key redaction, no paid fallback.
- **Pipeline** — the zoom job end to end over a generated grid video, with
  Whisper and the provider replaced by stand-ins: frames inspected around
  every change, frame count / duration / audio checked, reuse, repeated
  clicks, retries, cancellation, stale inputs, approval.
- **Captions** — lineage to the clean parent, and a real Remotion render
  proving captions keep their size and position over the zoomed video.

No test calls Gemini. A live smoke test lives in
`backend/scripts/gemini_smoke_test.py` and runs only when invoked by hand.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import (  # noqa: E402
    caption_inputs,
    caption_jobs,
    captions,
    gemini,
    jobs,
    processes,
    whisper_service,
    zoom_jobs,
    zoom_planner,
    zooms,
)
from backend.config import (  # noqa: E402
    DEFAULT_GEMINI_MODEL,
    GEMINI_API_KEY_ENV_VAR,
    WORKSPACE_ENV_VAR,
    ZOOM_PROVIDER_ENV_VAR,
)
from backend.llm import ProviderError  # noqa: E402
from backend.main import app  # noqa: E402
from backend.tests.test_captions import FakeWhisper  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg.exe") and shutil.which("ffprobe.exe")), reason="FFmpeg / FFprobe are not on PATH"
)
needs_renderer = pytest.mark.skipif(
    not caption_jobs.renderer_status()["ready"], reason="The Remotion renderer is not prepared"
)

FAKE_KEY = "AIzaTESTKEY_0123456789abcdefghijklmnop"


# --- pure: presets and validation ----------------------------------------------------


def test_the_preset_catalog_is_the_legacy_one_and_fixed():
    assert zoom_planner.PRESET_SCALE == {
        "NORMAL": 1.00, "PUNCH_LIGHT": 1.08, "PUNCH_MEDIUM": 1.12, "PUNCH_STRONG": 1.20,
    }
    item = zoom_planner.RESPONSE_SCHEMA["properties"]["intervals"]["items"]
    assert item["properties"]["preset"]["enum"] == ["NORMAL", "PUNCH_LIGHT", "PUNCH_MEDIUM", "PUNCH_STRONG"]
    # The model cannot send a scale: there is no field for one.
    assert set(item["properties"]) == {"start", "end", "preset", "reason"}


def plan_of(*intervals, summary="A calm style."):
    return {"summary": summary, "intervals": [
        {"start": s, "end": e, "preset": p, "reason": "r"} for s, e, p in intervals
    ]}


def test_ai_chosen_variable_durations_are_kept_as_given():
    raw = plan_of((0, 0.4, "NORMAL"), (0.4, 7.32, "PUNCH_LIGHT"), (7.32, 7.6, "PUNCH_STRONG"), (7.6, 30.0, "NORMAL"))
    plan, errors = zoom_planner.validate_plan(raw, duration_seconds=30.0, fps=25.0)
    assert errors == []
    assert [(i["start"], i["end"], i["preset"]) for i in plan["intervals"]] == [
        (0.0, 0.4, "NORMAL"), (0.4, 7.32, "PUNCH_LIGHT"), (7.32, 7.6, "PUNCH_STRONG"), (7.6, 30.0, "NORMAL"),
    ]
    assert plan["normalisation"]["max_shift_seconds"] == 0.0


def test_a_single_interval_and_long_stretches_are_valid_no_pattern_is_imposed():
    plan, errors = zoom_planner.validate_plan(plan_of((0, 95.5, "NORMAL")), duration_seconds=95.5, fps=30.0)
    assert errors == [] and len(plan["intervals"]) == 1
    plan, errors = zoom_planner.validate_plan(
        plan_of((0, 80, "NORMAL"), (80, 95.5, "PUNCH_MEDIUM")), duration_seconds=95.5, fps=30.0
    )
    assert errors == [] and [i["end"] for i in plan["intervals"]] == [80.0, 95.5]


def test_boundaries_snap_to_the_frame_grid_by_at_most_half_a_frame():
    plan, errors = zoom_planner.validate_plan(
        plan_of((0, 1.013, "NORMAL"), (1.013, 2.0, "PUNCH_LIGHT")), duration_seconds=2.0, fps=30.0
    )
    assert errors == []
    assert plan["intervals"][0]["end"] == pytest.approx(1.0)  # 1.013 s is frame 30.39 → frame 30
    assert plan["normalisation"]["max_shift_seconds"] <= 0.5 / 30 + 1e-9


def test_hairline_gaps_are_closed_and_recorded_but_real_gaps_are_rejected():
    plan, errors = zoom_planner.validate_plan(
        plan_of((0.02, 1.0, "NORMAL"), (1.03, 2.0, "PUNCH_LIGHT"), (2.0, 2.98, "NORMAL")), duration_seconds=3.0, fps=25.0
    )
    assert errors == []
    assert plan["intervals"][0]["start"] == 0 and plan["intervals"][-1]["end"] == 3.0
    assert any("gap" in note for note in plan["normalisation"]["notes"])

    _plan, errors = zoom_planner.validate_plan(
        plan_of((0, 1.0, "NORMAL"), (1.5, 3.0, "PUNCH_LIGHT")), duration_seconds=3.0, fps=25.0
    )
    assert errors and "gap" in errors[0]


@pytest.mark.parametrize(
    "raw, fragment",
    [
        (plan_of((0, 3, "ZOOM_150")), "not a preset"),
        (plan_of((0, 3, "punch_light")), "not a preset"),
        ({"summary": "s", "intervals": [{"start": 0, "end": 3, "preset": "NORMAL", "scale": 1.5}]}, "unknown field"),
        ({"summary": "s", "intervals": [{"start": 0, "end": 3, "preset": "NORMAL", "command": "del"}]}, "unknown field"),
        ({"summary": "s", "intervals": [{"start": float("nan"), "end": 3, "preset": "NORMAL"}]}, "finite"),
        ({"summary": "s", "intervals": [{"start": "0", "end": 3, "preset": "NORMAL"}]}, "finite"),
        ({"summary": "s", "intervals": [{"start": True, "end": 3, "preset": "NORMAL"}]}, "finite"),
        (plan_of((0, 2, "NORMAL"), (1.5, 3, "PUNCH_LIGHT")), "overlap"),
        (plan_of((0, 1, "NORMAL"), (2, 3, "PUNCH_LIGHT"), (1, 2, "NORMAL")), ""),
        (plan_of((0, 5, "NORMAL")), "past the end"),
        (plan_of((1, 3, "NORMAL")), "must start at 0"),
        (plan_of((0, 2, "NORMAL")), "must end"),
        (plan_of((0, 1, "NORMAL"), (1, 0.5, "NORMAL")), "before it starts"),
        ({"summary": "", "intervals": []}, ""),
        ({"summary": "s", "intervals": [], "shell": "x"}, ""),
        ("not an object", ""),
    ],
)
def test_invalid_plans_are_rejected_with_a_reason(raw, fragment):
    plan, errors = zoom_planner.validate_plan(raw, duration_seconds=3.0, fps=25.0)
    assert plan is None and errors
    assert fragment.lower() in " ".join(errors).lower()


def test_an_interval_shorter_than_a_frame_is_rejected():
    _plan, errors = zoom_planner.validate_plan(
        plan_of((0, 1.0, "NORMAL"), (1.0, 1.01, "PUNCH_STRONG"), (1.01, 3.0, "NORMAL")), duration_seconds=3.0, fps=25.0
    )
    assert errors and "shorter than one frame" in errors[0]


# --- pure: rendering -------------------------------------------------------------


@pytest.mark.parametrize("width, height", [(1080, 1920), (1920, 1080), (474, 850), (320, 240)])
@pytest.mark.parametrize("preset", ["PUNCH_LIGHT", "PUNCH_MEDIUM", "PUNCH_STRONG"])
def test_crop_geometry_is_centred_even_inside_the_frame_and_not_stretched(width, height, preset):
    scale = zoom_planner.PRESET_SCALE[preset]
    crop = zooms.crop_geometry(width, height, scale)
    assert crop["width"] % 2 == 0 and crop["height"] % 2 == 0 and crop["x"] % 2 == 0 and crop["y"] % 2 == 0
    assert 0 <= crop["x"] and crop["x"] + crop["width"] <= width
    assert 0 <= crop["y"] and crop["y"] + crop["height"] <= height
    # Uniform scale back: the two axes agree to within one pixel's worth.
    assert abs(crop["width"] / crop["height"] - width / height) <= 1.0 / crop["height"] + 1e-9 + 2.0 / crop["width"]
    assert abs(height / crop["height"] - scale) < 0.02
    # Centred to within the even-pixel rounding.
    assert abs((crop["x"] + crop["width"] / 2) - width / 2) <= 2
    assert zooms.crop_geometry(width, height, 1.0) == {"width": width, "height": height, "x": 0, "y": 0}


def test_the_filter_switches_by_timestamp_with_no_trim_or_concat():
    intervals = [
        {"start": 0.0, "end": 1.0, "preset": "NORMAL"},
        {"start": 1.0, "end": 2.6, "preset": "PUNCH_STRONG"},
        {"start": 2.6, "end": 3.0, "preset": "PUNCH_LIGHT"},
        {"start": 3.0, "end": 4.0, "preset": "PUNCH_STRONG"},
    ]
    graph = zooms.build_filter(intervals, 320, 240, 25.0)
    assert "trim" not in graph and "concat" not in graph and "setpts" not in graph
    assert graph.count("crop=") == 2  # one branch per zoomed preset in use
    assert "gte(t,0.98)*lt(t,2.58)+gte(t,2.98)*lt(t,3.98)" in graph
    assert graph.endswith("[vout]")
    assert zooms.build_filter([{"start": 0, "end": 4, "preset": "NORMAL"}], 320, 240, 25.0) == "[0:v]setsar=1,format=yuv420p[vout]"


def test_the_command_maps_the_original_audio_exactly_once_and_keeps_timestamps():
    command = zooms.build_render_command(r"C:\a b\in & ‏קובץ.mp4", r"C:\r\filter.txt", r"C:\r\.partial.mp4", has_audio=True, audio_codec="aac")
    assert command.count("-map") == 2 and command[command.index("-map", command.index("-map") + 1) + 1] == "0:a:0"
    assert command[command.index("-c:a") + 1] == "copy"
    assert command[command.index("-fps_mode") + 1] == "passthrough"
    assert r"C:\a b\in & ‏קובץ.mp4" in command  # one argument, never a shell string
    other = zooms.build_render_command("in.mkv", "f.txt", "o.mp4", has_audio=True, audio_codec="opus")
    assert other[other.index("-c:a") + 1] == "aac"
    silent = zooms.build_render_command("in.mp4", "f.txt", "o.mp4", has_audio=False, audio_codec=None)
    assert "0:a:0" not in silent and "-c:a" not in silent


def test_the_request_holds_text_and_timing_only_and_labels_precision_honestly():
    segments = zoom_planner.transcript_view([
        {"start": 0.2, "end": 1.0, "text": "שלום לכולם", "words": [
            {"text": "שלום", "start": 0.2, "end": 0.5}, {"text": "לכולם", "start": 0.55, "end": 1.0}]},
        {"start": 1.2, "end": 2.0, "text": "מילה שתוקנה", "words": None},
    ])
    request = zoom_planner.build_request(
        duration_seconds=12.5, fps=30, width=1080, height=1920, segments=segments, language="he",
        clips=[{"take": 1, "start": 0, "end": 5}, {"take": 2, "start": 5, "end": 12.5}], preference="balanced",
    )
    text = json.dumps(request, ensure_ascii=False)
    assert request["transcript"]["timing_precision"].startswith("phrase-level times everywhere")
    assert request["video"]["orientation"] == "portrait" and request["takes"]["clips"][1]["start"] == 5
    for forbidden in ("path", "filename", ":\\", ".mp4"):
        assert forbidden not in text
    assert "You have NOT seen the video" in zoom_planner.SYSTEM_PROMPT
    assert "no fixed or minimum length" in zoom_planner.SYSTEM_PROMPT


# --- Gemini adapter -----------------------------------------------------------------


def quota_body(delay="37s", quota_id="GenerateRequestsPerMinutePerProjectPerModel-FreeTier"):
    return {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded for key " + FAKE_KEY,
                      "details": [
                          {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota_id}]},
                          {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay},
                      ]}}


def test_rate_limits_are_retryable_with_the_apis_delay_and_daily_quotas_are_not():
    error = gemini.classify_api_error(429, "RESOURCE_EXHAUSTED", "x", quota_body(), FAKE_KEY)
    assert error.kind == "quota" and error.retryable and error.retry_after_seconds == 37.0
    daily = gemini.classify_api_error(429, "RESOURCE_EXHAUSTED", "x", quota_body(quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier"), FAKE_KEY)
    assert daily.kind == "daily_quota" and not daily.retryable and "midnight Pacific" in daily.message


@pytest.mark.parametrize(
    "code, status, message, kind, retryable",
    [
        (400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key.", "auth", False),
        (403, "PERMISSION_DENIED", "denied", "permission", False),
        (404, "NOT_FOUND", "models/gemini-x is not found", "not_found", False),
        (500, "INTERNAL", "boom", "unavailable", True),
        (503, "UNAVAILABLE", "overloaded", "unavailable", True),
        (504, "DEADLINE_EXCEEDED", "slow", "timeout", True),
        (400, "INVALID_ARGUMENT", "bad schema", "invalid_request", False),
    ],
)
def test_api_errors_are_classified(code, status, message, kind, retryable):
    error = gemini.classify_api_error(code, status, message + " " + FAKE_KEY, {}, FAKE_KEY)
    assert (error.kind, error.retryable) == (kind, retryable)
    assert FAKE_KEY not in error.message


def test_key_like_strings_are_redacted_even_when_not_the_configured_key():
    assert "AIza" not in gemini.redact("leaked AIzaSyD-abcdefghijklmnopqrstuvwxyz012345 here")


def test_retries_are_bounded_and_follow_the_apis_delay():
    sleeps, waits = [], []
    calls = {"n": 0}

    def call():
        calls["n"] += 1
        raise gemini.GeminiError("quota", "Rate limit.", retryable=True, retry_after_seconds=2.0)

    with pytest.raises(gemini.GeminiError) as raised:
        gemini.call_with_retries(call, on_wait=waits.append, sleep=sleeps.append)
    assert calls["n"] == gemini.MAX_ATTEMPTS == 3
    assert sum(sleeps) == pytest.approx(2 * 3.0)  # 2 s + 1 s margin, twice
    assert waits and "attempt 2 of 3" in waits[0]
    assert "Gave up after 3 attempts" in raised.value.message


def test_daily_quota_and_long_delays_are_not_waited_out():
    for error in (
        gemini.GeminiError("daily_quota", "Daily quota."),
        gemini.GeminiError("quota", "Rate limit.", retryable=True, retry_after_seconds=3600),
    ):
        sleeps, calls = [], []

        def call(error=error):
            calls.append(1)
            raise error

        with pytest.raises(gemini.GeminiError):
            gemini.call_with_retries(call, sleep=sleeps.append)
        assert len(calls) == 1 and sleeps == []


def test_a_transient_failure_then_success_and_cancellation_while_waiting():
    outcomes = [gemini.GeminiError("unavailable", "503", retryable=True), {"data": {"ok": True}}]

    def call():
        item = outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    result, attempts = gemini.call_with_retries(call, sleep=lambda s: None)
    assert result == {"data": {"ok": True}} and attempts == 2

    def failing():
        raise gemini.GeminiError("quota", "Rate limit.", retryable=True, retry_after_seconds=10)

    with pytest.raises(gemini.GeminiError) as raised:
        gemini.call_with_retries(failing, cancelled=lambda: True, sleep=lambda s: None)
    assert raised.value.kind == "cancelled"


class _Response:
    def __init__(self, text=None, finish="STOP", block=None):
        self._text = text
        self.candidates = [type("C", (), {"finish_reason": finish})()]
        self.prompt_feedback = type("F", (), {"block_reason": block})() if block else None
        self.usage_metadata = type("U", (), {"prompt_token_count": 100, "candidates_token_count": 20,
                                             "thoughts_token_count": 5, "total_token_count": 125})()

    @property
    def text(self):
        return self._text


@pytest.mark.parametrize(
    "response, kind",
    [
        (_Response(block="SAFETY"), "blocked"),
        (_Response(text='{"a":', finish="MAX_TOKENS"), "truncated"),
        (_Response(text=""), "empty"),
        (_Response(text="not json"), "invalid_json"),
        (_Response(text="[1, 2]"), "invalid_json"),
    ],
)
def test_unusable_responses_are_classified(response, kind):
    with pytest.raises(gemini.GeminiError) as raised:
        gemini.parse_response(response)
    assert raised.value.kind == kind


def test_the_request_is_built_with_the_real_sdk_types_and_no_sdk_retries(monkeypatch):
    from google import genai
    from google.genai import errors

    captured = {}

    class FakeModels:
        def generate_content(self, *, model, contents, config):
            captured.update(model=model, contents=contents, config=config)
            if captured.get("fail"):
                raise errors.ClientError(429, quota_body("5s"))
            return _Response(text=json.dumps({"summary": "s", "intervals": []}))

    class FakeClient:
        def __init__(self, *, api_key, http_options):
            captured.update(api_key=api_key, http_options=http_options)
            self.models = FakeModels()

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(genai, "Client", FakeClient)
    client = gemini.GeminiClient("gemini-3.5-flash", FAKE_KEY, 42.0)
    assert FAKE_KEY not in repr(client)
    outcome = client.generate_json(system="sys", user="user text", schema=zoom_planner.RESPONSE_SCHEMA)
    assert outcome["data"] == {"summary": "s", "intervals": []}
    assert outcome["usage"]["total_token_count"] == 125
    config = captured["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == zoom_planner.RESPONSE_SCHEMA
    assert config.system_instruction == "sys"
    assert captured["http_options"].timeout == 42000
    assert captured["http_options"].retry_options.attempts == 1
    assert captured["model"] == "gemini-3.5-flash" and captured["closed"]

    captured["fail"] = True
    with pytest.raises(gemini.GeminiError) as raised:
        client.generate_json(system="sys", user="u", schema=None)
    assert raised.value.kind == "quota" and raised.value.retry_after_seconds == 5.0
    assert FAKE_KEY not in str(raised.value) and raised.value.__cause__ is None


def test_missing_key_is_reported_and_there_is_no_fallback_to_another_provider(monkeypatch):
    monkeypatch.delenv(GEMINI_API_KEY_ENV_VAR, raising=False)
    monkeypatch.delenv(ZOOM_PROVIDER_ENV_VAR, raising=False)
    # A configured paid provider for the older features must not be used.
    monkeypatch.setenv("VIDEO_FACTORY_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-used")
    with pytest.raises(ProviderError) as raised:
        zoom_planner.get_planner()
    assert GEMINI_API_KEY_ENV_VAR in raised.value.message
    status = zoom_planner.provider_status()
    assert status["provider"] == "gemini" and status["ready"] is False and status["model"] == DEFAULT_GEMINI_MODEL

    monkeypatch.setenv(ZOOM_PROVIDER_ENV_VAR, "anthropic")
    with pytest.raises(ProviderError):
        zoom_planner.get_planner()


def test_the_key_is_never_in_the_status(monkeypatch):
    monkeypatch.setenv(GEMINI_API_KEY_ENV_VAR, FAKE_KEY)
    monkeypatch.delenv(ZOOM_PROVIDER_ENV_VAR, raising=False)
    planner = zoom_planner.get_planner()
    assert FAKE_KEY not in json.dumps(planner.describe()) and FAKE_KEY not in repr(planner.__dict__)
    status = zoom_planner.provider_status()
    assert status["ready"] is True and FAKE_KEY not in json.dumps(status)


def test_the_frontend_never_reads_a_key():
    root = Path(__file__).resolve().parents[2] / "frontend"
    for path in list((root / "src").rglob("*.ts*")) + [root / "vite.config.ts"]:
        text = path.read_text(encoding="utf-8")
        assert "GEMINI_API_KEY" not in text and "API_KEY" not in text and "VITE_" not in text, path


# --- pipeline fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv(WORKSPACE_ENV_VAR, str(tmp_path / "workspace"))
    monkeypatch.setenv(GEMINI_API_KEY_ENV_VAR, FAKE_KEY)
    monkeypatch.delenv(ZOOM_PROVIDER_ENV_VAR, raising=False)
    monkeypatch.setattr(captions, "model_downloaded", lambda model_id: True)
    caption_inputs._fingerprint_cache.clear()
    caption_inputs._metadata_cache.clear()


@pytest.fixture
def client():
    with TestClient(app) as running:
        yield running


@pytest.fixture
def fake_whisper(monkeypatch):
    fake = FakeWhisper()
    monkeypatch.setattr(whisper_service.service, "transcribe", fake)
    return fake


STRONG_PLAN = plan_of((0, 1.0, "NORMAL"), (1.0, 2.6, "PUNCH_STRONG"), (2.6, 4.0, "NORMAL"),
                      summary="Normal framing with one strong punch-in on the key line.")


class FakePlanner:
    """Stands in for Gemini. Answers are dicts, exceptions or callables."""

    def __init__(self, *answers, gate=None):
        self.answers = list(answers) or [STRONG_PLAN]
        self.calls = []
        self.gate = gate

    def describe(self):
        return {"id": "gemini", "label": "Google Gemini", "model": DEFAULT_GEMINI_MODEL, "is_mock": False}

    def complete(self, message):
        self.calls.append(message)
        if self.gate is not None:
            self.gate.wait(10)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return {"data": answer, "usage": {"total_token_count": 10}}


@pytest.fixture
def planner(monkeypatch):
    fake = FakePlanner()
    monkeypatch.setattr(zoom_planner, "get_planner", lambda: fake)
    return fake


def make_grid_video(path: Path, seconds=4, size="320x240", rate=25, colour=None) -> Path:
    source = "color=c=%s:size=%s:rate=%d:duration=%s" % (colour, size, rate, seconds) if colour else \
        "testsrc2=size=%s:rate=%d:duration=%s" % (size, rate, seconds)
    subprocess.run(
        ["ffmpeg.exe", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source,
         "-f", "lavfi", "-i", "sine=frequency=880:duration=%s,volume='between(t,1,1.5)':eval=frame" % seconds,
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )
    return path


@pytest.fixture
def project(client):
    created = client.post("/projects", json={"name": "זומים"}).json()
    # These tests cover approval going straight to the export, which is what
    # happens with the B-roll and music steps switched off. The default
    # hand-over to B-roll (stage 3A) is tested in test_broll.py, to music
    # (stage 4) in test_music.py.
    response = client.put(f"/projects/{created['id']}/broll/settings", json={"enabled": False})
    assert response.status_code == 200, response.text
    response = client.put(f"/projects/{created['id']}/music/settings", json={"enabled": False})
    assert response.status_code == 200, response.text
    return created


@pytest.fixture
def grid(tmp_path):
    if not shutil.which("ffmpeg.exe"):
        pytest.skip("FFmpeg is not on PATH")
    folder = tmp_path / "צילומים עם רווח"
    folder.mkdir()
    return make_grid_video(folder / "טייק 1.mp4")


def add_source(client, project_id, path) -> str:
    response = client.post(f"/projects/{project_id}/sources", json={"path": str(path)})
    assert response.status_code == 201, response.text
    return "source.%s" % response.json()["sources"][-1]["id"]


def wait_for_job(client, project_id, job_id, timeout=120.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = client.get(f"/projects/{project_id}/jobs/{job_id}").json()
        if record["status"] in jobs.FINISHED_STATUSES:
            return record
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def start(client, project_id, key, **extra):
    response = client.post(f"/projects/{project_id}/zooms/runs", json={"input_key": key, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def run_to_end(client, project_id, key, **extra):
    body = start(client, project_id, key, **extra)
    if body["job"] is None:
        return body
    record = wait_for_job(client, project_id, body["job"]["id"])
    return {"job": record, "reused": None}


def frame_rgb(path, index, extra_filter=""):
    chain = "select=eq(n\\,%d)%s" % (index, extra_filter)
    raw = subprocess.run(
        ["ffmpeg.exe", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vf", chain,
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.uint8).astype(np.float64)


def psnr(a, b):
    mse = np.mean((a - b) ** 2)
    return 99.0 if mse == 0 else 10 * np.log10(255.0 ** 2 / mse)


def ffprobe_json(path, *arguments):
    return json.loads(subprocess.run(["ffprobe.exe", "-v", "error", *arguments, "-of", "json", str(path)],
                                     capture_output=True, check=True, text=True).stdout)


def audio_md5(path):
    return subprocess.run(
        ["ffmpeg.exe", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:a:0", "-f", "md5", "-"],
        capture_output=True, check=True, text=True,
    ).stdout.strip()


# --- pipeline ----------------------------------------------------------------------


@needs_ffmpeg
def test_the_whole_chain_transcribes_plans_renders_and_verifies(client, project, grid, fake_whisper, planner, tmp_path):
    key = add_source(client, project["id"], grid)
    outcome = run_to_end(client, project["id"], key)
    record = outcome["job"]
    assert record["status"] == "succeeded", record["error"]
    assert record["result"]["transcribed_now"] is True and len(fake_whisper.calls) == 1
    assert len(planner.calls) == 1

    # What was sent: transcript text and timings, no path, no file name, no key.
    sent = planner.calls[0]
    assert "שלום" in sent and str(grid) not in sent and "טייק" not in sent and FAKE_KEY not in sent

    run_id = record["result"]["run_id"]
    run = client.get(f"/projects/{project['id']}/zooms/runs/{run_id}").json()
    assert run["status"] == "succeeded" and "path" not in run["input"]
    plan = run["plan"]
    for field in ("schema_version", "cache_key", "provider", "prompt_version", "settings", "summary", "intervals", "timebase"):
        assert field in plan
    assert plan["input"]["fingerprint"].startswith("sha256:")
    assert plan["transcript"]["revision"] == 1 and plan["provider"]["model"] == DEFAULT_GEMINI_MODEL
    assert [(i["start"], i["end"], i["preset"]) for i in plan["intervals"]] == [
        (0.0, 1.0, "NORMAL"), (1.0, 2.6, "PUNCH_STRONG"), (2.6, 4.0, "NORMAL")]

    output = zooms.output_path(project["id"], zooms.read_run(project["id"], run_id))
    # Metadata: one H.264 video at the source size, one audio, same length,
    # same number of frames, and the audio bit-identical (copied once).
    probe = ffprobe_json(output, "-count_packets", "-show_entries", "stream=codec_type,codec_name,width,height,nb_read_packets:format=duration")
    video = [s for s in probe["streams"] if s["codec_type"] == "video"]
    audio = [s for s in probe["streams"] if s["codec_type"] == "audio"]
    assert len(video) == 1 and len(audio) == 1
    assert (video[0]["width"], video[0]["height"], video[0]["codec_name"]) == (320, 240, "h264")
    assert int(video[0]["nb_read_packets"]) == 100
    assert abs(float(probe["format"]["duration"]) - 4.0) < 0.05
    assert audio_md5(output) == audio_md5(grid)
    assert run["verification"]["video_frames"] == run["verification"]["input_video_frames"] == 100

    # Frames around every change: the zoom applies exactly from frame 25
    # (1.0 s) to frame 64 (2.56 s), and exactly once.
    geometry = zooms.crop_geometry(320, 240, 1.20)
    zoom_chain = ",crop=%d:%d:%d:%d,scale=320:240:flags=lanczos" % (geometry["width"], geometry["height"], geometry["x"], geometry["y"])
    double_chain = zoom_chain + zoom_chain
    for index, zoomed in [(0, False), (24, False), (25, True), (40, True), (64, True), (65, False), (99, False)]:
        actual = frame_rgb(output, index)
        plain = frame_rgb(grid, index)
        expected = frame_rgb(grid, index, zoom_chain) if zoomed else plain
        # The output is a second encode of the fixture's fine detail, so the
        # match is judged against the alternatives, not by an absolute score.
        assert psnr(actual, expected) > 28, (index, zoomed)
        if zoomed:
            assert psnr(actual, expected) > psnr(actual, plain) + 6, index
            assert psnr(actual, expected) > psnr(actual, frame_rgb(grid, index, double_chain)) + 5, index
        else:
            assert psnr(actual, expected) > psnr(actual, frame_rgb(grid, index, zoom_chain)) + 6, index

    # Originals untouched.
    assert grid.is_file()


@needs_ffmpeg
def test_asking_again_reuses_everything_without_a_job_or_a_provider_call(client, project, grid, fake_whisper, planner):
    key = add_source(client, project["id"], grid)
    first = run_to_end(client, project["id"], key)
    again = start(client, project["id"], key)
    assert again["job"] is None and again["reused"]["run_id"] == first["job"]["result"]["run_id"]
    assert len(planner.calls) == 1 and len(fake_whisper.calls) == 1


@needs_ffmpeg
def test_repeated_clicks_while_running_return_the_same_job(client, project, grid, fake_whisper, monkeypatch):
    gate = threading.Event()
    fake = FakePlanner(gate=gate)
    monkeypatch.setattr(zoom_planner, "get_planner", lambda: fake)
    key = add_source(client, project["id"], grid)
    first = start(client, project["id"], key)
    second = start(client, project["id"], key)
    assert first["job"]["id"] == second["job"]["id"]
    for _ in range(5):  # polling never calls the provider
        client.get(f"/projects/{project['id']}/zooms/inputs/{key}")
    gate.set()
    assert wait_for_job(client, project["id"], first["job"]["id"])["status"] == "succeeded"
    assert len(fake.calls) == 1


@needs_ffmpeg
def test_re_rendering_reuses_the_stored_plan_and_force_replan_asks_again(client, project, grid, fake_whisper, planner):
    key = add_source(client, project["id"], grid)
    first = run_to_end(client, project["id"], key)["job"]["result"]
    # The rendered file disappears: the render is redone, the plan is not.
    manifest = zooms.read_run(project["id"], first["run_id"])
    zooms.output_path(project["id"], manifest).unlink()
    second = run_to_end(client, project["id"], key)["job"]
    assert second["status"] == "succeeded"
    assert second["result"]["plan_reused"] and not second["result"]["render_reused"]
    assert second["result"]["run_id"] != first["run_id"] and len(planner.calls) == 1
    # Rendering a stored plan by id never calls the provider either.
    third = run_to_end(client, project["id"], key, plan_id=first["plan_id"])["job"]
    assert third["result"]["plan_id"] == first["plan_id"] and len(planner.calls) == 1

    replanned = run_to_end(client, project["id"], key, force_replan=True)["job"]
    assert replanned["status"] == "succeeded" and len(planner.calls) == 2
    assert replanned["result"]["plan_id"] != first["plan_id"]


@needs_ffmpeg
def test_an_invalid_plan_gets_one_repair_and_then_fails_without_a_render(client, project, grid, fake_whisper, monkeypatch):
    key = add_source(client, project["id"], grid)
    repaired = FakePlanner(plan_of((0, 2, "ZOOM_150"), (2, 4, "NORMAL")), STRONG_PLAN)
    monkeypatch.setattr(zoom_planner, "get_planner", lambda: repaired)
    record = run_to_end(client, project["id"], key)["job"]
    assert record["status"] == "succeeded" and len(repaired.calls) == 2
    assert "rejected by validation" in repaired.calls[1] and "ZOOM_150" in repaired.calls[1]
    plan = zooms.read_plan(project["id"], record["result"]["plan_id"])
    assert plan["audit"]["repair_used"] is True

    bad = FakePlanner(plan_of((0, 2, "NORMAL")))  # never covers the video
    monkeypatch.setattr(zoom_planner, "get_planner", lambda: bad)
    record = run_to_end(client, project["id"], key, force_replan=True)["job"]
    assert record["status"] == "failed" and len(bad.calls) == 2
    assert "rejected even after one correction" in record["error"] and "nothing was rendered" in record["error"]
    assert len(zooms.list_plans(project["id"])) == 1  # no plan was created for the failure
    assert len([m for m in zooms.list_runs(project["id"])]) == 1
    assert list(zooms.plans_directory(project["id"]).glob("rejected-*.json"))


@needs_ffmpeg
def test_quota_errors_are_retried_boundedly_and_daily_quota_fails_clearly(client, project, grid, fake_whisper, monkeypatch):
    key = add_source(client, project["id"], grid)
    flaky = FakePlanner(gemini.GeminiError("quota", "Gemini's free-tier rate limit was reached.", retryable=True, retry_after_seconds=0.01), STRONG_PLAN)
    monkeypatch.setattr(zoom_planner, "get_planner", lambda: flaky)
    record = run_to_end(client, project["id"], key)["job"]
    assert record["status"] == "succeeded" and len(flaky.calls) == 2
    plan = zooms.read_plan(project["id"], record["result"]["plan_id"])
    assert plan["audit"]["calls"][0]["attempts"] == 2

    daily = FakePlanner(gemini.classify_api_error(429, "RESOURCE_EXHAUSTED", FAKE_KEY, quota_body(quota_id="PerDay-FreeTier"), FAKE_KEY))
    monkeypatch.setattr(zoom_planner, "get_planner", lambda: daily)
    record = run_to_end(client, project["id"], key, force_replan=True)["job"]
    assert record["status"] == "failed" and len(daily.calls) == 1
    assert "midnight Pacific" in record["error"] and "another provider" in record["error"]


@needs_ffmpeg
def test_missing_key_fails_the_job_with_setup_guidance_and_nothing_is_sent(client, project, grid, fake_whisper, monkeypatch):
    monkeypatch.delenv(GEMINI_API_KEY_ENV_VAR)
    key = add_source(client, project["id"], grid)
    record = run_to_end(client, project["id"], key)["job"]
    assert record["status"] == "failed" and GEMINI_API_KEY_ENV_VAR in record["error"]
    assert zooms.list_runs(project["id"]) == []


@needs_ffmpeg
def test_cancelling_a_render_stops_ffmpeg_and_leaves_no_result(client, project, grid, fake_whisper, planner, monkeypatch):
    real_run = processes.run
    started = threading.Event()

    def slow_run(command, *, cancelled=None, on_output=None, **kwargs):
        if command[0] == "ffmpeg.exe" and "-/filter_complex" in command:
            started.set()
            while not cancelled():
                time.sleep(0.02)
            raise processes.ProcessCancelled()
        return real_run(command, cancelled=cancelled, on_output=on_output, **kwargs)

    monkeypatch.setattr(zoom_jobs.processes, "run", slow_run)
    key = add_source(client, project["id"], grid)
    body = start(client, project["id"], key)
    assert started.wait(30)
    client.post(f"/projects/{project['id']}/jobs/{body['job']['id']}/cancel")
    record = wait_for_job(client, project["id"], body["job"]["id"])
    assert record["status"] == "cancelled"
    (manifest,) = zooms.list_runs(project["id"])
    assert manifest["status"] == "cancelled" and manifest["output"] is None
    directory = zooms.run_directory(project["id"], manifest["run_id"])
    assert not (directory / zooms.PARTIAL_FILE_NAME).exists() and not (directory / zooms.OUTPUT_FILE_NAME).exists()
    # The plan survived, so trying again does not ask the provider.
    monkeypatch.setattr(zoom_jobs.processes, "run", real_run)
    assert run_to_end(client, project["id"], key)["job"]["status"] == "succeeded" and len(planner.calls) == 1


@needs_ffmpeg
def test_a_real_render_can_be_cancelled(client, project, tmp_path, fake_whisper, planner):
    long_video = make_grid_video(tmp_path / "long.mp4", seconds=40, size="1280x720", rate=30)
    planner.answers = [plan_of((0, 10, "NORMAL"), (10, 40, "PUNCH_MEDIUM"))]
    key = add_source(client, project["id"], long_video)
    body = start(client, project["id"], key)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        record = client.get(f"/projects/{project['id']}/jobs/{body['job']['id']}").json()
        if "Rendering" in (record["progress_message"] or ""):
            break
        time.sleep(0.05)
    client.post(f"/projects/{project['id']}/jobs/{body['job']['id']}/cancel")
    record = wait_for_job(client, project["id"], body["job"]["id"])
    assert record["status"] == "cancelled"
    (manifest,) = zooms.list_runs(project["id"])
    directory = zooms.run_directory(project["id"], manifest["run_id"])
    assert not (directory / zooms.PARTIAL_FILE_NAME).exists() and manifest["output"] is None


@needs_ffmpeg
def test_a_changed_input_makes_results_stale_and_unapprovable(client, project, grid, fake_whisper, planner):
    key = add_source(client, project["id"], grid)
    run_id = run_to_end(client, project["id"], key)["job"]["result"]["run_id"]
    run = client.get(f"/projects/{project['id']}/zooms/runs/{run_id}").json()
    assert run["review"]["reviewable"] and not run["review"]["stale"]

    make_grid_video(grid, seconds=4, size="320x240", rate=25, colour="red")  # re-recorded under the same name
    caption_inputs._fingerprint_cache.clear()
    run = client.get(f"/projects/{project['id']}/zooms/runs/{run_id}").json()
    assert run["review"]["stale"] and not run["review"]["reviewable"]
    response = client.post(f"/projects/{project['id']}/zooms/runs/{run_id}/approve",
                           json={"output_digest": run["output"]["fingerprint"]["digest"], "captions": run["review"]["overlay_reference"]})
    assert response.status_code == 409
    state = client.get(f"/projects/{project['id']}/zooms/inputs/{key}").json()
    assert state["current_run"] is None and state["transcript"] is None
    assert all(item["stale"] for item in state["runs"])


@needs_ffmpeg
def test_zoomed_videos_cannot_be_zoomed_again(client, project, grid, fake_whisper, planner):
    key = add_source(client, project["id"], grid)
    run_id = run_to_end(client, project["id"], key)["job"]["result"]["run_id"]
    response = client.post(f"/projects/{project['id']}/zooms/runs", json={"input_key": "zoom.%s" % run_id})
    assert response.status_code == 400 and "cannot be zoomed again" in response.json()["detail"]
    keys = [entry["key"] for entry in client.get(f"/projects/{project['id']}/zooms/status").json()["inputs"]]
    assert key in keys and not any(k.startswith("zoom.") for k in keys)


@needs_ffmpeg
def test_nothing_persisted_or_returned_contains_the_key(client, project, grid, fake_whisper, monkeypatch, tmp_path):
    key = add_source(client, project["id"], grid)
    leaky = FakePlanner(gemini.classify_api_error(400, "INVALID_ARGUMENT", "API key not valid: " + FAKE_KEY, {}, FAKE_KEY))
    monkeypatch.setattr(zoom_planner, "get_planner", lambda: leaky)
    run_to_end(client, project["id"], key)
    monkeypatch.setattr(zoom_planner, "get_planner", lambda: FakePlanner())
    run_to_end(client, project["id"], key)
    texts = [client.get(path).text for path in (
        f"/projects/{project['id']}/zooms/status", f"/projects/{project['id']}/jobs",
        f"/projects/{project['id']}/zooms/inputs/{key}", "/llm", "/capabilities")]
    for path in (tmp_path / "workspace").rglob("*"):
        if path.is_file() and path.suffix in (".json", ".log", ".txt"):
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
    assert texts and not any(FAKE_KEY in text for text in texts)


# --- approval, captions and lineage ------------------------------------------------------


def review(client, project_id, run_id):
    return client.get(f"/projects/{project_id}/zooms/runs/{run_id}").json()


def approve(client, project_id, run):
    return client.post(f"/projects/{project_id}/zooms/runs/{run['run_id']}/approve",
                       json={"output_digest": run["output"]["fingerprint"]["digest"],
                             "captions": run["review"]["overlay_reference"]})


@needs_ffmpeg
def test_approval_names_the_exact_result_and_is_outdated_by_caption_changes(client, project, grid, fake_whisper, planner, monkeypatch):
    monkeypatch.setattr(caption_jobs, "renderer_status", lambda: {"ready": False, "problems": ["not prepared"]})
    key = add_source(client, project["id"], grid)
    run_id = run_to_end(client, project["id"], key)["job"]["result"]["run_id"]
    run = review(client, project["id"], run_id)
    overlay = run["review"]["overlay"]
    assert overlay and overlay["revision"] == 1 and overlay["captions"]

    wrong = client.post(f"/projects/{project['id']}/zooms/runs/{run_id}/approve",
                        json={"output_digest": "sha256:other", "captions": run["review"]["overlay_reference"]})
    assert wrong.status_code == 409
    response = approve(client, project["id"], run)
    assert response.status_code == 200
    # The captioned export cannot start here (renderer not prepared), which is
    # reported instead of failing the approval.
    assert response.json()["next"]["kind"] == "blocked"
    assert review(client, project["id"], run_id)["review"]["approved"] is True

    # A new caption revision outdates the approval; no re-render happens.
    transcript_id = overlay["transcript_id"]
    edited = [dict(c, text=c["text"] + "!") for c in overlay["captions"]]
    saved = client.post(f"/projects/{project['id']}/captions/transcripts/{transcript_id}/revisions",
                        json={"base_revision": 1, "captions": edited})
    assert saved.status_code == 201, saved.text
    run = review(client, project["id"], run_id)
    assert run["review"]["approved"] is False and "captions" in run["review"]["approval_outdated_reason"]
    assert len(zooms.list_runs(project["id"])) == 1
    assert approve(client, project["id"], run).status_code == 200
    # Style changes outdate it too, again without re-rendering.
    client.put(f"/projects/{project['id']}/captions/style", json={"style": {**captions.default_style(), "font_size_percent": 9.0}})
    assert review(client, project["id"], run_id)["review"]["approved"] is False
    assert len(zooms.list_runs(project["id"])) == 1 and len(planner.calls) == 1


@needs_ffmpeg
def test_without_captions_approval_finishes_with_the_zoomed_file(client, project, grid, planner, monkeypatch):
    key = add_source(client, project["id"], grid)
    # Independent use: a transcript exists, but every caption was deleted.
    monkeypatch.setattr(whisper_service.service, "transcribe", FakeWhisper())
    run_id = run_to_end(client, project["id"], key)["job"]["result"]["run_id"]
    run = review(client, project["id"], run_id)
    transcript_id = run["review"]["overlay"]["transcript_id"]
    monkeypatch.setattr(zooms, "current_caption_overlay", lambda *a, **k: None)
    run = review(client, project["id"], run_id)
    assert run["review"]["overlay"] is None
    response = approve(client, project["id"], run)
    assert response.status_code == 200 and response.json()["next"]["kind"] == "final"
    video = client.get(f"/projects/{project['id']}/zooms/runs/{run_id}/video", headers={"Range": "bytes=0-99"})
    assert video.status_code == 206 and len(video.content) == 100
    assert transcript_id


@needs_ffmpeg
def test_the_zoomed_video_shares_its_parents_captions_through_lineage(client, project, grid, fake_whisper, planner):
    key = add_source(client, project["id"], grid)
    run_id = run_to_end(client, project["id"], key)["job"]["result"]["run_id"]
    zoom_key = "zoom.%s" % run_id
    listing = [entry["key"] for entry in client.get(f"/projects/{project['id']}/captions/inputs").json()["inputs"]]
    assert zoom_key in listing

    detail = client.get(f"/projects/{project['id']}/captions/inputs/{zoom_key}").json()
    assert detail["timing_source"] == {"key": key, "via_zoom": True}
    assert len(detail["transcripts"]) == 1 and detail["transcripts"][0]["stale"] is False

    # Transcribing the zoomed video reuses the parent's transcript; Whisper
    # never runs on the zoomed file.
    response = client.post(f"/projects/{project['id']}/captions/transcriptions", json={"input_key": zoom_key})
    assert response.status_code == 201 and response.json()["reused"] is not None
    assert len(fake_whisper.calls) == 1

    # Captions of an unrelated video cannot be put on it.
    other = add_source(client, project["id"], make_grid_video(grid.parent / "אחר.mp4", seconds=4, colour="green"))
    queued = client.post(f"/projects/{project['id']}/captions/transcriptions", json={"input_key": other}).json()
    wait_for_job(client, project["id"], queued["job"]["id"])
    other_transcript = next(t for t in captions.list_transcripts(project["id"]) if t["input"]["key"] == other)
    refused = client.post(f"/projects/{project['id']}/captions/exports",
                          json={"transcript_id": other_transcript["id"], "revision": 1, "format": "srt", "input_key": zoom_key})
    assert refused.status_code == 409


@needs_ffmpeg
@needs_renderer
def test_captions_keep_their_size_and_position_over_the_zoomed_video(client, project, tmp_path, fake_whisper, planner):
    """A flat-colour video looks the same zoomed or not, so the only thing
    that could differ between the two exports is the caption layer — and it
    must not. A grid version then shows the picture really is zoomed under
    the same captions."""
    flat = make_grid_video(tmp_path / "flat.mp4", seconds=4, size="360x640", colour="0x3366aa")
    planner.answers = [plan_of((0, 0.4, "NORMAL"), (0.4, 4.0, "PUNCH_STRONG"))]
    key = add_source(client, project["id"], flat)
    run_id = run_to_end(client, project["id"], key)["job"]["result"]["run_id"]
    run = review(client, project["id"], run_id)
    reference = run["review"]["overlay_reference"]

    response = approve(client, project["id"], run)
    assert response.status_code == 200 and response.json()["next"]["kind"] == "caption_export"
    zoomed_job = wait_for_job(client, project["id"], response.json()["next"]["job"]["id"], timeout=300)
    assert zoomed_job["status"] == "succeeded", zoomed_job["error"]
    # Approving again does not start a second export.
    again = approve(client, project["id"], review(client, project["id"], run_id))
    assert again.json()["next"]["job"] is None

    clean = client.post(f"/projects/{project['id']}/captions/exports",
                        json={"transcript_id": reference["transcript_id"], "revision": reference["revision"], "format": "mp4"})
    clean_job = wait_for_job(client, project["id"], clean.json()["id"], timeout=300)
    assert clean_job["status"] == "succeeded", clean_job["error"]

    exports = {record["input_key"]: record for record in caption_jobs.list_exports(project["id"])}
    zoomed_file, _ = caption_jobs.resolve_export_file(project["id"], exports["zoom.%s" % run_id]["export_id"])
    clean_file, _ = caption_jobs.resolve_export_file(project["id"], exports[key]["export_id"])
    assert exports["zoom.%s" % run_id]["verification"]["streams"] == {"video": 1, "audio": 1}
    # Frames in the middle of the first three captions, whatever the grouping
    # (the 25 fps test video; a caption [start, end) is on frames ceil(start·fps)…).
    shown = captions.read_revision(project["id"], reference["transcript_id"], reference["revision"])["captions"]
    frames = [int((caption["start"] + caption["end"]) / 2 * 25) for caption in shown[:3]]
    assert len(frames) == 3
    for index in frames:
        a, b = frame_rgb(zoomed_file, index), frame_rgb(clean_file, index)
        assert psnr(a, b) > 40, index
        assert np.abs(a - frame_rgb(flat, index)).max() > 100, "no caption visible at frame %d" % index
    assert audio_md5(zoomed_file) == audio_md5(clean_file)


def test_the_capability_is_registered_with_an_executor():
    from backend import capabilities, job_tasks

    assert capabilities.ZOOMS in capabilities.CAPABILITIES
    assert capabilities.ZOOMS in job_tasks.EXECUTORS
    assert not any("Zoom" in item for item in capabilities.NOT_YET_SUPPORTED)
