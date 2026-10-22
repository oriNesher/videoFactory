"""The B-roll template registry (stage 3A).

A template says *how* an asset is shown while it is on screen. There are two,
on purpose, and no plugin mechanism: every template has a fixed renderer in
`frontend/src/broll/BrollLayer.tsx`, and this registry only describes the
settings that renderer accepts, with their defaults and ranges. The same
registry validates a placement typed by the user today and one written by an
AI planner in stage 3B, and is part of the machine-readable catalog.

- `fullframe` — the asset replaces the visible picture of the speaker for the
  length of the placement. The speaker's video keeps playing underneath, so
  speech continues and nothing restarts when the cutaway ends.
- `pip` — picture-in-picture: the asset in a box in one corner, the speaker
  still visible around it.

Fit is never a stretch. `contain` shows the whole asset (black bars where
the shapes differ, nothing cropped) and is the default, because screen
recordings and images are usually there to be *read*. `cover` fills the frame
and crops the overflow; it is an explicit choice. A picture-in-picture box
takes the asset's own aspect ratio, so nothing is cropped there either.

B-roll audio is muted by default: the speaker's own track is the
composition's only sound unless a placement explicitly turns its asset's
audio on.
"""

from typing import Any

from . import storage

TEMPLATE_CATALOG_VERSION = 1

FULLFRAME = "fullframe"
PIP = "pip"

# What the automatic planner may choose. "custom" (a free position from
# x_percent / y_percent) is a manual correction only.
# above_captions: centred across the frame, just above the caption line (just
# below it when captions sit at the top). auto resolves to it.
PIP_PLANNER_POSITIONS = ("auto", "above_captions", "top_left", "top_right", "bottom_left", "bottom_right")
PIP_POSITIONS = PIP_PLANNER_POSITIONS + ("custom",)
# The sizes the planner may choose (real-world trial: bigger than before).
PIP_PLANNER_MIN_SIZE = 40.0
PIP_PLANNER_MAX_SIZE = 80.0

# How a placement appears when it starts. Both templates have the same two
# settings; the renderer is `frontend/src/broll/entrance.ts`. "none" is a hard
# cut, the default, so plans made before entrances existed look as they did.
ENTRANCE_NONE = "none"
ENTRANCES = {
    ENTRANCE_NONE: "None (cut)",
    "fade": "Fade in",
    "zoom": "Zoom in",
    "slide": "Slide in",
    "pop": "Pop in",
    "blur": "Blur in",
    "spin": "Spin in",
    "wipe": "Wipe in",
}
ENTRANCE_KEYS = ("entrance", "entrance_seconds")
ENTRANCE_SETTINGS = {
    "entrance": {
        "type": "choice",
        "choices": list(ENTRANCES),
        "choice_labels": ENTRANCES,
        "default": ENTRANCE_NONE,
        "label": "Entrance",
        "description": (
            "How the B-roll appears. fade: from transparent. zoom: grows from smaller while fading in. "
            "slide: rises in from below the frame. pop: springs up from nothing with a small overshoot. "
            "blur: sharpens from a blur while fading in. spin: turns in while growing. wipe: revealed "
            "from left to right. none: a plain cut."
        ),
    },
    "entrance_seconds": {
        "type": "number",
        "min": 0.1,
        "max": 2.0,
        "default": 0.4,
        "step": 0.05,
        "label": "Entrance length",
        "unit": "s",
        "description": "How long the entrance takes; never longer than the placement itself.",
    },
}

TEMPLATES: list[dict] = [
    {
        "id": FULLFRAME,
        "version": 1,
        "label": "Full-frame cutaway",
        "description": (
            "The asset fills the frame instead of the speaker for the length of the "
            "placement. The speaker's audio continues underneath; when the cutaway "
            "ends, the speaker's video is shown at that same moment (it is never "
            "paused or restarted)."
        ),
        "settings": {
            "fit": {
                "type": "choice",
                "choices": ["contain", "cover"],
                "default": "contain",
                "label": "Fit",
                "description": (
                    "contain: the whole asset is visible, with black bars where its shape "
                    "differs from the frame (nothing is cropped). cover: the asset fills the "
                    "frame and whatever overflows is cropped equally from both sides."
                ),
            },
            "caption_scrim": {
                "type": "boolean",
                "default": True,
                "label": "Darken behind captions",
                "description": (
                    "A soft dark gradient over the asset on the side where captions are "
                    "placed, so they stay readable over bright pictures."
                ),
            },
            "muted": {
                "type": "boolean",
                "default": True,
                "label": "Mute the asset",
                "description": "Video assets only. Off mixes the asset's own audio under the speaker.",
            },
            **ENTRANCE_SETTINGS,
        },
    },
    {
        "id": PIP,
        "version": 1,
        "label": "Picture-in-picture",
        "description": (
            "The asset in a box over the speaker, by default centred just above the "
            "captions. The box has the asset's own aspect ratio, so nothing is cropped "
            "or stretched; no border."
        ),
        "settings": {
            "position": {
                "type": "choice",
                "choices": list(PIP_POSITIONS),
                "default": "auto",
                "label": "Position",
                "description": (
                    "auto / above_captions: centred across the frame, just above the captions "
                    "(just below them when captions sit at the top). A corner keeps the box "
                    "there. custom: wherever Horizontal and Vertical put the box's centre."
                ),
            },
            "size_percent": {
                "type": "number",
                "min": 10.0,
                "max": 90.0,
                "default": 70.0,
                "step": 1.0,
                "label": "Size",
                "unit": "% of the frame width",
                "description": (
                    "The box's width. Its height follows the asset's aspect ratio and is "
                    "limited to 45% of the frame height up to a size of 60, growing with "
                    "the size above that (the box shrinks to fit)."
                ),
            },
            "x_percent": {
                "type": "number",
                "min": 0.0,
                "max": 100.0,
                "default": 50.0,
                "step": 1.0,
                "label": "Horizontal",
                "unit": "% from the left, custom position",
                "description": "Where the box's centre is across the frame. Used when Position is custom.",
            },
            "y_percent": {
                "type": "number",
                "min": 0.0,
                "max": 100.0,
                "default": 30.0,
                "step": 1.0,
                "label": "Vertical",
                "unit": "% from the top, custom position",
                "description": "Where the box's centre is down the frame. Used when Position is custom.",
            },
            "rotation_degrees": {
                "type": "number",
                "min": -45.0,
                "max": 45.0,
                "default": 0.0,
                "step": 1.0,
                "label": "Angle",
                "unit": "degrees, clockwise",
                "description": "Tilts the box around its centre.",
            },
            "margin_percent": {
                "type": "number",
                "min": 2.0,
                "max": 10.0,
                "default": 4.0,
                "step": 0.5,
                "label": "Margin",
                "unit": "% of the shorter side",
                "description": "Distance from the frame edges.",
            },
            "muted": {
                "type": "boolean",
                "default": True,
                "label": "Mute the asset",
                "description": "Video assets only. Off mixes the asset's own audio under the speaker.",
            },
            **ENTRANCE_SETTINGS,
        },
    },
]

TEMPLATES_BY_ID = {template["id"]: template for template in TEMPLATES}
DEFAULT_TEMPLATE = FULLFRAME

# The tallest a picture-in-picture box may be, as a share of the frame height,
# so a portrait asset in a portrait frame cannot cover the speaker's face and
# the caption band at once. It holds up to a size of 60 (all the planner may
# choose); a larger manual size raises it in proportion.
# Mirrored in frontend/src/broll/layout.ts.
PIP_MAX_HEIGHT_FRACTION = 0.45


# Full-frame is only for assets shaped like the video (a phone-screen asset on
# a phone video): the asset's aspect ratio within this factor of the frame's,
# either way. 1.25 on a 9:16 frame admits 0.45–0.70 (phone screenshots and
# recordings); a landscape or square asset goes picture-in-picture instead.
FULLFRAME_ASPECT_TOLERANCE = 1.25


def fits_full_frame(asset_width: Any, asset_height: Any, frame_width: Any, frame_height: Any) -> bool:
    """Whether an asset fills the frame nicely enough to be shown full-frame."""
    try:
        asset = float(asset_width) / float(asset_height)
        frame = float(frame_width) / float(frame_height)
    except (TypeError, ValueError, ZeroDivisionError):
        return False
    if asset <= 0 or frame <= 0:
        return False
    ratio = asset / frame
    return 1 / FULLFRAME_ASPECT_TOLERANCE <= ratio <= FULLFRAME_ASPECT_TOLERANCE


class TemplateError(storage.ProjectError):
    """A template or a setting that is not allowed."""


def default_settings(template_id: str) -> dict:
    template = TEMPLATES_BY_ID[template_id]
    return {key: spec["default"] for key, spec in template["settings"].items()}


def validate_settings(template_id: Any, raw: Any) -> tuple[dict | None, list[str]]:
    """Fill defaults and check every value. Unknown keys are refused."""
    if not isinstance(template_id, str) or template_id not in TEMPLATES_BY_ID:
        return None, ["Unknown template %r. Available: %s." % (template_id, ", ".join(TEMPLATES_BY_ID))]
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return None, ["The template settings must be an object."]
    spec = TEMPLATES_BY_ID[template_id]["settings"]
    errors = []
    unknown = sorted(set(raw) - set(spec))
    if unknown:
        errors.append("Unknown setting(s) for %s: %s." % (template_id, ", ".join(unknown)))
    settings = {}
    for key, rule in spec.items():
        value = raw.get(key, rule["default"])
        if rule["type"] == "boolean":
            if not isinstance(value, bool):
                errors.append("%s must be true or false." % rule["label"])
                continue
        elif rule["type"] == "choice":
            if value not in rule["choices"]:
                errors.append("%s must be one of: %s." % (rule["label"], ", ".join(rule["choices"])))
                continue
        elif rule["type"] == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
                errors.append("%s must be a number." % rule["label"])
                continue
            if not rule["min"] <= value <= rule["max"]:
                errors.append("%s must be between %g and %g." % (rule["label"], rule["min"], rule["max"]))
                continue
            value = float(value)
        settings[key] = value
    return (settings if not errors else None), errors


def resolve_settings(template_id: str, settings: dict, *, caption_position_percent: float) -> dict:
    """Settings as the renderer receives them: `auto` made concrete.

    Resolved on the backend so the preview and the export are handed the same
    values. `auto` is `above_captions`; the renderer places that box from the
    caption layout it is given (see `caption_layout` in the B-roll props).
    `caption_position_percent` is kept for callers; the side is decided there.

    Without an entrance its two settings are left out, so a placement that
    is a plain cut composes exactly as before entrances existed (and an
    approval made then still names what is composed).
    """
    resolved = dict(settings)
    if template_id == PIP and resolved.get("position") == "auto":
        resolved["position"] = "above_captions"
    if resolved.get("entrance", ENTRANCE_NONE) == ENTRANCE_NONE:
        for key in ENTRANCE_KEYS:
            resolved.pop(key, None)
    return resolved


def entrance_of(settings: Any) -> dict:
    """The entrance settings of a placement, to carry over to another template."""
    settings = settings if isinstance(settings, dict) else {}
    return {key: settings[key] for key in ENTRANCE_KEYS if key in settings}


def catalog() -> dict:
    """The template part of the machine-readable B-roll catalog."""
    return {
        "template_catalog_version": TEMPLATE_CATALOG_VERSION,
        "default_template": DEFAULT_TEMPLATE,
        "templates": [
            {
                "id": template["id"],
                "version": template["version"],
                "label": template["label"],
                "description": template["description"],
                "settings": template["settings"],
                "defaults": default_settings(template["id"]),
            }
            for template in TEMPLATES
        ],
    }
