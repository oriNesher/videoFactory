"""Captions: transcripts, editable caption revisions, style, SRT and render props.

Three kinds of data, kept apart so that changing one never touches another:

**Raw transcript** — what Whisper heard, with word timestamps, for one input
video at one fingerprint with one set of transcription settings. Immutable.
Transcribing again creates a new transcript; nothing is overwritten.

**Caption revisions** — the editable captions built over one transcript:
groups of words with stable ids, text and timing. Every save is a new
immutable revision (`rev-0001.json`, `rev-0002.json`, …). Regrouping from the
transcript is also a new revision, so manual edits are never lost — the
previous revision stays readable and can be reopened.

**Style** — font, size, colour, outline/box, position. One per project, in
`project.settings["captions"]["style"]`. Changing it never re-transcribes and
never changes a caption revision.

On disk, beside the project:

    captions/transcripts/<transcript-id>/
      transcript.json          the raw transcript (immutable)
      revisions/rev-NNNN.json  caption revisions (immutable)
      logs/whisper.log         the worker's output for this transcription

Every timestamp is in seconds from the first frame of the input video the
transcript was made from. No source-to-edit remapping happens here: captioning
a cut output transcribes that output's own audio.
"""

import hashlib
import json
import math
import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import caption_inputs, storage
from .subtitles import RLM

TRANSCRIPT_SCHEMA_VERSION = 1
REVISION_SCHEMA_VERSION = 1

# Bumped when the way a transcript is produced changes in a way that should
# not be reused (the reuse key includes it).
TRANSCRIPTION_RECIPE_VERSION = 1

CAPTIONS_DIRECTORY = "captions"
TRANSCRIPTS_DIRECTORY = "transcripts"
REVISIONS_DIRECTORY = "revisions"
TRANSCRIPT_FILE = "transcript.json"

_TRANSCRIPT_ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")
_CAPTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

MAX_CAPTIONS = 5000
MAX_CAPTION_CHARACTERS = 300
# Timestamps are stored to the millisecond: finer than any frame rate, and
# what SRT can express.
TIME_DECIMALS = 3
# A caption may end this far past the measured duration (container rounding);
# it is then clamped to the duration.
DURATION_TOLERANCE_SECONDS = 0.05
# Word timings may sit this far outside their caption and still count as
# belonging to it (Whisper's word edges are approximate).
WORD_TOLERANCE_SECONDS = 0.02
MIN_CAPTION_SECONDS = 0.001

RTL_LANGUAGES = frozenset({"he", "ar", "fa", "ur", "yi"})


class CaptionError(storage.ProjectError):
    """A caption problem the user can act on."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# --- transcription settings ------------------------------------------------------

# Offered models. The default is the one the earlier subtitles module and the
# legacy script settled on for Hebrew. `repository` is where faster-whisper
# fetches it from; `id` is what it is loaded by.
MODELS: list[dict] = [
    {
        "id": "ivrit-ai/whisper-large-v3-turbo-ct2",
        "repository": "ivrit-ai/whisper-large-v3-turbo-ct2",
        "label": "ivrit.ai large-v3 turbo — Hebrew fine-tune (recommended for Hebrew)",
        "download": "about 1.6 GB",
    },
    {
        "id": "large-v3-turbo",
        "repository": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        "label": "Whisper large-v3 turbo — multilingual",
        "download": "about 1.6 GB",
    },
    {
        "id": "medium",
        "repository": "Systran/faster-whisper-medium",
        "label": "Whisper medium — multilingual",
        "download": "about 1.5 GB",
    },
    {
        "id": "small",
        "repository": "Systran/faster-whisper-small",
        "label": "Whisper small — multilingual, fast, less accurate",
        "download": "about 480 MB",
    },
]
MODELS_BY_ID = {model["id"]: model for model in MODELS}
DEFAULT_MODEL = MODELS[0]["id"]

LANGUAGES: list[dict] = [
    {"code": "he", "label": "Hebrew"},
    {"code": "en", "label": "English"},
    {"code": "ar", "label": "Arabic"},
    {"code": "ru", "label": "Russian"},
    {"code": "fr", "label": "French"},
    {"code": "es", "label": "Spanish"},
    {"code": "de", "label": "German"},
    {"code": "auto", "label": "Detect automatically"},
]
LANGUAGE_CODES = {language["code"] for language in LANGUAGES}
DEFAULT_LANGUAGE = "he"

COMPUTE_TYPES = {
    "cpu": ["int8", "int8_float32", "float32"],
    "cuda": ["float16", "int8_float16", "int8"],
}
DEFAULT_DEVICE = "cpu"


def default_cpu_threads() -> int:
    """Half the logical processors, at most eight: Whisper gains little past
    that on a laptop, and the interface stays responsive."""
    return max(1, min(8, (os.cpu_count() or 2) // 2))


def default_transcription_settings() -> dict:
    return {
        "model": DEFAULT_MODEL,
        "language": DEFAULT_LANGUAGE,
        "device": DEFAULT_DEVICE,
        "compute_type": COMPUTE_TYPES[DEFAULT_DEVICE][0],
        "beam_size": 5,
        "vad_filter": True,
    }


_cuda_devices: int | None = None


def cuda_device_count() -> int:
    """GPUs CTranslate2 can use. Cached: it does not change while running."""
    global _cuda_devices
    if _cuda_devices is None:
        try:
            import ctranslate2

            _cuda_devices = int(ctranslate2.get_cuda_device_count())
        except Exception:  # noqa: BLE001 - not installed, or no CUDA runtime
            _cuda_devices = 0
    return _cuda_devices


CUDA_UNAVAILABLE = (
    "No CUDA GPU is available to CTranslate2 on this computer. GPU transcription "
    "needs an NVIDIA GPU with its driver, CUDA 12 and cuDNN 9 installed. Choose "
    "CPU instead: it needs no GPU, only more time."
)


def validate_transcription_settings(raw: Any) -> dict:
    settings = default_transcription_settings()
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise CaptionError("The transcription settings are invalid.")
    for key in raw:
        if key not in settings:
            raise CaptionError('Transcription setting "%s" is not recognised.' % key)

    model = raw.get("model", settings["model"])
    if model not in MODELS_BY_ID:
        raise CaptionError("Unknown model. Choose one of: %s." % ", ".join(MODELS_BY_ID))
    language = raw.get("language", settings["language"])
    if language not in LANGUAGE_CODES:
        raise CaptionError("Unknown language. Choose one of: %s." % ", ".join(sorted(LANGUAGE_CODES)))
    device = raw.get("device", settings["device"])
    if device not in COMPUTE_TYPES:
        raise CaptionError('Device must be "cpu" or "cuda".')
    compute_type = raw.get("compute_type") or COMPUTE_TYPES[device][0]
    if compute_type not in COMPUTE_TYPES[device]:
        raise CaptionError(
            "Compute type %s is not offered on %s. Choose one of: %s."
            % (compute_type, device.upper(), ", ".join(COMPUTE_TYPES[device]))
        )
    beam_size = raw.get("beam_size", settings["beam_size"])
    if isinstance(beam_size, bool) or not isinstance(beam_size, int) or not 1 <= beam_size <= 10:
        raise CaptionError("Beam size must be a whole number from 1 to 10.")
    vad_filter = raw.get("vad_filter", settings["vad_filter"])
    if not isinstance(vad_filter, bool):
        raise CaptionError("The voice-activity filter setting must be true or false.")

    return {
        "model": model,
        "language": language,
        "device": device,
        "compute_type": compute_type,
        "beam_size": beam_size,
        "vad_filter": vad_filter,
    }


def reuse_key(fingerprint_digest: str, settings: dict) -> str:
    """Two transcriptions with the same key would produce the same transcript.

    The video's bytes and everything that changes Whisper's output. CPU
    thread count is left out: it changes speed, not text.
    """
    canonical = json.dumps(
        {
            "fingerprint": fingerprint_digest,
            "recipe": TRANSCRIPTION_RECIPE_VERSION,
            "model": settings["model"],
            "language": settings["language"],
            "device": settings["device"],
            "compute_type": settings["compute_type"],
            "beam_size": settings["beam_size"],
            "vad_filter": settings["vad_filter"],
            "condition_on_previous_text": True,
            "word_timestamps": True,
        },
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- model availability ----------------------------------------------------------


def hugging_face_cache() -> Path:
    explicit = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    if explicit:
        return Path(explicit)
    home = os.environ.get("HF_HOME")
    if home:
        return Path(home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def model_folder(model_id: str) -> Path:
    repository = MODELS_BY_ID[model_id]["repository"]
    return hugging_face_cache() / ("models--" + repository.replace("/", "--"))


def model_downloaded(model_id: str) -> bool:
    """A snapshot with the CTranslate2 weights in it, not just a started download."""
    snapshots = model_folder(model_id) / "snapshots"
    if not snapshots.is_dir():
        return False
    return any((snapshot / "model.bin").is_file() for snapshot in snapshots.iterdir())


# --- grouping --------------------------------------------------------------------

GROUPING_SPEC: dict[str, dict] = {
    "max_words": {
        "type": "integer", "default": 5, "min": 1, "max": 15, "step": 1, "unit": "words",
        "label": "Maximum words per caption",
        "description": "The most words on screen at once.",
    },
    "max_characters": {
        "type": "integer", "default": 32, "min": 8, "max": 90, "step": 1, "unit": "characters",
        "label": "Line length",
        "description": "A caption is split before it would pass this many characters. "
        "A single longer word gets a caption of its own and wraps.",
    },
    "min_words_before_punctuation": {
        "type": "integer", "default": 2, "min": 1, "max": 10, "step": 1, "unit": "words",
        "label": "Words before a punctuation split",
        "description": "A caption ends at a comma or full stop only once it has this many words.",
    },
    "pause_seconds": {
        "type": "number", "default": 0.45, "min": 0.1, "max": 3.0, "step": 0.05, "unit": "seconds",
        "label": "Split on pause",
        "description": "A pause at least this long between two words starts a new caption.",
    },
    "min_duration_seconds": {
        "type": "number", "default": 0.5, "min": 0.0, "max": 2.0, "step": 0.05, "unit": "seconds",
        "label": "Minimum time on screen",
        "description": "A very short caption is held this long when the gap before "
        "the next one allows. Word timings are not changed.",
    },
}

# Punctuation that may close a caption, including the Hebrew sof pasuq.
_PHRASE_ENDINGS = (".", "!", "?", "…", ",", ":", ";", "׃")


def default_grouping() -> dict:
    return {name: spec["default"] for name, spec in GROUPING_SPEC.items()}


def validate_grouping(raw: Any) -> dict:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise CaptionError("The grouping settings are invalid.")
    for name in raw:
        if name not in GROUPING_SPEC:
            raise CaptionError('Grouping setting "%s" is not recognised.' % name)
    validated = {}
    for name, spec in GROUPING_SPEC.items():
        value = raw.get(name, spec["default"])
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise CaptionError('"%s" must be a number.' % spec["label"])
        if spec["type"] == "integer":
            if float(value) != int(value):
                raise CaptionError('"%s" must be a whole number.' % spec["label"])
            value = int(value)
        else:
            value = round(float(value), 3)
        if not spec["min"] <= value <= spec["max"]:
            raise CaptionError(
                '"%s" must be between %s and %s %s.' % (spec["label"], spec["min"], spec["max"], spec["unit"])
            )
        validated[name] = value
    return validated


def clean_text(text: Any) -> str:
    """NFC, single spaces, no control characters. Never reorders anything."""
    normalised = unicodedata.normalize("NFC", str(text))
    normalised = "".join(
        " " if unicodedata.category(character) in ("Cc", "Zl", "Zp") else character
        for character in normalised
    )
    return " ".join(normalised.split())


def tokens(text: str) -> list[str]:
    return clean_text(text).split(" ") if clean_text(text) else []


def new_caption_id() -> str:
    return "c" + uuid.uuid4().hex[:10]


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _transcript_words(segment: dict) -> list[dict]:
    """A segment's usable words: text present, finite times, end not before start."""
    words = []
    for word in segment.get("words") or []:
        text = clean_text(word.get("word", ""))
        start, end = word.get("start"), word.get("end")
        if not text or not _finite(start) or not _finite(end):
            continue
        words.append({"text": text, "start": float(start), "end": max(float(start), float(end))})
    return words


def _split_words(words: list[dict], settings: dict) -> list[list[dict]]:
    """The legacy line rules, one segment at a time.

    A caption closes when it reaches the word limit; before a word that would
    take it past the character limit; before a pause of at least
    `pause_seconds`; or at closing punctuation once it holds enough words.
    Each closure remembers why, so the rebalancing below knows which splits
    it may move.
    """
    max_words = settings["max_words"]
    max_characters = settings["max_characters"]
    min_words = settings["min_words_before_punctuation"]
    pause = settings["pause_seconds"]

    groups: list[dict] = []
    current: list[dict] = []

    def length(items: list[dict]) -> int:
        return len(" ".join(item["text"] for item in items))

    def close(reason: str) -> None:
        nonlocal current
        if current:
            groups.append({"words": current, "reason": reason})
        current = []

    for index, word in enumerate(words):
        following = words[index + 1] if index + 1 < len(words) else None
        if current and (len(current) + 1 > max_words or length(current + [word]) > max_characters):
            close("limit")
        current.append(word)

        if following is None:
            close("end")
        elif following["start"] - word["end"] >= pause:
            close("pause")
        elif len(current) >= max_words or length(current) >= max_characters:
            close("limit")
        elif len(current) >= min_words and word["text"].endswith(_PHRASE_ENDINGS):
            close("punctuation")

    return _rebalance(groups, max_characters)


def _rebalance(groups: list[dict], max_characters: int) -> list[list[dict]]:
    """Avoid a lone word left behind by a length split.

    "a b c d e | f" (closed by the word limit, then the phrase ended) reads
    better as "a b c | d e f". Only splits made by a limit are moved, never a
    pause or punctuation split, and only when both halves still fit the line.
    """
    result = [group["words"] for group in groups]
    reasons = [group["reason"] for group in groups]
    for index in range(1, len(result)):
        if len(result[index]) != 1 or reasons[index - 1] != "limit":
            continue
        combined = result[index - 1] + result[index]
        half = (len(combined) + 1) // 2
        first, second = combined[:half], combined[half:]
        fits = all(
            len(" ".join(item["text"] for item in part)) <= max_characters for part in (first, second)
        )
        if fits and len(second) > 1:
            result[index - 1], result[index] = first, second
    return result


def group_transcript(transcript: dict, settings: dict) -> list[dict]:
    """Deterministic captions from a transcript: same input, same captions.

    Segment boundaries always split (Whisper ends a segment at a sentence or a
    pause). A segment without word timestamps becomes one caption with
    segment timing. Afterwards captions are kept in order and apart, held for
    the minimum time where the gap allows, and clamped to the video.
    """
    duration = float(transcript["input"]["metadata"]["duration_seconds"])
    captions: list[dict] = []

    for segment in transcript.get("segments") or []:
        words = _transcript_words(segment)
        if not words:
            text = clean_text(segment.get("text", ""))
            start, end = segment.get("start"), segment.get("end")
            if text and _finite(start) and _finite(end):
                captions.append({"start": float(start), "end": float(end), "text": text, "words": None})
            continue
        for group in _split_words(words, settings):
            captions.append(
                {
                    "start": group[0]["start"],
                    "end": group[-1]["end"],
                    "text": " ".join(word["text"] for word in group),
                    "words": [dict(word) for word in group],
                }
            )

    captions.sort(key=lambda caption: caption["start"])
    minimum = settings["min_duration_seconds"]
    tidy: list[dict] = []
    for index, caption in enumerate(captions):
        start = max(0.0, min(caption["start"], duration))
        if tidy and start < tidy[-1]["end"]:
            start = tidy[-1]["end"]
        end = min(max(caption["end"], start), duration)
        following = captions[index + 1]["start"] if index + 1 < len(captions) else duration
        limit = max(end, min(following, duration))
        if end - start < minimum:
            end = min(start + minimum, limit)
        if end - start < MIN_CAPTION_SECONDS:
            continue  # nothing left of it inside the video
        words = caption["words"]
        if words is not None:
            words = [
                {
                    "text": word["text"],
                    "start": _round(min(max(word["start"], start), end)),
                    "end": _round(min(max(word["end"], start), end)),
                }
                for word in words
            ]
        tidy.append(
            {
                "id": new_caption_id(),
                "start": _round(start),
                "end": _round(end),
                "text": caption["text"],
                "words": words,
                "word_timing": "word" if words is not None else "group",
                "timing_note": None if words is not None else "Whisper gave no word timing for this segment.",
            }
        )
    return tidy


def _round(value: float) -> float:
    return round(float(value), TIME_DECIMALS)


# --- validation ------------------------------------------------------------------


def _format_seconds(value: float) -> str:
    return "%.3f s" % value


def normalise_captions(raw: Any, duration: float) -> tuple[list[dict], list[dict]]:
    """Validate an edited caption list. Returns `(captions, errors)`.

    Errors are about timing and text the user must fix: each names the
    caption, the field and what to do. Word timing is never an error: when it
    no longer matches the text it is dropped to group-level timing, with a
    note saying why — a corrected word count must not invent alignment.
    """
    errors: list[dict] = []

    def fail(index: int, caption_id: Any, field: str, message: str) -> None:
        errors.append(
            {
                "index": index,
                "caption_id": caption_id if isinstance(caption_id, str) else None,
                "field": field,
                "message": "Caption %d: %s" % (index + 1, message),
            }
        )

    if not isinstance(raw, list):
        return [], [{"index": None, "caption_id": None, "field": "captions", "message": "The captions must be a list."}]
    if len(raw) > MAX_CAPTIONS:
        return [], [
            {"index": None, "caption_id": None, "field": "captions",
             "message": "Too many captions (%d; at most %d)." % (len(raw), MAX_CAPTIONS)}
        ]

    captions: list[dict] = []
    seen: set[str] = set()

    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            fail(index, None, "caption", "is not a caption.")
            continue
        caption_id = item.get("id")
        if not isinstance(caption_id, str) or not _CAPTION_ID_PATTERN.match(caption_id):
            fail(index, caption_id, "id", "has an invalid id.")
            continue
        if caption_id in seen:
            fail(index, caption_id, "id", "has the same id as an earlier caption.")
            continue
        seen.add(caption_id)

        text = clean_text(item.get("text", "")) if isinstance(item.get("text"), str) else ""
        if not text:
            fail(index, caption_id, "text", "has no text. Type the words, or merge it into a neighbour.")
        elif len(text) > MAX_CAPTION_CHARACTERS:
            fail(index, caption_id, "text", "is too long (%d characters; at most %d). Split it." % (len(text), MAX_CAPTION_CHARACTERS))

        start, end = item.get("start"), item.get("end")
        timing_ok = True
        for field, value in (("start", start), ("end", end)):
            if not _finite(value):
                fail(index, caption_id, field, "the %s time is not a number." % field)
                timing_ok = False
        if timing_ok:
            start, end = _round(start), _round(end)
            if start < 0:
                fail(index, caption_id, "start", "starts before the video (%s). Use 0 or later." % _format_seconds(start))
                timing_ok = False
            if end > duration + DURATION_TOLERANCE_SECONDS:
                fail(
                    index, caption_id, "end",
                    "ends at %s, after the video ends (%s). Move the end earlier."
                    % (_format_seconds(end), _format_seconds(duration)),
                )
                timing_ok = False
            else:
                end = min(end, _round(duration))
            if timing_ok and end - start < MIN_CAPTION_SECONDS:
                fail(index, caption_id, "end", "ends at or before its start (%s → %s). Move the end later." % (_format_seconds(start), _format_seconds(end)))
                timing_ok = False

        if not timing_ok or not text:
            continue

        if captions:
            previous = captions[-1]
            if start < previous["start"]:
                fail(
                    index, caption_id, "start",
                    "starts at %s, before caption %d (%s). Captions must stay in order; adjust the times."
                    % (_format_seconds(start), index, _format_seconds(previous["start"])),
                )
                continue
            if start < previous["end"]:
                fail(
                    index, caption_id, "start",
                    "starts at %s while caption %d is still on screen (until %s). Move this start "
                    "later, move the previous end earlier, or use \"Fix overlaps\"."
                    % (_format_seconds(start), index, _format_seconds(previous["end"])),
                )
                continue

        words, note = _normalise_words(item.get("words"), text, start, end)
        captions.append(
            {
                "id": caption_id,
                "start": start,
                "end": end,
                "text": text,
                "words": words,
                "word_timing": "word" if words is not None else "group",
                "timing_note": note if words is None else None,
            }
        )

    return captions, errors


def _normalise_words(raw: Any, text: str, start: float, end: float) -> tuple[list[dict] | None, str | None]:
    """Keep word timing only while it still describes this text exactly.

    Same number of words as the text, finite times, in order, inside the
    caption: the timing is kept and each word takes the corrected spelling.
    Anything else falls back to group-level timing, with the reason.
    """
    if raw is None:
        return None, "Group-level timing: no word timing is attached to this caption."
    if not isinstance(raw, list):
        return None, "Group-level timing: the word timing was unreadable."

    words_in_text = tokens(text)
    if len(raw) != len(words_in_text):
        return None, (
            "Group-level timing: the text now has %d words and the timing had %d, so word "
            "timing was removed rather than guessed. The caption's start and end are kept."
            % (len(words_in_text), len(raw))
        )

    kept = []
    previous_start = -math.inf
    for word, spelled in zip(raw, words_in_text):
        if not isinstance(word, dict) or not _finite(word.get("start")) or not _finite(word.get("end")):
            return None, "Group-level timing: a word had no usable time."
        word_start, word_end = float(word["start"]), float(word["end"])
        if (
            word_end < word_start
            or word_start < previous_start
            or word_start < start - WORD_TOLERANCE_SECONDS
            or word_end > end + WORD_TOLERANCE_SECONDS
        ):
            return None, (
                "Group-level timing: the caption's times no longer contain its words' times, "
                "so word timing was removed. Adjust the times or regroup to restore it."
            )
        previous_start = word_start
        kept.append(
            {
                "text": spelled,
                "start": _round(min(max(word_start, start), end)),
                "end": _round(min(max(word_end, start), end)),
            }
        )
    return kept, None


def _find(captions: list[dict], caption_id: Any) -> int:
    for index, caption in enumerate(captions):
        if caption.get("id") == caption_id:
            return index
    raise CaptionError("That caption is not in the list any more.", status_code=404)


def split_caption(captions: list[dict], caption_id: Any, word_index: Any, at_seconds: Any = None) -> list[dict]:
    """Split one caption before word `word_index` (1 … words−1).

    With word timing, the first part ends where its last word ends and the
    second starts where its first word starts: real timings, nothing invented.
    Without word timing the split time is `at_seconds` (the player position,
    when it lies inside the caption) or, failing that, a point proportional to
    the characters on each side — marked as an estimate to check by eye.
    """
    index = _find(captions, caption_id)
    caption = captions[index]
    words_in_text = tokens(caption["text"])
    if isinstance(word_index, bool) or not isinstance(word_index, int) or not 1 <= word_index < len(words_in_text):
        raise CaptionError("Choose a split point between two words of the caption.")

    first_text = " ".join(words_in_text[:word_index])
    second_text = " ".join(words_in_text[word_index:])
    words = caption.get("words")
    note = None
    if isinstance(words, list) and len(words) == len(words_in_text):
        first_words, second_words = words[:word_index], words[word_index:]
        first_end, second_start = first_words[-1]["end"], second_words[0]["start"]
        if second_start < first_end:
            second_start = first_end
    else:
        first_words = second_words = None
        start, end = caption["start"], caption["end"]
        if _finite(at_seconds) and start < at_seconds < end:
            first_end = second_start = float(at_seconds)
            note = "Group-level timing: split at the player position; check it by eye."
        else:
            share = len(first_text) / max(1, len(first_text) + len(second_text))
            first_end = second_start = start + (end - start) * share
            note = "Group-level timing: the split time is an estimate from the text length; adjust it by eye."

    first = {
        **caption,
        "end": _round(first_end),
        "text": first_text,
        "words": first_words,
        "word_timing": "word" if first_words else "group",
        "timing_note": note if not first_words else None,
    }
    second = {
        **caption,
        "id": new_caption_id(),
        "start": _round(second_start),
        "text": second_text,
        "words": second_words,
        "word_timing": "word" if second_words else "group",
        "timing_note": note if not second_words else None,
    }
    return captions[:index] + [first, second] + captions[index + 1 :]


def merge_with_next(captions: list[dict], caption_id: Any) -> list[dict]:
    """Join a caption with the one after it. The first id is kept.

    Word timing survives only if both had it; otherwise the merged caption is
    group-level, from the first start to the second end.
    """
    index = _find(captions, caption_id)
    if index + 1 >= len(captions):
        raise CaptionError("The last caption has no next caption to merge with.")
    first, second = captions[index], captions[index + 1]
    both_timed = isinstance(first.get("words"), list) and isinstance(second.get("words"), list)
    words = (first["words"] + second["words"]) if both_timed else None
    merged = {
        **first,
        "end": second["end"],
        "text": clean_text("%s %s" % (first["text"], second["text"])),
        "words": words,
        "word_timing": "word" if words is not None else "group",
        "timing_note": None if words is not None else "Group-level timing: merged from captions without complete word timing.",
    }
    return captions[:index] + [merged] + captions[index + 2 :]


def fix_overlaps(captions: list[dict]) -> list[dict]:
    """Trim each caption's end to the next one's start. Never moves a start."""
    fixed = [dict(caption) for caption in sorted(captions, key=lambda caption: caption["start"])]
    for current, following in zip(fixed, fixed[1:]):
        if current["end"] > following["start"]:
            current["end"] = max(following["start"], current["start"] + MIN_CAPTION_SECONDS)
    return fixed


# --- storage ---------------------------------------------------------------------


def captions_root(project_id: str) -> Path:
    return storage.project_directory(project_id) / CAPTIONS_DIRECTORY


def transcripts_root(project_id: str) -> Path:
    return captions_root(project_id) / TRANSCRIPTS_DIRECTORY


def transcript_directory(project_id: str, transcript_id: str) -> Path:
    return transcripts_root(project_id) / validate_transcript_id(transcript_id)


def validate_transcript_id(raw: Any) -> str:
    if not isinstance(raw, str) or not _TRANSCRIPT_ID_PATTERN.match(raw):
        raise CaptionError("Transcript not found.", status_code=404)
    return raw


def validate_revision_number(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1 or raw > 9999:
        raise CaptionError("Caption revision not found.", status_code=404)
    return raw


def new_transcript_id() -> str:
    return uuid.uuid4().hex[:12]


def write_transcript(project_id: str, document: dict) -> None:
    path = transcript_directory(project_id, document["id"]) / TRANSCRIPT_FILE
    try:
        storage.write_json_exclusive(path, document)
    except FileExistsError as error:  # pragma: no cover - ids are random
        raise CaptionError("A transcript with this id already exists.", status_code=409) from error
    except OSError as error:
        raise CaptionError("Saving the transcript failed: %s" % error, status_code=500) from error


def read_transcript(project_id: str, transcript_id: Any) -> dict:
    storage.validate_project_id(project_id)
    path = transcript_directory(project_id, transcript_id) / TRANSCRIPT_FILE
    try:
        document = storage.read_json(path)
    except FileNotFoundError as error:
        raise CaptionError("Transcript not found.", status_code=404) from error
    except (OSError, ValueError) as error:
        raise CaptionError("The transcript cannot be read: %s" % error, status_code=422) from error
    if not isinstance(document, dict) or document.get("kind") != "video_factory.transcript":
        raise CaptionError("The transcript file is not a transcript.", status_code=422)
    if (document.get("schema_version") or 0) > TRANSCRIPT_SCHEMA_VERSION:
        raise CaptionError("The transcript was written by a newer version.", status_code=422)
    return document


def list_transcripts(project_id: str) -> list[dict]:
    root = transcripts_root(project_id)
    if not root.is_dir():
        return []
    documents = []
    for directory in root.iterdir():
        if not directory.is_dir() or not _TRANSCRIPT_ID_PATTERN.match(directory.name):
            continue
        try:
            documents.append(read_transcript(project_id, directory.name))
        except storage.ProjectError:
            continue
    documents.sort(key=lambda document: document.get("created_at") or "", reverse=True)
    return documents


def revisions_directory(project_id: str, transcript_id: str) -> Path:
    return transcript_directory(project_id, transcript_id) / REVISIONS_DIRECTORY


def revision_path(project_id: str, transcript_id: str, revision: int) -> Path:
    return revisions_directory(project_id, transcript_id) / ("rev-%04d.json" % revision)


def revision_numbers(project_id: str, transcript_id: str) -> list[int]:
    directory = revisions_directory(project_id, transcript_id)
    if not directory.is_dir():
        return []
    numbers = []
    for path in directory.glob("rev-*.json"):
        match = re.match(r"^rev-(\d{4})$", path.stem)
        if match:
            numbers.append(int(match.group(1)))
    return sorted(numbers)


def read_revision(project_id: str, transcript_id: Any, revision: Any) -> dict:
    transcript_id = validate_transcript_id(transcript_id)
    revision = validate_revision_number(revision)
    try:
        document = storage.read_json(revision_path(project_id, transcript_id, revision))
    except FileNotFoundError as error:
        raise CaptionError("Caption revision %d not found." % revision, status_code=404) from error
    except (OSError, ValueError) as error:
        raise CaptionError("The caption revision cannot be read: %s" % error, status_code=422) from error
    if not isinstance(document, dict):
        raise CaptionError("The caption revision is corrupt.", status_code=422)
    return document


def latest_revision(project_id: str, transcript_id: str) -> dict | None:
    numbers = revision_numbers(project_id, transcript_id)
    return read_revision(project_id, transcript_id, numbers[-1]) if numbers else None


ORIGIN_GROUPING = "grouping"
ORIGIN_EDIT = "edit"
ORIGIN_REGROUP = "regroup"


def create_revision(
    project_id: str,
    transcript: dict,
    *,
    captions: list[dict],
    grouping: dict,
    origin: str,
    parent: int | None,
    note: str | None = None,
) -> dict:
    """Append an immutable revision. The caller has validated `captions`."""
    transcript_id = transcript["id"]
    numbers = revision_numbers(project_id, transcript_id)
    number = (numbers[-1] if numbers else 0) + 1

    manual = origin == ORIGIN_EDIT

    document = {
        "schema_version": REVISION_SCHEMA_VERSION,
        "kind": "video_factory.caption_revision",
        "transcript_id": transcript_id,
        "revision": number,
        "parent_revision": parent,
        "origin": origin,
        # True once a person has edited text or timing since the last
        # grouping: regrouping then asks first.
        "has_manual_edits": manual,
        "created_at": now(),
        "note": note,
        # Copied, so a revision says by itself which video it belongs to.
        "input": {
            "key": transcript["input"]["key"],
            "fingerprint": transcript["input"]["fingerprint"],
            "duration_seconds": transcript["input"]["metadata"]["duration_seconds"],
        },
        "language": transcript["result"]["language"],
        "grouping": grouping,
        "captions": captions,
    }
    try:
        storage.write_json_exclusive(revision_path(project_id, transcript_id, number), document)
    except FileExistsError as error:
        raise CaptionError(
            "Someone saved revision %d at the same moment. Reload and try again." % number,
            status_code=409,
        ) from error
    except OSError as error:
        raise CaptionError("Saving the captions failed: %s" % error, status_code=500) from error
    return document


def save_edited_revision(
    project_id: str,
    transcript_id: Any,
    *,
    base_revision: Any,
    captions: Any,
    grouping: Any = None,
) -> dict:
    """Save the editor's captions as a new revision, or refuse with every error."""
    transcript = read_transcript(project_id, transcript_id)
    base_revision = validate_revision_number(base_revision)
    base = read_revision(project_id, transcript["id"], base_revision)
    numbers = revision_numbers(project_id, transcript["id"])
    if numbers and base_revision != numbers[-1]:
        raise CaptionError(
            "These edits were made on revision %d, but revision %d has been saved since. "
            "Open the latest revision and apply your changes there." % (base_revision, numbers[-1]),
            status_code=409,
        )
    normalised, errors = normalise_captions(captions, transcript["input"]["metadata"]["duration_seconds"])
    if errors:
        raise CaptionValidationFailed(errors)
    return create_revision(
        project_id,
        transcript,
        captions=normalised,
        grouping=validate_grouping(grouping) if grouping is not None else base["grouping"],
        origin=ORIGIN_EDIT,
        parent=base_revision,
    )


class CaptionValidationFailed(CaptionError):
    def __init__(self, errors: list[dict]) -> None:
        shown = "; ".join(error["message"] for error in errors[:5])
        more = " (and %d more)" % (len(errors) - 5) if len(errors) > 5 else ""
        super().__init__("The captions were not saved. %s%s" % (shown, more), status_code=400)
        self.errors = errors


def regroup(project_id: str, transcript_id: Any, *, base_revision: Any, grouping: Any, confirm_discard_edits: bool) -> dict:
    """Rebuild captions from the transcript with new grouping, as a new revision.

    The previous revision is never touched. When it carries manual edits the
    request must confirm, because the new revision will not contain them.
    """
    transcript = read_transcript(project_id, transcript_id)
    settings = validate_grouping(grouping)
    parent = None
    if base_revision is not None:
        base = read_revision(project_id, transcript["id"], validate_revision_number(base_revision))
        parent = base["revision"]
        if base.get("has_manual_edits") and not confirm_discard_edits:
            raise CaptionError(
                "Revision %d has manual corrections. Regrouping starts again from the "
                "transcript, so the new revision will not include them (revision %d stays "
                "saved and can be reopened). Confirm to continue." % (parent, parent),
                status_code=409,
            )
    created = create_revision(
        project_id,
        transcript,
        captions=group_transcript(transcript, settings),
        grouping=settings,
        origin=ORIGIN_REGROUP if parent is not None else ORIGIN_GROUPING,
        parent=parent,
    )
    # Remembered as the project's grouping, which the next transcription uses.
    caption_inputs.update_captions_block(project_id, {"grouping": settings})
    return created


# --- describing for the interface -----------------------------------------------


def staleness(transcript: dict, current_fingerprint: dict | None) -> dict:
    """Is this transcript still about the video now behind its input key?"""
    if current_fingerprint is None:
        return {"stale": True, "reason": "The video is not available right now."}
    recorded = transcript["input"]["fingerprint"]["digest"]
    if recorded != current_fingerprint["digest"]:
        return {
            "stale": True,
            "reason": "The video has changed since it was transcribed (its content "
            "fingerprint differs). These captions are kept but cannot be used for "
            "the new video — transcribe it again.",
        }
    return {"stale": False, "reason": None}


def summarise_transcript(project_id: str, transcript: dict, current_fingerprint: dict | None) -> dict:
    numbers = revision_numbers(project_id, transcript["id"])
    latest = read_revision(project_id, transcript["id"], numbers[-1]) if numbers else None
    return {
        "id": transcript["id"],
        "created_at": transcript["created_at"],
        "input_key": transcript["input"]["key"],
        "input_label": transcript["input"]["label"],
        "fingerprint": transcript["input"]["fingerprint"]["digest"],
        "settings": transcript["settings"],
        "model_label": MODELS_BY_ID.get(transcript["settings"]["model"], {}).get("label", transcript["settings"]["model"]),
        "language": transcript["result"]["language"],
        "language_probability": transcript["result"].get("language_probability"),
        "word_count": transcript["result"].get("word_count"),
        "segment_count": len(transcript.get("segments") or []),
        "job_id": transcript.get("job_id"),
        "retranscription": transcript.get("retranscription", False),
        "revisions": numbers,
        "latest_revision": latest["revision"] if latest else None,
        "latest_has_manual_edits": bool(latest and latest.get("has_manual_edits")),
        **staleness(transcript, current_fingerprint),
    }


def find_reusable(project_id: str, key: str) -> dict | None:
    for transcript in list_transcripts(project_id):
        if transcript.get("reuse_key") == key:
            return transcript
    return None


def direction_for(language: str) -> str:
    return "rtl" if language in RTL_LANGUAGES else "ltr"


# --- style -----------------------------------------------------------------------

FONTS = [
    {"id": "heebo", "label": "Heebo", "license": "SIL Open Font License 1.1"},
    {"id": "rubik", "label": "Rubik", "license": "SIL Open Font License 1.1"},
    {"id": "noto-sans-hebrew", "label": "Noto Sans Hebrew", "license": "SIL Open Font License 1.1"},
]
FONT_IDS = {font["id"] for font in FONTS}

_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")

STYLE_SPEC: dict[str, dict] = {
    "font_family": {"type": "choice", "default": "heebo", "choices": sorted(FONT_IDS), "label": "Font"},
    "font_weight": {"type": "integer", "default": 700, "min": 300, "max": 900, "step": 100, "label": "Weight"},
    "font_size_percent": {
        "type": "number", "default": 6.5, "min": 2.0, "max": 15.0, "step": 0.25,
        "label": "Font size", "unit": "% of the shorter side",
    },
    "text_color": {"type": "colour", "default": "#ffffff", "label": "Text colour"},
    "background": {"type": "choice", "default": "outline", "choices": ["outline", "box", "none"], "label": "Readability"},
    "outline_color": {"type": "colour", "default": "#000000", "label": "Outline colour"},
    "outline_width_percent": {
        "type": "number", "default": 8.0, "min": 0.0, "max": 25.0, "step": 1.0,
        "label": "Outline width", "unit": "% of the font size",
    },
    "box_color": {"type": "colour", "default": "#000000", "label": "Box colour"},
    "box_opacity": {"type": "number", "default": 0.6, "min": 0.0, "max": 1.0, "step": 0.05, "label": "Box opacity"},
    "vertical_position_percent": {
        "type": "number", "default": 80.0, "min": 0.0, "max": 100.0, "step": 1.0,
        "label": "Vertical position", "unit": "% (0 top, 100 bottom of the safe area)",
    },
    "safe_margin_percent": {
        "type": "number", "default": 6.0, "min": 0.0, "max": 20.0, "step": 0.5,
        "label": "Safe margin", "unit": "% of each side",
    },
    "max_width_percent": {
        "type": "number", "default": 90.0, "min": 30.0, "max": 100.0, "step": 1.0,
        "label": "Maximum line width", "unit": "% of the safe area",
    },
    "highlight_active_word": {"type": "boolean", "default": False, "label": "Highlight the spoken word"},
    "highlight_color": {"type": "colour", "default": "#ffd400", "label": "Highlight colour"},
}


def default_style() -> dict:
    return {name: spec["default"] for name, spec in STYLE_SPEC.items()}


def validate_style(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise CaptionError("The caption style is invalid.")
    for name in raw:
        if name not in STYLE_SPEC:
            raise CaptionError('Style setting "%s" is not recognised.' % name)
    style = {}
    for name, spec in STYLE_SPEC.items():
        value = raw.get(name, spec["default"])
        kind = spec["type"]
        if kind == "choice":
            if value not in spec["choices"]:
                raise CaptionError('"%s" must be one of: %s.' % (spec["label"], ", ".join(spec["choices"])))
        elif kind == "colour":
            if not isinstance(value, str) or not _COLOUR.match(value):
                raise CaptionError('"%s" must be a colour such as #ffffff.' % spec["label"])
            value = value.lower()
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise CaptionError('"%s" must be true or false.' % spec["label"])
        else:
            if not _finite(value):
                raise CaptionError('"%s" must be a number.' % spec["label"])
            if kind == "integer":
                if float(value) != int(value):
                    raise CaptionError('"%s" must be a whole number.' % spec["label"])
                value = int(value)
            else:
                value = round(float(value), 3)
            if not spec["min"] <= value <= spec["max"]:
                raise CaptionError('"%s" must be between %s and %s.' % (spec["label"], spec["min"], spec["max"]))
        style[name] = value
    return style


def read_style(project: dict) -> dict:
    block = project.get("settings", {}).get(caption_inputs.SETTINGS_KEY)
    stored = block.get("style") if isinstance(block, dict) else None
    revision = block.get("style_revision", 0) if isinstance(block, dict) else 0
    try:
        style = validate_style(stored) if isinstance(stored, dict) else default_style()
    except CaptionError:
        style = default_style()  # a hand-edited block must never stop the screen opening
    return {"style": style, "style_revision": revision if isinstance(revision, int) else 0}


def save_style(project_id: str, raw: Any) -> dict:
    style = validate_style(raw)
    project = storage.read_project(project_id)
    current = read_style(project)
    revision = current["style_revision"] + (0 if current["style"] == style else 1)
    caption_inputs.update_captions_block(project_id, {"style": style, "style_revision": revision})
    return {"style": style, "style_revision": revision}


def read_grouping(project: dict) -> dict:
    block = project.get("settings", {}).get(caption_inputs.SETTINGS_KEY)
    try:
        return validate_grouping(block.get("grouping") if isinstance(block, dict) else None)
    except CaptionError:
        return default_grouping()


# --- SRT -------------------------------------------------------------------------


def srt_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(max(0, milliseconds), 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return "%02d:%02d:%02d,%03d" % (hours, minutes, seconds, millis)


def render_srt(captions: list[dict], direction: str) -> str:
    """SRT from edited captions: text and timing only, no styling.

    Times are rounded to milliseconds and kept strictly increasing within each
    cue. Right-to-left text is wrapped in RIGHT-TO-LEFT MARKs (as the older
    subtitles module does) so players that lay a cue out left-to-right still
    put Hebrew punctuation on the correct side; the words are not reordered.
    """
    blocks = []
    for number, caption in enumerate(captions, start=1):
        start = round(caption["start"] * 1000)
        end = max(start + 1, round(caption["end"] * 1000))
        text = caption["text"]
        if direction == "rtl":
            text = "%s%s%s" % (RLM, text, RLM)
        blocks.append("%d\n%s --> %s\n%s\n" % (number, srt_timestamp(start), srt_timestamp(end), text))
    return "\n".join(blocks)


# --- composition props ----------------------------------------------------------


def composition_props(composition: dict, captions: list[dict], style: dict, language: str) -> dict:
    """The props the Remotion composition renders from (without the video URL).

    The same shape the editor's preview builds from its draft; the composition
    itself turns seconds into frames.
    """
    return {
        "video": {
            "width": composition["width"],
            "height": composition["height"],
            "fps": composition["fps"],
            "duration_in_frames": composition["duration_in_frames"],
        },
        "captions": [
            {
                "id": caption["id"],
                "start": caption["start"],
                "end": caption["end"],
                "text": caption["text"],
                "words": caption["words"],
            }
            for caption in captions
        ],
        "style": style,
        "direction": direction_for(language),
    }


def catalog() -> dict:
    """Everything the captions screen needs to draw its controls."""
    cuda = cuda_device_count()
    return {
        "models": [
            {**model, "downloaded": model_downloaded(model["id"]), "default": model["id"] == DEFAULT_MODEL}
            for model in MODELS
        ],
        "model_cache": str(hugging_face_cache()),
        "languages": LANGUAGES,
        "devices": [
            {"id": "cpu", "label": "CPU", "available": True, "reason": None},
            {"id": "cuda", "label": "NVIDIA GPU (CUDA)", "available": cuda > 0, "reason": None if cuda > 0 else CUDA_UNAVAILABLE},
        ],
        "compute_types": COMPUTE_TYPES,
        "cpu_threads": default_cpu_threads(),
        "transcription_defaults": default_transcription_settings(),
        "grouping": [{"name": name, **spec} for name, spec in GROUPING_SPEC.items()],
        "grouping_defaults": default_grouping(),
        "style": [{"name": name, **spec} for name, spec in STYLE_SPEC.items()],
        "style_defaults": default_style(),
        "fonts": FONTS,
    }
