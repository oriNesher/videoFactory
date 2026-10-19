"""Background music: settings, composition and review (stage 4).

Music goes on the video the B-roll stage decided on — the same base video,
with the B-roll the user approved (none when B-roll was skipped or is
switched off) and the captions — as one more layer of the same Remotion
composition. The preview beside the stage plays it live; the export renders
exactly that. Sound effects (a later addition) will be further audio layers
on this same stage.

**Settings** live in `project.settings["music"]`, one choice per video:

    {"enabled": true,            # off: B-roll's approval exports right away, as before
     "track_id": "<audio library id>" | null,
     "volume_percent": 15,       # linear gain under the speaker's own track
     "start_seconds": 0.0,       # where in the track the video starts
     "fade_in_seconds": 1.0, "fade_out_seconds": 2.0,
     "loop": true}               # a track shorter than the video starts again

The speaker's audio is never changed: music is mixed under it, once.

**Review.** Like B-roll, the user approves the composed result or continues
without music. A decision records a digest of exactly what was composed —
the props, the base video, the B-roll decision it builds on, the captions
and the track's fingerprint — so any later change to any of them (a new
B-roll decision, a re-recorded track, a different volume) marks it outdated.

    intermediates/music/decisions/<input-key>.json
"""

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import audio_library, broll, caption_inputs, storage

SETTINGS_KEY = "music"
DECISION_KIND = "video_factory.music_decision"

DECISION_APPROVED = "approved"
DECISION_SKIPPED = "skipped"

# What the stage builds on: the B-roll stage's outcome for this video.
BROLL_APPROVED = "approved"
BROLL_SKIPPED = "skipped"
BROLL_OFF = "off"
BROLL_PENDING = "pending"

DEFAULTS = {
    "volume_percent": 15.0,
    "start_seconds": 0.0,
    "fade_in_seconds": 1.0,
    "fade_out_seconds": 2.0,
    "loop": True,
}
LIMITS = {
    "volume_percent": (0.0, 100.0),
    "start_seconds": (0.0, 3600.0),
    "fade_in_seconds": (0.0, 30.0),
    "fade_out_seconds": (0.0, 30.0),
}
LABELS = {
    "volume_percent": "Volume",
    "start_seconds": "Start in the track",
    "fade_in_seconds": "Fade in",
    "fade_out_seconds": "Fade out",
}
# The start point must leave at least this much of the track to play.
MIN_REMAINING_SECONDS = 0.5


class MusicError(storage.ProjectError):
    """A music problem the user can act on."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _hash(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- settings ---------------------------------------------------------------------


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value


def read_settings(project: dict) -> dict:
    block = project.get("settings", {}).get(SETTINGS_KEY)
    block = block if isinstance(block, dict) else {}
    settings = {
        # On by default: B-roll's decision continues here. Off sends it
        # straight to the export, as before this stage existed.
        "enabled": block["enabled"] if isinstance(block.get("enabled"), bool) else True,
        "track_id": block["track_id"] if isinstance(block.get("track_id"), str) else None,
        "loop": block["loop"] if isinstance(block.get("loop"), bool) else DEFAULTS["loop"],
    }
    for name, (low, high) in LIMITS.items():
        value = block.get(name)
        settings[name] = float(value) if _number(value) and low <= value <= high else DEFAULTS[name]
    return settings


def save_settings(project_id: str, changes: Any) -> dict:
    if not isinstance(changes, dict):
        raise MusicError("The music settings are invalid.")
    known = {"enabled", "track_id", "loop", *LIMITS}
    unknown = set(changes) - known
    if unknown:
        raise MusicError("Unknown music setting: %s." % ", ".join(sorted(unknown)))
    project = storage.read_project(project_id)
    current = read_settings(project)
    for name in ("enabled", "loop"):
        if name in changes:
            if not isinstance(changes[name], bool):
                raise MusicError("%s must be true or false." % name)
            current[name] = changes[name]
    for name, (low, high) in LIMITS.items():
        if name in changes:
            value = changes[name]
            if not _number(value) or not low <= value <= high:
                raise MusicError("%s must be a number from %g to %g." % (LABELS[name], low, high))
            current[name] = round(float(value), 3)
    if "track_id" in changes:
        track_id = changes["track_id"]
        if track_id is not None:
            entry = audio_library.read_entry(track_id)
            if entry["kind"] != audio_library.KIND_MUSIC:
                raise MusicError("That track is not a music track.")
            if "start_seconds" not in changes:
                current["start_seconds"] = 0.0  # a new track starts at its beginning
        current["track_id"] = track_id
    project["settings"][SETTINGS_KEY] = current
    storage.write_project(project)
    return current


# --- the video music goes on -------------------------------------------------------


def input_key(project_id: str) -> str | None:
    """The video the B-roll stage works on (its selection, else its default)."""
    project = storage.read_project(project_id)
    selected = broll.read_settings(project)["selected_input"]
    if selected and selected in {entry["key"] for entry in broll.list_inputs(project_id)}:
        return selected
    return broll.default_input(project_id)


def _broll_state(project_id: str, project: dict, composed: dict) -> str:
    if not broll.read_settings(project)["enabled"]:
        return BROLL_OFF
    review = broll.review_state(project_id, composed)
    if review["approved"]:
        return BROLL_APPROVED
    if review["skipped"]:
        return BROLL_SKIPPED
    return BROLL_PENDING


def compose(project_id: str, key: Any) -> dict:
    """Everything the preview and the export render from, as of now.

    Built on `broll.compose`: the same props with the B-roll the user decided
    on, plus `music`. Track problems are listed in `issues` and leave the
    music out of `props`; music with issues cannot be approved.
    """
    composed = broll.compose(project_id, key)
    project = storage.read_project(project_id)
    settings = read_settings(project)
    broll_state = _broll_state(project_id, project, composed)

    props = copy.deepcopy(composed["props"])
    if broll_state in (BROLL_SKIPPED, BROLL_OFF):
        # The picture without B-roll, whatever plan is stored.
        props["broll"]["placements"] = []

    issues: list[str] = []
    track = None
    if settings["track_id"] is not None:
        entry = audio_library.find_entry(settings["track_id"])
        if entry is None:
            issues.append("The chosen track is no longer in the music library. Choose another one.")
        else:
            status, reason = audio_library.status_of(entry)
            if status != audio_library.STATUS_AVAILABLE:
                issues.append(reason)
            elif settings["start_seconds"] > entry["measured"]["duration_seconds"] - MIN_REMAINING_SECONDS:
                issues.append(
                    "The start point (%.1f s) is past the end of the track (%.1f s)."
                    % (settings["start_seconds"], entry["measured"]["duration_seconds"])
                )
            else:
                track = entry

    props["music"] = (
        {
            "asset": {"id": track["id"], "duration_seconds": track["measured"]["duration_seconds"]},
            "volume": round(settings["volume_percent"] / 100.0, 4),
            "start_seconds": settings["start_seconds"],
            "fade_in_seconds": settings["fade_in_seconds"],
            "fade_out_seconds": settings["fade_out_seconds"],
            "loop": settings["loop"],
        }
        if track is not None else None
    )
    track_reference = {"id": track["id"], "digest": track["fingerprint"]["digest"]} if track else None
    broll_digest = composed["digest"] if broll_state == BROLL_APPROVED else None
    digest = _hash(
        {
            "props": props,
            "base": composed["base"]["digest"],
            "broll_state": broll_state,
            "broll": broll_digest,
            "captions": composed["captions_reference"],
            "track": track_reference,
        }
    )
    return {
        "base": composed["base"],
        "described": composed["described"],
        "broll": composed,
        "broll_state": broll_state,
        "broll_digest": broll_digest,
        "captions_reference": composed["captions_reference"],
        "settings": settings,
        "track": track,
        "track_reference": track_reference,
        "issues": issues,
        "props": props,
        "digest": digest,
    }


def without_music(props: dict) -> dict:
    return {**props, "music": None}


def has_overlays(props: dict) -> bool:
    """Whether anything is drawn or mixed over the base video at all."""
    return bool(props["captions"] or props["broll"]["placements"] or props.get("music"))


# --- review --------------------------------------------------------------------------


def decisions_root(project_id: str) -> Path:
    return storage.project_directory(project_id) / "intermediates" / "music" / "decisions"


def _decision_path(project_id: str, key: str) -> Path:
    caption_inputs.validate_key(key)
    return decisions_root(project_id) / ("%s.json" % key)


def read_decision(project_id: str, key: str) -> dict | None:
    try:
        document = storage.read_json(_decision_path(project_id, key))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) and document.get("kind") == DECISION_KIND else None


def _blocked_reason(composed: dict) -> str | None:
    if composed["broll_state"] == BROLL_PENDING:
        return "Approve or skip B-roll first; music goes on the result you decided on."
    return None


def review_state(project_id: str, composed: dict) -> dict:
    """Whether the latest decision still names what is composed now."""
    decision = read_decision(project_id, composed["base"]["key"])
    valid = decision is not None and decision.get("composition_digest") == composed["digest"]
    reason = None
    if decision is not None and not valid:
        if decision.get("base_digest") != composed["base"]["digest"]:
            reason = "The video changed since this decision. Review it again."
        elif decision.get("broll_state") != composed["broll_state"] or decision.get("broll_digest") != composed["broll_digest"]:
            reason = "The B-roll decision changed since this decision. Review the music again."
        elif decision.get("captions") != composed["captions_reference"]:
            reason = "The captions or their style changed since this decision. Review it again."
        elif decision.get("track") != composed["track_reference"]:
            reason = "The music track changed since this decision. Review it again."
        else:
            reason = "The music settings changed since this decision. Review it again."
    blocked = _blocked_reason(composed)
    return {
        "decision": decision,
        "approved": valid and decision["decision"] == DECISION_APPROVED,
        "skipped": valid and decision["decision"] == DECISION_SKIPPED,
        "outdated_reason": reason,
        "blocked_reason": blocked,
        "can_approve": blocked is None and not composed["issues"] and composed["props"]["music"] is not None,
        "can_skip": blocked is None,
    }


def decide(project_id: str, key: Any, *, decision: str, composition_digest: Any) -> tuple[dict, dict]:
    """Approve the composed music, or continue without music, exactly as reviewed."""
    composed = compose(project_id, key)
    if composition_digest != composed["digest"]:
        raise MusicError("The result changed while you were reviewing it. Review it again.", status_code=409)
    blocked = _blocked_reason(composed)
    if blocked:
        raise MusicError(blocked, status_code=409)
    if decision == DECISION_APPROVED:
        if composed["issues"]:
            raise MusicError("Fix the music problem first: %s" % composed["issues"][0], status_code=409)
        if composed["props"]["music"] is None:
            raise MusicError("Choose a music track first, or continue without music.", status_code=409)
    elif decision != DECISION_SKIPPED:
        raise MusicError("Unknown decision.")
    rendered = composed["props"] if decision == DECISION_APPROVED else without_music(composed["props"])
    document = {
        "kind": DECISION_KIND,
        "decision": decision,
        "decided_at": now(),
        "input_key": composed["base"]["key"],
        "base_digest": composed["base"]["digest"],
        "broll_state": composed["broll_state"],
        "broll_digest": composed["broll_digest"],
        "captions": composed["captions_reference"],
        "track": composed["track_reference"],
        "settings": {name: composed["settings"][name] for name in ("volume_percent", "start_seconds", "fade_in_seconds", "fade_out_seconds", "loop")},
        "composition_digest": composed["digest"],
        # Nothing over the base video at all: the base file is the finished video.
        "has_overlays": has_overlays(rendered),
    }
    try:
        storage.write_json_atomic(_decision_path(project_id, composed["base"]["key"]), document)
    except OSError as error:
        raise MusicError("Saving the decision failed: %s" % error, status_code=500) from error
    return document, composed
