"""The cut map: which source time ended up where in the output.

Captions, zooms and B-roll will all need to answer "this moment of the source —
where is it in the edited video?" and the reverse. A run therefore records, for
every clip it produced, the source intervals that were kept and where each one
landed, and writes the whole thing as `cut-map.json` beside its manifest.

**Conventions.** Every time is in seconds, as a decimal number, measured from
the first frame of the file it refers to. Every interval is half-open,
`[start, end)`. Three timelines are involved:

- *source*: the original recording;
- *output*: one trimmed clip (`clips\\NNNN_…_trimmed.mp4`);
- *sequence*: the clips joined in run order (`combined\\combined.mp4`).

To map a source time `t` that lies inside a retained interval:

    output   = interval.output_start + (t - interval.source_start)
    sequence = clip.sequence_start_seconds + output

A source time inside a removed interval has no image in the output.

**Where the intervals come from.** Never from durations. In boundary-only mode
they are the interval the renderer was given. In full-clip mode they are read
from Auto-Editor's own timeline (`--export v3`), produced with the same editing
flags as the render. If that export is missing or unusable the map says
`available: false` with the reason, rather than inventing intervals.

**Tolerance.** A map is checked against the rendered file: the kept intervals
must add up to the measured duration of the clip within one video frame plus
two AAC frames (about 0.083 s at 25 fps and 48 kHz). Encoders round to whole
frames, so exact equality is not achievable; anything beyond that tolerance
marks the clip's map unavailable.
"""

from pathlib import Path
from typing import Any

from . import cutting, storage

CUT_MAP_SCHEMA_VERSION = 1
CUT_MAP_KIND = "video_factory.cut_map"
CUT_MAP_FILE_NAME = "cut-map.json"

SOURCE_BOUNDARY = "boundary_plan"
SOURCE_AUTO_EDITOR = "auto_editor_v3_timeline"

POSITION_LEADING = "leading"
POSITION_INTERNAL = "internal"
POSITION_TRAILING = "trailing"

_AAC_FRAME_SAMPLES = 1024
_DEFAULT_FRAME_RATE = 25.0
_DEFAULT_SAMPLE_RATE = 48000


def tolerance_seconds(video: dict | None, audio: dict | None) -> float:
    """How far a rendered clip may differ from the intervals that made it."""
    frame_rate = (video or {}).get("frame_rate")
    if not isinstance(frame_rate, (int, float)) or not 1.0 <= frame_rate <= 240.0:
        frame_rate = _DEFAULT_FRAME_RATE
    sample_rate = (audio or {}).get("sample_rate") or _DEFAULT_SAMPLE_RATE
    return round(1.0 / frame_rate + 2.0 * _AAC_FRAME_SAMPLES / sample_rate, 4)


def unavailable(reason: str) -> dict:
    return {"available": False, "reason": reason}


def _removed(retained: list[dict], source_duration: float) -> list[dict]:
    """The complement of the retained intervals within the source."""
    removed: list[dict] = []
    cursor = 0.0

    for index, interval in enumerate(retained):
        if interval["source_start"] - cursor > 1e-6:
            removed.append(
                {
                    "source_start": round(cursor, 6),
                    "source_end": interval["source_start"],
                    "position": POSITION_LEADING if index == 0 else POSITION_INTERNAL,
                }
            )
        cursor = interval["source_end"]

    if source_duration - cursor > 1e-6:
        removed.append(
            {
                "source_start": round(cursor, 6),
                "source_end": round(source_duration, 6),
                "position": POSITION_TRAILING,
            }
        )

    return removed


def _finish(
    origin: str,
    retained: list[dict],
    source_duration: float,
    measured: float,
    video: dict | None,
    audio: dict | None,
) -> dict:
    nominal = round(sum(i["output_end"] - i["output_start"] for i in retained), 6)
    tolerance = tolerance_seconds(video, audio)
    difference = round(measured - nominal, 6)

    entry = {
        "available": abs(difference) <= tolerance,
        "origin": origin,
        "retained": retained,
        "removed": _removed(retained, source_duration),
        "nominal_output_seconds": nominal,
        "measured_output_seconds": round(measured, 6),
        "difference_seconds": difference,
        "tolerance_seconds": tolerance,
    }
    if not entry["available"]:
        entry["reason"] = (
            "The rendered clip is %.3f s long but its intervals add up to %.3f s, "
            "which is outside the %.3f s tolerance. The intervals are recorded "
            "but must not be relied on." % (measured, nominal, tolerance)
        )
    return entry


def boundary_entry(
    boundary: dict, measured: float, video: dict | None, audio: dict | None
) -> dict:
    """The map of a boundary-only clip: one interval, the one that was rendered."""
    start = boundary["start_seconds"]
    end = boundary["end_seconds"]
    retained = [
        {
            "source_start": start,
            "source_end": end,
            "output_start": 0.0,
            "output_end": round(end - start, 6),
        }
    ]
    return _finish(
        SOURCE_BOUNDARY, retained, boundary["source_duration_seconds"], measured, video, audio
    )


def parse_timebase(raw: Any) -> float | None:
    """Auto-Editor writes the timeline rate as "25/1"."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return float(raw)
    if not isinstance(raw, str):
        return None
    numerator, _, denominator = raw.partition("/")
    try:
        top = float(numerator)
        bottom = float(denominator) if denominator else 1.0
    except ValueError:
        return None
    return top / bottom if top > 0 and bottom > 0 else None


def full_clip_entry(
    timeline: Any,
    source_duration: float,
    measured: float,
    video: dict | None,
    audio: dict | None,
) -> dict:
    """The map of a full-clip run, from Auto-Editor's exported v3 timeline."""
    if not isinstance(timeline, dict):
        return unavailable("Auto-Editor's timeline export could not be read.")

    rate = parse_timebase(timeline.get("timebase"))
    tracks = timeline.get("v")
    if rate is None or not isinstance(tracks, list) or len(tracks) != 1:
        return unavailable(
            "Auto-Editor's timeline export is not in the expected single-track shape."
        )

    retained: list[dict] = []
    cursor = 0

    for clip in tracks[0] if isinstance(tracks[0], list) else []:
        if not isinstance(clip, dict):
            return unavailable("Auto-Editor's timeline export holds an unreadable clip.")

        start, length, offset = clip.get("start"), clip.get("dur"), clip.get("offset")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (start, length, offset)):
            return unavailable("Auto-Editor's timeline export holds an unreadable clip.")
        if clip.get("speed", 1) not in (1, 1.0):
            return unavailable(
                "The timeline changes playback speed, which this map cannot express."
            )
        if start != cursor or length <= 0:
            return unavailable(
                "Auto-Editor's timeline export has gaps or overlaps between its clips."
            )

        retained.append(
            {
                "source_start": round(offset / rate, 6),
                "source_end": round((offset + length) / rate, 6),
                "output_start": round(start / rate, 6),
                "output_end": round((start + length) / rate, 6),
            }
        )
        cursor = start + length

    if not retained:
        return unavailable("Auto-Editor's timeline export holds no clips.")

    entry = _finish(SOURCE_AUTO_EDITOR, retained, source_duration, measured, video, audio)
    entry["timeline_rate"] = rate
    return entry


# --- the run-level document --------------------------------------------------


def _sequence_spans(manifest: dict, included: list[dict]) -> tuple[dict, dict]:
    """Where each clip sits in the joined sequence, and a description of it."""
    combined = manifest.get("combined")
    rendered = isinstance(combined, dict) and combined.get("status") == cutting.CLIP_SUCCEEDED
    segments = (combined or {}).get("segments") if rendered else None

    spans: dict = {}
    if isinstance(segments, list):
        # What the join actually used, including any black tail it dropped.
        for segment in segments:
            spans[segment["output_id"]] = (
                segment["start_seconds"],
                round(segment["start_seconds"] + segment["duration_seconds"], 6),
            )
    else:
        # No joined file (or one from before segments were recorded): the
        # positions the clips would take if concatenated in run order.
        cursor = 0.0
        for clip in included:
            length = clip.get("duration_seconds") or 0.0
            spans[clip["output_id"]] = (round(cursor, 6), round(cursor + length, 6))
            cursor += length

    nominal = round(max((end for _start, end in spans.values()), default=0.0), 6)
    tolerance = round(
        sum(tolerance_seconds(clip.get("video"), clip.get("audio")) for clip in included), 4
    )

    sequence: dict = {
        "rendered": rendered,
        "output_id": combined.get("output_id") if rendered else None,
        "strategy": combined.get("strategy") if rendered else None,
        "clip_count": len(spans),
        "nominal_duration_seconds": nominal,
        "measured_duration_seconds": None,
        "tolerance_seconds": tolerance,
        "within_tolerance": None,
        "positions": (
            "measured from the joined file's own segments"
            if isinstance(segments, list)
            else "nominal: the clips concatenated in run order"
        ),
    }

    if rendered and combined.get("duration_seconds") is not None:
        measured = combined["duration_seconds"]
        sequence["measured_duration_seconds"] = measured
        sequence["within_tolerance"] = abs(measured - nominal) <= max(tolerance, 0.05)

    return spans, sequence


def build(manifest: dict) -> dict:
    """The cut map of a run, derived entirely from its manifest."""
    mode = cutting.job_mode(manifest)
    succeeded = [
        clip for clip in manifest.get("clips") or []
        if clip.get("status") == cutting.CLIP_SUCCEEDED
    ]
    succeeded.sort(key=lambda clip: clip.get("order") or 0)

    sources = {source["source_id"]: source for source in manifest.get("sources") or []}
    spans, sequence = _sequence_spans(manifest, succeeded)

    clips: list[dict] = []
    for clip in manifest.get("clips") or []:
        source = sources.get(clip.get("source_id"), {})
        clip_map = clip.get("cut_map")
        if not isinstance(clip_map, dict):
            clip_map = unavailable(
                "This clip was not produced, so there is nothing to map."
                if clip.get("status") != cutting.CLIP_SUCCEEDED
                else "No cut map was recorded for this clip."
            )

        span = spans.get(clip.get("output_id"))
        entry = {
            "output_id": clip.get("output_id"),
            "status": clip.get("status"),
            "order": clip.get("order"),
            "source_id": clip.get("source_id"),
            "source_filename": clip.get("source_filename"),
            "source_fingerprint": source.get("fingerprint"),
            "source_duration_seconds": clip.get("source_duration_seconds"),
            "output_filename": clip.get("filename"),
            "output_duration_seconds": clip.get("duration_seconds"),
            "sequence_index": (
                list(spans).index(clip["output_id"]) if span is not None else None
            ),
            "sequence_start_seconds": span[0] if span is not None else None,
            "sequence_end_seconds": span[1] if span is not None else None,
            **clip_map,
        }
        if mode == cutting.CUT_MODE_BOUNDARY and isinstance(clip.get("boundary"), dict):
            boundary = clip["boundary"]
            entry["boundary"] = {
                key: boundary.get(key)
                for key in (
                    "start_origin", "end_origin", "activity_start_seconds",
                    "activity_end_seconds", "leading_padding_seconds",
                    "trailing_padding_seconds", "override", "needs_review",
                )
            }
        clips.append(entry)

    available = bool(succeeded) and all(
        isinstance(clip.get("cut_map"), dict) and clip["cut_map"].get("available")
        for clip in succeeded
    )

    return {
        "schema_version": CUT_MAP_SCHEMA_VERSION,
        "kind": CUT_MAP_KIND,
        "project_id": manifest.get("project_id"),
        "run_id": manifest.get("run_id"),
        "run_status": manifest.get("status"),
        "created_at": cutting.now(),
        "available": available,
        "mode": mode,
        "settings": (
            manifest.get("boundary_settings")
            if mode == cutting.CUT_MODE_BOUNDARY
            else manifest.get("settings")
        ),
        "settings_revision": manifest.get("settings_revision"),
        "applied_plan": manifest.get("applied_plan"),
        "plan": manifest.get("plan"),
        "config_fingerprint": manifest.get("config_fingerprint"),
        "timebase": {
            "unit": "seconds",
            "intervals": "half-open [start, end)",
            "origin": "the first frame of the file each time refers to",
        },
        "sequence": sequence,
        "clips": clips,
    }


def summary(document: dict) -> dict:
    """What the manifest keeps about its map: enough to say if it can be used."""
    clips = document["clips"]
    mapped = [clip for clip in clips if clip.get("available")]
    described = {
        "available": document["available"],
        "schema_version": document["schema_version"],
        "relative_path": CUT_MAP_FILE_NAME,
        "mapped_clip_count": len(mapped),
        "clip_count": len(clips),
        "sequence_rendered": document["sequence"]["rendered"],
    }
    if not document["available"]:
        reasons = [
            "%s: %s" % (clip.get("source_filename"), clip.get("reason"))
            for clip in clips
            if not clip.get("available") and clip.get("reason")
        ]
        described["reason"] = " ".join(reasons) or "No clip of this run has a cut map."
    return described


def path(project_id: str, run_id: str) -> Path:
    return cutting.run_directory(project_id, run_id) / CUT_MAP_FILE_NAME


def write(manifest: dict) -> dict:
    """Build the map, write it beside the manifest and note it in the manifest.

    The caller persists the manifest. A map that cannot be written is recorded
    as unavailable instead of failing a run whose video is fine.
    """
    document = build(manifest)
    try:
        storage.write_json_atomic(path(manifest["project_id"], manifest["run_id"]), document)
    except OSError as error:
        manifest["cut_map"] = unavailable("The cut map could not be written: %s" % error)
    else:
        manifest["cut_map"] = summary(document)
    return manifest["cut_map"]


def read(project_id: str, run_id: str) -> dict:
    project_id = storage.validate_project_id(project_id)
    run_id = cutting.validate_run_id(run_id)

    try:
        document = storage.read_json(path(project_id, run_id))
    except FileNotFoundError as error:
        raise cutting.RunNotFound("This run has no cut map.") from error
    except (OSError, ValueError) as error:
        raise cutting.CuttingError(
            "The cut map cannot be read: %s" % error, status_code=422
        ) from error

    if not isinstance(document, dict):
        raise cutting.CuttingError("The cut map file is corrupt.", status_code=422)

    version = document.get("schema_version")
    if isinstance(version, int) and version > CUT_MAP_SCHEMA_VERSION:
        raise cutting.CuttingError(
            "The cut map was written by a newer version (%d) and is not supported (%d)."
            % (version, CUT_MAP_SCHEMA_VERSION),
            status_code=422,
        )
    return document
