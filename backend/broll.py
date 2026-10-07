"""B-roll plans, placement validation, composition and review (stage 3A).

A **B-roll plan** belongs to one base video — the exact bytes of it — and
lists placements on that video's own timeline:

    intermediates/broll/
      plans/<plan-id>/rev-0001.json     immutable revisions, one per change
      decisions/<input-key>.json        the latest approve / skip decision

**Timeline.** Every time in a plan is seconds from the first frame of the
base video: the edited (cut, possibly zoomed) video, never an original clip.
A zoomed video shares its clean parent's timing frame for frame, so a plan
on either is on the same clock. Overlays never change the base video's
length, speed or speech: the base keeps playing underneath every placement.

**Placements** (schema version 1):

    {"id": "p1a2b3c4d", "asset_id": "<library id>", "template_id": "fullframe" | "pip",
     "start": 3.2, "end": 6.2,          # seconds on the base timeline, half-open
     "source_start": 0.0,               # video assets: in-point in the asset; images: null
     "settings": {...}}                 # the template's settings, defaults filled in

Validation is the same for a placement typed by the user and for one a
planner writes in stage 3B: known, available assets and templates; finite
times; at least `MIN_PLACEMENT_SECONDS`; inside the base video; for a video
asset, `source_start + (end − start)` within the asset's measured length —
a short asset is never looped or frozen to fill a longer placement; and **no
overlaps**: only one B-roll is on screen at a time, and a conflicting plan is
refused with both placements named rather than layered arbitrarily.

**Frames.** The composition converts seconds to frames with the captions'
rule (`frontend/src/captions/timing.ts`): a placement `[start, end)` is on
screen on frames `ceil(start·fps)` … `ceil(end·fps) − 1`. Back-to-back
placements meet on one frame boundary. A video asset's in-point is rounded
to the nearest composition frame (`round(source_start·fps)`).

**Review.** The composed result — base video, B-roll, captions — is what the
user approves. A decision records a digest of exactly what was composed
(resolved props, base and asset fingerprints, plan revision, captions), so
any later change to any of them marks it outdated. Approving never renders
for review; the review player is the export's own composition.
"""

import hashlib
import json
import math
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import broll_library, broll_templates, caption_inputs, captions, storage, zooms

SETTINGS_KEY = "broll"

PLAN_SCHEMA_VERSION = 1
PLAN_KIND = "video_factory.broll_plan"
DECISION_KIND = "video_factory.broll_decision"

ORIGIN_MANUAL = "manual"
ORIGIN_PLANNER = "planner"  # reserved for stage 3B; validated exactly like manual plans
ORIGINS = (ORIGIN_MANUAL, ORIGIN_PLANNER)

OVERLAP_POLICY = "reject"
MIN_PLACEMENT_SECONDS = 0.2
DEFAULT_PLACEMENT_SECONDS = {broll_library.MEDIA_IMAGE: 3.0, broll_library.MEDIA_VIDEO: 4.0}
MAX_PLACEMENTS = 200
# How far past the end of the base video (or of a video asset) a time may be
# before it is an error rather than rounding: a millisecond.
TIME_TOLERANCE_SECONDS = 0.001

DECISION_APPROVED = "approved"
DECISION_SKIPPED = "skipped"

TIMELINE_NOTE = (
    "Seconds from the first frame of the base video (the edited video, after cutting and "
    "zooms). Never original-clip time."
)

_PLAN_ID = re.compile(r"^[0-9a-f]{12}$")
_PLACEMENT_ID = re.compile(r"^p[0-9a-f]{8}$")


class BrollError(storage.ProjectError):
    """A B-roll problem the user can act on."""


class BrollValidationFailed(BrollError):
    def __init__(self, errors: list[dict]) -> None:
        shown = "; ".join(error["message"] for error in errors[:5])
        more = " (and %d more)" % (len(errors) - 5) if len(errors) > 5 else ""
        super().__init__("The B-roll plan was not saved. %s%s" % (shown, more), status_code=400)
        self.errors = errors


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _hash(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _clock(seconds: float) -> str:
    minutes, rest = divmod(max(0.0, seconds), 60)
    return "%d:%04.1f" % (minutes, rest)


# --- settings ---------------------------------------------------------------------


def read_settings(project: dict) -> dict:
    block = project.get("settings", {}).get(SETTINGS_KEY)
    block = block if isinstance(block, dict) else {}
    selected = block.get("selected_input")
    enabled = block.get("enabled")
    return {
        # On by default: an approved zoom result continues to B-roll, where it
        # can be skipped in one click. Off sends it straight to the export.
        "enabled": enabled if isinstance(enabled, bool) else True,
        "selected_input": selected if isinstance(selected, str) else None,
    }


def save_settings(project_id: str, *, enabled: Any = None, selected_input: Any = None) -> dict:
    project = storage.read_project(project_id)
    current = read_settings(project)
    if enabled is not None:
        if not isinstance(enabled, bool):
            raise BrollError("enabled must be true or false.")
        current["enabled"] = enabled
    if selected_input is not None:
        caption_inputs.resolve(project_id, selected_input)
        current["selected_input"] = selected_input
    project["settings"][SETTINGS_KEY] = current
    storage.write_project(project)
    return current


# --- base videos -------------------------------------------------------------------


def list_inputs(project_id: str) -> list[dict]:
    """Every video B-roll can go on: cut outputs, footage, imports, zoomed videos."""
    return caption_inputs.list_inputs(project_id)


def default_input(project_id: str) -> str | None:
    """The factory's latest approved result first: an approved, current zoom
    output; otherwise the zoom module's own default (newest merged cut video,
    then a cut clip, then the first available video)."""
    for manifest in zooms.list_runs(project_id):
        if manifest.get("status") != zooms.RUN_SUCCEEDED:
            continue
        try:
            if zooms.review_state(project_id, manifest)["approved"]:
                return "zoom.%s" % manifest["run_id"]
        except storage.ProjectError:
            continue
    return zooms.default_input(project_id)


def describe_base(project_id: str, key: Any) -> dict:
    """The base video as it is now, or a refusal saying why it cannot be used."""
    described = caption_inputs.describe(project_id, key)
    if not described["available"] or described["metadata"] is None or described["fingerprint"] is None:
        raise BrollError(described["error"] or "The video is not available.", status_code=409)
    return described


def base_reference(described: dict) -> dict:
    composition = described["composition"]
    return {
        "key": described["key"],
        "kind": described["kind"],
        "label": described["label"],
        "digest": described["fingerprint"]["digest"],
        "duration_seconds": described["metadata"]["duration_seconds"],
        "fps": composition["fps"],
        "width": composition["width"],
        "height": composition["height"],
        "duration_in_frames": composition["duration_in_frames"],
    }


# --- plans ------------------------------------------------------------------------


def plans_root(project_id: str) -> Path:
    return storage.project_directory(project_id) / "intermediates" / "broll" / "plans"


def decisions_root(project_id: str) -> Path:
    return storage.project_directory(project_id) / "intermediates" / "broll" / "decisions"


def validate_plan_id(raw: Any) -> str:
    if not isinstance(raw, str) or not _PLAN_ID.match(raw):
        raise BrollError("B-roll plan not found.", status_code=404)
    return raw


def _revision_path(project_id: str, plan_id: str, revision: int) -> Path:
    return plans_root(project_id) / validate_plan_id(plan_id) / ("rev-%04d.json" % revision)


def revision_numbers(project_id: str, plan_id: str) -> list[int]:
    directory = plans_root(project_id) / validate_plan_id(plan_id)
    if not directory.is_dir():
        return []
    numbers = []
    for path in directory.glob("rev-*.json"):
        match = re.match(r"^rev-(\d{4})\.json$", path.name)
        if match:
            numbers.append(int(match.group(1)))
    return sorted(numbers)


def read_revision(project_id: str, plan_id: Any, revision: Any) -> dict:
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise BrollError("B-roll plan revision not found.", status_code=404)
    try:
        document = storage.read_json(_revision_path(project_id, plan_id, revision))
    except FileNotFoundError as error:
        raise BrollError("B-roll plan revision not found.", status_code=404) from error
    except (OSError, ValueError) as error:
        raise BrollError("The B-roll plan cannot be read: %s" % error, status_code=422) from error
    if not isinstance(document, dict) or document.get("kind") != PLAN_KIND:
        raise BrollError("The file is not a B-roll plan.", status_code=422)
    if (document.get("schema_version") or 0) > PLAN_SCHEMA_VERSION:
        raise BrollError("The B-roll plan was written by a newer version.", status_code=422)
    return document


def latest_revision(project_id: str, plan_id: str) -> dict | None:
    numbers = revision_numbers(project_id, plan_id)
    return read_revision(project_id, plan_id, numbers[-1]) if numbers else None


def list_plans(project_id: str) -> list[dict]:
    """The latest revision of every plan, newest first."""
    root = plans_root(project_id)
    if not root.is_dir():
        return []
    documents = []
    for directory in root.iterdir():
        if not directory.is_dir() or not _PLAN_ID.match(directory.name):
            continue
        try:
            latest = latest_revision(project_id, directory.name)
        except storage.ProjectError:
            continue
        if latest is not None:
            documents.append(latest)
    documents.sort(key=lambda document: document.get("created_at") or "", reverse=True)
    return documents


def find_plan(project_id: str, key: str, digest: str) -> dict | None:
    """The newest plan for exactly these base bytes, at its latest revision."""
    for document in list_plans(project_id):
        base = document.get("base") or {}
        if base.get("key") == key and base.get("digest") == digest:
            return document
    return None


# --- validation --------------------------------------------------------------------


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def library_index() -> dict[str, dict]:
    return {entry["id"]: entry for entry in broll_library.read_entries()}


def validate_placements(raw: Any, base: dict, assets: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """Check and normalise a placement list against the base video and library.

    Returns `(placements sorted by start, errors)`. Each error is
    `{"index", "placement_id", "message"}`, phrased for the user.
    """
    if not isinstance(raw, list):
        return [], [{"index": None, "placement_id": None, "message": "The placements must be a list."}]
    if len(raw) > MAX_PLACEMENTS:
        return [], [{"index": None, "placement_id": None, "message": "Up to %d placements." % MAX_PLACEMENTS}]

    duration = float(base["duration_seconds"])
    errors: list[dict] = []
    placements: list[dict] = []
    seen_ids: set[str] = set()

    for index, item in enumerate(raw):
        label = "B-roll %d" % (index + 1)

        def fail(message: str, placement_id: Any = None) -> None:
            errors.append({"index": index, "placement_id": placement_id, "message": "%s: %s" % (label, message)})

        if not isinstance(item, dict):
            fail("must be an object.")
            continue
        placement_id = item.get("id")
        if not isinstance(placement_id, str) or not _PLACEMENT_ID.match(placement_id):
            fail("has no valid id.")
            continue
        if placement_id in seen_ids:
            fail("repeats the id %s." % placement_id, placement_id)
            continue
        seen_ids.add(placement_id)
        unknown = sorted(set(item) - {"id", "asset_id", "template_id", "start", "end", "source_start", "settings"})
        if unknown:
            fail("has unknown field(s): %s." % ", ".join(unknown), placement_id)
            continue

        asset = assets.get(item.get("asset_id")) if isinstance(item.get("asset_id"), str) else None
        if asset is None:
            fail("its asset is not in the B-roll library (it may have been removed).", placement_id)
            continue
        status, reason = broll_library.status_of(asset)
        if status != broll_library.STATUS_AVAILABLE:
            fail("its asset \"%s\" cannot be used: %s" % (broll_library.public_entry(asset)["display_name"], reason), placement_id)
            continue

        settings, setting_errors = broll_templates.validate_settings(item.get("template_id"), item.get("settings"))
        if setting_errors:
            for message in setting_errors:
                fail(message, placement_id)
            continue

        start, end = item.get("start"), item.get("end")
        if not _finite(start) or not _finite(end):
            fail("start and end must be finite numbers of seconds.", placement_id)
            continue
        start, end = float(start), float(end)
        if start < 0:
            fail("starts before the beginning of the video.", placement_id)
            continue
        if end > duration + TIME_TOLERANCE_SECONDS:
            fail("ends at %s, after the end of the video (%s)." % (_clock(end), _clock(duration)), placement_id)
            continue
        end = min(end, duration)
        if end - start < MIN_PLACEMENT_SECONDS - 1e-9:
            fail("must last at least %.1f s (it lasts %.2f s)." % (MIN_PLACEMENT_SECONDS, max(0.0, end - start)), placement_id)
            continue

        source_start = item.get("source_start")
        if asset["media_type"] == broll_library.MEDIA_VIDEO:
            source_start = 0.0 if source_start is None else source_start
            if not _finite(source_start) or source_start < 0:
                fail("the in-point in the asset must be a number of seconds, 0 or more.", placement_id)
                continue
            source_start = float(source_start)
            available = float(asset["measured"]["duration_seconds"] or 0.0)
            if source_start + (end - start) > available + TIME_TOLERANCE_SECONDS:
                fail(
                    "needs %.2f s of the asset from %.2f s, but the asset is %.2f s long. Shorten the "
                    "placement or start earlier in the asset; a short video is never looped or frozen."
                    % (end - start, source_start, available),
                    placement_id,
                )
                continue
        elif source_start is not None:
            fail("an image has no in-point; source_start must be null.", placement_id)
            continue

        placements.append(
            {
                "id": placement_id,
                "asset_id": asset["id"],
                "template_id": item["template_id"],
                "start": round(start, 6),
                "end": round(end, 6),
                "source_start": round(source_start, 6) if source_start is not None else None,
                "settings": settings,
            }
        )

    placements.sort(key=lambda placement: (placement["start"], placement["end"]))
    for previous, current in zip(placements, placements[1:]):
        if current["start"] < previous["end"] - 1e-9:
            errors.append(
                {
                    "index": None,
                    "placement_id": current["id"],
                    "message": (
                        "Two B-roll placements overlap (%s–%s and %s–%s). Only one B-roll can be on "
                        "screen at a time; shorten or move one of them."
                        % (_clock(previous["start"]), _clock(previous["end"]), _clock(current["start"]), _clock(current["end"]))
                    ),
                }
            )
    return placements, errors


def new_placement_id() -> str:
    return "p" + uuid.uuid4().hex[:8]


# --- saving --------------------------------------------------------------------------


def save_plan(
    project_id: str,
    key: Any,
    placements: Any,
    *,
    base_revision: Any,
    origin: str = ORIGIN_MANUAL,
    note: str | None = None,
) -> dict:
    """Validate and store a new immutable revision of the base video's plan.

    `base_revision` is the revision the change was made on (0 or None for a
    first plan). Saving on top of anything else is refused, so two tabs, or a
    tab and a future planner, cannot silently overwrite each other.
    """
    if origin not in ORIGINS:
        raise BrollError("Unknown plan origin.")
    described = describe_base(project_id, key)
    base = base_reference(described)
    current = find_plan(project_id, base["key"], base["digest"])
    expected = current["revision"] if current else 0
    if (base_revision or 0) != expected:
        raise BrollError(
            "The B-roll plan changed since this screen loaded it (now revision %d). Reload and try again." % expected,
            status_code=409,
        )
    normalised, errors = validate_placements(placements, base, library_index())
    if errors:
        raise BrollValidationFailed(errors)

    plan_id = current["plan_id"] if current else uuid.uuid4().hex[:12]
    revision = expected + 1
    document = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "kind": PLAN_KIND,
        "plan_id": plan_id,
        "revision": revision,
        "parent_revision": expected or None,
        "created_at": now(),
        "origin": origin,
        "note": note,
        "base": base,
        "timeline": TIMELINE_NOTE,
        "overlap_policy": OVERLAP_POLICY,
        "template_catalog_version": broll_templates.TEMPLATE_CATALOG_VERSION,
        "placements": normalised,
    }
    try:
        storage.write_json_exclusive(_revision_path(project_id, plan_id, revision), document)
    except FileExistsError as error:
        raise BrollError("Another change was saved at the same moment. Reload and try again.", status_code=409) from error
    except OSError as error:
        raise BrollError("Saving the B-roll plan failed: %s" % error, status_code=500) from error
    return document


def _current_placements(project_id: str, base: dict) -> tuple[list[dict], int]:
    plan = find_plan(project_id, base["key"], base["digest"])
    return (list(plan["placements"]) if plan else []), (plan["revision"] if plan else 0)


def add_placement(
    project_id: str, key: Any, *, asset_id: Any, at_seconds: Any, template_id: Any = None, base_revision: Any
) -> dict:
    """The minimal "Add B-roll" action: put an asset at the playhead.

    The default length (3 s for an image, 4 s for a video) is clamped to what
    the asset has, to the end of the base video and to the start of the next
    placement, so adding never creates an overlap. Starting inside an existing
    placement is refused.
    """
    described = describe_base(project_id, key)
    base = base_reference(described)
    placements, revision = _current_placements(project_id, base)
    if (base_revision or 0) != revision:
        raise BrollError("The B-roll plan changed since this screen loaded it. Reload and try again.", status_code=409)

    asset = broll_library.read_entry(asset_id)
    status, reason = broll_library.status_of(asset)
    if status != broll_library.STATUS_AVAILABLE:
        raise BrollError("This asset cannot be used: %s" % reason, status_code=409)
    template_id = template_id or broll_templates.DEFAULT_TEMPLATE
    if template_id not in broll_templates.TEMPLATES_BY_ID:
        raise BrollError("Unknown template.")
    if not _finite(at_seconds):
        raise BrollError("The playhead position must be a number of seconds.")

    duration = base["duration_seconds"]
    # The playhead is on a frame; snap tiny float noise onto it.
    start = round(min(max(0.0, float(at_seconds)), duration), 6)
    for placement in placements:
        if placement["start"] - 1e-9 <= start < placement["end"] - 1e-9:
            raise BrollError(
                "There is already B-roll from %s to %s. Move the playhead outside it, or remove it first."
                % (_clock(placement["start"]), _clock(placement["end"])),
                status_code=409,
            )
    following = [placement["start"] for placement in placements if placement["start"] > start + 1e-9]
    room = min([duration, *following]) - start
    length = DEFAULT_PLACEMENT_SECONDS[asset["media_type"]]
    if asset["media_type"] == broll_library.MEDIA_VIDEO:
        length = min(length, float(asset["measured"]["duration_seconds"] or 0.0))
    length = min(length, room)
    if length < MIN_PLACEMENT_SECONDS - 1e-9:
        raise BrollError(
            "There is not enough room at %s: B-roll needs at least %.1f s before %s."
            % (_clock(start), MIN_PLACEMENT_SECONDS, "the next B-roll" if following and min(following) < duration else "the end of the video"),
            status_code=409,
        )
    placement = {
        "id": new_placement_id(),
        "asset_id": asset["id"],
        "template_id": template_id,
        "start": start,
        "end": round(start + length, 6),
        "source_start": 0.0 if asset["media_type"] == broll_library.MEDIA_VIDEO else None,
        "settings": broll_templates.default_settings(template_id),
    }
    return save_plan(
        project_id, key, [*placements, placement], base_revision=revision,
        note="Added %s at %s." % (broll_library.public_entry(asset)["display_name"], _clock(start)),
    )


def update_placement(project_id: str, key: Any, placement_id: Any, changes: Any, *, base_revision: Any) -> dict:
    """Change one placement's template, timing, in-point or settings."""
    if not isinstance(changes, dict) or not changes:
        raise BrollError("Nothing to change.")
    allowed = {"template_id", "start", "end", "source_start", "settings"}
    if set(changes) - allowed:
        raise BrollError("Only the template, start, end, in-point and settings can be changed.")
    described = describe_base(project_id, key)
    base = base_reference(described)
    placements, revision = _current_placements(project_id, base)
    target = next((placement for placement in placements if placement["id"] == placement_id), None)
    if target is None:
        raise BrollError("That B-roll placement is not in the current plan.", status_code=404)
    updated = {**target, **changes}
    if "template_id" in changes and changes["template_id"] != target["template_id"] and "settings" not in changes:
        # Settings belong to a template; switching starts from the new one's defaults.
        updated["settings"] = None
    others = [placement for placement in placements if placement["id"] != placement_id]
    return save_plan(project_id, key, [*others, updated], base_revision=base_revision, note="Changed a placement.")


def remove_placement(project_id: str, key: Any, placement_id: Any, *, base_revision: Any) -> dict:
    described = describe_base(project_id, key)
    base = base_reference(described)
    placements, _revision = _current_placements(project_id, base)
    remaining = [placement for placement in placements if placement["id"] != placement_id]
    if len(remaining) == len(placements):
        raise BrollError("That B-roll placement is not in the current plan.", status_code=404)
    return save_plan(project_id, key, remaining, base_revision=base_revision, note="Removed a placement.")


# --- composition ------------------------------------------------------------------


def captions_for_base(project_id: str, key: str) -> dict | None:
    """The structured captions that belong on this base video, or None.

    For a zoomed video these are its clean parent's (timing lineage). Never a
    previously captioned export: captions stay a separate, top layer.
    """
    try:
        identity = caption_inputs.timing_identity(project_id, key)
    except storage.ProjectError:
        return None
    if identity["key"] is None or identity["fingerprint"] is None:
        return None
    return zooms.current_caption_overlay(project_id, identity["key"], identity["fingerprint"]["digest"])


def compose(project_id: str, key: Any) -> dict:
    """Everything the review player and the export render from, as of now.

    `props` is the composition's input without media URLs (the browser and
    the render script each add their own). Placements with problems are left
    out of `props` and listed in `issues`; a plan with issues cannot be
    approved or exported. `digest` names exactly what is composed.
    """
    described = describe_base(project_id, key)
    base = base_reference(described)
    plan = find_plan(project_id, base["key"], base["digest"])
    library = library_index()
    usable, issues = [], []
    if plan is not None:
        usable, issues = validate_placements(plan["placements"], base, library)
        # An overlap names two placements; neither is drawn until it is fixed.
        if any(issue["index"] is None for issue in issues):
            usable = []

    overlay = captions_for_base(project_id, base["key"])
    project = storage.read_project(project_id)
    style = overlay["style"] if overlay else captions.read_style(project)["style"]
    position = float(style.get("vertical_position_percent", 80))
    assets_used = {placement["asset_id"]: library[placement["asset_id"]] for placement in usable}

    props = {
        "video": {
            "width": base["width"],
            "height": base["height"],
            "fps": base["fps"],
            "duration_in_frames": base["duration_in_frames"],
        },
        "captions": [
            {key_: caption[key_] for key_ in ("id", "start", "end", "text", "words")}
            for caption in (overlay["captions"] if overlay else [])
        ],
        "style": style,
        "direction": overlay["direction"] if overlay else "rtl",
        "broll": {
            "placements": [
                {
                    "id": placement["id"],
                    "template_id": placement["template_id"],
                    "start": placement["start"],
                    "end": placement["end"],
                    "source_start": placement["source_start"],
                    "settings": broll_templates.resolve_settings(
                        placement["template_id"], placement["settings"], caption_position_percent=position
                    ),
                    "asset": {
                        "id": placement["asset_id"],
                        "media_type": assets_used[placement["asset_id"]]["media_type"],
                        "width": assets_used[placement["asset_id"]]["measured"]["width"],
                        "height": assets_used[placement["asset_id"]]["measured"]["height"],
                        "has_audio": bool(assets_used[placement["asset_id"]]["measured"].get("has_audio")),
                    },
                }
                for placement in usable
            ],
            # Where captions sit, so a full-frame cutaway can darken that side.
            "caption_band": ("bottom" if position >= 50 else "top") if overlay else None,
        },
    }
    asset_digests = {asset_id: entry["fingerprint"]["digest"] for asset_id, entry in sorted(assets_used.items())}
    plan_reference = {"plan_id": plan["plan_id"], "revision": plan["revision"]} if plan else None
    captions_reference = zooms.overlay_reference(overlay)
    digest = _hash(
        {
            "props": props,
            "base": base["digest"],
            "assets": asset_digests,
            "plan": plan_reference,
            "captions": captions_reference,
            "templates": broll_templates.TEMPLATE_CATALOG_VERSION,
        }
    )
    return {
        "base": base,
        "described": described,
        "plan": plan,
        "plan_reference": plan_reference,
        "issues": issues,
        "placements": usable,
        "assets": assets_used,
        "asset_digests": asset_digests,
        "overlay": overlay,
        "captions_reference": captions_reference,
        "props": props,
        "digest": digest,
    }


# --- review --------------------------------------------------------------------------


def _decision_path(project_id: str, key: str) -> Path:
    caption_inputs.validate_key(key)
    return decisions_root(project_id) / ("%s.json" % key)


def read_decision(project_id: str, key: str) -> dict | None:
    try:
        document = storage.read_json(_decision_path(project_id, key))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) and document.get("kind") == DECISION_KIND else None


def review_state(project_id: str, composed: dict) -> dict:
    """Whether the latest decision still names what is composed now."""
    decision = read_decision(project_id, composed["base"]["key"])
    valid = decision is not None and decision.get("composition_digest") == composed["digest"]
    reason = None
    if decision is not None and not valid:
        if decision.get("base_digest") != composed["base"]["digest"]:
            reason = "The video changed since this decision. Review it again."
        elif decision.get("plan") != composed["plan_reference"]:
            reason = "The B-roll plan changed since this decision. Review it again."
        elif decision.get("captions") != composed["captions_reference"]:
            reason = "The captions or their style changed since this decision. Review it again."
        elif decision.get("asset_digests") != composed["asset_digests"]:
            reason = "A B-roll asset changed since this decision. Review it again."
        else:
            reason = "What is composed changed since this decision. Review it again."
    return {
        "decision": decision,
        "approved": valid and decision["decision"] == DECISION_APPROVED,
        "skipped": valid and decision["decision"] == DECISION_SKIPPED,
        "outdated_reason": reason,
        "can_approve": not composed["issues"] and bool(composed["placements"]),
    }


def decide(project_id: str, key: Any, *, decision: str, composition_digest: Any) -> tuple[dict, dict]:
    """Approve the composed result, or skip B-roll, exactly as reviewed."""
    composed = compose(project_id, key)
    if composition_digest != composed["digest"]:
        raise BrollError("The result changed while you were reviewing it. Review it again.", status_code=409)
    if decision == DECISION_APPROVED:
        if composed["issues"]:
            raise BrollError("Fix the B-roll problems listed first: %s" % composed["issues"][0]["message"], status_code=409)
        if not composed["placements"]:
            raise BrollError("There is no B-roll to approve. Add some, or skip B-roll.", status_code=409)
    elif decision != DECISION_SKIPPED:
        raise BrollError("Unknown decision.")
    document = {
        "kind": DECISION_KIND,
        "decision": decision,
        "decided_at": now(),
        "input_key": composed["base"]["key"],
        "base_digest": composed["base"]["digest"],
        "plan": composed["plan_reference"],
        "placement_count": len(composed["placements"]) if decision == DECISION_APPROVED else 0,
        "asset_digests": composed["asset_digests"],
        "captions": composed["captions_reference"],
        "composition_digest": composed["digest"],
    }
    try:
        storage.write_json_atomic(_decision_path(project_id, composed["base"]["key"]), document)
    except OSError as error:
        raise BrollError("Saving the decision failed: %s" % error, status_code=500) from error
    return document, composed


# --- the stage 3B contract --------------------------------------------------------


def constraints() -> dict:
    return {
        "timeline": TIMELINE_NOTE,
        "interval": "half-open [start, end) in seconds",
        "min_placement_seconds": MIN_PLACEMENT_SECONDS,
        "max_placements": MAX_PLACEMENTS,
        "overlap_policy": OVERLAP_POLICY,
        "overlap_rule": "At most one placement on screen at any time; back-to-back (end == next start) is allowed.",
        "bounds_rule": "0 <= start < end <= base duration_seconds.",
        "video_source_rule": (
            "For a video asset, source_start >= 0 and source_start + (end - start) <= the asset's "
            "duration_seconds. Short assets are never looped or frozen."
        ),
        "image_source_rule": "For an image asset, source_start is null.",
        "frame_rule": "On screen on frames ceil(start*fps) .. ceil(end*fps)-1 of the base video.",
        "audio_rule": "The base video's audio always plays, once. Asset audio is muted unless a placement unmutes it.",
        "default_placement_seconds": DEFAULT_PLACEMENT_SECONDS,
        "plan_schema_version": PLAN_SCHEMA_VERSION,
    }


def catalog(project_id: str | None = None, key: Any = None) -> dict:
    """The machine-readable catalog a stage 3B planner will be given.

    No paths and no filenames anywhere; see `broll_library.catalog_entry`.
    """
    document = {
        "kind": "video_factory.broll_catalog",
        **broll_templates.catalog(),
        "constraints": constraints(),
        "assets_note": broll_library.CATALOG_NOTE,
        "assets": [broll_library.catalog_entry(entry) for entry in broll_library.read_entries()],
        "base": None,
    }
    if project_id is not None and key is not None:
        base = base_reference(describe_base(project_id, key))
        document["base"] = {
            "duration_seconds": base["duration_seconds"],
            "fps": base["fps"],
            "width": base["width"],
            "height": base["height"],
            "duration_in_frames": base["duration_in_frames"],
        }
    return document

