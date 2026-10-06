"""Zooms (stage 2B): plans, renders, approvals and media lineage on disk.

Layout, under the project directory:

    intermediates/zooms/
      plans/<plan-id>.json         immutable; one accepted zoom plan
      plans/rejected-<id>.json     a model answer that failed validation (audit)
      runs/<run-id>/
        manifest.json              status, input snapshot, plan id, verification
        filter.txt                 the FFmpeg filter graph that was run
        zoomed.mp4                 (".partial.mp4" while rendering)
        logs/ffmpeg.log
        approval.json              written once, when the result is approved

**Inputs are clean video.** A zoom input is a cutting output, project footage
or an import — never a zoom output (that would zoom twice) and never a
captioned export (captions would be zoomed with the picture).

**Lineage.** Zooming changes framing only: every frame keeps its timestamp
and the audio is copied. A zoom output therefore records its *timing parent*
— the input's key and fingerprint — and the captions module reuses the
parent's transcripts and caption timings for it instead of transcribing the
zoomed file again. If the parent's bytes change, the lineage no longer holds
and nothing is reused.

**Reuse.** A plan is identified by a cache key over everything that shaped
it (input bytes, transcript revision, provider, model, prompt version,
presets, preference); a render by a key over the input bytes, the plan id and
the renderer. Asking again with the same keys returns the existing result
instead of calling Gemini or FFmpeg.
"""

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import caption_inputs, captions, cutting, media, storage, zoom_planner

SETTINGS_KEY = "zooms"

RUN_SCHEMA_VERSION = 1
PLAN_DOCUMENT_KIND = "video_factory.zoom_plan"
RUN_DOCUMENT_KIND = "video_factory.zoom_run"

RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_INTERRUPTED = "interrupted"  # derived: running, but its job has stopped

OUTPUT_FILE_NAME = "zoomed.mp4"
PARTIAL_FILE_NAME = ".partial.mp4"
FILTER_FILE_NAME = "filter.txt"

# What the renderer actually does — reported in the interface as-is.
RENDERER = {
    "version": "center-crop-overlay-v1",
    "method": (
        "Centre crop scaled back to the full frame (Lanczos), switched instantly at "
        "the planned times. No animation, no face detection or tracking."
    ),
    "origin": "legacy/ZoomRenderer/render_zoom_plan.py (crop and scale), adapted",
}
ENCODER = {"video_codec": "libx264", "preset": "medium", "crf": 18, "pixel_format": "yuv420p"}

ALLOWED_INPUT_KINDS = (caption_inputs.KIND_CUT, caption_inputs.KIND_SOURCE, caption_inputs.KIND_IMPORT)

_ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")

# FFprobe tolerance for the output duration: one frame plus AAC padding.
AAC_PADDING_TOLERANCE_SECONDS = 0.05


class ZoomError(storage.ProjectError):
    """A zoom problem the user can act on."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _hash(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_id(raw: Any, what: str = "Zoom run") -> str:
    if not isinstance(raw, str) or not _ID_PATTERN.match(raw):
        raise ZoomError("%s not found." % what, status_code=404)
    return raw


# --- locations ------------------------------------------------------------------


def zooms_root(project_id: str) -> Path:
    return storage.project_directory(project_id) / "intermediates" / "zooms"


def plans_directory(project_id: str) -> Path:
    return zooms_root(project_id) / "plans"


def runs_directory(project_id: str) -> Path:
    return zooms_root(project_id) / "runs"


def run_directory(project_id: str, run_id: str) -> Path:
    return runs_directory(project_id) / validate_id(run_id)


# --- settings ---------------------------------------------------------------------


def read_settings(project: dict) -> dict:
    block = project.get("settings", {}).get(SETTINGS_KEY)
    block = block if isinstance(block, dict) else {}
    preference = block.get("preference")
    selected = block.get("selected_input")
    return {
        "preference": preference if preference in zoom_planner.PREFERENCES else zoom_planner.DEFAULT_PREFERENCE,
        "selected_input": selected if isinstance(selected, str) else None,
    }


def save_settings(project_id: str, *, preference: Any = None, selected_input: Any = None) -> dict:
    project = storage.read_project(project_id)
    current = read_settings(project)
    if preference is not None:
        if preference not in zoom_planner.PREFERENCES:
            raise ZoomError("Unknown zoom preference. Choose one of: %s." % ", ".join(zoom_planner.PREFERENCES))
        current["preference"] = preference
    if selected_input is not None:
        validate_input_key(project_id, selected_input)
        current["selected_input"] = selected_input
    project["settings"][SETTINGS_KEY] = current
    storage.write_project(project)
    return current


# --- inputs ----------------------------------------------------------------------------


def validate_input_key(project_id: str, key: Any) -> dict:
    """Resolve a zoom input, refusing anything that is not clean video."""
    kind, _ = caption_inputs.parse_key(key)
    if kind not in ALLOWED_INPUT_KINDS:
        raise ZoomError(
            "A zoomed video cannot be zoomed again. Choose the clean video it was made from."
        )
    return caption_inputs.resolve(project_id, key)


def list_inputs(project_id: str) -> list[dict]:
    return [
        entry for entry in caption_inputs.list_inputs(project_id) if entry["kind"] in ALLOWED_INPUT_KINDS
    ]


def default_input(project_id: str) -> str | None:
    """The newest merged cut video, else the newest cut output, else the first
    available video, else nothing."""
    entries = [entry for entry in list_inputs(project_id) if entry["available"]]
    for entry in entries:
        if entry["key"].endswith("." + cutting.COMBINED_OUTPUT_ID):
            return entry["key"]
    for entry in entries:
        if entry["kind"] == caption_inputs.KIND_CUT:
            return entry["key"]
    return entries[0]["key"] if entries else None


def clip_boundaries(project_id: str, key: str) -> list[dict] | None:
    """Where takes were joined, in the merged video's own timebase.

    Read from the cutting run's manifest (`combined.segments`, which already
    accounts for black tails cut at the joins). None for anything that is not
    a merged cut output.
    """
    kind, parts = caption_inputs.parse_key(key)
    if kind != caption_inputs.KIND_CUT or parts[1] != cutting.COMBINED_OUTPUT_ID:
        return None
    try:
        manifest = cutting.read_manifest(project_id, parts[0])
    except storage.ProjectError:
        return None
    segments = (manifest.get("combined") or {}).get("segments") or []
    clips = []
    for index, segment in enumerate(segments, start=1):
        try:
            start = float(segment["start_seconds"])
            end = start + float(segment["duration_seconds"])
        except (KeyError, TypeError, ValueError):
            return None
        clips.append({"take": index, "start": round(start, 3), "end": round(end, 3)})
    return clips or None


# --- transcripts -----------------------------------------------------------------


def find_transcript(project_id: str, key: str, digest: str) -> dict | None:
    """The newest transcript of exactly these bytes, with its latest revision.

    The latest caption revision is used rather than the raw transcript, so
    corrections the user made in the captions editor reach the planner.
    """
    for transcript in captions.list_transcripts(project_id):
        if transcript["input"]["key"] != key or transcript["input"]["fingerprint"]["digest"] != digest:
            continue
        revision = captions.latest_revision(project_id, transcript["id"])
        if revision is None:
            continue
        return {"transcript": transcript, "revision": revision}
    return None


def current_caption_overlay(project_id: str, key: str, digest: str) -> dict | None:
    """The captions that belong on a zoom output made from `key` at `digest`.

    The same structured captions the Captions section edits, drawn over the
    zoomed picture by the same Remotion composition — never burned in first.
    """
    found = find_transcript(project_id, key, digest)
    if found is None:
        return None
    transcript, revision = found["transcript"], found["revision"]
    duration = transcript["input"]["metadata"]["duration_seconds"]
    normalised, errors = captions.normalise_captions(revision["captions"], duration)
    if errors or not normalised:
        return None
    style = captions.read_style(storage.read_project(project_id))
    return {
        "transcript_id": transcript["id"],
        "revision": revision["revision"],
        "style_revision": style["style_revision"],
        "style": style["style"],
        "language": transcript["result"]["language"],
        "direction": captions.direction_for(transcript["result"]["language"]),
        "captions": normalised,
    }


# --- plans -------------------------------------------------------------------------


def plan_cache_key(*, input_digest: str, transcript_id: str, revision: int, provider: dict, preference: str) -> str:
    return _hash(
        {
            "plan_schema": zoom_planner.PLAN_SCHEMA_VERSION,
            "input": input_digest,
            "transcript": transcript_id,
            "revision": revision,
            "provider": provider["id"],
            "model": provider["model"],
            "prompt": zoom_planner.PROMPT_VERSION,
            "presets": zoom_planner.PRESET_CATALOG_VERSION,
            "preference": preference,
        }
    )


def write_plan(project_id: str, document: dict) -> None:
    path = plans_directory(project_id) / ("%s.json" % document["plan_id"])
    try:
        storage.write_json_exclusive(path, document)
    except OSError as error:
        raise ZoomError("Saving the zoom plan failed: %s" % error, status_code=500) from error


def write_rejected(project_id: str, document: dict) -> str | None:
    path = plans_directory(project_id) / ("rejected-%s.json" % uuid.uuid4().hex[:12])
    try:
        storage.write_json_exclusive(path, document)
    except OSError:
        return None
    return path.name


def read_plan(project_id: str, plan_id: Any) -> dict:
    plan_id = validate_id(plan_id, "Zoom plan")
    try:
        document = storage.read_json(plans_directory(project_id) / ("%s.json" % plan_id))
    except FileNotFoundError as error:
        raise ZoomError("Zoom plan not found.", status_code=404) from error
    except (OSError, ValueError) as error:
        raise ZoomError("The zoom plan cannot be read: %s" % error, status_code=422) from error
    if not isinstance(document, dict) or document.get("kind") != PLAN_DOCUMENT_KIND:
        raise ZoomError("The zoom plan file is not a zoom plan.", status_code=422)
    if (document.get("schema_version") or 0) > zoom_planner.PLAN_SCHEMA_VERSION:
        raise ZoomError("The zoom plan was written by a newer version.", status_code=422)
    return document


def list_plans(project_id: str) -> list[dict]:
    directory = plans_directory(project_id)
    if not directory.is_dir():
        return []
    documents = []
    for path in directory.glob("*.json"):
        if not _ID_PATTERN.match(path.stem):
            continue
        try:
            documents.append(read_plan(project_id, path.stem))
        except storage.ProjectError:
            continue
    documents.sort(key=lambda document: document.get("created_at") or "", reverse=True)
    return documents


def find_plan(project_id: str, cache_key: str) -> dict | None:
    """A previously accepted plan for the same inputs — re-validated, not trusted."""
    for document in list_plans(project_id):
        if document.get("cache_key") != cache_key:
            continue
        if revalidate_plan(document):
            return document
    return None


def revalidate_plan(document: dict) -> bool:
    """A stored plan must still pass today's validation before it is rendered."""
    video = document.get("input") or {}
    plan, errors = zoom_planner.validate_plan(
        {"summary": document.get("summary") or "-", "intervals": [
            {key: interval[key] for key in ("start", "end", "preset")}
            for interval in document.get("intervals") or []
            if isinstance(interval, dict) and all(key in interval for key in ("start", "end", "preset"))
        ]},
        duration_seconds=float(video.get("duration_seconds") or 0),
        fps=float(video.get("fps") or 0) or 30.0,
    )
    return plan is not None and not errors and len(plan["intervals"]) == len(document.get("intervals") or [])


# --- rendering ---------------------------------------------------------------------


def even(value: float) -> int:
    whole = int(round(value))
    return max(2, whole - whole % 2)


def crop_geometry(width: int, height: int, scale: float) -> dict:
    """The centred crop that, scaled back to `width`×`height`, gives `scale`.

    The crop's height is rounded to an even number of pixels and its width
    is derived from that height and the frame's aspect ratio, so the scale
    back is uniform to within one pixel — no visible stretching. The offset
    is centred and even (4:2:0 chroma), so nothing outside the frame is ever
    sampled and no black border can appear.
    """
    if scale <= 1.0:
        return {"width": width, "height": height, "x": 0, "y": 0}
    crop_height = min(height, even(height / scale))
    crop_width = min(width, even(crop_height * width / height))
    x = (width - crop_width) // 2
    y = (height - crop_height) // 2
    return {"width": crop_width, "height": crop_height, "x": x - x % 2, "y": y - y % 2}


def _number(value: float) -> str:
    return ("%.6f" % value).rstrip("0").rstrip(".") or "0"


def build_filter(intervals: list[dict], width: int, height: int, fps: float) -> str:
    """The FFmpeg filter graph for a plan. Plain numbers and preset scales only.

    One branch per zoomed preset in use, each a centre crop scaled back to the
    full frame, overlaid on the untouched frame only while one of that
    preset's intervals is active. Every frame keeps its own timestamp — there
    is no trimming or concatenation — so duration, frame count and audio
    sync are exactly those of the input, including variable-frame-rate video.

    A frame belongs to the interval its timestamp falls in; boundaries are on
    the frame grid and compared half a frame early so floating-point noise in
    a timestamp cannot push a frame across a boundary.
    """
    half = 0.5 / fps
    zoomed = [preset for preset in zoom_planner.PRESET_IDS if zoom_planner.PRESET_SCALE[preset] > 1.0
              and any(interval["preset"] == preset for interval in intervals)]
    pad = ",pad=%d:%d:0:0:black" % (width + width % 2, height + height % 2) if (width % 2 or height % 2) else ""

    if not zoomed:
        return "[0:v]setsar=1,format=yuv420p%s[vout]" % pad

    labels = ["base"] + ["p%d" % index for index in range(len(zoomed))]
    parts = ["[0:v]setsar=1,split=%d%s" % (len(labels), "".join("[%s]" % label for label in labels))]
    current = "base"
    for index, preset in enumerate(zoomed):
        geometry = crop_geometry(width, height, zoom_planner.PRESET_SCALE[preset])
        parts.append(
            "[p%d]crop=%d:%d:%d:%d,scale=%d:%d:flags=lanczos,setsar=1[z%d]"
            % (index, geometry["width"], geometry["height"], geometry["x"], geometry["y"], width, height, index)
        )
        windows = "+".join(
            "gte(t,%s)*lt(t,%s)" % (_number(interval["start"] - half), _number(interval["end"] - half))
            for interval in intervals
            if interval["preset"] == preset
        )
        output = "o%d" % index
        parts.append("[%s][z%d]overlay=0:0:enable='%s'[%s]" % (current, index, windows, output))
        current = output
    parts.append("[%s]format=yuv420p%s[vout]" % (current, pad))
    return ";".join(parts)


def build_render_command(input_path: str, filter_path: str, output_path: str, *, has_audio: bool, audio_codec: str | None) -> list[str]:
    """An argument list, never text. The model contributes nothing here but
    validated numbers inside the filter file.

    Audio is copied when it is already AAC (bit-identical, so sync cannot
    drift); anything else is encoded to AAC once. Exactly one audio stream —
    the original — is mapped.
    """
    command = [
        "ffmpeg.exe", "-y", "-hide_banner", "-nostdin", "-nostats",
        "-progress", "pipe:1",
        "-i", input_path,
        "-/filter_complex", filter_path,
        "-map", "[vout]",
    ]
    if has_audio:
        command += ["-map", "0:a:0"]
        command += ["-c:a", "copy"] if audio_codec == "aac" else ["-c:a", "aac", "-b:a", "192k"]
    command += [
        "-sn", "-dn",
        "-c:v", ENCODER["video_codec"], "-preset", ENCODER["preset"], "-crf", str(ENCODER["crf"]),
        "-pix_fmt", ENCODER["pixel_format"],
        # Keep every frame's own timestamp: no frames dropped or duplicated.
        "-fps_mode", "passthrough",
        "-movflags", "+faststart",
        output_path,
    ]
    return command


def render_cache_key(input_digest: str, plan_id: str) -> str:
    return _hash({"input": input_digest, "plan": plan_id, "renderer": RENDERER["version"], "encoder": ENCODER})


# --- verification -------------------------------------------------------------------


def count_video_frames(path: str) -> int | None:
    try:
        raw = media._ffprobe(  # noqa: SLF001 - the shared FFprobe runner
            path, ["-select_streams", "v:0", "-count_packets", "-show_entries", "stream=nb_read_packets", "-of", "json"]
        )
        streams = json.loads(raw).get("streams") or []
        return int(streams[0]["nb_read_packets"]) if streams else None
    except (media.MediaError, ValueError, KeyError, TypeError):
        return None


def verify_output(path: str, *, metadata: dict, input_frames: int | None) -> dict:
    """FFprobe the render. Raises `media.MediaError` with the reason."""
    described = media.verify_output(path, require_audio=metadata["has_audio"])
    raw = media._ffprobe(  # noqa: SLF001
        path, ["-show_entries", "stream=codec_type,codec_name,width,height", "-of", "json"]
    )
    streams = json.loads(raw).get("streams") or []
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    expected_audio = 1 if metadata["has_audio"] else 0
    if len(video) != 1 or len(audio) != expected_audio:
        raise media.MediaError(
            "The zoomed video has %d video and %d audio streams; 1 and %d were expected."
            % (len(video), len(audio), expected_audio)
        )
    width = metadata["display_width"] + metadata["display_width"] % 2
    height = metadata["display_height"] + metadata["display_height"] % 2
    if (video[0].get("width"), video[0].get("height")) != (width, height):
        raise media.MediaError(
            "The zoomed video is %s×%s, not %d×%d like its input."
            % (video[0].get("width"), video[0].get("height"), width, height)
        )
    fps = metadata["fps"]
    expected = metadata["duration_seconds"]
    tolerance = 1.0 / fps + AAC_PADDING_TOLERANCE_SECONDS
    difference = described["duration_seconds"] - expected
    if abs(difference) > tolerance:
        raise media.MediaError(
            "The zoomed video lasts %.3f s; its input lasts %.3f s (tolerance ±%.3f s)."
            % (described["duration_seconds"], expected, tolerance)
        )
    frames = count_video_frames(path)
    if input_frames is not None and frames is not None and frames != input_frames:
        raise media.MediaError(
            "The zoomed video has %d frames; its input has %d. Timing was not preserved." % (frames, input_frames)
        )
    return {
        "checked_at": now(),
        "duration_seconds": round(described["duration_seconds"], 3),
        "input_duration_seconds": round(expected, 3),
        "difference_seconds": round(difference, 3),
        "tolerance_seconds": round(tolerance, 3),
        "width": width,
        "height": height,
        "video_frames": frames,
        "input_video_frames": input_frames,
        "streams": {"video": 1, "audio": expected_audio},
        "audio_codec": audio[0].get("codec_name") if audio else None,
    }


# --- runs --------------------------------------------------------------------------


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def write_manifest(project_id: str, manifest: dict) -> None:
    try:
        storage.write_json_atomic(run_directory(project_id, manifest["run_id"]) / "manifest.json", manifest)
    except OSError as error:
        raise ZoomError("Saving the zoom run failed: %s" % error, status_code=500) from error


def _job_active(project_id: str, job_id: Any) -> bool:
    from . import jobs

    if not isinstance(job_id, str):
        return False
    try:
        return jobs.read_job(project_id, job_id)["status"] in jobs.ACTIVE_STATUSES
    except storage.ProjectError:
        return False


def read_run(project_id: str, run_id: Any) -> dict:
    directory = run_directory(project_id, run_id)
    try:
        manifest = storage.read_json(directory / "manifest.json")
    except FileNotFoundError as error:
        raise ZoomError("Zoom run not found.", status_code=404) from error
    except (OSError, ValueError) as error:
        raise ZoomError("The zoom run cannot be read: %s" % error, status_code=422) from error
    if not isinstance(manifest, dict) or manifest.get("kind") != RUN_DOCUMENT_KIND:
        raise ZoomError("The zoom run file is not a zoom run.", status_code=422)
    if manifest.get("status") == RUN_RUNNING and not _job_active(project_id, manifest.get("job_id")):
        manifest = {**manifest, "status": RUN_INTERRUPTED, "error": "The backend stopped while this run was rendering."}
    return manifest


def list_runs(project_id: str) -> list[dict]:
    directory = runs_directory(project_id)
    if not directory.is_dir():
        return []
    manifests = []
    for path in directory.iterdir():
        if not _ID_PATTERN.match(path.name):
            continue
        try:
            manifests.append(read_run(project_id, path.name))
        except storage.ProjectError:
            continue
    manifests.sort(key=lambda manifest: manifest.get("created_at") or "", reverse=True)
    return manifests


def output_path(project_id: str, manifest: dict) -> Path:
    """The finished file, confined to the run's own directory."""
    if manifest.get("status") != RUN_SUCCEEDED or not manifest.get("output"):
        raise ZoomError("This zoom run has no finished video.", status_code=404)
    directory = run_directory(project_id, manifest["run_id"]).resolve()
    path = (directory / manifest["output"]["filename"]).resolve()
    if not path.is_relative_to(directory) or not path.is_file():
        raise ZoomError("The zoomed video is no longer in its folder.", status_code=404)
    return path


def output_intact(project_id: str, manifest: dict) -> bool:
    try:
        path = output_path(project_id, manifest)
        return caption_inputs.fingerprint(str(path))["digest"] == manifest["output"]["fingerprint"]["digest"]
    except storage.ProjectError:
        return False


def find_reusable_run(project_id: str, render_key: str) -> dict | None:
    for manifest in list_runs(project_id):
        if manifest.get("render_key") == render_key and manifest.get("status") == RUN_SUCCEEDED and output_intact(project_id, manifest):
            return manifest
    return None


# --- lineage -----------------------------------------------------------------------


def timing_parent(project_id: str, run_id: str) -> dict:
    """`{key, digest}` of the clean video a zoom output shares its timing with.

    Raises when the output is missing or has changed since it was verified,
    because then nothing derived from the parent may be applied to it.
    """
    manifest = read_run(project_id, run_id)
    if manifest.get("status") != RUN_SUCCEEDED:
        raise ZoomError("That zoom run did not finish successfully.", status_code=404)
    if not output_intact(project_id, manifest):
        raise ZoomError("The zoomed video changed or disappeared after it was made.", status_code=409)
    return {
        "key": manifest["input"]["key"],
        "digest": manifest["input"]["fingerprint"]["digest"],
        "label": manifest["input"]["label"],
    }


def input_is_current(project_id: str, manifest: dict) -> tuple[bool, str | None]:
    """Is the run's input still the same bytes under the same key?"""
    try:
        resolved = caption_inputs.resolve(project_id, manifest["input"]["key"])
        if not os.path.isfile(resolved["path"]):
            return False, "The video this was made from is missing."
        digest = caption_inputs.fingerprint(resolved["path"])["digest"]
    except storage.ProjectError as error:
        return False, error.message
    if digest != manifest["input"]["fingerprint"]["digest"]:
        return False, "The video this was made from has changed since; zoom it again."
    return True, None


# --- approval ----------------------------------------------------------------------


def approval_path(project_id: str, run_id: str) -> Path:
    return run_directory(project_id, run_id) / "approval.json"


def read_approval(project_id: str, run_id: str) -> dict | None:
    try:
        document = storage.read_json(approval_path(project_id, run_id))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def overlay_reference(overlay: dict | None) -> dict | None:
    if overlay is None:
        return None
    return {
        "transcript_id": overlay["transcript_id"],
        "revision": overlay["revision"],
        "style_revision": overlay["style_revision"],
    }


def review_state(project_id: str, manifest: dict) -> dict:
    """What the reviewer sees and approves, and whether an approval still holds.

    An approval names the exact output (by fingerprint) and the exact caption
    revision and style revision drawn over it. If any of them, or the input,
    has changed since, the approval is reported as outdated and the result
    must be reviewed again. Nothing is re-rendered for that.
    """
    current, input_reason = input_is_current(project_id, manifest)
    intact = manifest.get("status") == RUN_SUCCEEDED and output_intact(project_id, manifest)
    overlay = None
    if current:
        overlay = current_caption_overlay(
            project_id, manifest["input"]["key"], manifest["input"]["fingerprint"]["digest"]
        )
    approval = read_approval(project_id, manifest["run_id"])
    approval_valid = False
    approval_reason = None
    if approval is not None:
        if not intact:
            approval_reason = "The zoomed video changed or disappeared after it was approved."
        elif not current:
            approval_reason = input_reason
        elif approval.get("output_digest") != manifest["output"]["fingerprint"]["digest"]:
            approval_reason = "The approval was for a different file."
        elif approval.get("captions") != overlay_reference(overlay):
            approval_reason = "The captions or their style changed after approval. Review and approve again."
        else:
            approval_valid = True
    return {
        "reviewable": intact and current,
        "stale": not current,
        "stale_reason": input_reason,
        "output_intact": intact,
        "overlay": overlay,
        "overlay_reference": overlay_reference(overlay),
        "approval": approval,
        "approved": approval_valid,
        "approval_outdated_reason": approval_reason,
    }


def approve(project_id: str, run_id: Any, *, output_digest: Any, captions_reference: Any) -> dict:
    """Approve exactly what was reviewed, or refuse if it is not what is current."""
    manifest = read_run(project_id, run_id)
    state = review_state(project_id, manifest)
    if not state["reviewable"]:
        raise ZoomError(state["stale_reason"] or "This result can no longer be approved.", status_code=409)
    if output_digest != manifest["output"]["fingerprint"]["digest"]:
        raise ZoomError("The result changed while you were reviewing it. Review it again.", status_code=409)
    if captions_reference != state["overlay_reference"]:
        raise ZoomError(
            "The captions shown with the result have changed since it was opened. Review it again.",
            status_code=409,
        )
    document = {
        "run_id": manifest["run_id"],
        "approved_at": now(),
        "output_digest": output_digest,
        "plan_id": manifest["plan_id"],
        "input_key": manifest["input"]["key"],
        "input_digest": manifest["input"]["fingerprint"]["digest"],
        "captions": state["overlay_reference"],
    }
    # One approval per review: a later approval (after captions changed)
    # replaces the outdated one, atomically.
    try:
        storage.write_json_atomic(approval_path(project_id, manifest["run_id"]), document)
    except OSError as error:
        raise ZoomError("Saving the approval failed: %s" % error, status_code=500) from error
    return document
