"""The plan provider: one real integration, plus a deterministic mock.

Two rules shape this module.

**The model proposes; the backend decides.** A provider only ever returns parsed
JSON. It does not validate it, does not save it and does not execute anything —
`plans.validate_actions` does that, against the backend's own catalogs.

**Nothing leaves the machine unless the user asked for it.** Only an explicit
plan-generation request with the real provider configured sends anything, and
what it sends is exactly `build_request`: the typed instruction, the capability
catalog, the resource catalog (ids, filenames, availability, sizes — never
absolute paths) and the required response schema. No video, audio or image data
is uploaded in this milestone, and the API key never appears in a plan, a job
record, a log line or the frontend bundle.
"""

import json
import os
from typing import Any

from . import capabilities
from .config import API_KEY_ENV_VAR, get_llm_settings

MOCK = "mock"
ANTHROPIC = "anthropic"

MOCK_LABEL = "מצב הדגמה (ללא AI)"
ANTHROPIC_LABEL = "Anthropic Claude"

MAX_OUTPUT_TOKENS = 8000

# At most one retry inside the SDK, so a provider outage fails in bounded time
# instead of holding the queue.
MAX_RETRIES = 1

# What a real request sends to the provider. Shown in the README and returned by
# the status endpoint so it is never a surprise.
DATA_SENT_TO_PROVIDER = [
    "ההנחיה שכתבת",
    "קטלוג היכולות של האפליקציה",
    "קטלוג המשאבים של הפרויקט: מזהים, שמות קבצים, זמינות וגודל",
    "סכמת התשובה הנדרשת",
]

DATA_NOT_SENT_TO_PROVIDER = [
    "קובצי וידאו, אודיו או תמונות",
    "נתיבים מלאים במחשב",
    "שם הפרויקט או תוכן אחר מהמחשב",
]


class ProviderError(Exception):
    """A provider failure with a message meant for the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# --- the request ------------------------------------------------------------


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "supported": {
            "type": "boolean",
            "description": "false when no registered capability can do what was asked",
        },
        "summary": {
            "type": "string",
            "description": "Hebrew, one short paragraph describing the plan",
        },
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "unique, e.g. a1"},
                    "capability_id": {"type": "string"},
                    "resource_ids": {"type": "array", "items": {"type": "string"}},
                    "parameters": {"type": "object"},
                    "note": {"type": "string", "description": "Hebrew, why this action"},
                },
                "required": ["id", "capability_id", "parameters"],
            },
        },
        "explanation": {
            "type": "string",
            "description": "Hebrew, required when supported is false",
        },
    },
    "required": ["supported"],
}

SYSTEM_PROMPT = """You plan work for Video Factory, a local Hebrew-language video \
production app. You do not execute anything: you return a proposal that the \
backend validates, the user reviews and edits, and the user alone approves.

Hard rules:
- Use ONLY capability ids from the capability catalog below. Never invent one.
- Use ONLY resource ids from the resource catalog below. Never invent one, and \
never refer to a file by path.
- Use ONLY the parameters each capability declares, within their stated \
constraints. Anything else is rejected by the backend.
- Never output a shell command, a script, code, or any executable expression. \
There is no field for one, and a plan that contains one is rejected.
- Filenames do NOT reveal what is in a video. Do not infer content, speech or \
structure from a filename.
- If what the user asked for cannot be done with the registered capabilities, \
return {"supported": false, "explanation": "..."} explaining in Hebrew what is \
missing. Never invent actions to look helpful.

Write "summary", "note" and "explanation" in Hebrew.
Respond with a single JSON object matching the response schema. No markdown, no \
code fences, no text before or after the JSON."""


def build_request(instruction: str, capability_catalog: dict, resource_catalog: dict) -> dict:
    """Exactly what is sent to a provider — and the mock's only input too."""
    return {
        "instruction": instruction,
        "capability_catalog": capability_catalog,
        "resource_catalog": resource_catalog,
        "response_schema": RESPONSE_SCHEMA,
    }


def _user_message(request: dict) -> str:
    return (
        "User instruction (Hebrew):\n%s\n\n"
        "Capability catalog:\n%s\n\n"
        "Project resource catalog:\n%s\n\n"
        "Required response schema:\n%s"
        % (
            request["instruction"],
            json.dumps(request["capability_catalog"], ensure_ascii=False, indent=2),
            json.dumps(request["resource_catalog"], ensure_ascii=False, indent=2),
            json.dumps(request["response_schema"], ensure_ascii=False, indent=2),
        )
    )


def _extract_json(text: str) -> dict:
    """Parse the provider's reply, tolerating stray prose around the object."""
    candidate = text.strip()

    if not candidate:
        raise ProviderError("המודל החזיר תשובה ריקה.")

    try:
        parsed = json.loads(candidate)
    except ValueError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            raise ProviderError("תשובת המודל אינה JSON תקין.") from None
        try:
            parsed = json.loads(candidate[start : end + 1])
        except ValueError:
            raise ProviderError("תשובת המודל אינה JSON תקין.") from None

    if not isinstance(parsed, dict):
        raise ProviderError("תשובת המודל אינה במבנה הצפוי.")

    return parsed


# --- providers --------------------------------------------------------------


class MockProvider:
    """A deterministic stand-in. Always labelled; never presented as AI.

    Same instruction in, same proposal out — no randomness, no network, no key.
    It exists so the whole plan flow (generate, review, edit, approve, execute)
    can be developed and tested without an API call.
    """

    id = MOCK
    label = MOCK_LABEL
    is_mock = True

    TOOL_KEYWORDS = (
        "כלי",
        "כלים",
        "בדיק",
        "בדוק",
        "גרס",
        "אבחון",
        "התקנ",
        "זמין",
        "ffmpeg",
        "ffprobe",
        "auto",
        "editor",
        "tool",
        "check",
        "version",
        "diagnos",
    )

    TOOL_ALIASES = {
        "ffmpeg": "ffmpeg",
        "ffprobe": "ffprobe",
        "auto-editor": "auto_editor",
        "auto editor": "auto_editor",
        "auto_editor": "auto_editor",
        "autoeditor": "auto_editor",
    }

    def __init__(self, model: str = "deterministic-rules") -> None:
        self.model = model

    def describe(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "model": self.model,
            "is_mock": True,
        }

    def propose(self, request: dict) -> dict:
        instruction = request["instruction"]
        lowered = instruction.lower()

        if not any(keyword in lowered for keyword in self.TOOL_KEYWORDS):
            return {
                "supported": False,
                "explanation": (
                    "מצב הדגמה: הבקשה אינה מתאימה לאף יכולת שממומשת כרגע "
                    "באפליקציה. היכולת היחידה שקיימת היא בדיקת כלי העיבוד "
                    "המותקנים. יכולות עריכה (חיתוך, כתוביות, זומים, B-roll, "
                    "אודיו) עדיין לא נבנו."
                ),
            }

        requested = [
            name
            for alias, name in self.TOOL_ALIASES.items()
            if alias in lowered
        ]
        tools = sorted(set(requested)) or list(
            capabilities.CAPABILITIES[capabilities.TOOL_CHECK]["parameters"]["tools"][
                "default"
            ]
        )

        return {
            "supported": True,
            "summary": (
                "מצב הדגמה: תוכנית אבחון הבודקת שהכלים החיצוניים (%s) מותקנים, "
                "נגישים דרך ה־PATH ומדווחים גרסה. התוכנית אינה עורכת וידאו."
                % ", ".join(capabilities.tool_label(name) for name in tools)
            ),
            "actions": [
                {
                    "id": "a1",
                    "capability_id": capabilities.TOOL_CHECK,
                    "resource_ids": [],
                    "parameters": {"tools": tools},
                    "note": "בדיקת זמינות וגרסה של כלי העיבוד.",
                }
            ],
        }


class AnthropicProvider:
    """The one real integration: Anthropic's Messages API through the official SDK."""

    id = ANTHROPIC
    label = ANTHROPIC_LABEL
    is_mock = False

    def __init__(self, model: str, api_key: str, timeout_seconds: float) -> None:
        self.model = model
        self._api_key = api_key
        self._timeout = timeout_seconds

    def describe(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "model": self.model,
            "is_mock": False,
        }

    def propose(self, request: dict) -> dict:
        try:
            import anthropic
        except ImportError as error:
            raise ProviderError(
                "חבילת anthropic אינה מותקנת. התקן אותה עם "
                "pip install -r requirements.txt כדי להשתמש בספק אמיתי."
            ) from error

        client = anthropic.Anthropic(
            api_key=self._api_key,
            timeout=self._timeout,
            max_retries=MAX_RETRIES,
        )

        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=SYSTEM_PROMPT,
                output_config={"effort": "low"},
                messages=[{"role": "user", "content": _user_message(request)}],
            )
        except anthropic.APITimeoutError as error:
            raise ProviderError(
                "הפנייה לספק ה־AI חרגה מהזמן המוקצב (%.0f שניות)." % self._timeout
            ) from error
        except anthropic.AuthenticationError as error:
            raise ProviderError(
                "מפתח ה־API של הספק אינו תקף. בדוק את ANTHROPIC_API_KEY."
            ) from error
        except anthropic.RateLimitError as error:
            raise ProviderError(
                "הספק החזיר חריגה ממכסת הבקשות. נסה שוב מאוחר יותר."
            ) from error
        except anthropic.APIStatusError as error:
            raise ProviderError(
                "הספק החזיר שגיאה (קוד %s)." % getattr(error, "status_code", "לא ידוע")
            ) from error
        except anthropic.APIConnectionError as error:
            raise ProviderError("אין חיבור לספק ה־AI.") from error
        except Exception as error:  # noqa: BLE001 - bounded, never leaks the key
            raise ProviderError("הפנייה לספק ה־AI נכשלה: %s" % type(error).__name__) from error

        if getattr(response, "stop_reason", None) == "refusal":
            raise ProviderError("הספק סירב לענות על הבקשה הזו.")

        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        return _extract_json(text)


# --- selection --------------------------------------------------------------


def get_provider() -> Any:
    """The configured provider, or a clear error explaining what is missing."""
    settings = get_llm_settings()
    name = settings["provider"]

    if name == MOCK:
        return MockProvider()

    if name == ANTHROPIC:
        api_key = os.environ.get(API_KEY_ENV_VAR, "").strip()
        if not api_key:
            raise ProviderError(
                "הספק מוגדר כ־anthropic אך לא הוגדר מפתח %s. אפשר להגדיר מפתח "
                "או לעבוד במצב הדגמה (VIDEO_FACTORY_LLM_PROVIDER=mock)."
                % API_KEY_ENV_VAR
            )
        return AnthropicProvider(settings["model"], api_key, settings["timeout_seconds"])

    raise ProviderError(
        'ספק AI לא מוכר: "%s". ערכים נתמכים: mock, anthropic.' % name
    )


def provider_status() -> dict:
    """What the interface shows. Never includes the key itself."""
    settings = get_llm_settings()
    name = settings["provider"]

    if name == MOCK:
        return {
            "provider": MOCK,
            "label": MOCK_LABEL,
            "model": "deterministic-rules",
            "is_mock": True,
            "ready": True,
            "message": (
                "מצב הדגמה: התוכניות נוצרות על ידי כללים קבועים במחשב הזה. "
                "אלו אינן תשובות של מודל AI ושום מידע אינו נשלח לאינטרנט."
            ),
            "data_sent": [],
            "data_not_sent": DATA_SENT_TO_PROVIDER + DATA_NOT_SENT_TO_PROVIDER,
        }

    if name == ANTHROPIC:
        ready = settings["api_key_present"]
        return {
            "provider": ANTHROPIC,
            "label": ANTHROPIC_LABEL,
            "model": settings["model"],
            "is_mock": False,
            "ready": ready,
            "message": (
                "ספק אמיתי מוגדר (%s). בקשה ליצירת תוכנית תשלח טקסט ומטא־דאטה "
                "לשרתי Anthropic." % settings["model"]
                if ready
                else "הספק מוגדר כ־anthropic אך לא הוגדר ANTHROPIC_API_KEY בסביבת השרת."
            ),
            "data_sent": DATA_SENT_TO_PROVIDER,
            "data_not_sent": DATA_NOT_SENT_TO_PROVIDER,
        }

    return {
        "provider": name,
        "label": name,
        "model": settings["model"],
        "is_mock": False,
        "ready": False,
        "message": 'ספק AI לא מוכר: "%s". ערכים נתמכים: mock, anthropic.' % name,
        "data_sent": [],
        "data_not_sent": DATA_SENT_TO_PROVIDER + DATA_NOT_SENT_TO_PROVIDER,
    }
