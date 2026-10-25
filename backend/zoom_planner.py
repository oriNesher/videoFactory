"""Zoom planning: what is asked of the model, and what is accepted back.

**The decision model.** A plan is a list of intervals that together cover the
whole video, each naming one preset from a fixed catalog. The model chooses
the preset and both ends of every interval — so it alone decides how long
each framing lasts. Nothing here imposes a duration, a number of zooms or a
rhythm, and nothing rewrites valid timings into a pattern. The only changes
made to an accepted answer are technical and recorded (see
`validate_plan`): snapping to the frame grid and closing hairline gaps.

**What the model is given** (`build_request`): the transcript text with its
timings (labelled honestly as phrase- or word-level and approximate), the
video's duration, frame rate and orientation, the clip joins when the video
is a join of takes, the preset catalog, and the editorial preference. It is
not given video, audio, file names, paths or the project name, and the
prompt tells it so: it cannot see framing or hear delivery.

**What comes back** is JSON constrained by `RESPONSE_SCHEMA` and then
validated here against the actual video. A model answer is never executed:
the backend builds the FFmpeg command from validated numbers and preset ids.
"""

import json
import math
from collections.abc import Callable
from typing import Any

from . import gemini
from .config import GEMINI_API_KEY_ENV_VAR, get_zoom_provider_settings
from .llm import ProviderError

# --- the preset catalog ---------------------------------------------------------
#
# Carried over unchanged from legacy/ZoomRenderer/render_zoom_plan.py, including
# the names. The legacy plans also carried `focus: FACE_CENTER`, which only
# ever meant a fixed centre crop; it is not part of this schema because there
# is no face tracking to choose.

PRESET_CATALOG_VERSION = 1
PRESETS: list[dict] = [
    {"id": "NORMAL", "scale": 1.00, "label": "Normal (no zoom)",
     "description": "The full frame as recorded."},
    {"id": "PUNCH_LIGHT", "scale": 1.08, "label": "Light punch-in (108%)",
     "description": "A subtle tighter framing."},
    {"id": "PUNCH_MEDIUM", "scale": 1.12, "label": "Medium punch-in (112%)",
     "description": "A clearly tighter framing."},
    {"id": "PUNCH_STRONG", "scale": 1.20, "label": "Strong punch-in (120%)",
     "description": "The tightest framing; for selective emphasis."},
]
PRESET_SCALE = {preset["id"]: preset["scale"] for preset in PRESETS}
PRESET_IDS = [preset["id"] for preset in PRESETS]

PLAN_SCHEMA_VERSION = 1
PROMPT_VERSION = "zoom-plan-v2"

# The canonical timebase of every plan, stated in the plan and in the request.
TIMEBASE = "input_seconds"
TIMEBASE_DESCRIPTION = (
    "seconds from the first frame of this exact video file (after cutting and "
    "joining, if it is a cut output)"
)

PREFERENCES = {
    "subtle": "Restrained: mostly the normal frame, with occasional light punch-ins on the most important lines.",
    "balanced": "Balanced: a natural rhythm of framing changes that follows the structure of the speech, with stronger framing kept for real emphasis.",
    "energetic": "Energetic: more frequent changes suited to fast short-form video, still following the speech and never changing within a phrase.",
}
DEFAULT_PREFERENCE = "balanced"

MAX_INTERVALS = 2000
MAX_SUMMARY_CHARACTERS = 600
MAX_REASON_CHARACTERS = 300
ALLOWED_INTERVAL_KEYS = {"start", "end", "preset", "reason"}

# Technical normalisation limits. Anything beyond these is a rejected plan,
# never a silent fix.
COVERAGE_TOLERANCE_SECONDS = 0.15  # first start / last end, beyond one frame
JOIN_TOLERANCE_SECONDS = 0.05  # gap or overlap between neighbours, beyond one frame


# --- request -------------------------------------------------------------------------

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One or two sentences, in English, describing the editing style applied.",
        },
        "intervals": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number", "minimum": 0, "description": "seconds, in the stated timebase"},
                    "end": {"type": "number", "minimum": 0, "description": "seconds, in the stated timebase"},
                    "preset": {"type": "string", "enum": PRESET_IDS},
                    "reason": {"type": "string", "description": "a few words, in English"},
                },
                "required": ["start", "end", "preset"],
            },
        },
    },
    "required": ["summary", "intervals"],
}

SYSTEM_PROMPT = """You plan punch-in zooms for a talking-head video in Video \
Factory, a local video editor. The backend validates your answer and renders \
it with FFmpeg; you never execute anything.

What you have: the transcript (text and timings), the video's duration, frame \
rate and orientation, where recorded takes were joined (if any), the preset \
catalog, and an editorial preference. You have NOT seen the video or heard \
the audio. Do not describe the speaker's framing, expressions or delivery.

How the output works:
- Return intervals that together cover the whole video: the first starts at \
0, each next one starts exactly where the previous ended, and the last ends \
at the video's duration. No gaps, no overlaps, in time order.
- Each interval uses exactly one preset id from the catalog. Never invent a \
scale or a preset. NORMAL is the unzoomed frame.
- Changes between intervals are instant cuts to a centre crop of the frame.
- You decide where every interval starts and ends. There is no required \
number of zooms, no fixed or minimum length, and no regular pattern: a long \
NORMAL stretch, or very few changes, is fine when the content calls for it.

Editorial guidance:
- Follow the speech: change framing at the start of a sentence or phrase, or \
at a join between takes, never in the middle of a word or phrase.
- At a join between takes, prefer changing the preset (so the framing on \
each side differs). The join is a cut already, and a framing change there \
makes it read as a deliberate edit rather than a jump.
- Use stronger presets selectively, for real emphasis: a hook, a key claim, \
a punchline, a call to action. Overusing them removes their effect.
- Prefer a natural rhythm over constant change; avoid distracting rapid \
switches.
- Transcript timings come from speech recognition and are approximate \
(phrase-level unless word timings are given, and word timings are \
approximate too). Place changes in the gaps between phrases when you can.

Write "summary" and every "reason" in English. Respond only with JSON \
matching the response schema."""


def describe_timing(segments: list[dict]) -> str:
    worded = sum(1 for segment in segments if segment.get("words"))
    if not segments:
        return "none"
    if worded == len(segments):
        return "word"
    if worded == 0:
        return "phrase"
    return "mixed"


def transcript_view(captions: list[dict]) -> list[dict]:
    """The transcript as sent: phrase times, text, and word times when known."""
    view = []
    for caption in captions:
        item = {
            "start": round(float(caption["start"]), 3),
            "end": round(float(caption["end"]), 3),
            "text": caption["text"],
        }
        words = caption.get("words")
        if words:
            item["words"] = [[word["text"], round(float(word["start"]), 3), round(float(word["end"]), 3)] for word in words]
        view.append(item)
    return view


def build_request(
    *,
    duration_seconds: float,
    fps: float,
    width: int,
    height: int,
    segments: list[dict],
    language: str | None,
    clips: list[dict] | None,
    preference: str,
) -> dict:
    """Exactly what is sent. No file name, path or project name."""
    timing = describe_timing(segments)
    return {
        "video": {
            "duration_seconds": round(duration_seconds, 3),
            "frame_rate": round(fps, 3),
            "orientation": "portrait" if height > width else "landscape" if width > height else "square",
            "timebase": TIMEBASE_DESCRIPTION,
        },
        "takes": (
            {
                "note": "The video is several recorded takes joined in this order. A join is a hard cut already.",
                "clips": clips,
            }
            if clips and len(clips) > 1
            else {"note": "One continuous take.", "clips": None}
        ),
        "transcript": {
            "language": language,
            "timing_precision": {
                "word": "phrase times plus approximate per-word times from speech recognition",
                "phrase": "phrase-level times only; positions of words inside a phrase are unknown",
                "mixed": "phrase-level times everywhere; approximate word times where given",
                "none": "no speech",
            }[timing],
            "segments": segments,
        },
        "presets": [
            {"id": preset["id"], "scale": preset["scale"], "description": preset["description"]}
            for preset in PRESETS
        ],
        "editorial_preference": PREFERENCES[preference],
        "response_schema": RESPONSE_SCHEMA,
    }


def user_message(request: dict) -> str:
    return json.dumps(request, ensure_ascii=False, indent=1)


def repair_message(request: dict, previous: Any, errors: list[str]) -> str:
    return (
        "%s\n\nYour previous answer was rejected by validation:\n- %s\n\nPrevious answer:\n%s\n\n"
        "Return a complete corrected plan for the whole video."
        % (user_message(request), "\n- ".join(errors[:20]), json.dumps(previous, ensure_ascii=False)[:20000])
    )


# --- validation -------------------------------------------------------------------


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _quantise(seconds: float, fps: float) -> float:
    return round(round(seconds * fps) / fps, 6)


def validate_plan(raw: Any, *, duration_seconds: float, fps: float) -> tuple[dict | None, list[str]]:
    """Accept a model answer, or say exactly why not.

    Returns `(plan, [])` or `(None, errors)`. Accepted plans are normalised
    technically, and every change is recorded:

    - every boundary moves to the nearest frame of the video's frame rate,
      which is what the renderer can honour anyway;
    - a first start or last end within `COVERAGE_TOLERANCE_SECONDS` (plus one
      frame) of 0 / the duration is snapped to it;
    - a gap or overlap of at most `JOIN_TOLERANCE_SECONDS` (plus one frame)
      between neighbours is closed at the earlier interval's end.

    Larger gaps, overlaps, disorder, unknown presets or keys, non-finite or
    out-of-range times, and intervals shorter than one frame are errors.
    """
    errors: list[str] = []
    if not isinstance(raw, dict):
        return None, ["The answer is not a JSON object."]
    unknown = sorted(set(raw) - {"summary", "intervals"})
    if unknown:
        errors.append("Unknown top-level field(s): %s." % ", ".join(unknown))

    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append('"summary" must be a non-empty string.')
        summary = ""
    summary = " ".join(summary.split())[:MAX_SUMMARY_CHARACTERS]

    items = raw.get("intervals")
    if not isinstance(items, list) or not items:
        errors.append('"intervals" must be a non-empty list.')
        return None, errors
    if len(items) > MAX_INTERVALS:
        return None, errors + ["Too many intervals (%d; at most %d)." % (len(items), MAX_INTERVALS)]

    frame = 1.0 / fps
    intervals: list[dict] = []
    for number, item in enumerate(items, start=1):
        label = "Interval %d" % number
        if not isinstance(item, dict):
            errors.append("%s is not an object." % label)
            continue
        extra = sorted(set(item) - ALLOWED_INTERVAL_KEYS)
        if extra:
            errors.append("%s has unknown field(s): %s." % (label, ", ".join(extra)))
        preset = item.get("preset")
        if preset not in PRESET_SCALE:
            errors.append("%s uses %r, which is not a preset. Use one of: %s." % (label, preset, ", ".join(PRESET_IDS)))
        start, end = item.get("start"), item.get("end")
        if not _finite(start) or not _finite(end):
            errors.append("%s needs finite numeric start and end times." % label)
            continue
        start, end = float(start), float(end)
        if start < 0:
            errors.append("%s starts before 0 (%.3f s)." % (label, start))
        if end > duration_seconds + COVERAGE_TOLERANCE_SECONDS + frame:
            errors.append("%s ends at %.3f s, past the end of the video (%.3f s)." % (label, end, duration_seconds))
        if end <= start:
            errors.append("%s ends (%.3f s) before it starts (%.3f s)." % (label, end, start))
        reason = item.get("reason")
        reason = " ".join(reason.split())[:MAX_REASON_CHARACTERS] if isinstance(reason, str) else ""
        intervals.append({"start": start, "end": end, "preset": preset, "reason": reason, "number": number})

    if errors:
        return None, errors

    notes: list[str] = []
    max_shift = 0.0

    def shift(before: float, after: float) -> None:
        nonlocal max_shift
        max_shift = max(max_shift, abs(after - before))

    # Coverage at both ends.
    first, last = intervals[0], intervals[-1]
    if first["start"] > COVERAGE_TOLERANCE_SECONDS + frame:
        errors.append("The plan starts at %.3f s; it must start at 0." % first["start"])
    elif first["start"] != 0:
        notes.append("First start moved from %.3f s to 0." % first["start"])
        shift(first["start"], 0.0)
        first["start"] = 0.0
    if abs(last["end"] - duration_seconds) > COVERAGE_TOLERANCE_SECONDS + frame:
        errors.append(
            "The plan ends at %.3f s; it must end at the video's duration, %.3f s." % (last["end"], duration_seconds)
        )
    elif last["end"] != duration_seconds:
        notes.append("Last end moved from %.3f s to the duration, %.3f s." % (last["end"], duration_seconds))
        shift(last["end"], duration_seconds)
        last["end"] = duration_seconds

    # Order and joins.
    for previous, current in zip(intervals, intervals[1:]):
        gap = current["start"] - previous["end"]
        if current["start"] < previous["start"]:
            errors.append("Intervals %d and %d are not in time order." % (previous["number"], current["number"]))
        elif abs(gap) > JOIN_TOLERANCE_SECONDS + frame:
            kind = "gap" if gap > 0 else "overlap"
            errors.append(
                "There is a %.3f s %s between interval %d (ends %.3f s) and interval %d (starts %.3f s)."
                % (abs(gap), kind, previous["number"], previous["end"], current["number"], current["start"])
            )
        elif gap != 0:
            notes.append(
                "Closed a %.3f s %s before interval %d." % (abs(gap), "gap" if gap > 0 else "overlap", current["number"])
            )
            shift(current["start"], previous["end"])
            current["start"] = previous["end"]
    if errors:
        return None, errors

    # Frame grid. The end of the video stays exactly at its duration.
    quantised = []
    for index, interval in enumerate(intervals):
        start = 0.0 if index == 0 else _quantise(interval["start"], fps)
        end = duration_seconds if index == len(intervals) - 1 else _quantise(interval["end"], fps)
        shift(interval["start"], start)
        shift(interval["end"], end)
        quantised.append({**interval, "start": start, "end": end})
    for index in range(1, len(quantised)):
        quantised[index]["start"] = quantised[index - 1]["end"]
    for interval in quantised:
        if interval["end"] - interval["start"] < frame * 0.5:
            errors.append(
                "Interval %d is shorter than one frame after aligning to the %.3f fps frame grid."
                % (interval["number"], fps)
            )
    if errors:
        return None, errors

    return (
        {
            "summary": summary,
            "intervals": [
                {
                    "start": round(interval["start"], 6),
                    "end": round(interval["end"], 6),
                    "preset": interval["preset"],
                    "scale": PRESET_SCALE[interval["preset"]],
                    "reason": interval["reason"],
                }
                for interval in quantised
            ],
            "normalisation": {
                "frame_rate": fps,
                "max_shift_seconds": round(max_shift, 6),
                "notes": notes + ["Every boundary aligned to the nearest frame (max shift %.3f s)." % max_shift],
            },
        },
        [],
    )


def plan_statistics(intervals: list[dict]) -> dict:
    """Seconds per preset and number of changes. Descriptive only."""
    per_preset = {preset: 0.0 for preset in PRESET_IDS}
    for interval in intervals:
        per_preset[interval["preset"]] += interval["end"] - interval["start"]
    changes = sum(1 for a, b in zip(intervals, intervals[1:]) if a["preset"] != b["preset"])
    return {
        "seconds_per_preset": {key: round(value, 3) for key, value in per_preset.items()},
        "interval_count": len(intervals),
        "framing_changes": changes,
    }


# --- providers ------------------------------------------------------------------


class GeminiZoomPlanner:
    """Gemini through `gemini.GeminiClient`. Returns plain dicts only."""

    id = gemini.GEMINI
    label = gemini.GEMINI_LABEL
    is_mock = False

    def __init__(self, model: str, api_key: str, timeout_seconds: float) -> None:
        self.model = model
        self._client = gemini.GeminiClient(model, api_key, timeout_seconds)

    def describe(self) -> dict:
        return {"id": self.id, "label": self.label, "model": self.model, "is_mock": False}

    def complete(self, message: str) -> dict:
        """`{"data": dict, "usage": dict}` — may raise `gemini.GeminiError`."""
        return self._client.generate_json(system=SYSTEM_PROMPT, user=message, schema=RESPONSE_SCHEMA)


class MockZoomPlanner:
    """Fixed rules, no network, always labelled as a demo — never as AI.

    Only used when VIDEO_FACTORY_ZOOM_PROVIDER=mock is set explicitly; it is
    never a fallback for a failing real provider.
    """

    id = "mock"
    label = "Demo rules (no AI)"
    is_mock = True
    model = "deterministic-rules"

    def describe(self) -> dict:
        return {"id": self.id, "label": self.label, "model": self.model, "is_mock": True}

    def complete(self, message: str) -> dict:
        request = json.loads(message.split("\n\nYour previous answer", 1)[0])
        duration = request["video"]["duration_seconds"]
        cursor, intervals = 0.0, []
        for segment in request["transcript"]["segments"]:
            text = segment["text"].strip()
            if text.endswith(("?", "!")) and segment["start"] > cursor and segment["end"] <= duration:
                intervals.append({"start": cursor, "end": segment["start"], "preset": "NORMAL", "reason": "demo"})
                intervals.append({"start": segment["start"], "end": segment["end"], "preset": "PUNCH_LIGHT",
                                  "reason": "demo: question or exclamation"})
                cursor = segment["end"]
        intervals.append({"start": cursor, "end": duration, "preset": "NORMAL", "reason": "demo"})
        intervals = [interval for interval in intervals if interval["end"] > interval["start"]]
        return {
            "data": {
                "summary": "Demo rules, not AI: a light punch-in on questions and exclamations, normal framing elsewhere.",
                "intervals": intervals,
            },
            "usage": {},
        }


def get_planner() -> Any:
    """The configured zoom planner, or a `ProviderError` saying what is missing."""
    import os

    settings = get_zoom_provider_settings()
    if settings["provider"] == gemini.GEMINI:
        key = os.environ.get(GEMINI_API_KEY_ENV_VAR, "").strip()
        if not key:
            raise ProviderError(
                "Zoom planning uses Gemini, but %s is not set in the backend's environment. "
                "Set it as a Windows user environment variable, then start the backend from a "
                "new terminal." % GEMINI_API_KEY_ENV_VAR
            )
        return GeminiZoomPlanner(settings["model"], key, settings["timeout_seconds"])
    if settings["provider"] == "mock":
        return MockZoomPlanner()
    raise ProviderError(
        'Unknown zoom provider "%s". Supported values: gemini, mock.' % settings["provider"]
    )


def provider_status() -> dict:
    """What the interface shows. Presence of the key only — never its value."""
    settings = get_zoom_provider_settings()
    if settings["provider"] == "mock":
        return {
            "provider": "mock", "label": MockZoomPlanner.label, "model": MockZoomPlanner.model,
            "is_mock": True, "ready": True,
            "message": "Demo rules on this machine. Nothing is sent anywhere; this is not AI.",
            "data_sent": [], "free_tier_note": None,
        }
    if settings["provider"] == gemini.GEMINI:
        ready = settings["api_key_present"]
        return {
            "provider": gemini.GEMINI,
            "label": gemini.GEMINI_LABEL,
            "model": settings["model"],
            "is_mock": False,
            "ready": ready,
            "message": (
                "Zoom plans are made by %s. Transcript text and timings are sent to Google; "
                "the video and audio stay on this computer." % settings["model"]
                if ready
                else "Set %s in the backend's environment to plan zooms with Gemini." % GEMINI_API_KEY_ENV_VAR
            ),
            "data_sent": [
                "the transcript text with its phrase/word timings",
                "the video's duration, frame rate and orientation",
                "where takes were joined, if the video is a join",
                "the zoom preset catalog and the editorial preference",
            ],
            "free_tier_note": (
                "On Gemini's free tier, Google may use prompts and responses to improve its "
                "products, and human reviewers may read them. Do not use it for confidential scripts."
            ),
        }
    return {
        "provider": settings["provider"], "label": settings["provider"], "model": settings["model"],
        "is_mock": False, "ready": False,
        "message": 'Unknown zoom provider "%s". Supported values: gemini, mock.' % settings["provider"],
        "data_sent": [], "free_tier_note": None,
    }


def request_plan(
    planner: Any,
    request: dict,
    *,
    duration_seconds: float,
    fps: float,
    cancelled: Callable[[], bool],
    on_wait: Callable[[str], None],
    on_stage: Callable[[str], None],
) -> dict:
    """Ask for a plan, validate it, allow one repair, or raise `ProviderError`.

    Returns the accepted plan plus an audit trail: attempts per call, whether
    a repair was needed, the raw answers and token usage.
    """
    audit: dict = {"calls": [], "repair_used": False, "rejections": []}

    def ask(message: str) -> dict:
        outcome, attempts = gemini.call_with_retries(
            lambda: planner.complete(message), cancelled=cancelled, on_wait=on_wait
        )
        audit["calls"].append({"attempts": attempts, "usage": outcome.get("usage") or {}})
        return outcome["data"]

    on_stage("Asking %s for a zoom plan…" % planner.describe()["label"])
    answer = ask(user_message(request))
    audit["first_answer"] = answer
    plan, errors = validate_plan(answer, duration_seconds=duration_seconds, fps=fps)
    if plan is None:
        audit["rejections"].append(errors)
        audit["repair_used"] = True
        on_stage("The plan had %d problem(s); asking once for a corrected plan…" % len(errors))
        answer = ask(repair_message(request, answer, errors))
        audit["repaired_answer"] = answer
        plan, errors = validate_plan(answer, duration_seconds=duration_seconds, fps=fps)
        if plan is None:
            audit["rejections"].append(errors)
            failure = ProviderError(
                "The zoom plan was rejected even after one correction, so nothing was rendered: %s"
                % "; ".join(errors[:5])
            )
            failure.audit = audit  # type: ignore[attr-defined]
            raise failure
    return {"plan": plan, "audit": audit}
