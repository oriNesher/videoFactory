"""A small adapter over Google's official `google-genai` SDK.

It does one thing: send a system instruction and a user message, ask for JSON
matching a schema, and hand back a parsed `dict` — or raise `GeminiError` with
a sentence a person can act on. Callers never see SDK response objects, so the
zoom planner is not coupled to Gemini and a test can replace the client with a
stand-in that returns canned JSON.

Credentials: the key is read by the caller from `GEMINI_API_KEY` in the
backend's environment and passed in. It is held only on the client object; it
is never logged, never put in an error message (every message that could echo
provider text is redacted), never written to a job, plan or manifest.

Retries are deliberately *not* left to the SDK: its built-in policy is turned
off (`attempts=1`) and `call_with_retries` decides, visibly and boundedly,
whether to wait and try again. A daily quota or a long wait is reported, not
waited out, and there is never a fallback to another provider or model.
"""

import json
import re
import time
from collections.abc import Callable
from typing import Any

from .llm import ProviderError

GEMINI = "gemini"
GEMINI_LABEL = "Google Gemini"

# Generous: the reply is a list of intervals, but thinking tokens count too.
MAX_OUTPUT_TOKENS = 16384

# Bounded retry policy (see `call_with_retries`).
MAX_ATTEMPTS = 3
MAX_WAIT_SECONDS = 65.0
DEFAULT_TRANSIENT_WAIT_SECONDS = (3.0, 8.0)

# Anything that looks like a Google API key is removed from any text that
# could reach a message, a log or a job record.
_KEY_PATTERN = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")


class GeminiError(ProviderError):
    """A Gemini failure, classified so the caller can decide what to do.

    `kind` is one of: sdk_missing, auth, permission, not_found, invalid_request,
    quota, daily_quota, timeout, unavailable, connection, blocked, truncated,
    empty, invalid_json, failed.
    """

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


def redact(text: Any, secret: str | None = None) -> str:
    value = str(text or "")
    if secret:
        value = value.replace(secret, "[redacted]")
    value = _KEY_PATTERN.sub("[redacted]", value)
    return value[:400]


def _parse_duration(value: Any) -> float | None:
    """`"37s"` or `"1.5s"` (protobuf Duration as JSON) → seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s\s*", value)
        if match:
            return float(match.group(1))
    return None


def _error_details(details: Any) -> list[dict]:
    if isinstance(details, dict):
        inner = details.get("error") if isinstance(details.get("error"), dict) else details
        items = inner.get("details")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    return []


def quota_info(details: Any) -> dict:
    """What a 429 body says about waiting: RetryInfo and QuotaFailure, if any."""
    retry_after = None
    daily = False
    quota_ids: list[str] = []
    for item in _error_details(details):
        kind = str(item.get("@type") or "")
        if kind.endswith("google.rpc.RetryInfo"):
            retry_after = _parse_duration(item.get("retryDelay"))
        elif kind.endswith("google.rpc.QuotaFailure"):
            for violation in item.get("violations") or []:
                if not isinstance(violation, dict):
                    continue
                quota_id = str(violation.get("quotaId") or violation.get("quotaMetric") or "")
                quota_ids.append(quota_id)
                if "perday" in quota_id.lower().replace("_", ""):
                    daily = True
    return {"retry_after_seconds": retry_after, "daily": daily, "quota_ids": quota_ids}


def classify_api_error(code: Any, status: Any, message: Any, details: Any, secret: str | None) -> GeminiError:
    """Map an HTTP error from the API to a `GeminiError`. Pure, for testing."""
    text = redact(message, secret)
    status = str(status or "")
    lowered = text.lower()

    if code == 429 or status == "RESOURCE_EXHAUSTED":
        info = quota_info(details)
        if info["daily"]:
            return GeminiError(
                "daily_quota",
                "The Gemini free-tier daily request quota for this model is used up. "
                "Daily quotas reset at midnight Pacific time; try again after that. "
                "Nothing was switched to another provider.",
            )
        wait = info["retry_after_seconds"]
        return GeminiError(
            "quota",
            "Gemini's free-tier rate limit was reached%s."
            % (" (the API asked to wait %.0f s)" % wait if wait is not None else ""),
            retryable=True,
            retry_after_seconds=wait,
        )
    if code in (401,) or "api key not valid" in lowered or "api_key_invalid" in lowered:
        return GeminiError(
            "auth",
            "Gemini rejected the API key. Check GEMINI_API_KEY in the backend's "
            "environment (and restart the backend from a new terminal after changing it).",
        )
    if code == 403 or status == "PERMISSION_DENIED":
        return GeminiError(
            "permission",
            "Gemini refused the request for this key (permission denied): %s" % text,
        )
    if code == 404 or status == "NOT_FOUND":
        return GeminiError(
            "not_found",
            "The Gemini model is not available to this key. Check "
            "VIDEO_FACTORY_GEMINI_MODEL: %s" % text,
        )
    if code == 504 or status == "DEADLINE_EXCEEDED":
        return GeminiError("timeout", "Gemini did not answer within its deadline.", retryable=True)
    if isinstance(code, int) and code >= 500:
        return GeminiError(
            "unavailable",
            "Gemini is temporarily unavailable (status %s)." % code,
            retryable=True,
        )
    if status == "FAILED_PRECONDITION":
        return GeminiError(
            "permission",
            "Gemini cannot serve this request for this key or region: %s" % text,
        )
    return GeminiError("invalid_request", "Gemini rejected the request (status %s): %s" % (code, text))


class GeminiClient:
    """One bounded JSON request at a time. Holds the key; never reveals it."""

    def __init__(self, model: str, api_key: str, timeout_seconds: float) -> None:
        self.model = model
        self._api_key = api_key
        self._timeout = timeout_seconds

    def __repr__(self) -> str:  # never show the key, even in a debugger or a log
        return "GeminiClient(model=%r)" % self.model

    def generate_json(self, *, system: str, user: str, schema: dict | None) -> dict:
        """Send one request and return `{"data": dict, "usage": dict}`."""
        try:
            from google import genai
            from google.genai import errors, types
        except ImportError as error:
            raise GeminiError(
                "sdk_missing",
                "The google-genai package is not installed. Run: "
                ".\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt",
            ) from error

        try:
            import httpx
        except ImportError:  # pragma: no cover - a dependency of google-genai
            httpx = None

        client = genai.Client(
            api_key=self._api_key,
            http_options=types.HttpOptions(
                timeout=int(self._timeout * 1000),
                # Our own retry policy decides; the SDK's would hide waiting.
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=schema,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            # No tools are offered, so nothing may be called automatically.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        try:
            response = client.models.generate_content(model=self.model, contents=user, config=config)
        except errors.APIError as error:
            raise classify_api_error(
                getattr(error, "code", None),
                getattr(error, "status", None),
                getattr(error, "message", None),
                getattr(error, "details", None),
                self._api_key,
            ) from None
        except Exception as error:  # noqa: BLE001 - classified, never echoed raw
            if httpx is not None and isinstance(error, httpx.TimeoutException):
                raise GeminiError(
                    "timeout",
                    "Gemini did not answer within %.0f seconds." % self._timeout,
                    retryable=True,
                ) from None
            if httpx is not None and isinstance(error, httpx.TransportError):
                raise GeminiError(
                    "connection",
                    "There is no connection to Gemini (check the internet connection).",
                    retryable=True,
                ) from None
            raise GeminiError("failed", "The Gemini call failed: %s" % type(error).__name__) from None
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

        return {"data": parse_response(response), "usage": usage_of(response)}


def usage_of(response: Any) -> dict:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return {}
    return {
        name: getattr(usage, name)
        for name in ("prompt_token_count", "candidates_token_count", "thoughts_token_count", "total_token_count")
        if isinstance(getattr(usage, name, None), int)
    }


def parse_response(response: Any) -> dict:
    """The JSON object in a response, or a classified error. Pure-ish, for tests."""
    feedback = getattr(response, "prompt_feedback", None)
    if feedback is not None and getattr(feedback, "block_reason", None):
        raise GeminiError("blocked", "Gemini declined the request (%s)." % feedback.block_reason)

    candidates = getattr(response, "candidates", None) or []
    reason = str(getattr(candidates[0], "finish_reason", "") or "") if candidates else ""
    if "MAX_TOKENS" in reason:
        raise GeminiError("truncated", "Gemini's answer was cut off before it was complete.", retryable=True)
    if "SAFETY" in reason or "PROHIBITED" in reason or "RECITATION" in reason:
        raise GeminiError("blocked", "Gemini stopped its answer (%s)." % reason)

    try:
        text = response.text
    except Exception:  # noqa: BLE001 - the SDK raises when there is no text part
        text = None
    if not text or not text.strip():
        raise GeminiError("empty", "Gemini returned an empty answer.", retryable=True)
    try:
        data = json.loads(text)
    except ValueError:
        raise GeminiError("invalid_json", "Gemini's answer was not valid JSON.", retryable=True) from None
    if not isinstance(data, dict):
        raise GeminiError("invalid_json", "Gemini's answer was not a JSON object.", retryable=True)
    return data


def call_with_retries(
    call: Callable[[], Any],
    *,
    cancelled: Callable[[], bool] = lambda: False,
    on_wait: Callable[[str], None] = lambda message: None,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = MAX_ATTEMPTS,
    max_wait_seconds: float = MAX_WAIT_SECONDS,
) -> tuple[Any, int]:
    """Run `call`, retrying only retryable errors, at most `max_attempts` times.

    - A rate limit with a `RetryInfo` delay waits that delay (plus a second)
      when it is at most `max_wait_seconds`; a longer one fails at once with
      the delay in the message, rather than holding the job queue.
    - A daily quota is never retried.
    - Other transient errors wait a few seconds.
    - Waiting is announced through `on_wait` and stops on cancellation.

    Returns `(result, attempts_used)`.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return call(), attempt
        except GeminiError as error:
            if not error.retryable or attempt >= max_attempts:
                if error.retryable and attempt >= max_attempts:
                    error.message = "%s Gave up after %d attempts." % (error.message, attempt)
                    error.args = (error.message,)
                raise
            wait = error.retry_after_seconds
            if wait is None:
                wait = DEFAULT_TRANSIENT_WAIT_SECONDS[min(attempt - 1, len(DEFAULT_TRANSIENT_WAIT_SECONDS) - 1)]
            elif wait > max_wait_seconds:
                error.message = (
                    "%s That is longer than this app waits inside one job (%.0f s); "
                    "press Retry later." % (error.message, max_wait_seconds)
                )
                error.args = (error.message,)
                raise
            else:
                wait += 1.0
            reason = error.message

        remaining = wait
        while remaining > 0:
            if cancelled():
                raise GeminiError("cancelled", "Cancelled while waiting to retry.")
            on_wait(
                "%s Waiting %.0f s before attempt %d of %d…"
                % (reason, remaining, attempt + 1, max_attempts)
            )
            step = min(1.0, remaining)
            sleep(step)
            remaining -= step
