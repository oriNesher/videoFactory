"""The capability catalog: what this application can actually be asked to do.

The catalog is owned by the backend, never by the model. It is the *only* list
of operations a plan may reference, and every registered capability here is
implemented and executable today. An installed executable is not a capability:
FFmpeg and Auto-Editor are on this machine, but nothing in Video Factory drives
them for editing yet, so no editing capability is registered.

Parameters are described with a deliberately small spec language (type, range,
choices) so that the backend can validate a model's proposal exactly, and so
that an action can never carry a shell command, a script or an expression —
there is no parameter type that could hold one.
"""

from typing import Any

from . import storage
from .tools import TOOL_LABELS, TOOL_NAMES

# Bumped whenever a capability is added, removed or its parameters change.
# Plans record the version they were generated against.
CATALOG_VERSION = 1

# Kinds are how a capability is classified for the user. `diagnostic` means it
# inspects the machine and reports; it never touches video.
KIND_DIAGNOSTIC = "diagnostic"
KIND_EDITING = "editing"

KIND_LABELS = {
    KIND_DIAGNOSTIC: "אבחון",
    KIND_EDITING: "עריכת וידאו",
}

TOOL_CHECK = "diagnostics.tool_check"

# Defence in depth. The whitelist below already makes these impossible, but a
# plan that so much as *tries* to carry executable content is rejected loudly.
FORBIDDEN_PARAMETER_KEYS = frozenset(
    {
        "command",
        "commands",
        "cmd",
        "script",
        "shell",
        "code",
        "python",
        "exec",
        "eval",
        "expression",
        "args",
        "argv",
        "executable",
        "path",
    }
)

CAPABILITIES: dict[str, dict] = {
    TOOL_CHECK: {
        "id": TOOL_CHECK,
        "version": 1,
        "kind": KIND_DIAGNOSTIC,
        "title": "בדיקת כלי עיבוד",
        "purpose": (
            "בודקת אילו כלי עיבוד חיצוניים מותקנים ונגישים דרך ה־PATH ומדווחת "
            "את הגרסה שלהם. פעולת אבחון בלבד: היא אינה עורכת וידאו, אינה קוראת "
            "חומרי גלם ואינה כותבת קבצים."
        ),
        "executable": True,
        "resource_types": [],
        "max_resources": 0,
        "parameters": {
            "tools": {
                "type": "string_list",
                "required": False,
                "choices": list(TOOL_NAMES),
                "default": list(TOOL_NAMES),
                "min_items": 1,
                "max_items": len(TOOL_NAMES),
                "description": "אילו כלים לבדוק. ברירת המחדל: כל הכלים.",
            }
        },
    }
}

# Areas the user may well ask about, which have no capability yet. This list is
# text for the model's benefit only — nothing here can be referenced by a plan.
NOT_YET_SUPPORTED = [
    "חיתוך אוטומטי של שתיקות (Auto-Editor)",
    "תמלול (Whisper) וכתוביות",
    "זומים ואפקטים",
    "B-roll והרכבת שכבות (Remotion)",
    "מוזיקת רקע ואפקטים קוליים",
    "ייצוא סופי והקלטה (OBS)",
]


class CapabilityError(storage.ProjectError):
    """An invalid capability reference or parameter value."""


def list_capabilities() -> list[dict]:
    """The catalog as shown to the user and sent to the model."""
    return [dict(capability) for capability in CAPABILITIES.values()]


def catalog() -> dict:
    return {
        "catalog_version": CATALOG_VERSION,
        "capabilities": list_capabilities(),
        "not_yet_supported": list(NOT_YET_SUPPORTED),
        "kind_labels": dict(KIND_LABELS),
    }


def get_capability(capability_id: Any) -> dict:
    if not isinstance(capability_id, str) or not capability_id:
        raise CapabilityError("לכל פעולה חייב להיות מזהה יכולת.")

    capability = CAPABILITIES.get(capability_id)
    if capability is None:
        known = ", ".join(sorted(CAPABILITIES)) or "אין כרגע יכולות זמינות"
        raise CapabilityError(
            'היכולת "%s" אינה קיימת במערכת. היכולות הקיימות: %s.'
            % (capability_id, known)
        )

    if not capability.get("executable"):
        raise CapabilityError(
            'היכולת "%s" רשומה אך אינה זמינה להרצה כרגע.' % capability_id
        )

    return capability


# --- parameter validation ---------------------------------------------------


def _fail(action_label: str, message: str) -> CapabilityError:
    return CapabilityError("%s: %s" % (action_label, message))


def _validate_string(spec: dict, name: str, value: Any, action_label: str) -> str:
    if not isinstance(value, str):
        raise _fail(action_label, 'הפרמטר "%s" חייב להיות טקסט.' % name)

    text = value.strip()
    max_length = spec.get("max_length", 500)
    if len(text) > max_length:
        raise _fail(
            action_label, 'הפרמטר "%s" ארוך מדי (עד %d תווים).' % (name, max_length)
        )

    choices = spec.get("choices")
    if choices is not None and text not in choices:
        raise _fail(
            action_label,
            'הערך של "%s" אינו נתמך. ערכים אפשריים: %s.' % (name, ", ".join(choices)),
        )

    return text


def _validate_number(spec: dict, name: str, value: Any, action_label: str) -> Any:
    if spec["type"] == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise _fail(action_label, 'הפרמטר "%s" חייב להיות מספר שלם.' % name)
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _fail(action_label, 'הפרמטר "%s" חייב להיות מספר.' % name)

    minimum = spec.get("min")
    maximum = spec.get("max")
    if minimum is not None and value < minimum:
        raise _fail(action_label, 'הפרמטר "%s" קטן מהמינימום (%s).' % (name, minimum))
    if maximum is not None and value > maximum:
        raise _fail(action_label, 'הפרמטר "%s" גדול מהמקסימום (%s).' % (name, maximum))

    return value


def _validate_string_list(spec: dict, name: str, value: Any, action_label: str) -> list:
    if not isinstance(value, list):
        raise _fail(action_label, 'הפרמטר "%s" חייב להיות רשימה.' % name)

    items: list[str] = []
    for entry in value:
        item = _validate_string(spec, name, entry, action_label)
        if item in items:
            raise _fail(action_label, 'הפרמטר "%s" מכיל ערך כפול: %s.' % (name, item))
        items.append(item)

    min_items = spec.get("min_items")
    max_items = spec.get("max_items")
    if min_items is not None and len(items) < min_items:
        raise _fail(
            action_label, 'הפרמטר "%s" חייב לכלול לפחות %d ערכים.' % (name, min_items)
        )
    if max_items is not None and len(items) > max_items:
        raise _fail(
            action_label, 'הפרמטר "%s" יכול לכלול עד %d ערכים.' % (name, max_items)
        )

    return items


def validate_parameters(capability: dict, raw: Any, action_label: str) -> dict:
    """Check a proposal's parameters against the registered specification.

    Only declared keys are accepted, and each is checked by type, range and
    allowed values. Anything else is rejected rather than passed through.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _fail(action_label, "הפרמטרים של הפעולה אינם תקינים.")

    specs: dict = capability["parameters"]
    validated: dict = {}

    for name in raw:
        if not isinstance(name, str):
            raise _fail(action_label, "שם פרמטר אינו תקין.")
        if name.lower() in FORBIDDEN_PARAMETER_KEYS and name not in specs:
            raise _fail(
                action_label,
                'הפרמטר "%s" אסור: תוכנית עריכה לא יכולה להכיל פקודות, קוד או '
                "נתיבים להרצה." % name,
            )
        if name not in specs:
            allowed = ", ".join(sorted(specs)) or "אין פרמטרים"
            raise _fail(
                action_label,
                'הפרמטר "%s" אינו מוכר ליכולת הזו. פרמטרים אפשריים: %s.'
                % (name, allowed),
            )

    for name, spec in specs.items():
        if name not in raw:
            if spec.get("required"):
                raise _fail(action_label, 'חסר הפרמטר החובה "%s".' % name)
            default = spec.get("default")
            if default is not None:
                validated[name] = list(default) if isinstance(default, list) else default
            continue

        value = raw[name]
        kind = spec["type"]

        if kind == "string":
            validated[name] = _validate_string(spec, name, value, action_label)
        elif kind in ("integer", "number"):
            validated[name] = _validate_number(spec, name, value, action_label)
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise _fail(
                    action_label, 'הפרמטר "%s" חייב להיות כן/לא.' % name
                )
            validated[name] = value
        elif kind == "string_list":
            validated[name] = _validate_string_list(spec, name, value, action_label)
        else:  # pragma: no cover - a registry mistake, not user input
            raise _fail(action_label, 'סוג פרמטר לא מוכר עבור "%s".' % name)

    return validated


def tool_label(name: str) -> str:
    return TOOL_LABELS.get(name, name)
