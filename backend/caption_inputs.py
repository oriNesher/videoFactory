"""What can be captioned, and what exactly it is.

A captioning input is one video, named by a key the backend can resolve —
never by a path from the browser:

    cut.<run-id>.<output-id>   a verified output of a cutting run (a trimmed
                               clip, or the merged video)
    source.<source-id>         a video in the project's footage list
    import.<import-id>         a finished video added only for captioning

Imports live in `project.settings["captions"]["imports"]`, apart from the
footage list on purpose: a video edited elsewhere should be captioned, not
offered to the cutting module as raw footage.

Everything derived from an input — transcripts, caption revisions, exports —
records the input's key *and* its fingerprint (size plus the first and last
mebibyte, `media.fingerprint`). A different fingerprint under the same key
means the video changed; the old captions are then reported stale and are
never applied to it.

This module also decides the composition's frame rate and dimensions; see
`composition_metadata`.
"""

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import cutting, folders, media, storage

SETTINGS_KEY = "captions"

KIND_CUT = "cut"
KIND_SOURCE = "source"
KIND_IMPORT = "import"

KIND_LABELS = {
    KIND_CUT: "Cutting output",
    KIND_SOURCE: "Project footage",
    KIND_IMPORT: "Imported for captions",
}

_KEY_PATTERN = re.compile(
    r"^(?:cut\.[0-9a-f]{12,32}\.(?:combined|clip-[0-9]{4})"
    r"|source\.[0-9a-f]{32}"
    r"|import\.[0-9a-f]{32})$"
)

MAX_IMPORTS = 200

# Frame rates a composition snaps to when the source's rate is not exactly
# constant. Phone recordings are usually variable-frame-rate and report an
# average such as 29.87; rendering at that exact number would produce an odd
# constant rate for no benefit.
STANDARD_FRAME_RATES = (
    24000 / 1001, 24.0, 25.0, 30000 / 1001, 30.0, 48.0, 50.0, 60000 / 1001, 60.0,
)
SNAP_TOLERANCE = 0.03
MIN_FPS = 1.0
MAX_FPS = 120.0
FALLBACK_FPS = 30.0
# Above this the relative difference between the stream's nominal and average
# rates marks the source as variable-frame-rate.
VFR_TOLERANCE = 0.002


class CaptionInputError(storage.ProjectError):
    """A captioning input that cannot be used, phrased for the user."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- keys ----------------------------------------------------------------------


def validate_key(raw: Any) -> str:
    if not isinstance(raw, str) or not _KEY_PATTERN.match(raw):
        raise CaptionInputError("Unknown video for captioning.", status_code=404)
    return raw


def parse_key(key: str) -> tuple[str, list[str]]:
    kind, *rest = validate_key(key).split(".")
    return kind, rest


# --- imports ---------------------------------------------------------------------


def _captions_block(project: dict) -> dict:
    block = project.get("settings", {}).get(SETTINGS_KEY)
    return block if isinstance(block, dict) else {}


def read_imports(project: dict) -> list[dict]:
    imports = _captions_block(project).get("imports")
    if not isinstance(imports, list):
        return []
    return [
        entry
        for entry in imports
        if isinstance(entry, dict)
        and isinstance(entry.get("id"), str)
        and isinstance(entry.get("path"), str)
    ]


def update_captions_block(project_id: str, changes: dict) -> dict:
    """Merge `changes` into `settings["captions"]` and save the project."""
    project = storage.read_project(project_id)
    block = dict(_captions_block(project))
    block.update(changes)
    project["settings"][SETTINGS_KEY] = block
    return storage.write_project(project)


def add_import(project_id: str, raw_path: Any) -> dict:
    """Add a video for captioning only. `None` opens the machine's file dialog."""
    if raw_path is None:
        chosen = folders.choose_video_file()
        if chosen["cancelled"]:
            return {"cancelled": True, "input_key": None}
        raw_path = chosen["path"]

    path = storage.validate_source_path(raw_path)
    project = storage.read_project(project_id)
    imports = read_imports(project)

    for entry in imports:
        if os.path.normcase(entry["path"]) == os.path.normcase(path):
            return {"cancelled": False, "input_key": "import.%s" % entry["id"], "duplicate": True}
    if len(imports) >= MAX_IMPORTS:
        raise CaptionInputError("Up to %d videos can be imported for captions." % MAX_IMPORTS)

    try:
        described = media.probe(path)
    except media.MediaError as error:
        raise CaptionInputError(error.message) from error
    if not described["has_video"]:
        raise CaptionInputError('"%s" has no video track.' % os.path.basename(path))

    entry = {"id": uuid.uuid4().hex, "path": path, "added_at": _now()}
    update_captions_block(project_id, {"imports": [*imports, entry]})
    return {"cancelled": False, "input_key": "import.%s" % entry["id"], "duplicate": False}


# --- resolving -------------------------------------------------------------------


def resolve(project_id: str, key: Any) -> dict:
    """Turn a key into `{key, kind, label, path, ...}`, or refuse.

    Paths come only from the project file or from a cutting run's own
    manifest (resolved and confined to the run directory by `cutting`).
    """
    kind, parts = parse_key(key)
    project = storage.read_project(project_id)

    if kind == KIND_CUT:
        run_id, output_id = parts
        try:
            path, entry = cutting.resolve_output(project_id, run_id, output_id)
            manifest = cutting.read_manifest(project_id, run_id)
        except storage.ProjectError as error:
            raise CaptionInputError(
                "That cutting output is no longer available: %s" % error.message, status_code=404
            ) from error
        if manifest.get("status") != cutting.RUN_SUCCEEDED:
            raise CaptionInputError(
                "That cutting run did not finish successfully, so its outputs are "
                "not offered for captions.",
                status_code=404,
            )
        name = (
            "Merged video" if output_id == cutting.COMBINED_OUTPUT_ID
            else "Clip %s" % (entry.get("source_filename") or entry.get("filename") or output_id)
        )
        return {
            "key": key,
            "kind": kind,
            "label": "%s — cut run %s" % (name, run_id[:6]),
            "filename": entry.get("filename") or path.name,
            "path": str(path),
            "run_id": run_id,
            "output_id": output_id,
            "created_at": manifest.get("finished_at") or manifest.get("created_at"),
        }

    entries = project["sources"] if kind == KIND_SOURCE else read_imports(project)
    entry = next((item for item in entries if item["id"] == parts[0]), None)
    if entry is None:
        raise CaptionInputError("That video is not in this project any more.", status_code=404)
    return {
        "key": key,
        "kind": kind,
        "label": os.path.basename(entry["path"]),
        "filename": os.path.basename(entry["path"]),
        "path": entry["path"],
        "run_id": None,
        "output_id": None,
        "created_at": entry.get("added_at"),
    }


def list_inputs(project_id: str) -> list[dict]:
    """Every video that can be captioned, newest cutting outputs first.

    Cheap: no probing, no hashing. Details come from `describe`.
    """
    project = storage.read_project(project_id)
    inputs: list[dict] = []

    for manifest in cutting.list_runs(project_id):
        if manifest.get("status") != cutting.RUN_SUCCEEDED:
            continue
        run_id = manifest["run_id"]
        combined = manifest.get("combined")
        if isinstance(combined, dict) and combined.get("status") == cutting.CLIP_SUCCEEDED:
            inputs.append(_listing("cut.%s.%s" % (run_id, cutting.COMBINED_OUTPUT_ID), project_id))
        for clip in manifest.get("clips") or []:
            if clip.get("status") == cutting.CLIP_SUCCEEDED and clip.get("output_id"):
                inputs.append(_listing("cut.%s.%s" % (run_id, clip["output_id"]), project_id))

    for source in project["sources"]:
        inputs.append(_listing("source.%s" % source["id"], project_id))
    for entry in read_imports(project):
        inputs.append(_listing("import.%s" % entry["id"], project_id))

    return [entry for entry in inputs if entry is not None]


def _listing(key: str, project_id: str) -> dict | None:
    try:
        resolved = resolve(project_id, key)
    except storage.ProjectError:
        return None
    return {
        "key": key,
        "kind": resolved["kind"],
        "kind_label": KIND_LABELS[resolved["kind"]],
        "label": resolved["label"],
        "filename": resolved["filename"],
        "available": os.path.isfile(resolved["path"]),
        "created_at": resolved["created_at"],
    }


# --- fingerprints and metadata -------------------------------------------------

# Keyed by (path, size, mtime): reopening the screen must not re-hash or
# re-probe every video, and a re-recorded file is noticed because its size or
# modification time changes.
_fingerprint_cache: dict[tuple, dict] = {}
_metadata_cache: dict[tuple, dict] = {}


def _stat_key(path: str) -> tuple:
    stat = os.stat(path)
    return (os.path.normcase(path), stat.st_size, stat.st_mtime_ns)


def fingerprint(path: str) -> dict:
    """`{"method", "digest", "size_bytes"}` — no mtime, so a copy matches."""
    try:
        cache_key = _stat_key(path)
    except OSError as error:
        raise CaptionInputError("The video cannot be read: %s" % error) from error
    if cache_key not in _fingerprint_cache:
        try:
            full = media.fingerprint(path)
        except media.MediaError as error:
            raise CaptionInputError(error.message) from error
        _fingerprint_cache[cache_key] = {
            "method": full["method"],
            "digest": full["digest"],
            "size_bytes": full["size_bytes"],
        }
    return dict(_fingerprint_cache[cache_key])


def _rate(value: Any) -> float | None:
    if not isinstance(value, str) or "/" not in value:
        return None
    top, _, bottom = value.partition("/")
    try:
        numerator, denominator = float(top), float(bottom)
    except ValueError:
        return None
    if denominator == 0 or numerator <= 0:
        return None
    return numerator / denominator


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list") or []:
        if isinstance(side, dict) and "rotation" in side:
            try:
                return int(round(float(side["rotation"])))
            except (TypeError, ValueError):
                pass
    try:
        return int((stream.get("tags") or {}).get("rotate", 0))
    except (TypeError, ValueError):
        return 0


def choose_frame_rate(avg: float | None, nominal: float | None) -> dict:
    """The composition's frame rate, and why.

    - A constant-rate source (nominal and average agree within 0.2 %) keeps
      its exact rate, including NTSC fractions such as 30000/1001.
    - A variable-rate source whose nominal rate is a standard one, with an
      average within 3 % of it, uses the nominal rate (a "30 fps" phone clip
      averaging 29.87 renders at 30).
    - Otherwise the average is used, snapped to the nearest standard rate if
      within 3 %, else rounded to a whole number.
    - Nothing sane at all falls back to 30 fps.

    The render samples the source by timestamp at that constant rate, so a
    variable-rate source stays in sync with its audio; a frame of the source
    may be shown twice or skipped where its own timing was uneven.
    """
    sane = lambda value: value is not None and MIN_FPS <= value <= MAX_FPS  # noqa: E731

    if sane(avg) and sane(nominal) and abs(avg - nominal) <= VFR_TOLERANCE * nominal:
        return {"fps": round(nominal, 6), "variable": False, "method": "constant"}

    # A phone that records "30 fps" with uneven timing reports r=30 and an
    # average just below; the intended rate is the nominal one.
    if sane(avg) and sane(nominal):
        standard = min(STANDARD_FRAME_RATES, key=lambda rate: abs(rate - nominal))
        if abs(standard - nominal) <= VFR_TOLERANCE * standard and abs(avg - nominal) <= SNAP_TOLERANCE * nominal:
            return {"fps": round(standard, 6), "variable": True, "method": "nominal"}

    base = avg if sane(avg) else nominal if sane(nominal) else None
    if base is None:
        return {"fps": FALLBACK_FPS, "variable": True, "method": "fallback"}

    nearest = min(STANDARD_FRAME_RATES, key=lambda rate: abs(rate - base))
    if abs(nearest - base) <= SNAP_TOLERANCE * nearest:
        return {"fps": round(nearest, 6), "variable": True, "method": "snapped"}
    return {"fps": float(max(MIN_FPS, min(MAX_FPS, round(base)))), "variable": True, "method": "rounded"}


def probe_metadata(path: str) -> dict:
    """What the composition needs: display size, frame rate, duration, audio."""
    try:
        cache_key = _stat_key(path)
    except OSError as error:
        raise CaptionInputError("The video file is missing: %s" % path) from error
    if cache_key in _metadata_cache:
        return dict(_metadata_cache[cache_key])

    try:
        raw = media._ffprobe(  # noqa: SLF001 - the shared FFprobe runner
            path,
            [
                "-show_entries",
                "stream=index,codec_type,codec_name,width,height,avg_frame_rate,"
                "r_frame_rate,pix_fmt,sample_rate,channels:stream_side_data=rotation"
                ":stream_tags=rotate",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
            ],
        )
        data = json.loads(raw)
    except media.MediaError as error:
        raise CaptionInputError(error.message) from error
    except ValueError as error:
        raise CaptionInputError("FFprobe returned an unreadable answer for this video.") from error

    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise CaptionInputError("This file has no video track.")

    try:
        duration = float((data.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        duration = 0.0
    if not duration or duration != duration or duration <= 0:
        raise CaptionInputError("The duration of this video cannot be read; it may be damaged.")

    coded_width, coded_height = int(video.get("width") or 0), int(video.get("height") or 0)
    if coded_width <= 0 or coded_height <= 0:
        raise CaptionInputError("The picture size of this video cannot be read.")

    rotation = _rotation(video)
    width, height = coded_width, coded_height
    if abs(rotation) % 180 == 90:
        # Players (and Remotion) show the picture rotated, so the frame is too.
        width, height = height, width

    rate = choose_frame_rate(_rate(video.get("avg_frame_rate")), _rate(video.get("r_frame_rate")))

    metadata = {
        "duration_seconds": round(duration, 6),
        "display_width": width,
        "display_height": height,
        "coded_width": coded_width,
        "coded_height": coded_height,
        "rotation": rotation,
        "video_codec": video.get("codec_name") or "",
        "avg_frame_rate": video.get("avg_frame_rate"),
        "r_frame_rate": video.get("r_frame_rate"),
        "fps": rate["fps"],
        "fps_method": rate["method"],
        "variable_frame_rate": rate["variable"],
        "has_audio": audio is not None,
        "audio_codec": (audio or {}).get("codec_name"),
        "audio_sample_rate": int((audio or {}).get("sample_rate") or 0) or None,
    }
    _metadata_cache[cache_key] = metadata
    return dict(metadata)


def composition_metadata(metadata: dict) -> dict:
    """Explicit composition dimensions, frame rate and length for one video.

    H.264 in 4:2:0 needs even dimensions; an odd side gets one extra pixel,
    which the letterboxed video leaves black. Nothing is cropped or scaled
    non-uniformly.
    """
    width = metadata["display_width"] + metadata["display_width"] % 2
    height = metadata["display_height"] + metadata["display_height"] % 2
    fps = metadata["fps"]
    frames = max(1, round(metadata["duration_seconds"] * fps))
    notes = []
    if (width, height) != (metadata["display_width"], metadata["display_height"]):
        notes.append(
            "The video is %d×%d; H.264 needs even dimensions, so the export is %d×%d "
            "with a one-pixel black edge." % (metadata["display_width"], metadata["display_height"], width, height)
        )
    if metadata["variable_frame_rate"]:
        notes.append(
            "The video has a variable frame rate (average %s). It is previewed and "
            "exported at a constant %s fps; audio stays in sync, and a source "
            "frame may be repeated or skipped where its timing was uneven."
            % (metadata.get("avg_frame_rate"), _fps_label(fps))
        )
    return {
        "width": width,
        "height": height,
        "fps": fps,
        "duration_in_frames": frames,
        "notes": notes,
    }


def _fps_label(fps: float) -> str:
    return ("%.3f" % fps).rstrip("0").rstrip(".")


def describe(project_id: str, key: Any) -> dict:
    """The full picture of one input: where it is, what it is, its fingerprint."""
    resolved = resolve(project_id, key)
    path = resolved["path"]
    described = {k: v for k, v in resolved.items() if k != "path"}
    described["kind_label"] = KIND_LABELS[resolved["kind"]]
    described["available"] = os.path.isfile(path)
    described["metadata"] = None
    described["composition"] = None
    described["fingerprint"] = None
    described["error"] = None

    if not described["available"]:
        described["error"] = "The video file is missing: %s" % os.path.basename(path)
        return described
    try:
        metadata = probe_metadata(path)
        described["metadata"] = metadata
        described["composition"] = composition_metadata(metadata)
        described["fingerprint"] = fingerprint(path)
    except storage.ProjectError as error:
        described["error"] = error.message
    return described


def resolve_for_job(project_id: str, key: Any) -> dict:
    """Everything a job needs, frozen now: path, fingerprint, metadata."""
    resolved = resolve(project_id, key)
    if not os.path.isfile(resolved["path"]):
        raise CaptionInputError("The video file is missing: %s" % resolved["filename"])
    metadata = probe_metadata(resolved["path"])
    return {
        **resolved,
        "fingerprint": fingerprint(resolved["path"]),
        "metadata": metadata,
    }


def resolve_stream_path(project_id: str, key: Any) -> Path:
    resolved = resolve(project_id, key)
    path = Path(resolved["path"])
    if not path.is_file():
        raise CaptionInputError("The video file is missing.", status_code=404)
    return path
