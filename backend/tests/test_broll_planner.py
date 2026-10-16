"""Tests for stage 3B: automatic B-roll selection and timing.

Every model answer here is controlled (`FakeBrollPlanner`); no test calls
Gemini. Covered:

- **Catalog** — only available, user-described assets are offered; no paths
  or file names; undescribed assets produce one library-level message; the
  shortlist for large libraries is recorded.
- **Validation** — unknown assets/templates/settings, times, source ranges,
  described moments, overlaps, coverage, malformed answers; frame
  quantisation and its tolerance; valid empty plans.
- **Provider** — repair once then fail, missing key, daily quota, timeouts
  with bounded retries, no fallback.
- **Workflow** — the job snapshot, transcript reuse, cache reuse and
  duplicate protection, manual corrections never overwritten, restoring
  revisions, approval of the exact revision (also of "no B-roll"), the
  zoom → B-roll hand-over and continuation.
- **Rendering** — one real Remotion export of a planner-made plan, checked
  frame by frame, by audio and with FFprobe.
"""

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from backend import (
    broll,
    broll_library,
    broll_planner,
    broll_planner_jobs,
    caption_jobs,
    captions,
    gemini,
    jobs,
    storage,
    zooms,
)
from backend.config import DEFAULT_GEMINI_MODEL, GEMINI_API_KEY_ENV_VAR, ZOOM_PROVIDER_ENV_VAR
from backend.tests.test_broll import (  # noqa: F401 - fixtures
    _frame,
    _pcm,
    _rms,
    add,
    add_source,
    client,
    environment,
    fake_whisper,
    import_asset,
    make_asset_video,
    make_image,
    media_dir,
    needs_ffmpeg,
    needs_renderer,
    project,
    state_of,
    wait_for_job,
)
from backend.tests.test_zooms import FakePlanner, ffprobe_json, make_grid_video, plan_of

pytestmark = needs_ffmpeg


class FakeBrollPlanner:
    """Stands in for Gemini. Answers are dicts, exceptions, or callables of the request."""

    def __init__(self, *answers, gate=None):
        self.answers = list(answers)
        self.calls = []
        self.gate = gate

    def describe(self):
        return {"id": "gemini", "label": "Google Gemini", "model": DEFAULT_GEMINI_MODEL, "is_mock": False}

    def complete(self, message, schema):
        self.calls.append({"message": message, "schema": schema})
        if self.gate is not None:
            self.gate.wait(20)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if callable(answer):
            answer = answer(json.loads(message.split("\n\nYour previous answer", 1)[0]))
        if isinstance(answer, Exception):
            raise answer
        return {"data": answer, "usage": {"total_token_count": 42}}

    def requests(self):
        return [json.loads(call["message"].split("\n\nYour previous answer", 1)[0]) for call in self.calls]


@pytest.fixture
def fake(monkeypatch):
    holder = FakeBrollPlanner({"summary": "Nothing fits.", "placements": []})
    monkeypatch.setattr(broll_planner, "get_planner", lambda: holder)
    monkeypatch.setattr(broll_planner, "SLEEP", lambda seconds: None)
    return holder


@pytest.fixture
def planner(monkeypatch):
    """The zoom planner's stand-in, for a 6 s video."""
    from backend import zoom_planner

    stand_in = FakePlanner(plan_of((0, 1.0, "NORMAL"), (1.0, 6.0, "PUNCH_STRONG")))
    monkeypatch.setattr(zoom_planner, "get_planner", lambda: stand_in)
    return stand_in


@pytest.fixture
def base_file(tmp_path):
    folder = tmp_path / "צילומים"
    folder.mkdir()
    return make_grid_video(folder / "טייק 1.mp4", seconds=6, size="360x640", rate=25)


@pytest.fixture
def lib(client, media_dir):
    """A small library: a described image, a video with one described moment,
    and an undescribed image."""
    image = import_asset(client, make_image(media_dir / "תמונה אדומה.png"))
    clip = import_asset(client, make_asset_video(media_dir / "הקלטת מסך.mp4"))
    plain = import_asset(client, make_image(media_dir / "ללא תיאור.png", colour="yellow"))
    assert client.patch(f"/broll/library/{image['id']}", json={
        "description": "גרף מכירות אדום", "tags": ["גרף", "מכירות"],
    }).status_code == 200
    response = client.patch(f"/broll/library/{clip['id']}", json={
        "description": "הקלטת מסך של מערכת הניהול",
        "moments": [{"start": 0.5, "end": 3.0, "description": "לוח הבקרה הירוק"}],
    })
    assert response.status_code == 200, response.text
    return {"image": image["id"], "clip": clip["id"], "plain": plain["id"]}


@pytest.fixture
def setup(client, project, base_file, lib, fake_whisper):
    return {"project": project["id"], "key": add_source(client, project["id"], base_file), **lib}


def answer(*placements, summary="Two visuals that match the speech.", suggestions=None):
    document = {"summary": summary, "placements": list(placements)}
    if suggestions is not None:
        document["missing_asset_suggestions"] = suggestions
    return document


def image_at(asset, start, end, **extra):
    return {"asset_id": asset, "template_id": "fullframe", "start": start, "end": end,
            "rationale": "The speaker mentions sales.", **extra}


def clip_at(asset, start, end, source_start=0.5, **extra):
    return {"asset_id": asset, "template_id": "pip", "start": start, "end": end, "source_start": source_start,
            "rationale": "The dashboard is discussed.", **extra}


def start(client, s, **body):
    return client.post(f"/projects/{s['project']}/broll/inputs/{s['key']}/planner/runs", json=body)


def plan_and_wait(client, s, **body):
    response = start(client, s, **body)
    assert response.status_code == 201, response.text
    job = response.json()["job"]
    assert job is not None, response.json()
    return wait_for_job(client, s["project"], job["id"])


# --- the catalog -------------------------------------------------------------------------


def test_only_described_available_assets_are_offered_without_paths_or_names(client, setup, fake):
    fake.answers = [answer(summary="No match.")]
    record = plan_and_wait(client, setup)
    assert record["status"] == "succeeded", record["error"]
    (request,) = fake.requests()
    offered = {asset["asset_id"] for asset in request["assets"]}
    assert offered == {setup["image"], setup["clip"]}  # the undescribed image is left out
    assert fake.calls[0]["schema"]["properties"]["placements"]["items"]["properties"]["asset_id"]["enum"] == sorted(
        offered, key=[setup["image"], setup["clip"]].index)
    text = json.dumps(request, ensure_ascii=False)
    for forbidden in ("תמונה אדומה", "הקלטת מסך.mp4", ".png", ".mp4", "C:\\", "AIza", "בי-רול"):
        assert forbidden not in text, forbidden
    clip = next(asset for asset in request["assets"] if asset["asset_id"] == setup["clip"])
    assert clip["described_moments"] == [{"source_start": 0.5, "source_end": 3.0, "description": "לוח הבקרה הירוק"}]
    assert "described_by_user" in clip and "duration_seconds" in clip["measured"]
    # Only the whitelisted settings; asset audio cannot be unmuted by the planner.
    templates = {template["id"]: template for template in request["templates"]}
    assert set(templates["fullframe"]["settings_you_may_set"]) == {"fit"}
    assert set(templates["pip"]["settings_you_may_set"]) == {"position", "size_percent"}
    assert request["transcript"]["segments"][0]["text"].startswith("שלום")
    assert "words" not in request["transcript"]["segments"][0]  # phrase level only
    state = state_of(client, setup["project"], setup["key"])
    catalog = state["planner"]["catalog"]
    assert catalog["described_count"] == 2 and catalog["undescribed_count"] == 1
    assert "left out of automatic planning" in catalog["message"]


def test_an_undescribed_library_gives_one_actionable_message_and_sends_nothing(client, project, base_file, media_dir, fake):
    import_asset(client, make_image(media_dir / "a.png"))
    import_asset(client, make_image(media_dir / "b.png", colour="blue"))
    s = {"project": project["id"], "key": add_source(client, project["id"], base_file)}
    response = start(client, s)
    assert response.status_code == 409 and "None of the 2 usable asset(s)" in response.json()["detail"]
    assert "Asset library" in response.json()["detail"] and "skip" in response.json()["detail"]
    assert fake.calls == [] and jobs.list_jobs(project["id"]) == []
    state = state_of(client, s["project"], s["key"])
    assert state["planner"]["catalog"]["ready"] is False
    # Undescribed assets stay usable by hand.
    assert add(client, s["project"], s["key"], client.get("/broll/library").json()["assets"][0]["id"], 0.5).status_code == 201


def test_large_libraries_are_shortlisted_by_word_overlap_and_the_choice_is_recorded():
    def candidate(index, words):
        return {"asset_id": "%012x" % index, "order": index, "words": words, "view": {}}

    candidates = [candidate(index, "נוף ים") for index in range(50)]
    candidates[45]["words"] = "גרף המכירות"
    chosen, info = broll_planner.shortlist(candidates, "ועכשיו נראה את הגרף של המכירות", limit=10)
    assert len(chosen) == 10 and candidates[45] in chosen and info["method"] == "word_overlap"
    assert info["described"] == 50 and len(info["left_out"]) == 40 and "40 left out" in info["note"]
    all_of_them, info = broll_planner.shortlist(candidates[:5], "x")
    assert len(all_of_them) == 5 and info["method"] == "all" and info["left_out"] == []


def test_moments_are_validated_against_the_asset(client, lib):
    bad = client.patch(f"/broll/library/{lib['clip']}", json={"moments": [{"start": 2.5, "end": 9, "description": "x"}]})
    assert bad.status_code == 400 and "inside the video" in bad.json()["detail"]
    image = client.patch(f"/broll/library/{lib['image']}", json={"moments": [{"start": 0, "end": 1, "description": "x"}]})
    assert image.status_code == 400 and "Only a video" in image.json()["detail"]
    empty = client.patch(f"/broll/library/{lib['clip']}", json={"moments": [{"start": 0, "end": 1, "description": " "}]})
    assert empty.status_code == 400


# --- validation ---------------------------------------------------------------------------


@pytest.fixture
def checker(setup, client):
    base = broll.base_reference(broll.describe_base(setup["project"], setup["key"]))
    candidates = broll_planner.readiness(broll_library.visible_entries(setup["project"]))["candidates"]
    offered = {candidate["asset_id"]: candidate for candidate in candidates}

    def check(raw):
        return broll_planner.validate_answer(raw, base=base, offered=offered, library=broll.library_index(setup["project"]))

    return check


@pytest.mark.parametrize(
    "build, fragment",
    [
        (lambda s: "not json", "not a JSON object"),
        (lambda s: {"summary": "x", "placements": "none"}, "must be a list"),
        (lambda s: {"placements": []}, "summary"),
        (lambda s: {**answer(), "command": "rm"}, "Unknown top-level"),
        (lambda s: answer(image_at(s["plain"], 1, 3)), "not one of the offered assets"),
        (lambda s: answer(image_at("0123456789ab", 1, 3)), "not one of the offered assets"),
        (lambda s: answer({**image_at(s["image"], 1, 3), "template_id": "kenburns"}), "uses template"),
        (lambda s: answer(image_at(s["image"], 1, 3, settings={"muted": False})), "cannot be set"),
        (lambda s: answer(image_at(s["image"], 1, 3, settings={"position": "top_left"})), "cannot be set"),
        (lambda s: answer(clip_at(s["clip"], 1, 2.5, settings={"size_percent": 90})), "between 20 and 60"),
        (lambda s: answer(image_at(s["image"], float("nan"), 3)), "finite"),
        (lambda s: answer(image_at(s["image"], "1", 3)), "finite"),
        (lambda s: answer(image_at(s["image"], -0.5, 1)), "starts before 0"),
        (lambda s: answer(image_at(s["image"], 5, 6.2)), "past the end"),
        (lambda s: answer(image_at(s["image"], 1, 1.5)), "at least 1.0 s"),
        (lambda s: answer(image_at(s["image"], 2, 1)), "at least 1.0 s"),
        (lambda s: answer({**image_at(s["image"], 1, 3), "rationale": " "}), "rationale"),
        (lambda s: answer({**image_at(s["image"], 1, 3), "path": "C:\\x"}), "unknown field"),
        (lambda s: answer(image_at(s["image"], 1, 3, source_start=0.5)), "omit source_start"),
        (lambda s: answer(clip_at(s["clip"], 1, 3, source_start=0.0)), "described moment"),
        (lambda s: answer(clip_at(s["clip"], 1, 3, source_start=1.5)), "described moment"),  # runs past 3.0
        (lambda s: answer(image_at(s["image"], 0.5, 2.5), clip_at(s["clip"], 2.0, 3.0)), "overlap"),
        (lambda s: answer(image_at(s["image"], 0, 2.5), clip_at(s["clip"], 3.0, 4.0, source_start=0.5),
                          image_at(s["image"], 4.0, 5.0)), "at most 50%"),
    ],
)
def test_invalid_answers_are_rejected_with_a_reason(setup, checker, build, fragment):
    plan, errors = checker(build(setup))
    assert plan is None and any(fragment in error for error in errors), errors


def test_a_video_described_only_as_a_whole_plays_from_its_start(client, setup, checker):
    client.patch(f"/broll/library/{setup['clip']}", json={"moments": []})
    candidates = broll_planner.readiness(broll_library.visible_entries(setup["project"]))["candidates"]
    clip = next(candidate for candidate in candidates if candidate["asset_id"] == setup["clip"])
    assert "must be 0" in clip["view"]["source_rule"]
    base = broll.base_reference(broll.describe_base(setup["project"], setup["key"]))
    offered = {candidate["asset_id"]: candidate for candidate in candidates}
    plan, errors = broll_planner.validate_answer(answer(clip_at(setup["clip"], 1, 2.5, source_start=1.0)), base=base,
                                                 offered=offered, library=broll.library_index(setup["project"]))
    assert plan is None and "described only as a whole" in errors[0]
    plan, errors = broll_planner.validate_answer(answer({**clip_at(setup["clip"], 1, 2.5), "source_start": None}),
                                                 base=base, offered=offered, library=broll.library_index(setup["project"]))
    assert errors == [] and plan["placements"][0]["source_start"] == 0.0
    assert any("default" in note for note in plan["normalisation"]["notes"])


def test_valid_answers_are_quantised_to_frames_and_kept_otherwise(setup, checker):
    plan, errors = checker(answer(
        image_at(setup["image"], 0.59, 1.987, settings={"fit": "cover"}, spoken_text="שלום לכולם"),
        clip_at(setup["clip"], 4.5, 6.015, source_start=0.53),  # end within half a frame past the end
        suggestions=[{"at_seconds": 2.0, "description": "A photo of the team"}, {"oops": 1}],
    ))
    assert errors == [], errors
    first, second = plan["placements"]
    # 25 fps, the composition's own rule: shown from frame ceil(t·25) — 0.59 → 15 (0.60),
    # 1.987 → 50 (2.00), 4.5 → 113 (4.52); the in-point 0.53 → round(13.25) = 13 (0.52).
    assert (first["start"], first["end"]) == (0.6, 2.0) and first["settings"]["fit"] == "cover"
    assert first["settings"]["muted"] is True and first["source_start"] is None
    assert (second["start"], second["end"], second["source_start"]) == (4.52, 6.0, 0.52)
    assert second["settings"]["position"] == "auto"
    assert plan["normalisation"]["max_shift_seconds"] < 0.04  # under one frame
    assert plan["explanations"][first["id"]] == {"rationale": "The speaker mentions sales.", "spoken_text": "שלום לכולם"}
    assert plan["suggestions"] == [{"at_seconds": 2.0, "description": "A photo of the team"}]
    assert plan["statistics"]["placement_count"] == 2


def test_an_empty_plan_is_valid(checker):
    plan, errors = checker(answer(summary="Nothing in the library matches this talk about holidays."))
    assert errors == [] and plan["placements"] == [] and "holidays" in plan["summary"]


# --- the job: planning, provider failures, reuse ------------------------------------------


def test_a_valid_plan_becomes_a_reviewable_revision_with_its_reasons(client, setup, fake, fake_whisper):
    fake.answers = [answer(image_at(setup["image"], 0.6, 1.6), clip_at(setup["clip"], 2.0, 3.2),
                           suggestions=[{"description": "A shot of the office"}])]
    record = plan_and_wait(client, setup)
    assert record["status"] == "succeeded", record["error"]
    assert record["result"]["applied"] and record["result"]["placement_count"] == 2
    assert len(fake_whisper.calls) == 1  # transcribed once because there was no transcript

    state = state_of(client, setup["project"], setup["key"])
    plan = state["plan"]
    assert plan["origin"] == "planner" and plan["revision"] == 1 and plan["planner"]["run_id"]
    assert [(p["start"], p["end"]) for p in state["props"]["broll"]["placements"]] == [(0.6, 1.6), (2.0, 3.2)]
    run = state["planner"]["run"]
    assert run["summary"] == "Two visuals that match the speech." and run["suggestions"][0]["description"] == "A shot of the office"
    assert set(run["explanations"]) == {p["id"] for p in plan["placements"]}
    assert run["provider"]["model"] == DEFAULT_GEMINI_MODEL and run["prompt_version"] == broll_planner.PROMPT_VERSION
    assert run["preferences"] == {"preference": "restrained", "caption_band": "bottom"}
    stored = client.get(f"/projects/{setup['project']}/broll/planner/runs/{run['run_id']}").json()
    assert stored["base"]["digest"] == plan["base"]["digest"] and stored["transcript"]["revision"] == 1
    assert stored["catalog"]["asset_digests"].keys() == {setup["image"], setup["clip"]}
    assert stored["template_catalog_version"] == 1 and stored["request_sent"]["transcript"]["segments"]
    assert "AIza" not in json.dumps(stored) and "AIza" not in json.dumps(jobs.list_jobs(setup["project"]))
    assert state["review"]["can_approve"]


def test_pressing_plan_again_or_polling_never_asks_twice(client, setup, fake, fake_whisper):
    gate = threading.Event()
    fake.gate = gate
    fake.answers = [answer(image_at(setup["image"], 0.6, 1.6))]
    first = start(client, setup).json()["job"]
    again = start(client, setup).json()["job"]
    assert again["id"] == first["id"]  # a second click returns the running job
    for _ in range(3):
        state_of(client, setup["project"], setup["key"])  # polling sends nothing
    gate.set()
    assert wait_for_job(client, setup["project"], first["id"])["status"] == "succeeded"
    assert len(fake.calls) == 1

    reused = start(client, setup)
    assert reused.status_code == 201 and reused.json()["job"] is None and reused.json()["reused"]["run_id"]
    assert len(fake.calls) == 1 and len(fake_whisper.calls) == 1

    # Caption styling that does not move the caption band is not a planning input.
    style = captions.read_style(storage.read_project(setup["project"]))["style"]
    changed = {**style, "font_weight": 400 if style["font_weight"] != 400 else 500}
    assert client.put(f"/projects/{setup['project']}/captions/style", json={"style": changed}).status_code == 200
    assert start(client, setup).json()["job"] is None and len(fake.calls) == 1

    # Moving captions to the top half is a layout constraint: making a new plan
    # needs confirming (a plan exists) and then asks again.
    moved = {**changed, "vertical_position_percent": 15}
    client.put(f"/projects/{setup['project']}/captions/style", json={"style": moved})
    refused = start(client, setup)
    assert refused.status_code == 409 and "confirm" in refused.json()["detail"]
    record = plan_and_wait(client, setup, replace_current=True)
    assert record["status"] == "succeeded" and len(fake.calls) == 2
    assert json.loads(fake.calls[1]["message"])["captions"]["band"] == "top"


def test_a_malformed_answer_gets_one_repair_and_then_fails_without_changing_anything(client, setup, fake):
    good = answer(image_at(setup["image"], 0.6, 1.6))
    fake.answers = [{"summary": "x", "placements": [image_at(setup["plain"], 0, 1)]}, good]
    record = plan_and_wait(client, setup)
    assert record["status"] == "succeeded" and len(fake.calls) == 2
    assert "rejected by validation" in fake.calls[1]["message"] and "not one of the offered assets" in fake.calls[1]["message"]
    run = state_of(client, setup["project"], setup["key"])["planner"]["run"]
    assert run["audit"]["repair_used"] is True

    fake.calls.clear()
    fake.answers = [{"summary": "x"}, {"summary": "still wrong", "placements": [image_at(setup["image"], 0, 5)]}]
    record = plan_and_wait(client, setup, force_replan=True, replace_current=True)
    assert record["status"] == "failed" and "rejected even after one correction" in record["error"]
    assert "rejected-" in record["error"] and len(fake.calls) == 2
    assert state_of(client, setup["project"], setup["key"])["plan"]["revision"] == 1  # untouched
    assert any(path.name.startswith("rejected-") for path in broll_planner_jobs.planner_root(setup["project"]).iterdir())


@pytest.mark.parametrize(
    "error, attempts, fragment",
    [
        (gemini.GeminiError("daily_quota", "The Gemini free-tier daily request quota for this model is used up."), 1, "daily request quota"),
        (gemini.GeminiError("timeout", "Gemini did not answer within 90 seconds.", retryable=True), 3, "Gave up after 3 attempts"),
        (gemini.GeminiError("quota", "Gemini's free-tier rate limit was reached.", retryable=True, retry_after_seconds=0.01), 3, "rate limit"),
        (gemini.GeminiError("unavailable", "Gemini is temporarily unavailable (status 503).", retryable=True), 3, "temporarily unavailable"),
        (gemini.GeminiError("invalid_json", "Gemini's answer was not valid JSON.", retryable=True), 3, "not valid JSON"),
    ],
)
def test_provider_failures_are_errors_with_bounded_retries_never_an_empty_plan(client, setup, fake, error, attempts, fragment):
    fake.answers = [error]
    record = plan_and_wait(client, setup)
    assert record["status"] == "failed" and fragment in record["error"]
    assert len(fake.calls) == attempts
    state = state_of(client, setup["project"], setup["key"])
    assert state["plan"] is None and state["planner"]["last_job"]["status"] == "failed"
    # Retry re-runs the same snapshot; this time it works.
    fake.calls.clear()
    fake.answers = [answer(summary="Nothing fits.")]
    retried = client.post(f"/projects/{setup['project']}/jobs/{record['id']}/retry").json()
    assert wait_for_job(client, setup["project"], retried["id"])["status"] == "succeeded" and len(fake.calls) == 1


def test_a_missing_key_refuses_to_start_and_nothing_falls_back(client, setup, monkeypatch):
    monkeypatch.delenv(GEMINI_API_KEY_ENV_VAR, raising=False)
    sent = []
    monkeypatch.setattr(broll_planner.MockBrollPlanner, "complete", lambda self, *a: sent.append(a))
    monkeypatch.setattr(gemini.GeminiClient, "generate_json", lambda self, **k: sent.append(k))
    response = start(client, setup)
    assert response.status_code == 409 and GEMINI_API_KEY_ENV_VAR in response.json()["detail"]
    assert sent == [] and jobs.list_jobs(setup["project"]) == []
    assert state_of(client, setup["project"], setup["key"])["planner"]["provider"]["ready"] is False
    monkeypatch.setenv(ZOOM_PROVIDER_ENV_VAR, "anthropic")
    with pytest.raises(Exception, match="Unknown planning provider"):
        broll_planner.get_planner()


def test_the_demo_provider_is_labelled_and_matches_words(client, setup, monkeypatch):
    monkeypatch.setenv(ZOOM_PROVIDER_ENV_VAR, "mock")
    client.patch(f"/broll/library/{setup['image']}", json={"description": "מבחן"})  # a word the speaker says
    record = plan_and_wait(client, setup)
    assert record["status"] == "succeeded", record["error"]
    state = state_of(client, setup["project"], setup["key"])
    assert state["planner"]["run"]["provider"]["is_mock"] and "not AI" in state["planner"]["run"]["summary"]
    assert [p["asset_id"] for p in state["plan"]["placements"]] == [setup["image"]]


# --- corrections, revisions, approval ----------------------------------------------------


def test_manual_corrections_are_kept_and_a_new_plan_never_overwrites_silently(client, setup, fake):
    fake.answers = [answer(image_at(setup["image"], 0.6, 1.6), clip_at(setup["clip"], 2.0, 3.2))]
    plan_and_wait(client, setup)
    pid, key = setup["project"], setup["key"]
    plan = state_of(client, pid, key)["plan"]
    removed = client.delete(f"/projects/{pid}/broll/inputs/{key}/placements/{plan['placements'][1]['id']}?base_revision=1")
    assert removed.status_code == 200 and removed.json()["origin"] == "manual"
    state = state_of(client, pid, key)
    assert state["planner"]["edited_since"] and state["planner"]["run"]["run_id"] == plan["planner"]["run_id"]

    refused = start(client, setup, force_replan=True)
    assert refused.status_code == 409 and state_of(client, pid, key)["plan"]["revision"] == 2
    fake.answers = [answer(image_at(setup["image"], 4.0, 5.5))]
    record = plan_and_wait(client, setup, force_replan=True, replace_current=True)
    assert record["status"] == "succeeded" and record["result"]["applied_revision"] == 3
    history = state_of(client, pid, key)["revisions"]
    assert [(r["revision"], r["origin"], r["placement_count"]) for r in history] == [(3, "planner", 1), (2, "manual", 1), (1, "planner", 2)]

    restored = client.post(f"/projects/{pid}/broll/inputs/{key}/plan/restore/2", json={"base_revision": 3})
    assert restored.status_code == 201 and restored.json()["revision"] == 4 and restored.json()["origin"] == "restore"
    assert restored.json()["placements"] == broll.read_revision(pid, plan["plan_id"], 2)["placements"]


def test_an_edit_during_planning_is_never_overwritten(client, setup, fake):
    gate = threading.Event()
    fake.gate = gate
    fake.answers = [answer(image_at(setup["image"], 0.6, 1.6))]
    job = start(client, setup).json()["job"]
    deadline = time.monotonic() + 60
    while not fake.calls and time.monotonic() < deadline:
        time.sleep(0.05)
    # While the model "thinks": the user places B-roll by hand and edits a description.
    assert add(client, setup["project"], setup["key"], setup["clip"], 4.0, revision=0).status_code == 201
    client.patch(f"/broll/library/{setup['image']}", json={"description": "something else entirely"})
    gate.set()
    record = wait_for_job(client, setup["project"], job["id"])
    assert record["status"] == "succeeded" and record["result"]["applied"] is False
    assert "your changes were kept" in record["result"]["not_applied_reason"]
    state = state_of(client, setup["project"], setup["key"])
    assert state["plan"]["revision"] == 1 and state["plan"]["origin"] == "manual"
    # The job used the description as it was when requested (immutable snapshot).
    assert "גרף מכירות אדום" in fake.calls[0]["message"] and "something else" not in fake.calls[0]["message"]
    applied = client.post(f"/projects/{setup['project']}/broll/inputs/{setup['key']}/planner/runs/{record['result']['run_id']}/apply",
                          json={"base_revision": 1})
    assert applied.status_code == 201 and applied.json()["revision"] == 2 and applied.json()["origin"] == "planner"


def test_approval_names_the_exact_revision_and_unrelated_assets_do_not_outdate_it(client, setup, fake, media_dir, monkeypatch):
    monkeypatch.setattr(caption_jobs, "renderer_status", lambda: {"ready": False, "problems": ["not prepared"]})
    fake.answers = [answer(image_at(setup["image"], 0.6, 1.6))]
    plan_and_wait(client, setup)
    pid, key = setup["project"], setup["key"]
    state = state_of(client, pid, key)
    approved = client.post(f"/projects/{pid}/broll/inputs/{key}/approve", json={"composition_digest": state["composition_digest"]})
    assert approved.status_code == 200 and approved.json()["next"]["kind"] == "blocked"  # renderer missing in this test
    assert state_of(client, pid, key)["review"]["approved"]

    import_asset(client, make_image(media_dir / "unrelated.png", colour="green"))
    assert state_of(client, pid, key)["review"]["approved"]  # an unrelated asset changes nothing

    fake.answers = [answer(image_at(setup["image"], 3.0, 4.2))]
    plan_and_wait(client, setup, force_replan=True, replace_current=True)
    state = state_of(client, pid, key)
    assert not state["review"]["approved"] and "plan changed" in state["review"]["outdated_reason"]


def test_a_valid_no_broll_result_explains_itself_and_can_be_approved(client, setup, fake, monkeypatch):
    monkeypatch.setattr(caption_jobs, "renderer_status", lambda: {"ready": False, "problems": ["not prepared"]})
    fake.answers = [answer(summary="The talk is a greeting; no described asset illustrates it.")]
    record = plan_and_wait(client, setup)
    assert record["status"] == "succeeded" and record["result"]["placement_count"] == 0
    state = state_of(client, setup["project"], setup["key"])
    assert state["plan"]["placements"] == [] and "greeting" in state["planner"]["run"]["summary"]
    assert state["review"]["can_approve"] and state["props"]["broll"]["placements"] == []
    response = client.post(f"/projects/{setup['project']}/broll/inputs/{setup['key']}/approve",
                           json={"composition_digest": state["composition_digest"]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision"]["decision"] == "approved" and body["decision"]["placement_count"] == 0
    # It continues like a skip: to the captions export (blocked here: no renderer) — never a B-roll export.
    assert body["next"]["kind"] in ("caption_export", "blocked")
    assert state_of(client, setup["project"], setup["key"])["review"]["approved"]


def test_approving_zooms_continues_into_planning_on_the_zoomed_video(client, setup, fake, fake_whisper, planner):
    pid = setup["project"]
    job = client.post(f"/projects/{pid}/zooms/runs", json={"input_key": setup["key"]}).json()["job"]
    assert wait_for_job(client, pid, job["id"])["status"] == "succeeded"
    run_id = zooms.list_runs(pid)[0]["run_id"]
    run = client.get(f"/projects/{pid}/zooms/runs/{run_id}").json()
    fake.answers = [answer(image_at(setup["image"], 0.6, 1.6))]
    response = client.post(f"/projects/{pid}/zooms/runs/{run_id}/approve",
                           json={"output_digest": run["output"]["fingerprint"]["digest"], "captions": run["review"]["overlay_reference"]})
    following = response.json()["next"]
    assert following["kind"] == "broll" and following["planning"]["status"] == "started"
    record = wait_for_job(client, pid, following["planning"]["job_id"])
    assert record["status"] == "succeeded", record["error"]
    zoom_key = "zoom.%s" % run_id
    state = state_of(client, pid, zoom_key)
    assert state["plan"]["origin"] == "planner" and len(state["plan"]["placements"]) == 1
    (request,) = fake.requests()
    assert request["zooms"]["intervals"] == [{"start": 1.0, "end": 6.0, "preset": "PUNCH_STRONG"}]
    assert len(fake_whisper.calls) == 1  # the clean parent's transcript, not a new one
    # Approving zooms again does not plan twice.
    again = broll_planner_jobs.auto_start(pid, zoom_key)
    assert again["status"] == "existing" and len(fake.calls) == 1


# --- a real composition ------------------------------------------------------------------------


@needs_renderer
def test_a_planned_composition_renders_at_the_planned_times(client, setup, fake, fake_whisper, base_file, tmp_path):
    """Planner-made plan → review props → approval → the Stage 3A export.

    6 s, 360×640, 25 fps base with a tone at 1.0–1.5 s and Hebrew captions.
    The plan: the red image full-frame at 0.59–1.6 s (shown from frame
    ceil(0.59 × 25) = 15, stored as 0.6; frames 15–39) and the clip picture-in-picture at 2.0–3.2 s from its described
    0.5 s moment (lime, frames 50–79), whose own 300 Hz tone must stay muted.
    """
    fake.answers = [answer(image_at(setup["image"], 0.59, 1.6), clip_at(setup["clip"], 2.0, 3.2, source_start=0.5))]
    assert plan_and_wait(client, setup)["status"] == "succeeded"
    pid, key = setup["project"], setup["key"]
    state = state_of(client, pid, key)
    assert state["issues"] == [] and state["captions"]["caption_count"] > 0
    response = client.post(f"/projects/{pid}/broll/inputs/{key}/approve", json={"composition_digest": state["composition_digest"]})
    assert response.json()["next"]["kind"] == "broll_export"
    record = wait_for_job(client, pid, response.json()["next"]["job"]["id"])
    assert record["status"] == "succeeded", record["error"]
    from backend import broll_jobs

    (export,) = broll_jobs.list_exports(pid)
    exported, _ = broll_jobs.resolve_export_file(pid, export["export_id"])
    snapshot = json.loads((broll_jobs.export_directory(pid, export["export_id"]) / "snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["props"] == state["props"] and snapshot["plan"] == {"plan_id": state["plan"]["plan_id"], "revision": 1}

    probe = ffprobe_json(exported, "-count_packets", "-show_entries",
                         "stream=codec_type,codec_name,width,height,nb_read_packets:format=duration")
    streams = probe["streams"]
    assert [(s["codec_type"], s["codec_name"]) for s in streams] == [("video", "h264"), ("audio", "aac")]
    assert (streams[0]["width"], streams[0]["height"]) == (360, 640) and int(streams[0]["nb_read_packets"]) == 150
    assert float(probe["format"]["duration"]) == pytest.approx(6.0, abs=0.1)

    def red_rows(frame):
        return sum(1 for y in range(640) if frame[y, 180, 0] > 200 and frame[y, 180, 1] < 60 and frame[y, 180, 2] < 60)

    def lime(pixel):
        return pixel[1] > 200 and pixel[0] < 80 and pixel[2] < 80

    assert red_rows(_frame(exported, 14)) < 50 and _frame(exported, 14)[:180].max() > 60  # the speaker
    for index in (15, 27, 39):
        assert abs(red_rows(_frame(exported, index)) - 202.5) <= 3, index  # letterboxed image
    assert red_rows(_frame(exported, 40)) < 50
    for index in (50, 65, 79):
        assert lime(_frame(exported, index)[68, 274]), index  # PiP top right, from the 0.5 s moment
    for index in (49, 80):
        assert not lime(_frame(exported, index)[68, 274]), index
    band = _frame(exported, 20)[420:640]
    assert (band > 230).all(axis=2).sum() > 50  # the caption is readable over the image

    ours, source = _pcm(exported), _pcm(base_file)
    assert 0.89 < _rms(ours, 1.05, 1.45) / _rms(source, 1.05, 1.45) < 1.12  # the speech once, unchanged
    assert _rms(ours, 2.05, 3.15) < 0.003  # the asset's tone is muted
    assert len(fake_whisper.calls) == 1 and len(fake.calls) == 1  # nothing upstream redone

    # Kept for a person to look at (VIDEO_FACTORY_KEEP_FRAMES=<folder> to keep them after the test).
    target = Path(os.environ.get("VIDEO_FACTORY_KEEP_FRAMES") or tmp_path)
    target.mkdir(parents=True, exist_ok=True)
    for index in (14, 20, 39, 60):
        subprocess.run(["ffmpeg.exe", "-hide_banner", "-loglevel", "error", "-y", "-i", str(exported), "-vf",
                        "select=eq(n\\,%d)" % index, "-frames:v", "1", str(target / ("planned-%03d.png" % index))], check=True)
    assert base_file.is_file()
