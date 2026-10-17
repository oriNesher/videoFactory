"""The project's stages as one pipeline: what is accepted, and what the video is now.

The editor shows five stages in order — footage, cutting, captions, zooms,
B-roll — and a stage counts as done only when the user accepted it:

    footage    the saved clip list, accepted here
    cutting    one cut run's merged video, accepted here
    captions   the caption revision and style on that video, accepted here
    zooms      an approved zoom run of that video (`zooms.approve`)
    broll      an approved or skipped B-roll decision on that zoomed video
               (`broll.decide`), or B-roll switched off

The first three acceptances live in `project.settings["pipeline"]`. Each one
names exactly what was accepted (ids, a fingerprint, revision numbers), so it
becomes outdated by itself when that result changes; nothing is cleared. A
stage is done only when its own acceptance holds and the stage before it is
done.

The preview is the furthest video reached — the zoomed video, else the
accepted (or newest) merged cut, else the first clip — composed exactly like
the B-roll review: current captions and B-roll drawn live over it.

Reading never renders, transcribes or calls a provider, so polling is free.
"""

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

from . import broll, caption_inputs, cutting, storage, zooms

SETTINGS_KEY = "pipeline"

STAGE_FOOTAGE = "footage"
STAGE_CUTTING = "cutting"
STAGE_CAPTIONS = "captions"
STAGE_ZOOMS = "zooms"
STAGE_BROLL = "broll"

STAGES = (STAGE_FOOTAGE, STAGE_CUTTING, STAGE_CAPTIONS, STAGE_ZOOMS, STAGE_BROLL)
ACCEPTED_HERE = (STAGE_FOOTAGE, STAGE_CUTTING, STAGE_CAPTIONS)

LABELS = {
    STAGE_FOOTAGE: "Footage",
    STAGE_CUTTING: "Cutting & merging",
    STAGE_CAPTIONS: "Captions",
    STAGE_ZOOMS: "Zooms",
    STAGE_BROLL: "B-roll",
}


class PipelineError(storage.ProjectError):
    """A stage that cannot be accepted, phrased for the user."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _token(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_acceptances(project: dict) -> dict:
    block = project.get("settings", {}).get(SETTINGS_KEY)
    block = block if isinstance(block, dict) else {}
    return {stage: block[stage] for stage in ACCEPTED_HERE if isinstance(block.get(stage), dict)}


# --- stages ------------------------------------------------------------------------


def _stage(stage_id: str, **values: Any) -> dict:
    return {
        "id": stage_id,
        "label": LABELS[stage_id],
        "accepted_here": stage_id in ACCEPTED_HERE,
        "done": False,
        # What "Accept" would accept now (stages accepted here only), with the
        # token the request must echo back.
        "candidate": None,
        "blocked_reason": None,
        "acceptance": None,
        "outdated_reason": None,
        "summary": "",
        **values,
    }


def _footage(project: dict, accepted: dict | None) -> dict:
    sources = project["sources"]
    ids = [source["id"] for source in sources]
    missing = [source for source in sources if not os.path.isfile(source["path"])]

    candidate, blocked = None, None
    if not sources:
        blocked = "Add the clips for this video first."
    elif missing:
        blocked = "%d clips are missing. Remove them or put them back." % len(missing)
    else:
        candidate = {"source_ids": ids, "summary": "%d clips, in this order" % len(ids)}
        candidate["token"] = _token({"source_ids": ids})

    outdated = None
    if accepted is not None:
        if accepted.get("source_ids") != ids:
            outdated = "The clips or their order changed after they were accepted."
        elif missing:
            outdated = "%d accepted clips are missing." % len(missing)
    return _stage(
        STAGE_FOOTAGE,
        done=accepted is not None and outdated is None,
        candidate=candidate,
        blocked_reason=blocked,
        acceptance=accepted,
        outdated_reason=outdated,
        summary="%d clips" % len(sources),
    )


def _merged_key(manifest: dict) -> str | None:
    combined = manifest.get("combined")
    if manifest.get("status") != cutting.RUN_SUCCEEDED or not isinstance(combined, dict):
        return None
    if combined.get("status") != cutting.CLIP_SUCCEEDED:
        return None
    return "cut.%s.%s" % (manifest["run_id"], cutting.COMBINED_OUTPUT_ID)


def _digest(project_id: str, key: str) -> str | None:
    try:
        described = caption_inputs.describe(project_id, key)
    except storage.ProjectError:
        return None
    return (described.get("fingerprint") or {}).get("digest")


def _run_source_ids(manifest: dict) -> list[str]:
    return [entry.get("source_id") for entry in manifest.get("sources") or []]


def _cutting(project_id: str, project: dict, accepted: dict | None) -> dict:
    footage_ids = {source["id"] for source in project["sources"]}
    candidate, blocked = None, None
    for manifest in cutting.list_runs(project_id):
        key = _merged_key(manifest)
        if key is None or not set(_run_source_ids(manifest)) <= footage_ids:
            continue
        digest = _digest(project_id, key)
        if digest is None:
            continue
        candidate = {
            "key": key,
            "run_id": manifest["run_id"],
            "digest": digest,
            "created_at": manifest.get("finished_at") or manifest.get("created_at"),
            "summary": "Merged video of cut run %s" % manifest["run_id"][:6],
        }
        candidate["token"] = _token({"key": key, "digest": digest})
        break
    if candidate is None:
        blocked = "Render the clips and the merged video first."

    outdated = None
    if accepted is not None:
        current = _digest(project_id, accepted.get("key") or "") if accepted.get("key") else None
        try:
            manifest = cutting.read_manifest(project_id, accepted.get("run_id") or "")
        except storage.ProjectError:
            manifest = None
        if current is None:
            outdated = "The accepted merged video is no longer available."
        elif current != accepted.get("digest"):
            outdated = "The accepted merged video changed on disk."
        elif manifest is not None and not set(_run_source_ids(manifest)) <= footage_ids:
            outdated = "The accepted cut uses clips that are no longer in the footage."
    return _stage(
        STAGE_CUTTING,
        done=accepted is not None and outdated is None,
        candidate=candidate,
        blocked_reason=blocked,
        acceptance=accepted,
        outdated_reason=outdated,
        summary=candidate["summary"] if candidate else "Not cut yet",
    )


def _overlay_reference(project_id: str, key: str | None) -> tuple[dict | None, int]:
    """The captions on `key` now: `{transcript_id, revision, style_revision}`."""
    if key is None:
        return None, 0
    overlay = broll.captions_for_base(project_id, key)
    return zooms.overlay_reference(overlay), len(overlay["captions"]) if overlay else 0


def _captions(project_id: str, cut_key: str | None, accepted: dict | None) -> dict:
    reference, count = _overlay_reference(project_id, cut_key)
    candidate, blocked = None, None
    if cut_key is None:
        blocked = "Accept the cut first; captions are made on the merged video."
    elif reference is None:
        blocked = "Transcribe the merged video and review its captions first."
    else:
        candidate = {
            "key": cut_key,
            **reference,
            "summary": "%d captions, revision %d" % (count, reference["revision"]),
        }
        candidate["token"] = _token({"key": cut_key, **reference})

    outdated = None
    if accepted is not None:
        if accepted.get("key") != cut_key:
            outdated = "These captions were accepted for a different video than the accepted cut."
        elif {name: accepted.get(name) for name in ("transcript_id", "revision", "style_revision")} != reference:
            outdated = "The captions or their style changed after they were accepted."
    return _stage(
        STAGE_CAPTIONS,
        done=accepted is not None and outdated is None,
        candidate=candidate,
        blocked_reason=blocked,
        acceptance=accepted,
        outdated_reason=outdated,
        summary=candidate["summary"] if candidate else "No captions yet",
    )


def _zoom_runs_for(project_id: str, cut_key: str | None) -> list[dict]:
    """Finished zoom runs of the cut's current bytes, newest first."""
    if cut_key is None:
        return []
    return [
        manifest for manifest in zooms.list_runs(project_id)
        if manifest.get("status") == zooms.RUN_SUCCEEDED
        and manifest["input"]["key"] == cut_key
        and zooms.input_is_current(project_id, manifest)[0]
    ]


def _zooms(project_id: str, runs: list[dict]) -> tuple[dict, str | None]:
    approved = None
    for manifest in runs:
        try:
            if zooms.review_state(project_id, manifest)["approved"]:
                approved = manifest
                break
        except storage.ProjectError:
            continue
    chosen = approved or (runs[0] if runs else None)
    key = "zoom.%s" % chosen["run_id"] if chosen else None
    summary = (
        "Approved zoom run %s" % approved["run_id"][:6] if approved
        else "Zoom run %s, not approved yet" % chosen["run_id"][:6] if chosen
        else "Not zoomed yet"
    )
    return _stage(STAGE_ZOOMS, done=approved is not None, summary=summary), key


def _broll(project_id: str, project: dict, zoom_key: str | None, zooms_done: bool) -> dict:
    if not broll.read_settings(project)["enabled"]:
        return _stage(STAGE_BROLL, done=zooms_done, summary="B-roll is switched off")
    if zoom_key is None:
        return _stage(STAGE_BROLL, summary="Waiting for zooms")
    try:
        review = broll.review_state(project_id, broll.compose(project_id, zoom_key))
    except storage.ProjectError as error:
        return _stage(STAGE_BROLL, summary=error.message)
    if review["approved"]:
        summary = "Approved"
    elif review["skipped"]:
        summary = "Skipped"
    else:
        summary = review["outdated_reason"] or "Not reviewed yet"
    return _stage(STAGE_BROLL, done=review["approved"] or review["skipped"], summary=summary)


# --- the whole picture ------------------------------------------------------------


def _preview(project_id: str, project: dict, stage_id: str, key: str | None) -> dict | None:
    if key is None:
        return None
    try:
        composed = broll.compose(project_id, key)
    except storage.ProjectError as error:
        return {"stage": stage_id, "key": key, "label": None, "props": None, "digest": None, "error": error.message}
    return {
        "stage": stage_id,
        "key": key,
        "label": composed["base"]["label"],
        # Without media URLs, exactly like the B-roll review: the browser adds them.
        "props": composed["props"],
        "digest": composed["digest"],
        "assets": {asset_id: {"digest": entry["fingerprint"]["digest"]} for asset_id, entry in composed["assets"].items()},
        "error": None,
    }


def state(project_id: str) -> dict:
    project = storage.read_project(project_id)
    accepted = read_acceptances(project)

    footage = _footage(project, accepted.get(STAGE_FOOTAGE))
    cut = _cutting(project_id, project, accepted.get(STAGE_CUTTING))
    cut_key = (cut["acceptance"] or {}).get("key") if cut["done"] else None
    caption_stage = _captions(project_id, cut_key, accepted.get(STAGE_CAPTIONS))
    zoom_stage, zoom_key = _zooms(project_id, _zoom_runs_for(project_id, cut_key))
    broll_stage = _broll(project_id, project, zoom_key if zoom_stage["done"] else None, zoom_stage["done"])

    stages = [footage, cut, caption_stage, zoom_stage, broll_stage]
    # A stage is done only on top of a done stage: a change upstream reopens
    # everything after it without touching what was recorded there.
    for previous, current in zip(stages, stages[1:]):
        if not previous["done"] and current["done"]:
            current["done"] = False
            current["outdated_reason"] = current["outdated_reason"] or "%s is not accepted." % previous["label"]
    current_stage = next((stage["id"] for stage in stages if not stage["done"]), None)

    # The furthest video reached, accepted or not.
    first_source = next((source for source in project["sources"] if os.path.isfile(source["path"])), None)
    if zoom_key is not None:
        preview_stage, preview_key = STAGE_BROLL if zoom_stage["done"] else STAGE_ZOOMS, zoom_key
    elif cut_key is not None or cut["candidate"] is not None:
        preview_stage = STAGE_CAPTIONS if cut_key is not None else STAGE_CUTTING
        preview_key = cut_key or cut["candidate"]["key"]
    elif first_source is not None:
        preview_stage, preview_key = STAGE_FOOTAGE, "source.%s" % first_source["id"]
    else:
        preview_stage, preview_key = STAGE_FOOTAGE, None

    return {
        "stages": stages,
        "current_stage": current_stage,
        "stages_left": sum(1 for stage in stages if not stage["done"]),
        "preview": _preview(project_id, project, preview_stage, preview_key),
    }


def accept(project_id: str, stage_id: Any, token: Any) -> dict:
    """Accept exactly what the user was shown, or refuse if it changed since."""
    if stage_id not in ACCEPTED_HERE:
        raise PipelineError("This stage is approved in its own section.", status_code=404)
    current = state(project_id)
    stage = next(entry for entry in current["stages"] if entry["id"] == stage_id)
    index = STAGES.index(stage_id)
    if index > 0 and not current["stages"][index - 1]["done"]:
        raise PipelineError("Accept %s first." % LABELS[STAGES[index - 1]], status_code=409)
    candidate = stage["candidate"]
    if candidate is None:
        raise PipelineError(stage["blocked_reason"] or "There is nothing to accept yet.", status_code=409)
    if token != candidate["token"]:
        raise PipelineError("The result changed while you were looking at it. Review it again.", status_code=409)

    record = {name: value for name, value in candidate.items() if name not in ("token", "summary")}
    record["accepted_at"] = _now()
    project = storage.read_project(project_id)
    block = project["settings"].get(SETTINGS_KEY)
    block = dict(block) if isinstance(block, dict) else {}
    block[stage_id] = record
    project["settings"][SETTINGS_KEY] = block
    storage.write_project(project)
    return state(project_id)
