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

PIP_POSITIONS = ("auto", "top_left", "top_right", "bottom_left", "bottom_right")

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
        },
    },
    {
        "id": PIP,
        "version": 1,
        "label": "Picture-in-picture",
        "description": (
            "The asset in a box in one corner while the speaker stays visible. The box "
            "has the asset's own aspect ratio, so nothing is cropped or stretched."
        ),
        "settings": {
            "position": {
                "type": "choice",
                "choices": list(PIP_POSITIONS),
                "default": "auto",
                "label": "Corner",
                "description": (
                    "auto: the right-hand corner on the opposite half of the frame from the "
                    "captions (top when captions sit in the lower half, which is the default)."
                ),
            },
            "size_percent": {
                "type": "number",
                "min": 20.0,
                "max": 60.0,
                "default": 40.0,
                "step": 1.0,
                "label": "Size",
                "unit": "% of the frame width",
                "description": (
                    "The box's width. Its height follows the asset's aspect ratio and is "
                    "limited to 45% of the frame height (the box shrinks to fit)."
                ),
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
        },
    },
]

TEMPLATES_BY_ID = {template["id"]: template for template in TEMPLATES}
DEFAULT_TEMPLATE = FULLFRAME

# The tallest a picture-in-picture box may be, as a share of the frame height,
# so a portrait asset in a portrait frame cannot cover the speaker's face and
# the caption band at once. Mirrored in frontend/src/broll/layout.ts.
PIP_MAX_HEIGHT_FRACTION = 0.45


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
    """Settings as the renderer receives them: `auto` replaced by a corner.

    Resolved on the backend, from the caption style, so the preview and the
    export are handed the same concrete values.
    """
    resolved = dict(settings)
    if template_id == PIP and resolved.get("position") == "auto":
        resolved["position"] = "top_right" if caption_position_percent >= 50 else "bottom_right"
    return resolved


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
