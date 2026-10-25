"""Planning with an AI outside the app (ChatGPT, Claude, …), by hand.

The zoom and B-roll planners can hand out exactly the instructions they would
send to Gemini — the system prompt and the request — as one text to paste
into another chat. The answer pasted back is parsed here and then goes
through the same validation as a Gemini answer: an imported plan is never
trusted more than one the app asked for itself, and it is recorded as
imported, not as made by a provider.
"""

import json
from typing import Any

from .llm import ProviderError

PROVIDER = {"id": "external", "label": "External AI (imported)", "model": "imported", "is_mock": False}

MAX_ANSWER_CHARACTERS = 500_000


def build_prompt(system: str, user_message: str) -> str:
    """One self-contained text: the instructions, then the request as JSON."""
    return (
        "%s\n\n"
        "---\n\n"
        "The input follows as JSON. Answer with a single JSON object that matches its "
        '"response_schema", and nothing else: no explanation, no Markdown.\n\n'
        "%s\n" % (system, user_message)
    )


def parse_answer(raw: Any) -> Any:
    """The pasted answer as JSON. Tolerates code fences and prose around the object."""
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        raise ProviderError("Paste the AI's answer first.")
    if len(raw) > MAX_ANSWER_CHARACTERS:
        raise ProviderError("The pasted answer is too long (%d characters)." % len(raw))
    text = raw.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ProviderError("The pasted answer contains no JSON object.")
    try:
        return json.loads(text[start : end + 1])
    except ValueError as error:
        raise ProviderError("The pasted answer is not valid JSON: %s" % error) from None
