"""Tests for stage 2A: transcripts, editable captions, style, exports.

- **Pure** — grouping, validation and normalisation, split/merge, SRT,
  frame-rate choice.
- **Persistence and staleness** — transcripts and revisions over the API,
  with Whisper replaced by a stand-in; reuse, retranscription, regrouping,
  a changed video, style changes.
- **The Whisper service** — a fake worker process that speaks the serve
  protocol: model reuse, cancellation killing the process, failures.
- **Exports** — immutable snapshots, SRT, MP4 cancellation with a stand-in
  renderer, and real Remotion renders of a generated fixture (skipped when
  the renderer is not prepared on this machine).

Nothing here touches `workspace\\`, loads a real model or downloads anything.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import (  # noqa: E402
    caption_inputs,
    caption_jobs,
    captions,
    jobs,
    processes,
    whisper_service,
)
from backend.config import WORKSPACE_ENV_VAR  # noqa: E402
from backend.main import app  # noqa: E402

GROUPING = captions.default_grouping()

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg.exe") and shutil.which("ffprobe.exe")),
    reason="FFmpeg / FFprobe are not on PATH",
)
needs_renderer = pytest.mark.skipif(
    not caption_jobs.renderer_status()["ready"],
    reason="The Remotion renderer is not prepared (node, npm install, headless browser)",
)


def word(start, end, text):
    return {"start": start, "end": end, "word": " " + text}


def transcript_of(segments, duration=30.0):
    return {"input": {"metadata": {"duration_seconds": duration}}, "segments": segments}


def segment(words, text=""):
    return {"start": words[0]["start"] if words else 0.0, "end": words[-1]["end"] if words else 1.0, "text": text, "words": words}


# --- pure: grouping ----------------------------------------------------------------


def texts(groups):
    return [caption["text"] for caption in groups]


def test_grouping_closes_at_the_word_limit_and_rebalances_a_lone_word():
    words = [word(i * 0.3, i * 0.3 + 0.25, "w%d" % i) for i in range(6)]
    result = captions.group_transcript(transcript_of([segment(words)]), {**GROUPING, "max_words": 5})
    # 5 + 1 would leave one word alone; the limit split is moved to 3 + 3.
    assert texts(result) == ["w0 w1 w2", "w3 w4 w5"]
    assert all(caption["word_timing"] == "word" for caption in result)


def test_grouping_splits_on_pauses_and_never_moves_a_pause_split():
    words = [word(0.0, 0.3, "שלום"), word(0.35, 0.6, "לכם"), word(2.0, 2.3, "היום")]
    result = captions.group_transcript(transcript_of([segment(words)]), GROUPING)
    assert texts(result) == ["שלום לכם", "היום"]
    assert result[0]["end"] == pytest.approx(0.6)
    assert result[1]["start"] == pytest.approx(2.0)


def test_grouping_splits_at_punctuation_only_after_enough_words():
    words = [word(0.0, 0.2, "כן,"), word(0.2, 0.4, "אני"), word(0.4, 0.6, "יודע."), word(0.6, 0.8, "אבל"), word(0.8, 1.0, "לא")]
    result = captions.group_transcript(transcript_of([segment(words)]), {**GROUPING, "min_words_before_punctuation": 2})
    assert texts(result) == ["כן, אני יודע.", "אבל לא"]


def test_grouping_respects_the_line_length_and_gives_a_long_word_its_own_caption():
    long_word = "א" * 40
    words = [word(0.0, 0.2, "קצר"), word(0.2, 0.9, long_word), word(0.9, 1.1, "סוף"), word(1.1, 1.3, "טוב")]
    result = captions.group_transcript(transcript_of([segment(words)]), {**GROUPING, "max_characters": 20})
    assert texts(result) == ["קצר", long_word, "סוף טוב"]


def test_segment_boundaries_always_split_and_wordless_segments_keep_segment_timing():
    first = segment([word(0.0, 0.4, "אחת"), word(0.4, 0.8, "שתיים")])
    bare = {"start": 1.0, "end": 2.0, "text": " בלי מילים ", "words": []}
    result = captions.group_transcript(transcript_of([first, bare]), GROUPING)
    assert texts(result) == ["אחת שתיים", "בלי מילים"]
    assert result[1]["words"] is None and result[1]["word_timing"] == "group"
    assert (result[1]["start"], result[1]["end"]) == (1.0, 2.0)


def test_short_captions_are_held_but_never_into_the_next_caption_or_past_the_end():
    words = [word(0.0, 0.1, "כן"), word(0.3, 0.4, "לא")]
    settings = {**GROUPING, "pause_seconds": 0.1, "min_duration_seconds": 0.5}
    result = captions.group_transcript(transcript_of([segment(words)], duration=0.6), settings)
    assert [(c["start"], c["end"]) for c in result] == [(0.0, 0.3), (0.3, 0.6)]
    # Word timings are not stretched with the caption.
    assert result[0]["words"][0]["end"] == pytest.approx(0.1)


def test_grouping_is_deterministic_apart_from_ids():
    words = [word(i * 0.4, i * 0.4 + 0.3, "מילה%d" % i) for i in range(12)]
    a = captions.group_transcript(transcript_of([segment(words)]), GROUPING)
    b = captions.group_transcript(transcript_of([segment(words)]), GROUPING)
    strip = lambda items: [{k: v for k, v in item.items() if k != "id"} for item in items]  # noqa: E731
    assert strip(a) == strip(b)
    assert len({c["id"] for c in a}) == len(a)


def test_grouping_validation_rejects_bad_values():
    with pytest.raises(captions.CaptionError):
        captions.validate_grouping({"max_words": 0})
    with pytest.raises(captions.CaptionError):
        captions.validate_grouping({"max_words": 2.5})
    with pytest.raises(captions.CaptionError):
        captions.validate_grouping({"pause_seconds": float("nan")})
    with pytest.raises(captions.CaptionError):
        captions.validate_grouping({"unknown": 1})


# --- pure: validation and word timing ------------------------------------------------


def cap(id_, start, end, text, words=None):
    return {"id": id_, "start": start, "end": end, "text": text, "words": words}


def timed(id_, pairs):
    words = [{"text": t, "start": s, "end": e} for t, s, e in pairs]
    return cap(id_, pairs[0][1], pairs[-1][2], " ".join(t for t, _, _ in pairs), words)


def test_valid_captions_pass_and_are_rounded_to_milliseconds():
    result, errors = captions.normalise_captions([cap("a", 0.12345, 1.0, " שלום   עולם ")], 10.0)
    assert errors == []
    assert result[0]["start"] == 0.123 and result[0]["text"] == "שלום עולם"


@pytest.mark.parametrize(
    "bad, field",
    [
        (cap("a", float("nan"), 1.0, "x"), "start"),
        (cap("a", 0.0, float("inf"), "x"), "end"),
        (cap("a", -0.5, 1.0, "x"), "start"),
        (cap("a", 1.0, 1.0, "x"), "end"),
        (cap("a", 2.0, 1.0, "x"), "end"),
        (cap("a", 0.0, 10.5, "x"), "end"),
        (cap("a", 0.0, 1.0, "   "), "text"),
        (cap("a b", 0.0, 1.0, "x"), "id"),
        ({"id": "a", "start": "0", "end": 1, "text": "x"}, "start"),
    ],
)
def test_invalid_captions_report_the_field_and_the_caption(bad, field):
    _result, errors = captions.normalise_captions([bad], 10.0)
    assert errors and errors[0]["field"] == field
    assert errors[0]["message"].startswith("Caption 1:")


def test_an_end_just_past_the_duration_is_clamped_not_refused():
    result, errors = captions.normalise_captions([cap("a", 0.0, 10.03, "x")], 10.0)
    assert errors == [] and result[0]["end"] == 10.0


def test_overlaps_and_disorder_are_refused_with_what_to_do():
    _r, errors = captions.normalise_captions([cap("a", 0.0, 2.0, "x"), cap("b", 1.5, 3.0, "y")], 10.0)
    assert errors[0]["caption_id"] == "b" and "Fix overlaps" in errors[0]["message"]
    _r, errors = captions.normalise_captions([cap("a", 2.0, 3.0, "x"), cap("b", 1.0, 1.5, "y")], 10.0)
    assert "in order" in errors[0]["message"]
    _r, errors = captions.normalise_captions([cap("a", 0.0, 1.0, "x"), cap("a", 1.0, 2.0, "y")], 10.0)
    assert errors[0]["field"] == "id"


def test_back_to_back_captions_are_not_an_overlap():
    _r, errors = captions.normalise_captions([cap("a", 0.0, 1.0, "x"), cap("b", 1.0, 2.0, "y")], 10.0)
    assert errors == []


def test_fix_overlaps_trims_ends_and_never_moves_starts():
    fixed = captions.fix_overlaps([cap("a", 0.0, 2.0, "x"), cap("b", 1.5, 3.0, "y")])
    assert [(c["start"], c["end"]) for c in fixed] == [(0.0, 1.5), (1.5, 3.0)]


def test_a_spelling_correction_keeps_word_timing():
    original = timed("a", [("שלום", 0.0, 0.4), ("עולמ", 0.5, 0.9)])
    edited = {**original, "text": "שלום עולם"}
    result, errors = captions.normalise_captions([edited], 10.0)
    assert errors == []
    assert result[0]["word_timing"] == "word"
    assert [w["text"] for w in result[0]["words"]] == ["שלום", "עולם"]
    assert result[0]["words"][1]["start"] == 0.5


def test_a_changed_word_count_drops_to_group_timing_without_inventing_alignment():
    original = timed("a", [("שלום", 0.0, 0.4), ("עולם", 0.5, 0.9)])
    edited = {**original, "text": "שלום לכל העולם"}
    result, errors = captions.normalise_captions([edited], 10.0)
    assert errors == []
    assert result[0]["words"] is None and result[0]["word_timing"] == "group"
    assert "3 words" in result[0]["timing_note"] and "2" in result[0]["timing_note"]
    assert (result[0]["start"], result[0]["end"]) == (0.0, 0.9)


def test_moving_a_caption_away_from_its_words_drops_word_timing():
    original = timed("a", [("שלום", 1.0, 1.4), ("עולם", 1.5, 1.9)])
    moved = {**original, "start": 3.0, "end": 4.0}
    result, _errors = captions.normalise_captions([moved], 10.0)
    assert result[0]["words"] is None
    assert "no longer contain" in result[0]["timing_note"]


def test_split_with_word_timing_uses_real_word_boundaries():
    original = timed("a", [("אחת", 0.0, 0.3), ("שתיים", 0.4, 0.8), ("שלוש", 1.0, 1.4)])
    first, second = captions.split_caption([original], "a", 2)
    assert (first["id"], first["text"], first["end"]) == ("a", "אחת שתיים", 0.8)
    assert (second["text"], second["start"], second["end"]) == ("שלוש", 1.0, 1.4)
    assert second["id"] != "a" and second["word_timing"] == "word"


def test_split_without_word_timing_uses_the_player_position_or_marks_an_estimate():
    original = cap("a", 0.0, 2.0, "אחת שתיים")
    first, second = captions.split_caption([original], "a", 1, at_seconds=0.7)
    assert first["end"] == second["start"] == 0.7
    first, second = captions.split_caption([original], "a", 1, at_seconds=5.0)
    assert 0.0 < first["end"] < 2.0 and "estimate" in first["timing_note"]
    with pytest.raises(captions.CaptionError):
        captions.split_caption([original], "a", 0)
    with pytest.raises(captions.CaptionError):
        captions.split_caption([original], "a", 2)


def test_merge_keeps_word_timing_only_when_both_had_it():
    a = timed("a", [("אחת", 0.0, 0.3)])
    b = timed("b", [("שתיים", 0.4, 0.8)])
    (merged,) = captions.merge_with_next([a, b], "a")
    assert (merged["id"], merged["text"], merged["start"], merged["end"]) == ("a", "אחת שתיים", 0.0, 0.8)
    assert len(merged["words"]) == 2
    (merged,) = captions.merge_with_next([a, cap("b", 0.4, 0.8, "שתיים")], "a")
    assert merged["words"] is None and merged["word_timing"] == "group"
    with pytest.raises(captions.CaptionError):
        captions.merge_with_next([a], "a")


# --- pure: SRT, style, frame rate ---------------------------------------------------


def test_srt_is_built_from_the_edited_captions_with_rtl_marks():
    srt = captions.render_srt([cap("a", 0.0, 1.2345, "שלום, world 3"), cap("b", 61.5, 62.0, "סוף.")], "rtl")
    blocks = srt.strip().split("\n\n")
    assert blocks[0] == "1\n00:00:00,000 --> 00:00:01,234\n‏שלום, world 3‏"
    assert blocks[1].startswith("2\n00:01:01,500 --> 00:01:02,000\n")
    assert "‏" not in captions.render_srt([cap("a", 0.0, 1.0, "hello")], "ltr")


def test_srt_cues_never_end_before_they_start_after_rounding():
    srt = captions.render_srt([cap("a", 1.0, 1.0004, "x")], "ltr")
    assert "00:00:01,000 --> 00:00:01,001" in srt


def test_style_validation():
    assert captions.validate_style({}) == captions.default_style()
    with pytest.raises(captions.CaptionError):
        captions.validate_style({"text_color": "white"})
    with pytest.raises(captions.CaptionError):
        captions.validate_style({"font_family": "Comic Sans"})
    with pytest.raises(captions.CaptionError):
        captions.validate_style({"font_size_percent": 50})


@pytest.mark.parametrize(
    "avg, nominal, expected, variable",
    [
        (25.0, 25.0, 25.0, False),
        (30000 / 1001, 30000 / 1001, round(30000 / 1001, 6), False),
        (29.87, 30.0, 30.0, True),  # phone VFR: its nominal standard rate
        (29.87, 90000.0, round(30000 / 1001, 6), True),  # absurd nominal: average, snapped to the nearest standard
        (17.3, 17.3, 17.3, False),  # unusual but constant: kept
        (None, None, 30.0, True),
    ],
)
def test_frame_rate_choice(avg, nominal, expected, variable):
    chosen = caption_inputs.choose_frame_rate(avg, nominal)
    assert chosen["fps"] == pytest.approx(expected) and chosen["variable"] is variable


def test_composition_dimensions_are_even_and_length_is_rounded_to_frames():
    metadata = {"display_width": 475, "display_height": 850, "fps": 30.0, "duration_seconds": 8.21, "variable_frame_rate": False}
    composition = caption_inputs.composition_metadata(metadata)
    assert (composition["width"], composition["height"]) == (476, 850)
    assert composition["duration_in_frames"] == 246
    assert composition["notes"]


# --- API fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv(WORKSPACE_ENV_VAR, str(tmp_path / "workspace"))
    monkeypatch.setattr(captions, "model_downloaded", lambda model_id: True)
    caption_inputs._fingerprint_cache.clear()
    caption_inputs._metadata_cache.clear()


@pytest.fixture
def client():
    with TestClient(app) as running:
        yield running


def make_video(path: Path, seconds: float = 3.0, size: str = "320x240", audio: bool = True, rate: int = 25) -> Path:
    """A generated clip: a test pattern and, optionally, a tone from 1.0 to 1.5 s."""
    command = ["ffmpeg.exe", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "testsrc2=size=%s:rate=%d:duration=%s" % (size, rate, seconds)]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=880:duration=%s,volume='between(t,1,1.5)':eval=frame" % seconds,
                    "-c:a", "aac", "-shortest"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(command, check=True)
    return path


@pytest.fixture
def video(tmp_path):
    if not shutil.which("ffmpeg.exe"):
        pytest.skip("FFmpeg is not on PATH")
    folder = tmp_path / "תיקייה עם רווחים"
    folder.mkdir()
    return make_video(folder / "צילום 1.mp4")


@pytest.fixture
def project(client):
    return client.post("/projects", json={"name": "כתוביות"}).json()


def add_source(client, project_id, path) -> str:
    response = client.post(f"/projects/{project_id}/sources", json={"path": str(path)})
    assert response.status_code == 201, response.text
    return response.json()["sources"][-1]["id"]


def wait_for_job(client, project_id, job_id, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = client.get(f"/projects/{project_id}/jobs/{job_id}").json()
        if record["status"] in jobs.FINISHED_STATUSES:
            return record
        time.sleep(0.05)
    raise AssertionError("job did not finish")


FAKE_WORDS = [
    ("שלום", 0.2, 0.5), ("לכולם,", 0.55, 0.9), ("זה", 1.0, 1.2), ("מבחן", 1.25, 1.6),
    ("של", 1.65, 1.8), ("Remotion", 1.85, 2.3), ("4.0", 2.35, 2.6), ("היום.", 2.65, 2.9),
]


class FakeWhisper:
    """Stands in for `whisper_service.service.transcribe`."""

    def __init__(self, words=FAKE_WORDS, block_until_cancelled=False, failure=None):
        self.words = words
        self.block = block_until_cancelled
        self.failure = failure
        self.calls = []

    def __call__(self, *, model, device, compute_type, cpu_threads, request, cancelled, on_event):
        self.calls.append({"model": model, "device": device, "compute_type": compute_type, "request": request})
        on_event({"event": "loading"})
        on_event({"event": "ready", "load_seconds": 0.1})
        if self.failure:
            raise whisper_service.WhisperFailed(*self.failure)
        if self.block:
            Path(request["output"]).write_text("partial", encoding="utf-8")
            while not cancelled():
                time.sleep(0.02)
            raise whisper_service.WhisperCancelled()
        on_event({"event": "progress", "id": request["id"], "position": 1.5})
        document = {
            "language": "he",
            "language_probability": 0.99,
            "duration_seconds": 3.0,
            "segments": [
                {
                    "start": self.words[0][1], "end": self.words[-1][2],
                    "text": " ".join(w for w, _, _ in self.words),
                    "words": [{"word": " " + w, "start": s, "end": e, "probability": 0.9} for w, s, e in self.words],
                }
            ],
        }
        Path(request["output"]).write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return {"output": request["output"], "reused_model": False, "load_seconds": 0.1, "log": "fake log"}


@pytest.fixture
def fake_whisper(monkeypatch):
    fake = FakeWhisper()
    monkeypatch.setattr(whisper_service.service, "transcribe", fake)
    return fake


def transcribe(client, project_id, key, **extra):
    response = client.post(f"/projects/{project_id}/captions/transcriptions", json={"input_key": key, **extra})
    assert response.status_code == 201, response.text
    body = response.json()
    if body["job"] is None:
        return body
    record = wait_for_job(client, project_id, body["job"]["id"])
    assert record["status"] == "succeeded", record["error"]
    return {"job": record, "reused": None}


def detail(client, project_id, key):
    return client.get(f"/projects/{project_id}/captions/inputs/{key}").json()


# --- inputs ---------------------------------------------------------------------------


@needs_ffmpeg
def test_inputs_list_footage_and_imports_and_describe_them(client, project, video, tmp_path):
    source_id = add_source(client, project["id"], video)
    imported = make_video(tmp_path / "ערוך סופי.mp4", size="240x320")
    response = client.post(f"/projects/{project['id']}/captions/imports", json={"path": str(imported)})
    assert response.status_code == 201 and response.json()["input_key"].startswith("import.")
    import_key = response.json()["input_key"]

    listing = client.get(f"/projects/{project['id']}/captions/inputs").json()
    keys = [entry["key"] for entry in listing["inputs"]]
    assert keys == ["source.%s" % source_id, import_key]
    # An import is not offered to cutting as footage.
    assert len(client.get(f"/projects/{project['id']}").json()["sources"]) == 1

    described = detail(client, project["id"], import_key)["input"]
    assert described["composition"] == {"width": 240, "height": 320, "fps": 25.0, "duration_in_frames": 75, "notes": []}
    assert described["fingerprint"]["digest"].startswith("sha256:")
    assert "path" not in described  # the browser never sees a path

    stream = client.get(f"/projects/{project['id']}/captions/inputs/{import_key}/stream", headers={"Range": "bytes=0-99"})
    assert stream.status_code == 206 and len(stream.content) == 100


def test_unknown_or_malformed_input_keys_are_refused(client, project):
    for key in ("source.zz", "..%2F..%2Fwindows", "source.%s" % ("0" * 32)):
        assert client.get(f"/projects/{project['id']}/captions/inputs/{key}").status_code == 404


# --- transcription, revisions, staleness --------------------------------------------


@needs_ffmpeg
def test_transcription_persists_a_raw_transcript_and_revision_one(client, project, video, fake_whisper):
    key = "source.%s" % add_source(client, project["id"], video)
    result = transcribe(client, project["id"], key)
    transcript_id = result["job"]["result"]["transcript_id"]
    assert fake_whisper.calls[0]["model"] == captions.DEFAULT_MODEL
    assert fake_whisper.calls[0]["request"]["language"] == "he"

    transcript = captions.read_transcript(project["id"], transcript_id)
    assert transcript["input"]["key"] == key
    assert transcript["input"]["fingerprint"]["digest"].startswith("sha256:")
    assert transcript["settings"]["model"] == captions.DEFAULT_MODEL and transcript["settings"]["word_timestamps"] is True
    assert transcript["result"]["word_count"] == len(FAKE_WORDS)
    assert len(transcript["segments"][0]["words"]) == len(FAKE_WORDS)

    revision = client.get(f"/projects/{project['id']}/captions/transcripts/{transcript_id}/revisions/1").json()
    assert revision["origin"] == "grouping" and revision["has_manual_edits"] is False
    assert " ".join(c["text"] for c in revision["captions"]) == " ".join(w for w, _, _ in FAKE_WORDS)
    assert revision["input"]["key"] == key

    summary = detail(client, project["id"], key)["transcripts"][0]
    assert summary["stale"] is False and summary["revisions"] == [1]


@needs_ffmpeg
def test_a_matching_transcript_is_reused_and_retranscription_makes_a_new_one(client, project, video, fake_whisper):
    key = "source.%s" % add_source(client, project["id"], video)
    first = transcribe(client, project["id"], key)["job"]["result"]["transcript_id"]

    again = transcribe(client, project["id"], key)
    assert again["reused"]["id"] == first and len(fake_whisper.calls) == 1

    other_language = transcribe(client, project["id"], key, settings={"language": "en"})
    assert other_language["job"]["result"]["transcript_id"] != first

    forced = transcribe(client, project["id"], key, force=True)
    new_id = forced["job"]["result"]["transcript_id"]
    assert new_id != first and len(fake_whisper.calls) == 3
    # The earlier transcript is untouched.
    assert captions.read_transcript(project["id"], first)["id"] == first
    assert len(detail(client, project["id"], key)["transcripts"]) == 3


@needs_ffmpeg
def test_transcription_refusals_are_specific(client, project, video, tmp_path, monkeypatch, fake_whisper):
    silent = make_video(tmp_path / "silent.mp4", audio=False)
    silent_key = "source.%s" % add_source(client, project["id"], silent)
    response = client.post(f"/projects/{project['id']}/captions/transcriptions", json={"input_key": silent_key})
    assert response.status_code == 400 and "no audio track" in response.json()["detail"]

    key = "source.%s" % add_source(client, project["id"], video)
    monkeypatch.setattr(captions, "_cuda_devices", 0)
    response = client.post(f"/projects/{project['id']}/captions/transcriptions",
                           json={"input_key": key, "settings": {"device": "cuda"}, "force": True})
    assert response.status_code == 503 and "CUDA" in response.json()["detail"]

    monkeypatch.setattr(captions, "model_downloaded", lambda model_id: False)
    response = client.post(f"/projects/{project['id']}/captions/transcriptions", json={"input_key": key, "force": True})
    assert response.status_code == 409 and "Download model" in response.json()["detail"]
    assert fake_whisper.calls == []


@needs_ffmpeg
def test_a_failed_or_cancelled_transcription_leaves_no_transcript(client, project, video, monkeypatch):
    key = "source.%s" % add_source(client, project["id"], video)
    monkeypatch.setattr(whisper_service.service, "transcribe", FakeWhisper(failure=("no_audio", "decoder said no")))
    response = client.post(f"/projects/{project['id']}/captions/transcriptions", json={"input_key": key})
    record = wait_for_job(client, project["id"], response.json()["job"]["id"])
    assert record["status"] == "failed" and "No audio could be decoded" in record["error"]

    blocking = FakeWhisper(block_until_cancelled=True)
    monkeypatch.setattr(whisper_service.service, "transcribe", blocking)
    response = client.post(f"/projects/{project['id']}/captions/transcriptions", json={"input_key": key, "force": True})
    job_id = response.json()["job"]["id"]
    deadline = time.monotonic() + 10
    while not blocking.calls and time.monotonic() < deadline:
        time.sleep(0.02)
    client.post(f"/projects/{project['id']}/jobs/{job_id}/cancel")
    assert wait_for_job(client, project["id"], job_id)["status"] == "cancelled"

    assert captions.list_transcripts(project["id"]) == []
    root = captions.transcripts_root(project["id"])
    assert not root.exists() or not any(root.iterdir())


@needs_ffmpeg
def test_edits_save_new_revisions_and_earlier_revisions_stay(client, project, video, fake_whisper):
    key = "source.%s" % add_source(client, project["id"], video)
    transcript_id = transcribe(client, project["id"], key)["job"]["result"]["transcript_id"]
    base = f"/projects/{project['id']}/captions/transcripts/{transcript_id}"
    first = client.get(base + "/revisions/1").json()

    edited = [dict(c) for c in first["captions"]]
    edited[0]["text"] = edited[0]["text"].replace("שלום", "שלומות")  # same word count
    edited[-1]["text"] = edited[-1]["text"] + " נוסף"  # one more word
    response = client.post(base + "/revisions", json={"base_revision": 1, "captions": edited})
    assert response.status_code == 201, response.text
    second = response.json()
    assert second["revision"] == 2 and second["parent_revision"] == 1 and second["has_manual_edits"]
    assert second["captions"][0]["word_timing"] == "word" and second["captions"][0]["words"][0]["text"] == "שלומות"
    assert second["captions"][-1]["word_timing"] == "group"

    # Revision 1 on disk is unchanged.
    assert client.get(base + "/revisions/1").json() == first
    # Saving on top of a superseded revision is refused rather than losing work.
    stale = client.post(base + "/revisions", json={"base_revision": 1, "captions": edited})
    assert stale.status_code == 409

    bad = [dict(c) for c in second["captions"]]
    bad[1]["start"] = bad[0]["start"]
    response = client.post(base + "/revisions", json={"base_revision": 2, "captions": bad})
    assert response.status_code == 400 and "Caption 2" in response.json()["detail"]
    assert captions.revision_numbers(project["id"], transcript_id) == [1, 2]


@needs_ffmpeg
def test_operations_split_merge_and_validate_without_saving(client, project, video, fake_whisper):
    key = "source.%s" % add_source(client, project["id"], video)
    transcript_id = transcribe(client, project["id"], key)["job"]["result"]["transcript_id"]
    base = f"/projects/{project['id']}/captions/transcripts/{transcript_id}"
    draft = client.get(base + "/revisions/1").json()["captions"]

    split = client.post(base + "/operations", json={"captions": draft, "operation": "split", "caption_id": draft[0]["id"], "word_index": 1}).json()
    assert split["valid"] and len(split["captions"]) == len(draft) + 1
    merged = client.post(base + "/operations", json={"captions": split["captions"], "operation": "merge", "caption_id": draft[0]["id"]}).json()
    assert [c["text"] for c in merged["captions"]] == [c["text"] for c in draft]

    overlapping = [dict(c) for c in draft]
    overlapping[0]["end"] = overlapping[1]["start"] + 0.2
    checked = client.post(base + "/validate", json={"captions": overlapping}).json()
    assert not checked["valid"] and checked["errors"][0]["caption_id"] == overlapping[1]["id"]
    fixed = client.post(base + "/operations", json={"captions": overlapping, "operation": "fix_overlaps"}).json()
    assert fixed["valid"]
    assert captions.revision_numbers(project["id"], transcript_id) == [1]


@needs_ffmpeg
def test_regrouping_asks_before_dropping_manual_edits_and_keeps_them_saved(client, project, video, fake_whisper):
    key = "source.%s" % add_source(client, project["id"], video)
    transcript_id = transcribe(client, project["id"], key)["job"]["result"]["transcript_id"]
    base = f"/projects/{project['id']}/captions/transcripts/{transcript_id}"
    edited = client.get(base + "/revisions/1").json()["captions"]
    edited[0]["text"] = edited[0]["text"] + " תיקון"
    client.post(base + "/revisions", json={"base_revision": 1, "captions": edited})

    refused = client.post(base + "/regroup", json={"base_revision": 2, "grouping": {"max_words": 2}})
    assert refused.status_code == 409 and "manual corrections" in refused.json()["detail"]

    regrouped = client.post(base + "/regroup", json={"base_revision": 2, "grouping": {"max_words": 2}, "confirm_discard_edits": True}).json()
    assert regrouped["revision"] == 3 and regrouped["origin"] == "regroup" and not regrouped["has_manual_edits"]
    assert all(len(c["text"].split()) <= 2 for c in regrouped["captions"])
    assert "תיקון" in client.get(base + "/revisions/2").json()["captions"][0]["text"]
    # The grouping becomes the project's default for the next transcription.
    assert client.get(f"/projects/{project['id']}/captions/inputs").json()["grouping"]["max_words"] == 2


@needs_ffmpeg
def test_a_changed_video_marks_its_captions_stale_and_blocks_export(client, project, video, fake_whisper):
    key = "source.%s" % add_source(client, project["id"], video)
    transcript_id = transcribe(client, project["id"], key)["job"]["result"]["transcript_id"]

    make_video(video, seconds=4.0)  # re-recorded under the same name
    caption_inputs._fingerprint_cache.clear()
    caption_inputs._metadata_cache.clear()

    summary = detail(client, project["id"], key)["transcripts"][0]
    assert summary["stale"] is True and "changed" in summary["reason"]
    response = client.post(f"/projects/{project['id']}/captions/exports",
                           json={"transcript_id": transcript_id, "revision": 1, "format": "srt"})
    assert response.status_code == 409
    # Nothing was deleted, and the new video can be transcribed afresh.
    assert captions.read_transcript(project["id"], transcript_id)
    assert transcribe(client, project["id"], key)["job"]["result"]["transcript_id"] != transcript_id


@needs_ffmpeg
def test_switching_inputs_keeps_every_input_s_captions(client, project, video, tmp_path, fake_whisper):
    first = "source.%s" % add_source(client, project["id"], video)
    second = "source.%s" % add_source(client, project["id"], make_video(tmp_path / "b.mp4"))
    transcribe(client, project["id"], first)
    for key in (second, first, second):
        assert client.put(f"/projects/{project['id']}/captions/selection", json={"input_key": key}).status_code == 200
    assert len(detail(client, project["id"], first)["transcripts"]) == 1
    assert detail(client, project["id"], second)["transcripts"] == []
    assert client.get(f"/projects/{project['id']}/captions/inputs").json()["selected"] == second


@needs_ffmpeg
def test_style_changes_never_transcribe_or_touch_revisions(client, project, video, fake_whisper):
    key = "source.%s" % add_source(client, project["id"], video)
    transcript_id = transcribe(client, project["id"], key)["job"]["result"]["transcript_id"]
    before = captions.revision_numbers(project["id"], transcript_id)
    calls = len(fake_whisper.calls)

    response = client.put(f"/projects/{project['id']}/captions/style",
                          json={"style": {**captions.default_style(), "font_family": "rubik", "text_color": "#FFEE00"}})
    assert response.status_code == 200 and response.json()["style"]["text_color"] == "#ffee00"
    assert response.json()["style_revision"] == 1
    assert len(fake_whisper.calls) == calls
    assert captions.revision_numbers(project["id"], transcript_id) == before
    assert len(captions.list_transcripts(project["id"])) == 1
    assert client.put(f"/projects/{project['id']}/captions/style", json={"style": {"text_color": "red"}}).status_code == 400


@needs_ffmpeg
def test_an_approved_plan_transcribes_through_the_same_path(client, project, video, fake_whisper):
    from backend import plans, storage

    source_id = add_source(client, project["id"], video)
    revision = plans.create_revision(
        storage.read_project(project["id"]),
        summary="Transcribe the take",
        actions=[{"id": "a1", "capability_id": "captions.transcribe", "resource_ids": [source_id],
                  "parameters": {"language": "he"}}],
        origin=plans.ORIGIN_AI,
        provider={"id": "mock", "label": "Demo", "model": None, "is_mock": True},
        instruction="transcribe",
    )
    base = f"/projects/{project['id']}/plans/{revision['plan_id']}/revisions/1"
    assert client.post(base + "/approve").status_code == 200
    record = wait_for_job(client, project["id"], client.post(base + "/execute").json()["id"])
    assert record["status"] == "succeeded", record["error"]
    output = record["result"]["action_results"][0]["output"]
    assert captions.read_transcript(project["id"], output["transcript_id"])["input"]["key"] == "source.%s" % source_id

    # Run again: the identical transcript is reused, Whisper is not called.
    record = wait_for_job(client, project["id"], client.post(base + "/execute").json()["id"])
    assert record["result"]["action_results"][0]["output"]["reused"] is True
    assert len(fake_whisper.calls) == 1


def test_caption_settings_do_not_outdate_plans(client, project):
    from backend import resources, storage

    before = resources.fingerprint(storage.read_project(project["id"]))
    client.put(f"/projects/{project['id']}/captions/style", json={"style": {"font_family": "rubik"}})
    assert resources.fingerprint(storage.read_project(project["id"])) == before


# --- exports ------------------------------------------------------------------------


@needs_ffmpeg
def test_an_export_snapshot_is_immutable_and_srt_uses_it(client, project, video, fake_whisper):
    key = "source.%s" % add_source(client, project["id"], video)
    transcript_id = transcribe(client, project["id"], key)["job"]["result"]["transcript_id"]
    base = f"/projects/{project['id']}/captions/transcripts/{transcript_id}"
    first = client.get(base + "/revisions/1").json()

    snapshot = caption_jobs.build_export_input(project["id"], {"transcript_id": transcript_id, "revision": 1, "format": "srt"})
    frozen = json.loads(json.dumps(snapshot))

    # Edit captions and style after the snapshot was taken.
    edited = [dict(c) for c in first["captions"]]
    edited[0]["text"] = "טקסט חדש לגמרי"
    client.post(base + "/revisions", json={"base_revision": 1, "captions": edited})
    client.put(f"/projects/{project['id']}/captions/style", json={"style": {"text_color": "#ff0000"}})
    assert snapshot == frozen

    # The export requested for revision 1 renders revision 1's text.
    response = client.post(f"/projects/{project['id']}/captions/exports",
                           json={"transcript_id": transcript_id, "revision": 1, "format": "srt"})
    record = wait_for_job(client, project["id"], response.json()["id"])
    assert record["status"] == "succeeded", record["error"]
    export = client.get(f"/projects/{project['id']}/captions/exports").json()["exports"][0]
    assert export["revision"] == 1 and export["downloadable"]
    body = client.get(f"/projects/{project['id']}/captions/exports/{export['export_id']}/file?download=true")
    assert body.status_code == 200 and "filename*=UTF-8''" in body.headers["content-disposition"]
    raw = body.content
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    assert first["captions"][0]["text"] in text and "טקסט חדש" not in text
    assert text.count("-->") == len(first["captions"])

    snapshot_file = caption_jobs.export_directory(project["id"], export["export_id"]) / "snapshot.json"
    assert json.loads(snapshot_file.read_text(encoding="utf-8"))["captions"] == frozen["captions"]

    # A second export of revision 2 goes to its own directory; the first stays.
    response = client.post(f"/projects/{project['id']}/captions/exports",
                           json={"transcript_id": transcript_id, "revision": 2, "format": "srt"})
    wait_for_job(client, project["id"], response.json()["id"])
    exports = client.get(f"/projects/{project['id']}/captions/exports").json()["exports"]
    assert len({e["export_id"] for e in exports}) == 2


@needs_ffmpeg
def test_a_cancelled_render_is_never_a_finished_export(client, project, video, fake_whisper, monkeypatch):
    key = "source.%s" % add_source(client, project["id"], video)
    transcript_id = transcribe(client, project["id"], key)["job"]["result"]["transcript_id"]
    monkeypatch.setattr(caption_jobs, "renderer_status", lambda: {"ready": True, "problems": []})
    seen = {}

    def fake_run(command, *, cancelled, on_output, cwd=None, timeout_seconds=None):
        job = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        seen["job"] = job
        Path(job["output_path"]).write_bytes(b"half a video")
        on_output('@@{"event":"stage","stage":"rendering"}\n')
        while not cancelled():
            time.sleep(0.02)
        raise processes.ProcessCancelled()

    monkeypatch.setattr(processes, "run", fake_run)
    response = client.post(f"/projects/{project['id']}/captions/exports",
                           json={"transcript_id": transcript_id, "revision": 1, "format": "mp4"})
    job_id = response.json()["id"]
    deadline = time.monotonic() + 10
    while "job" not in seen and time.monotonic() < deadline:
        time.sleep(0.02)
    client.post(f"/projects/{project['id']}/jobs/{job_id}/cancel")
    assert wait_for_job(client, project["id"], job_id)["status"] == "cancelled"

    export = client.get(f"/projects/{project['id']}/captions/exports").json()["exports"][0]
    assert export["status"] == "cancelled" and not export["downloadable"]
    directory = caption_jobs.export_directory(project["id"], export["export_id"])
    assert not any(path.suffix == ".mp4" for path in directory.rglob("*"))
    assert client.get(f"/projects/{project['id']}/captions/exports/{export['export_id']}/file").status_code == 404
    # The renderer was handed the snapshot's props, not the editor's state.
    assert seen["job"]["props"]["video"] == {"width": 320, "height": 240, "fps": 25.0, "duration_in_frames": 75}
    assert seen["job"]["props"]["direction"] == "rtl"


@needs_ffmpeg
def test_a_render_that_fails_verification_is_discarded(client, project, video, fake_whisper, monkeypatch):
    key = "source.%s" % add_source(client, project["id"], video)
    transcript_id = transcribe(client, project["id"], key)["job"]["result"]["transcript_id"]
    monkeypatch.setattr(caption_jobs, "renderer_status", lambda: {"ready": True, "problems": []})
    short = make_video(video.parent / "short.mp4", seconds=1.0)

    def fake_run(command, *, cancelled, on_output, cwd=None, timeout_seconds=None):
        job = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        shutil.copyfile(short, job["output_path"])  # wrong length
        return processes.ProcessResult(0, "", False)

    monkeypatch.setattr(processes, "run", fake_run)
    response = client.post(f"/projects/{project['id']}/captions/exports",
                           json={"transcript_id": transcript_id, "revision": 1, "format": "mp4"})
    record = wait_for_job(client, project["id"], response.json()["id"])
    assert record["status"] == "failed" and "expected" in record["error"]
    export = client.get(f"/projects/{project['id']}/captions/exports").json()["exports"][0]
    assert export["status"] == "failed" and not export["downloadable"]


# --- the Whisper service, with a fake worker process --------------------------------

FAKE_SERVE_WORKER = textwrap.dedent(
    r'''
    import json, os, sys, time
    def emit(e):
        sys.stdout.write("@@" + json.dumps(e) + "\n"); sys.stdout.flush()
    args = sys.argv
    model = args[args.index("--model") + 1]
    emit({"event": "loading"})
    if model == "missing":
        emit({"event": "fatal", "kind": "model_missing", "message": "not cached"}); sys.exit(2)
    emit({"event": "ready", "load_seconds": 0.01})
    for line in sys.stdin:
        request = json.loads(line)
        if request.get("command") == "exit":
            sys.exit(0)
        if request["input"] == "slow":
            emit({"event": "progress", "id": request["id"], "position": 0.5})
            time.sleep(60)
        if request["input"] == "bad":
            emit({"event": "error", "id": request["id"], "kind": "no_audio", "message": "no audio stream"}); continue
        with open(request["output"], "w") as handle:
            json.dump({"pid": os.getpid()}, handle)
        emit({"event": "done", "id": request["id"], "output": request["output"]})
    '''
)


@pytest.fixture
def service(tmp_path, monkeypatch):
    script = tmp_path / "fake_serve_worker.py"
    script.write_text(FAKE_SERVE_WORKER, encoding="utf-8")
    monkeypatch.setattr(whisper_service, "WORKER_SCRIPT", script)
    instance = whisper_service.WhisperService()
    yield instance
    instance.stop()


def run_service(service, tmp_path, model="m", source="ok", cancelled=lambda: False, name="out"):
    output = tmp_path / ("%s.json" % name)
    events = []
    result = service.transcribe(
        model=model, device="cpu", compute_type="int8", cpu_threads=2,
        request={"id": name, "input": source, "output": str(output)},
        cancelled=cancelled, on_event=events.append,
    )
    return result, json.loads(output.read_text()), events


def test_the_service_reuses_a_loaded_model_and_reloads_for_a_different_one(service, tmp_path):
    first, out1, events1 = run_service(service, tmp_path, name="a")
    second, out2, _ = run_service(service, tmp_path, name="b")
    assert not first["reused_model"] and second["reused_model"]
    assert out1["pid"] == out2["pid"]
    assert [e["event"] for e in events1][:2] == ["loading", "ready"]

    third, out3, _ = run_service(service, tmp_path, model="other", name="c")
    assert not third["reused_model"] and out3["pid"] != out2["pid"]


def test_the_service_kills_the_worker_on_cancel_and_recovers(service, tmp_path):
    flag = threading.Event()
    threading.Timer(0.5, flag.set).start()
    with pytest.raises(whisper_service.WhisperCancelled):
        run_service(service, tmp_path, source="slow", cancelled=flag.is_set, name="slow")
    assert service.status()["loaded"] is False
    result, _out, _ = run_service(service, tmp_path, name="after")
    assert result["reused_model"] is False


def test_the_service_reports_missing_models_and_request_failures(service, tmp_path):
    with pytest.raises(whisper_service.WhisperFailed) as failure:
        run_service(service, tmp_path, model="missing", name="x")
    assert failure.value.kind == "model_missing"

    with pytest.raises(whisper_service.WhisperFailed) as failure:
        run_service(service, tmp_path, source="bad", name="y")
    assert failure.value.kind == "no_audio"
    # A failed request does not throw away a healthy loaded model.
    assert run_service(service, tmp_path, name="z")[0]["reused_model"] is True


# --- the real renderer ----------------------------------------------------------------

BUNDLE_CACHE = Path(tempfile.gettempdir()) / "video-factory-test-bundles"


def _gray_crop(path: Path, at: float, crop: str) -> bytes:
    return subprocess.run(
        ["ffmpeg.exe", "-hide_banner", "-loglevel", "error", "-ss", str(at), "-i", str(path),
         "-frames:v", "1", "-vf", "crop=%s,format=gray" % crop, "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    ).stdout


def _difference(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / max(1, len(a))


def _audio_onset(path: Path) -> float:
    output = subprocess.run(
        ["ffmpeg.exe", "-hide_banner", "-i", str(path), "-af", "silencedetect=n=-40dB:d=0.2", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stderr
    for line in output.splitlines():
        if "silence_end" in line:
            return float(line.split("silence_end:")[1].split()[0])
    raise AssertionError("no sound found")


def _mean_volume(path: Path) -> float:
    output = subprocess.run(
        ["ffmpeg.exe", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stderr
    return float(output.split("mean_volume:")[1].split()[0])


@needs_ffmpeg
@needs_renderer
def test_real_remotion_export_has_captions_audio_once_and_the_right_length(client, project, video, fake_whisper, monkeypatch):
    monkeypatch.setattr(caption_jobs, "bundle_cache_directory", lambda: BUNDLE_CACHE)
    key = "source.%s" % add_source(client, project["id"], video)
    transcript_id = transcribe(client, project["id"], key)["job"]["result"]["transcript_id"]
    base = f"/projects/{project['id']}/captions/transcripts/{transcript_id}"
    # Known caption data: one caption from 1.0 s to 2.0 s, on a white box.
    known = [{"id": "known", "start": 1.0, "end": 2.0, "text": "שלום world 123, (בדיקה).", "words": None}]
    assert client.post(base + "/revisions", json={"base_revision": 1, "captions": known}).status_code == 201
    client.put(f"/projects/{project['id']}/captions/style", json={"style": {
        "background": "box", "box_color": "#ffffff", "box_opacity": 1.0, "text_color": "#000000",
        "vertical_position_percent": 100, "safe_margin_percent": 5}})

    response = client.post(f"/projects/{project['id']}/captions/exports",
                           json={"transcript_id": transcript_id, "revision": 2, "format": "mp4"})
    record = wait_for_job(client, project["id"], response.json()["id"], timeout=600)
    assert record["status"] == "succeeded", record["error"]

    export = client.get(f"/projects/{project['id']}/captions/exports").json()["exports"][0]
    verification = export["verification"]
    assert verification["streams"] == {"video": 1, "audio": 1}
    assert (verification["width"], verification["height"]) == (320, 240)
    assert abs(verification["duration_seconds"] - 3.0) <= verification["tolerance_seconds"]

    output = caption_jobs.export_directory(project["id"], export["export_id"]) / export["output"]["filename"]
    # Audio: the tone starts at 1.0 s in the source and in the export, at the same loudness.
    assert abs(_audio_onset(output) - _audio_onset(video)) < 0.03
    assert abs(_mean_volume(output) - _mean_volume(video)) < 1.0

    # Captions: the bottom band differs from the source only while the caption is on.
    band = "320:60:0:180"
    during = _difference(_gray_crop(output, 1.5, band), _gray_crop(video, 1.5, band))
    before = _difference(_gray_crop(output, 0.5, band), _gray_crop(video, 0.5, band))
    after = _difference(_gray_crop(output, 2.5, band), _gray_crop(video, 2.5, band))
    # Re-encoding a busy test pattern alone gives some difference; the caption
    # must add far more than that, and only while it is on screen.
    assert during > 2.5 * max(before, after), (before, during, after)


@needs_ffmpeg
@needs_renderer
def test_cancelling_a_real_render_stops_it_and_leaves_no_partial_file(client, project, tmp_path, fake_whisper, monkeypatch):
    monkeypatch.setattr(caption_jobs, "bundle_cache_directory", lambda: BUNDLE_CACHE)
    longer = make_video(tmp_path / "long.mp4", seconds=40.0, size="1280x720", rate=30)
    key = "source.%s" % add_source(client, project["id"], longer)
    transcript_id = transcribe(client, project["id"], key)["job"]["result"]["transcript_id"]
    response = client.post(f"/projects/{project['id']}/captions/exports",
                           json={"transcript_id": transcript_id, "revision": 1, "format": "mp4"})
    job_id = response.json()["id"]

    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        record = client.get(f"/projects/{project['id']}/jobs/{job_id}").json()
        if (record["progress_percent"] or 0) > 2 or record["status"] in jobs.FINISHED_STATUSES:
            break
        time.sleep(0.1)
    assert record["status"] == "running", record
    client.post(f"/projects/{project['id']}/jobs/{job_id}/cancel")
    assert wait_for_job(client, project["id"], job_id, timeout=30)["status"] == "cancelled"

    export = client.get(f"/projects/{project['id']}/captions/exports").json()["exports"][0]
    directory = caption_jobs.export_directory(project["id"], export["export_id"])
    assert export["status"] == "cancelled"
    assert not any(path.suffix == ".mp4" for path in directory.rglob("*"))
    # Nothing keeps rendering in the background: no process still has this
    # export's render job, or a partial file in this export's folder, on its
    # command line (node, the headless browser and the encoder all would).
    time.sleep(1.0)
    survivors = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and "
         "$_.CommandLine -like '*%s*' } | "
         "Select-Object -ExpandProperty ProcessId" % export["export_id"]],
        capture_output=True, text=True,
    ).stdout.split()
    assert survivors == []
    assert not any(path.suffix == ".mp4" for path in directory.rglob("*"))
