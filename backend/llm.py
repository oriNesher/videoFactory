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

MOCK_LABEL = "Demo mode (no AI)"
ANTHROPIC_LABEL = "Anthropic Claude"

MAX_OUTPUT_TOKENS = 8000

# At most one retry inside the SDK, so a provider outage fails in bounded time
# instead of holding the queue.
MAX_RETRIES = 1

# What a real request sends to the provider. Shown in the README and returned by
# the status endpoint so it is never a surprise.
DATA_SENT_TO_PROVIDER = [
    "the instruction you wrote",
    "the application's capability catalog",
    "the project's resource catalog: ids, file names, availability and size",
    "the required response schema",
]

DATA_NOT_SENT_TO_PROVIDER = [
    "video, audio or image files",
    "full paths on this machine",
    "the project name or other content from this machine",
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
            "description": "one short paragraph describing the plan",
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
                    "note": {"type": "string", "description": "why this action"},
                },
                "required": ["id", "capability_id", "parameters"],
            },
        },
        "explanation": {
            "type": "string",
            "description": "required when supported is false",
        },
    },
    "required": ["supported"],
}

SYSTEM_PROMPT = """You plan work for Video Factory, a local video \
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
return {"supported": false, "explanation": "..."} explaining what is \
missing. Never invent actions to look helpful.

Write "summary", "note" and "explanation" in English.
Respond with a single JSON object matching the response schema. No markdown, no \
code fences, no text before or after the JSON."""


CUT_SYSTEM_PROMPT = """You recommend cutting settings for Video Factory, a \
local video production app. The user records a script as short takes, one clip \
per take, and joins them. You do not execute anything: you return a proposal \
that the backend validates and the user reviews, edits and approves.

What you are given is a JSON object: the user's request, optional feedback on \
previews they listened to, the cutting mode they have selected with its \
current settings, the capabilities that implement each mode (with every \
parameter's meaning, unit and range), and measurements of each clip.

Hard rules:
- You have NOT heard or seen any media. You have durations, detected boundary \
times and loudness statistics only. Never claim to have listened, and never \
describe what is said in a clip.
- Propose ONLY parameters the capability for your chosen mode declares, within \
their ranges. Anything else is rejected by the backend.
- Keep the mode the user selected ("current.mode"). Propose the other mode \
only if the request explicitly asks for what only that mode does - removing \
pauses INSIDE a clip needs "full_clip"; trimming only the ends needs \
"boundary". A wish for tighter pacing alone is not such a request. If you do \
change it, say so first in the explanation.
- The detection is loudness-based and cannot tell speech from other sound. \
State uncertainty where the statistics are ambiguous (background level close \
to the threshold, activity barely above it, no activity found).
- Never output a command, a path, code or any executable expression.
- If the request cannot be met by these settings at all (for example removing \
a mistake or choosing the best take), return \
{"supported": false, "explanation": "..."}.

Respond with a single JSON object matching "response_schema". Write \
"explanation" and "limitations" in English. No markdown, no code fences, no \
text before or after the JSON."""

CUT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "supported": {"type": "boolean"},
        "mode": {"type": "string", "enum": ["boundary", "full_clip"]},
        "settings": {
            "type": "object",
            "description": "parameter name -> number, for the chosen mode only",
        },
        "explanation": {
            "type": "string",
            "description": "two or three sentences: what changes and why",
        },
        "limitations": {
            "type": "array",
            "items": {"type": "string"},
            "description": "what these settings cannot guarantee, or what is uncertain",
        },
    },
    "required": ["supported", "explanation"],
}


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
        "User instruction:\n%s\n\n"
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
        raise ProviderError("The model returned an empty response.")

    try:
        parsed = json.loads(candidate)
    except ValueError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            raise ProviderError("The model's response is not valid JSON.") from None
        try:
            parsed = json.loads(candidate[start : end + 1])
        except ValueError:
            raise ProviderError("The model's response is not valid JSON.") from None

    if not isinstance(parsed, dict):
        raise ProviderError("The model's response is not in the expected shape.")

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
        "install",
        "available",
        "path",
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
                    "Demo mode: the request does not match any capability implemented "
                    "in the application yet. The only one that exists is checking "
                    "the installed processing tools. Editing capabilities "
                    "(cutting, subtitles, zooms, B-roll, audio) have not been "
                    "built yet."
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
                "Demo mode: a diagnostic plan that checks the external tools (%s) are "
                "installed, reachable on PATH and reporting a version. The plan "
                "does not edit video."
                % ", ".join(capabilities.tool_label(name) for name in tools)
            ),
            "actions": [
                {
                    "id": "a1",
                    "capability_id": capabilities.TOOL_CHECK,
                    "resource_ids": [],
                    "parameters": {"tools": tools},
                    "note": "Check the processing tools' availability and version.",
                }
            ],
        }


    # --- cutting recommendations ---------------------------------------------

    TIGHTER_WORDS = ("tight", "faster", "shorter", "snappy", "less pause", "quicker")
    ROOMIER_WORDS = ("breath", "room", "natural", "relaxed", "longer", "more pause", "slower")
    CLIPPED_START_WORDS = ("first word is", "start is cut", "clipped at the start",
                           "beginning is cut", "cuts off the start")
    CLIPPED_END_WORDS = ("last word is", "end is cut", "clipped at the end",
                         "cut off", "cuts off the end")

    def recommend_cut(self, request: dict) -> dict:
        """Fixed rules over the request text and the measured statistics.

        Never changes the cutting mode, and says plainly that it is not a
        model. Its purpose is to let the whole recommendation flow — propose,
        edit, approve, apply — be exercised with no key and no network.
        """
        current = request["current"]
        mode = current["mode"]
        settings = dict(current["settings"])
        text = ("%s %s" % (request.get("request", ""), request.get("feedback") or "")).lower()
        changes: list[str] = []

        def has(words: tuple) -> bool:
            return any(word in text for word in words)

        if mode == "boundary":
            lead, trail = "leading_padding_seconds", "trailing_padding_seconds"
            if has(self.TIGHTER_WORDS):
                settings[trail] = round(max(0.15, settings[trail] - 0.1), 2)
                settings[lead] = round(max(0.05, settings[lead] - 0.05), 2)
                changes.append("less padding at both ends for tighter joins")
            if has(self.ROOMIER_WORDS):
                settings[trail] = round(min(5.0, settings[trail] + 0.15), 2)
                changes.append("more padding after the end for breathing room")
            if has(self.CLIPPED_START_WORDS):
                settings[lead] = round(min(5.0, settings[lead] + 0.1), 2)
                changes.append("more padding before the start")
            if has(self.CLIPPED_END_WORDS):
                settings[trail] = round(min(5.0, settings[trail] + 0.15), 2)
                changes.append("more padding after the end")

            backgrounds = [
                clip["background_level"]
                for clip in request.get("clips", [])
                if clip.get("background_level") is not None
            ]
            threshold = settings["detection_threshold"]
            if backgrounds and max(backgrounds) >= threshold * 0.5:
                settings["detection_threshold"] = round(
                    min(1.0, max(threshold, max(backgrounds) * 3)), 4
                )
                changes.append(
                    "a higher threshold, because the measured background level is "
                    "close to the current one"
                )
        else:
            if has(self.TIGHTER_WORDS):
                settings["margin_after_seconds"] = round(
                    max(0.1, settings["margin_after_seconds"] - 0.1), 2
                )
                changes.append("a shorter margin after each spoken part")
            if has(self.ROOMIER_WORDS):
                settings["margin_after_seconds"] = round(
                    min(10.0, settings["margin_after_seconds"] + 0.15), 2
                )
                changes.append("a longer margin after each spoken part")

        return {
            "supported": True,
            "mode": mode,
            "settings": settings,
            "explanation": (
                "Demo mode: fixed rules, not an AI model. %s"
                % (
                    "Proposed: %s." % "; ".join(changes)
                    if changes
                    else "Nothing in the request matched a rule, so the current "
                    "settings are proposed unchanged."
                )
            ),
            "limitations": [
                "Demo rules only match a few keywords and look at the measured "
                "statistics; no audio was listened to.",
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
        return self._complete(SYSTEM_PROMPT, _user_message(request))

    def recommend_cut(self, request: dict) -> dict:
        """Ask for cutting settings. Sends statistics and text, never media."""
        return self._complete(
            CUT_SYSTEM_PROMPT, json.dumps(request, ensure_ascii=False, indent=2)
        )

    def _complete(self, system: str, user: str) -> dict:
        """One bounded request, parsed as a JSON object."""
        try:
            import anthropic
        except ImportError as error:
            raise ProviderError(
                "The anthropic package is not installed. Install it with "
                "pip install -r requirements.txt to use a real provider."
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
                system=system,
                output_config={"effort": "low"},
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.APITimeoutError as error:
            raise ProviderError(
                "The AI provider call exceeded its time budget (%.0f seconds)." % self._timeout
            ) from error
        except anthropic.AuthenticationError as error:
            raise ProviderError(
                "The provider's API key is not valid. Check ANTHROPIC_API_KEY."
            ) from error
        except anthropic.RateLimitError as error:
            raise ProviderError(
                "The provider returned a rate-limit error. Try again later."
            ) from error
        except anthropic.APIStatusError as error:
            raise ProviderError(
                "The provider returned an error (status %s)." % getattr(error, "status_code", "unknown")
            ) from error
        except anthropic.APIConnectionError as error:
            raise ProviderError("There is no connection to the AI provider.") from error
        except Exception as error:  # noqa: BLE001 - bounded, never leaks the key
            raise ProviderError("The AI provider call failed: %s" % type(error).__name__) from error

        if getattr(response, "stop_reason", None) == "refusal":
            raise ProviderError("The provider declined to answer this request.")

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
                "The provider is set to anthropic but no %s key is configured. Set a "
                "key, or work in demo mode (VIDEO_FACTORY_LLM_PROVIDER=mock)."
                % API_KEY_ENV_VAR
            )
        return AnthropicProvider(settings["model"], api_key, settings["timeout_seconds"])

    raise ProviderError(
        'Unknown AI provider: "%s". Supported values: mock, anthropic.' % name
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
                "Demo mode: plans are produced by fixed rules on this machine. These "
                "are not answers from an AI model, and nothing is sent to the internet."
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
                "A real provider is configured (%s). Asking for a plan sends text and "
                "metadata to Anthropic's servers." % settings["model"]
                if ready
                else "The provider is set to anthropic but ANTHROPIC_API_KEY is not set in the server environment."
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
        "message": 'Unknown AI provider: "%s". Supported values: mock, anthropic.' % name,
        "data_sent": [],
        "data_not_sent": DATA_SENT_TO_PROVIDER + DATA_NOT_SENT_TO_PROVIDER,
    }
