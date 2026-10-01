"""Preview samples: a few seconds of the parts of a cut that matter.

In boundary-only trimming the only places a cut can be wrong are the first
moments of a clip, the last moments, and the join between two clips. So those
are what a sample shows — never the middle, never a random excerpt:

- **opening** — the first seconds of the edited clip, from the retained start,
  with a second file showing the same region of the *source* plus a few
  seconds before it, so it is audible what was removed;
- **ending** — the last seconds of the edited clip, with the source region and
  a few seconds after it;
- **join** — the end of one edited clip followed by the start of the next, to
  judge the rhythm of the transition.

Samples are rendered by a queued, cancellable job, from the same snapshot and
through the same `cut_runner.resolve_boundary` as the final run, with the same
seek and encoder settings. Each sample records exactly what it was cut from
(`basis`), so a later change to the settings or to a manual boundary can be
detected and the sample shown as stale instead of as current.

    workspace\\projects\\<id>\\intermediates\\cut-samples\\<sample-id>\\
      sample.json     what this is, and what produced it
      edited.mp4      the edited excerpt
      source.mp4      the source region around the cut (opening and ending)
      logs\\

A new sample never replaces an old one: every sample has its own directory.
"""

import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from . import boundaries, cut_runner, cutting, jobs, media, processes, storage

SAMPLE_SCHEMA_VERSION = 1

SAMPLES_DIRECTORY = "cut-samples"
SAMPLE_FILE_NAME = "sample.json"

KIND_OPENING = "opening"
KIND_ENDING = "ending"
KIND_JOIN = "join"
KINDS = (KIND_OPENING, KIND_ENDING, KIND_JOIN)

FILE_EDITED = "edited"
FILE_SOURCE = "source"
FILE_NAMES = {FILE_EDITED: "edited.mp4", FILE_SOURCE: "source.mp4"}

# How much of the source is shown beyond the cut, on the removed side.
SOURCE_CONTEXT_SECONDS = 3.0

MAX_SAMPLES_PER_JOB = 3 * cutting.MAX_SOURCES_PER_RUN

STATUS_SUCCEEDED = "succeeded"

_SAMPLE_ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")


class SampleNotFound(cutting.CuttingError):
    status_code = 404


# --- requests -----------------------------------------------------------------


def _all_requests(source_ids: list[str]) -> list[dict]:
    requests: list[dict] = []
    for index, source_id in enumerate(source_ids):
        requests.append({"kind": KIND_OPENING, "source_id": source_id})
        requests.append({"kind": KIND_ENDING, "source_id": source_id})
        if index + 1 < len(source_ids):
            requests.append(
                {
                    "kind": KIND_JOIN,
                    "source_id": source_id,
                    "next_source_id": source_ids[index + 1],
                }
            )
    return requests


def validate_requests(raw: Any, source_ids: list[str]) -> list[dict]:
    """Which samples to render. `None` means every one the selection allows."""
    if raw is None:
        return _all_requests(source_ids)

    if not isinstance(raw, list) or not raw:
        raise cutting.CuttingError("Choose at least one preview to render.")
    if len(raw) > MAX_SAMPLES_PER_JOB:
        raise cutting.CuttingError(
            "Up to %d previews can be rendered in one job." % MAX_SAMPLES_PER_JOB
        )

    validated: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict) or entry.get("kind") not in KINDS:
            raise cutting.CuttingError(
                "Unknown preview kind. Available kinds: %s." % ", ".join(KINDS)
            )

        kind = entry["kind"]
        source_id = entry.get("source_id")
        if source_id not in source_ids:
            raise cutting.CuttingError("A preview was requested for a clip that is not selected.")

        request = {"kind": kind, "source_id": source_id}

        if kind == KIND_JOIN:
            position = source_ids.index(source_id)
            if position + 1 >= len(source_ids):
                raise cutting.CuttingError(
                    "The last clip has nothing after it to preview a join with."
                )
            following = source_ids[position + 1]
            requested = entry.get("next_source_id")
            if requested is not None and requested != following:
                raise cutting.CuttingError(
                    "A join preview is only available between two clips that are "
                    "next to each other in the current order."
                )
            request["next_source_id"] = following

        if request not in validated:
            validated.append(request)

    return validated


def build_job_input(project_id: str, raw: Any) -> dict:
    """The sample job's snapshot: the cut snapshot plus which samples to make."""
    job_input = cutting.build_job_input(project_id, raw)

    if job_input["mode"] != cutting.CUT_MODE_BOUNDARY:
        raise cutting.CuttingError(
            "Preview samples are available in boundary-only mode. Full-clip "
            "silence removal has no cheap preview: run the cut to hear it."
        )

    job_input["samples"] = validate_requests(raw.get("samples"), job_input["source_ids"])
    return job_input


# --- storage ------------------------------------------------------------------


def samples_root(project_id: str) -> Path:
    return storage.project_directory(project_id) / "intermediates" / SAMPLES_DIRECTORY


def sample_directory(project_id: str, sample_id: str) -> Path:
    return samples_root(project_id) / sample_id


def validate_sample_id(raw: Any) -> str:
    if not isinstance(raw, str) or not _SAMPLE_ID_PATTERN.match(raw):
        raise SampleNotFound("Invalid preview id.")
    return raw


def read_sample(project_id: str, sample_id: str) -> dict:
    project_id = storage.validate_project_id(project_id)
    sample_id = validate_sample_id(sample_id)

    try:
        data = storage.read_json(sample_directory(project_id, sample_id) / SAMPLE_FILE_NAME)
    except FileNotFoundError as error:
        raise SampleNotFound("Preview not found.") from error
    except (OSError, ValueError) as error:
        raise cutting.CuttingError(
            "The preview details cannot be read: %s" % error, status_code=422
        ) from error

    if not isinstance(data, dict):
        raise cutting.CuttingError("The preview file is corrupt.", status_code=422)

    data["sample_id"] = sample_id
    data["project_id"] = project_id
    return data


def list_samples(project_id: str) -> list[dict]:
    """Every finished sample of a project, newest first."""
    project_id = storage.validate_project_id(project_id)
    root = samples_root(project_id)
    if not root.is_dir():
        return []

    samples: list[dict] = []
    for directory in root.iterdir():
        if not directory.is_dir() or not _SAMPLE_ID_PATTERN.match(directory.name):
            continue
        try:
            sample = read_sample(project_id, directory.name)
        except storage.ProjectError:
            continue
        if sample.get("status") == STATUS_SUCCEEDED:
            samples.append(sample)

    samples.sort(key=lambda sample: sample.get("created_at") or "", reverse=True)
    return samples


def resolve_file(project_id: str, sample_id: str, which: str) -> Path:
    """One of a sample's two files, by name. Ids only; the path is built here."""
    sample = read_sample(project_id, sample_id)

    # The manifest key that describes each file: a join has no source excerpt.
    described_by = {FILE_EDITED: "edited", FILE_SOURCE: "source_context"}
    if which not in described_by or not isinstance(sample.get(described_by[which]), dict):
        raise SampleNotFound("That preview file does not exist.")

    directory = sample_directory(project_id, sample_id).resolve()
    path = (directory / FILE_NAMES[which]).resolve()
    if not path.is_relative_to(directory) or not path.is_file():
        raise SampleNotFound("The preview file is no longer on disk.")
    return path


def slot(sample: dict) -> str:
    """What a sample is a preview *of*: the newest one per slot is shown."""
    return ":".join([sample.get("kind", "")] + list(sample.get("source_ids") or []))


# --- staleness ----------------------------------------------------------------


def sample_basis_key(job_input: dict, sources: list[dict]) -> str:
    """The key of what decides these sources' cuts, from a job snapshot."""
    configuration = {
        "mode": cutting.job_mode(job_input),
        "boundary_settings": job_input.get("boundary_settings"),
        "settings": job_input.get("settings"),
        "overrides": {
            source["source_id"]: source.get("override")
            for source in sources
            if source.get("override")
        },
    }
    return cutting.basis_key(
        configuration,
        [(source["source_id"], source["fingerprint"]["digest"]) for source in sources],
    )


def describe_current(
    project_id: str, configuration: dict, digests: dict[str, str | None]
) -> list[dict]:
    """The newest sample of every slot, each marked current or stale.

    `digests` maps every *selected* source id to its file's current digest
    (None when the file cannot be read). A sample is current only if rendering
    it again now would cut at the same places.
    """
    selected = configuration["source_ids"]
    newest: dict[str, dict] = {}
    for sample in list_samples(project_id):
        newest.setdefault(slot(sample), sample)

    described: list[dict] = []
    for sample in newest.values():
        source_ids = list(sample.get("source_ids") or [])
        reason: str | None = None

        if configuration["mode"] != cutting.CUT_MODE_BOUNDARY:
            reason = "The cutting mode changed since this preview was rendered."
        elif any(source_id not in selected for source_id in source_ids):
            reason = "A clip in this preview is no longer selected."
        elif sample.get("kind") == KIND_JOIN and (
            len(source_ids) != 2
            or selected.index(source_ids[0]) + 1 >= len(selected)
            or selected[selected.index(source_ids[0]) + 1] != source_ids[1]
        ):
            reason = "These two clips are no longer next to each other."
        elif any(digests.get(source_id) is None for source_id in source_ids):
            reason = "A source file of this preview cannot be read any more."
        else:
            current = cutting.basis_key(
                configuration, [(source_id, digests[source_id]) for source_id in source_ids]
            )
            if current != sample.get("basis_key"):
                reason = (
                    "The settings, a manual boundary or the source file changed "
                    "since this preview was rendered."
                )

        entry = dict(sample)
        entry["stale"] = reason is not None
        entry["stale_reason"] = reason
        described.append(entry)

    described.sort(key=lambda sample: sample.get("created_at") or "", reverse=True)
    return described


# --- rendering ----------------------------------------------------------------


def _discard_directory(directory: Path) -> None:
    """Remove a sample directory this job created and then abandoned."""
    shutil.rmtree(directory, ignore_errors=True)


def _render(
    context: jobs.JobContext,
    directory: Path,
    name: str,
    command: list[str],
    label: str,
) -> dict:
    """Run one FFmpeg render and read the result back. Raises on any failure."""
    output_path = Path(command[-1])

    try:
        result = processes.run(command, cancelled=lambda: context.cancelled)
    except processes.ProcessStartFailed as error:
        raise jobs.JobFailed(error.message) from error

    log = directory / cutting.LOGS_DIRECTORY / ("%s.log" % name)
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            "$ %s\n\n%s" % (processes.describe_command(command), result.output),
            encoding="utf-8", errors="replace", newline="\n",
        )
    except OSError:
        pass

    if not result.ok:
        raise jobs.JobFailed(
            "FFmpeg failed while rendering the %s (exit code %d).\n%s"
            % (label, result.exit_code, result.last_lines())
        )

    try:
        described = media.verify_output(str(output_path), require_audio=True)
    except media.MediaError as error:
        raise jobs.JobFailed("The %s is not valid: %s" % (label, error.message)) from error

    return {
        "filename": output_path.name,
        "duration_seconds": round(described["duration_seconds"], 3),
        "size_bytes": os.path.getsize(output_path),
        "command": processes.describe_command(command),
    }


def _source_summary(source: dict, boundary: dict) -> dict:
    return {
        "source_id": source["source_id"],
        "filename": source["filename"],
        "digest": source["fingerprint"]["digest"],
        "duration_seconds": source["duration_seconds"],
        "override": source.get("override"),
        "boundary": {
            key: boundary[key]
            for key in (
                "status", "start_seconds", "end_seconds", "start_origin", "end_origin",
                "retained_seconds", "needs_review",
            )
        },
    }


def run_samples(context: jobs.JobContext) -> dict:
    """Render the requested samples, one directory each."""
    project_id = context.project_id
    job_input = context.input
    settings = job_input["boundary_settings"]
    length = float(job_input.get("sample_seconds") or cutting.SAMPLE_SECONDS_SPEC["default"])
    requests = job_input.get("samples") or []
    sources = {source["source_id"]: source for source in job_input.get("sources") or []}

    if not requests:
        raise jobs.JobFailed("No previews were requested.")

    resolved: dict[str, dict] = {}
    analysis_seconds = 0.0

    def boundary_of(source_id: str) -> dict:
        nonlocal analysis_seconds
        if source_id not in resolved:
            started = time.monotonic()
            try:
                resolved[source_id] = cut_runner.resolve_boundary(
                    project_id,
                    sources[source_id],
                    settings,
                    cancelled=lambda: context.cancelled,
                )
            except processes.ProcessCancelled:
                raise jobs.JobCancelled() from None
            except storage.ProjectError as error:
                raise jobs.JobFailed(
                    '"%s": %s' % (sources[source_id]["filename"], error.message)
                ) from error
            analysis_seconds += time.monotonic() - started
        return resolved[source_id]

    produced: list[dict] = []
    render_seconds = 0.0

    for index, request in enumerate(requests):
        context.raise_if_cancelled()

        kind = request["kind"]
        source = sources[request["source_id"]]
        context.progress(
            "Rendering preview %d of %d: the %s of %s"
            % (index + 1, len(requests), kind, source["filename"]),
            percent=100.0 * index / len(requests),
        )

        boundary = boundary_of(source["source_id"])
        start, end = boundary["start_seconds"], boundary["end_seconds"]
        duration = float(source["duration_seconds"])
        involved = [source]
        notes: list[str] = []

        sample_id = uuid.uuid4().hex[:12]
        directory = sample_directory(project_id, sample_id)
        try:
            directory.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            raise jobs.JobFailed("The preview folder could not be created: %s" % error) from error

        edited_path = str(directory / FILE_NAMES[FILE_EDITED])
        source_path = str(directory / FILE_NAMES[FILE_SOURCE])
        has_audio = bool(source.get("audio"))
        source_context: dict | None = None
        started = time.monotonic()

        try:
            if end - start < length:
                notes.append(
                    "The edited clip is %.2f s long, shorter than the %.1f s preview "
                    "length, so the preview shows all of it." % (end - start, length)
                )

            if kind == KIND_OPENING:
                piece = (start, min(start + length, end))
                segments = [_segment(source, piece)]
                edited = _render(
                    context, directory, "edited",
                    cutting.build_trim_command(source["path"], edited_path, *piece, has_audio),
                    "opening preview",
                )
                region = (max(0.0, start - SOURCE_CONTEXT_SECONDS), piece[1])
                source_context = _render(
                    context, directory, "source",
                    cutting.build_trim_command(source["path"], source_path, *region, has_audio),
                    "source comparison",
                )
                source_context.update(
                    source_start_seconds=round(region[0], 6),
                    source_end_seconds=round(region[1], 6),
                    # Where, inside this excerpt, the edited clip begins.
                    cut_at_seconds=round(start - region[0], 3),
                    removed_shown_seconds=round(start - region[0], 3),
                )
                if start <= 0:
                    notes.append(
                        "Nothing is removed from the start of this clip, so the "
                        "source comparison has no earlier material to show."
                    )

            elif kind == KIND_ENDING:
                piece = (max(end - length, start), end)
                segments = [_segment(source, piece)]
                edited = _render(
                    context, directory, "edited",
                    cutting.build_trim_command(source["path"], edited_path, *piece, has_audio),
                    "ending preview",
                )
                region = (piece[0], min(duration, end + SOURCE_CONTEXT_SECONDS))
                source_context = _render(
                    context, directory, "source",
                    cutting.build_trim_command(source["path"], source_path, *region, has_audio),
                    "source comparison",
                )
                source_context.update(
                    source_start_seconds=round(region[0], 6),
                    source_end_seconds=round(region[1], 6),
                    # Where, inside this excerpt, the edited clip ends.
                    cut_at_seconds=round(end - region[0], 3),
                    removed_shown_seconds=round(region[1] - end, 3),
                )
                if end >= duration:
                    notes.append(
                        "Nothing is removed from the end of this clip, so the "
                        "source comparison has no later material to show."
                    )

            else:
                following = sources[request["next_source_id"]]
                following_boundary = boundary_of(following["source_id"])
                involved.append(following)

                if media.video_geometry(source) != media.video_geometry(following):
                    raise jobs.JobFailed(
                        'A join preview needs both clips at the same dimensions: "%s" '
                        'is %s and "%s" is %s.'
                        % (
                            source["filename"], media.geometry_label(source),
                            following["filename"], media.geometry_label(following),
                        )
                    )

                first = (max(end - length, start), end)
                second = (
                    following_boundary["start_seconds"],
                    min(
                        following_boundary["start_seconds"] + length,
                        following_boundary["end_seconds"],
                    ),
                )
                segments = [_segment(source, first), _segment(following, second)]
                frame_rate = max(
                    ((clip.get("video") or {}).get("frame_rate") or 0.0)
                    for clip in (source, following)
                )
                edited = _render(
                    context, directory, "edited",
                    cutting.build_join_sample_command(
                        [
                            {"path": source["path"], "start_seconds": first[0],
                             "end_seconds": first[1], "has_audio": has_audio},
                            {"path": following["path"], "start_seconds": second[0],
                             "end_seconds": second[1],
                             "has_audio": bool(following.get("audio"))},
                        ],
                        edited_path,
                        frame_rate,
                    ),
                    "join preview",
                )
                # Where, inside the preview, the second clip takes over.
                edited["join_at_seconds"] = round(first[1] - first[0], 3)

        except processes.ProcessCancelled:
            # A half-rendered preview is not a preview. This directory was
            # created by this job a moment ago; nothing else lives in it.
            _discard_directory(directory)
            raise jobs.JobCancelled() from None
        except (jobs.JobFailed, jobs.JobCancelled):
            _discard_directory(directory)
            raise

        render_seconds += time.monotonic() - started

        boundaries_used = [boundary] + (
            [resolved[involved[1]["source_id"]]] if len(involved) > 1 else []
        )
        for used, clip in zip(boundaries_used, involved):
            for warning in used["warnings"]:
                if warning["severity"] == boundaries.SEVERITY_WARN:
                    notes.append('"%s": %s' % (clip["filename"], warning["message"]))

        edited["segments"] = segments
        sample = {
            "schema_version": SAMPLE_SCHEMA_VERSION,
            "sample_id": sample_id,
            "project_id": project_id,
            "job_id": context.job_id,
            "created_at": cutting.now(),
            "status": STATUS_SUCCEEDED,
            "kind": kind,
            "source_ids": [clip["source_id"] for clip in involved],
            "sample_seconds": length,
            "sources": [
                _source_summary(clip, used) for clip, used in zip(involved, boundaries_used)
            ],
            # Everything that decided where this sample was cut. `basis_key` is
            # what a later state check compares against.
            "basis": {
                "mode": cutting.CUT_MODE_BOUNDARY,
                "boundary_settings": settings,
                "settings_revision": job_input.get("settings_revision"),
                "applied_plan": job_input.get("applied_plan"),
            },
            "basis_key": sample_basis_key(job_input, involved),
            "edited": edited,
            "source_context": source_context,
            "notes": notes,
        }

        try:
            storage.write_json_atomic(directory / SAMPLE_FILE_NAME, sample)
        except OSError as error:
            _discard_directory(directory)
            raise jobs.JobFailed("Saving the preview details failed: %s" % error) from error

        produced.append(
            {"sample_id": sample_id, "kind": kind, "source_ids": sample["source_ids"]}
        )

    return {
        "samples": produced,
        "sample_count": len(produced),
        "settings_revision": job_input.get("settings_revision"),
        # Reported apart on purpose: finding the boundary and encoding the
        # excerpt are different costs, and only the first shrinks when less
        # audio is analysed.
        "analysis_seconds": round(analysis_seconds, 3),
        "render_seconds": round(render_seconds, 3),
        "summary": "Rendered %d preview%s (analysis %.2f s, rendering %.2f s)."
        % (len(produced), "" if len(produced) == 1 else "s", analysis_seconds, render_seconds),
    }


def _segment(source: dict, piece: tuple) -> dict:
    return {
        "source_id": source["source_id"],
        "source_start_seconds": round(piece[0], 6),
        "source_end_seconds": round(piece[1], 6),
    }
