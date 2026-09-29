"""Executing one cutting run: clip by clip, then the join.

Runs inside the existing job queue (`backend/jobs.py`), on the worker thread,
so the HTTP server stays responsive while FFmpeg and Auto-Editor work.

The order of business is the legacy BAT's — trim every take, then join the
trimmed clips — with the guarantees the BAT did not offer:

- every run owns a fresh directory, so nothing earlier is ever overwritten;
- the manifest is written before the first clip and updated after every step,
  so a crash, a cancellation or a power cut still leaves a readable account;
- cancellation kills the encoder tree and stops the remaining clips *and* the
  join, and the run is recorded as cancelled rather than finished;
- nothing is called a result until FFprobe has read it back.
"""

import os
import time
from pathlib import Path

from . import cutting, jobs, media, processes

# Progress is persisted to disk, and the tools emit updates far faster than a
# person can read them. One write per interval keeps the interface live without
# turning a render into a stream of tiny file writes.
PROGRESS_INTERVAL_SECONDS = 0.4

# Tolerance when checking that a joined file is as long as its parts. Container
# rounding and a trailing partial audio frame can legitimately differ by a few
# hundredths of a second per clip; anything beyond this means the join lied.
JOIN_DURATION_TOLERANCE_SECONDS = 0.75
JOIN_DURATION_TOLERANCE_RATIO = 0.02


class _Throttle:
    """Let a progress callback through at most once per interval."""

    def __init__(self, interval: float = PROGRESS_INTERVAL_SECONDS) -> None:
        self._interval = interval
        self._last = 0.0

    def ready(self) -> bool:
        moment = time.monotonic()
        if moment - self._last < self._interval:
            return False
        self._last = moment
        return True


def _write_log(directory: Path, name: str, command: list[str], output: str) -> str:
    """Keep the tool's own words. Returns the path relative to the run."""
    path = directory / cutting.LOGS_DIRECTORY / name
    path.parent.mkdir(parents=True, exist_ok=True)

    body = "$ %s\n\n%s" % (processes.describe_command(command), output)
    try:
        path.write_text(body, encoding="utf-8", errors="replace", newline="\n")
    except OSError:
        return ""

    return str(path.relative_to(directory)).replace("\\", "/")


def _empty_clip_explanation(source: dict, settings: dict) -> str:
    """Say why a clip came back empty, with a number where one is available.

    "Lower the threshold and try again" is useless advice when the take peaks
    at 1.9% of full scale and the threshold is 4% — the user cannot tell how
    much lower. The measured peak turns that into a specific value to try.
    """
    filename = source["filename"]
    level = source.get("audio_level") or {}
    peak_ratio = level.get("peak_ratio")
    peak_db = level.get("max_db")
    threshold = settings["audio_threshold"]

    base = (
        'Nothing was left of "%s" after cutting: with these settings the whole '
        "file counted as silence." % filename
    )

    suggestion = media.suggested_threshold(level)

    if suggestion is not None and peak_ratio is not None and peak_db is not None:
        return (
            "%s The take averages %.1f dB and peaks at %.3f (%.1f dB), against an "
            "audio threshold of %s. Try a threshold of about %s."
            % (base, level["mean_db"], peak_ratio, peak_db, threshold, suggestion)
        )

    if suggestion is not None:
        return (
            "%s Try an audio threshold of about %s, or lower "
            '"minimum speech to keep" (now %ss).'
            % (base, suggestion, settings["min_speech_seconds"])
        )

    return (
        '%s Lower the audio threshold (now %s) or "minimum speech to keep" '
        "(now %ss) and try again."
        % (base, threshold, settings["min_speech_seconds"])
    )


def _finish(manifest: dict, status: str, error: str | None = None) -> dict:
    manifest["status"] = status
    manifest["finished_at"] = cutting.now()
    manifest["error"] = error
    cutting.write_manifest(manifest)
    return manifest


def run_cutting(context: jobs.JobContext, job_input: dict | None = None) -> dict:
    """Execute one cutting run.

    `job_input` defaults to the job's own input snapshot. A plan action passes
    its own, already-validated snapshot instead, so both entry points run
    exactly the same pipeline.
    """
    project_id = context.project_id
    job_input = context.input if job_input is None else job_input

    sources = job_input.get("sources") or []
    settings = job_input["settings"]
    output_mode = job_input["output_mode"]
    wants_combined = output_mode in (cutting.MODE_COMBINED, cutting.MODE_BOTH)

    if not sources:
        raise jobs.JobFailed("No files were selected for cutting.")

    run_id = cutting.new_run_id()
    directory = cutting.run_directory(project_id, run_id)

    try:
        (directory / cutting.CLIPS_DIRECTORY).mkdir(parents=True, exist_ok=False)
    except OSError as error:
        raise jobs.JobFailed("The run directory could not be created: %s" % error) from error

    # Before anything is rendered: would the output paths even fit?
    try:
        cutting.check_path_budget(directory, sources)
    except cutting.CuttingError as error:
        raise jobs.JobFailed(error.message) from error

    context.progress("Preparing a new run and checking tool versions…", percent=0.0)

    manifest = cutting.new_manifest(
        project_id, run_id, context.job_id, job_input, cutting.collect_tool_versions()
    )
    cutting.write_manifest(manifest)

    total_units = len(sources) + (1 if wants_combined else 0)

    def report(unit: int, fraction: float, message: str) -> None:
        context.progress(
            message, percent=100.0 * (unit + max(0.0, min(1.0, fraction))) / total_units
        )

    # --- trim each source, in the submitted order ---------------------------

    for index, source in enumerate(sources):
        try:
            context.raise_if_cancelled()
        except jobs.JobCancelled:
            _finish(manifest, cutting.RUN_CANCELLED, "The run was cancelled at your request.")
            raise

        number = "%04d" % (index + 1)
        output_name = cutting.clip_file_name(index + 1, source["filename"])
        output_path = directory / cutting.CLIPS_DIRECTORY / output_name
        output_id = "clip-%s" % number

        clip: dict = {
            "output_id": output_id,
            "source_id": source["source_id"],
            "order": index + 1,
            "source_filename": source["filename"],
            "source_duration_seconds": source["duration_seconds"],
            "filename": output_name,
            "relative_path": "%s/%s" % (cutting.CLIPS_DIRECTORY, output_name),
            "status": cutting.CLIP_FAILED,
            "duration_seconds": None,
            "size_bytes": None,
            "video": None,
            "audio": None,
            "removed_seconds": None,
            "error": None,
            "log_path": None,
            "started_at": cutting.now(),
            "finished_at": None,
        }

        command = cutting.build_cut_command(source["path"], str(output_path), settings)
        clip["command"] = processes.describe_command(command)

        report(
            index,
            0.0,
            "Cutting clip %d of %d: %s" % (index + 1, len(sources), source["filename"]),
        )

        throttle = _Throttle()

        def on_output(
            chunk: str,
            _index: int = index,
            _source: dict = source,
            _throttle: _Throttle = throttle,
        ) -> None:
            fraction = cutting.parse_machine_progress(chunk)
            if fraction is None or not _throttle.ready():
                return
            report(
                _index,
                fraction,
                "Cutting clip %d of %d: %s"
                % (_index + 1, len(sources), _source["filename"]),
            )

        try:
            result = processes.run(
                command, cancelled=lambda: context.cancelled, on_output=on_output
            )
        except processes.ProcessCancelled:
            clip["status"] = cutting.CLIP_CANCELLED
            clip["error"] = "The run was cancelled while this clip was being processed."
            clip["finished_at"] = cutting.now()
            # A half-written file is not a result. Removing it here is safe: it
            # was created by this run, inside this run's own directory.
            _discard(output_path)
            manifest["clips"].append(clip)
            _mark_remaining_skipped(manifest, sources, index + 1)
            _finish(manifest, cutting.RUN_CANCELLED, "The run was cancelled at your request.")
            raise jobs.JobCancelled() from None
        except processes.ProcessStartFailed as error:
            clip["error"] = error.message
            clip["finished_at"] = cutting.now()
            manifest["clips"].append(clip)
            _mark_remaining_skipped(manifest, sources, index + 1)
            _finish(manifest, cutting.RUN_FAILED, error.message)
            raise jobs.JobFailed(error.message) from error

        clip["log_path"] = _write_log(
            directory, "clip-%s.log" % number, command, result.output
        )
        clip["exit_code"] = result.exit_code
        clip["finished_at"] = cutting.now()

        # Everything cut away is a real outcome, not a crash: Auto-Editor says
        # so and exits non-zero, and the clip has to be named and explained
        # rather than quietly missing from the joined video later.
        if cutting.is_empty_timeline(result.output):
            clip["status"] = cutting.CLIP_EMPTY
            clip["duration_seconds"] = 0.0
            clip["removed_seconds"] = source["duration_seconds"]
            clip["error"] = _empty_clip_explanation(source, settings)
            _discard(output_path)
            manifest["clips"].append(clip)
            manifest["notes"].append(clip["error"])
            cutting.write_manifest(manifest)
            continue

        if not result.ok:
            message = (
                'Auto-Editor failed on "%s" (exit code %d).\n%s'
                % (source["filename"], result.exit_code, result.last_lines())
            )
            clip["error"] = message
            manifest["clips"].append(clip)
            _mark_remaining_skipped(manifest, sources, index + 1)
            _finish(manifest, cutting.RUN_FAILED, message)
            raise jobs.JobFailed(message)

        # A zero exit code is not proof. Read the file back.
        try:
            described = media.verify_output(str(output_path), require_audio=True)
        except media.MediaError as error:
            message = 'The output of "%s" is not valid: %s' % (
                source["filename"],
                error.message,
            )
            clip["error"] = message
            manifest["clips"].append(clip)
            _mark_remaining_skipped(manifest, sources, index + 1)
            _finish(manifest, cutting.RUN_FAILED, message)
            raise jobs.JobFailed(message) from error

        clip["status"] = cutting.CLIP_SUCCEEDED
        clip["duration_seconds"] = round(described["duration_seconds"], 3)
        clip["size_bytes"] = os.path.getsize(output_path)
        clip["video"] = described["video"]
        clip["audio"] = described["audio"]
        source_duration = source["duration_seconds"] or 0.0
        clip["removed_seconds"] = round(
            max(source_duration - described["duration_seconds"], 0.0), 3
        )

        manifest["clips"].append(clip)
        cutting.write_manifest(manifest)

    produced = [
        clip for clip in manifest["clips"] if clip["status"] == cutting.CLIP_SUCCEEDED
    ]

    if not produced:
        # Repeat the per-clip reasoning at run level rather than a vaguer
        # version of it: if one take explains the outcome, that is the message
        # worth showing.
        empty = [c for c in manifest["clips"] if c["status"] == cutting.CLIP_EMPTY]
        if len(empty) == 1 and empty[0].get("error"):
            message = empty[0]["error"]
        else:
            message = (
                "No clip was produced: with these settings every selected file "
                "counted as pure silence. %s"
                % " ".join(
                    clip["error"] for clip in empty if clip.get("error")
                )
            ).strip()

        _finish(manifest, cutting.RUN_FAILED, message)
        raise jobs.JobFailed(message)

    # --- join ---------------------------------------------------------------

    if wants_combined:
        try:
            context.raise_if_cancelled()
        except jobs.JobCancelled:
            _finish(manifest, cutting.RUN_CANCELLED, "The run was cancelled at your request.")
            raise

        try:
            _combine(context, manifest, directory, produced, len(sources), total_units)
        except jobs.JobCancelled:
            _finish(manifest, cutting.RUN_CANCELLED, "The run was cancelled at your request.")
            raise
        except jobs.JobFailed as error:
            _finish(manifest, cutting.RUN_FAILED, error.message)
            raise

    _finish(manifest, cutting.RUN_SUCCEEDED)

    empty = [clip for clip in manifest["clips"] if clip["status"] == cutting.CLIP_EMPTY]
    combined = manifest.get("combined") or {}

    return {
        "run_id": run_id,
        "output_mode": manifest["output_mode"],
        "clip_count": len(produced),
        "empty_clip_count": len(empty),
        "combined_output_id": combined.get("output_id"),
        "combined_strategy": combined.get("strategy"),
        "combined_complete": combined.get("complete"),
        "summary": _summarise(manifest, produced, empty, combined),
    }


def merge_run(context: jobs.JobContext, run_id: str) -> dict:
    """Merge the clips of a run that was cut without a merged video.

    Called from the subtitle job, so a run cut before merging became automatic
    gets its merged video in the background, just before it is transcribed.
    The same join as a cut run's own, on the clips already on disk, in their
    run order. The run's own status is left alone: its cut succeeded or failed
    on its own terms, and a failed merge is recorded on the merged-video entry
    instead, where the interface shows it. Returns the merged-video entry.
    """
    project_id = context.project_id

    try:
        manifest = cutting.read_manifest(project_id, run_id)
    except cutting.CuttingError as error:
        raise jobs.JobFailed(error.message) from error

    if cutting.has_merged_video(manifest):
        return manifest["combined"]

    directory = cutting.run_directory(project_id, run_id)
    produced = sorted(
        (
            clip
            for clip in manifest.get("clips") or []
            if clip.get("status") == cutting.CLIP_SUCCEEDED
            and (directory / (clip.get("relative_path") or "")).is_file()
        ),
        key=lambda clip: clip.get("order") or 0,
    )
    if not produced:
        raise jobs.JobFailed("None of this run's clips are on disk any more.")

    # Recorded as both from now on, so the merged video counts as a project
    # output alongside the clips.
    if manifest.get("output_mode") == cutting.MODE_CLIPS:
        manifest["output_mode"] = cutting.MODE_BOTH

    try:
        _combine(context, manifest, directory, produced, 0, 1)
    except jobs.JobCancelled:
        combined = manifest.get("combined") or {}
        combined["status"] = cutting.CLIP_CANCELLED
        combined["error"] = "Merging was cancelled at your request."
        manifest["combined"] = combined
        cutting.write_manifest(manifest)
        raise

    return manifest["combined"]


def _summarise(manifest: dict, produced: list, empty: list, combined: dict) -> str:
    parts = ["Produced %d trimmed clips from %d sources." % (len(produced), len(manifest["sources"]))]

    if empty:
        parts.append("%d files yielded no content and were left out." % len(empty))

    if combined.get("status") == cutting.CLIP_SUCCEEDED:
        parts.append(
            "Created a combined video %s long."
            % _duration_text(combined.get("duration_seconds"))
        )
        if combined.get("complete") is False:
            parts.append("Note: the combined video leaves out the files that yielded no content.")

    return " ".join(parts)


def _duration_text(seconds: float | None) -> str:
    if not seconds:
        return "—"
    minutes, remainder = divmod(int(seconds), 60)
    return "%d:%02d" % (minutes, remainder)


def _discard(path: Path) -> None:
    """Remove a file this run created and then decided was not a result."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _mark_remaining_skipped(manifest: dict, sources: list, start: int) -> None:
    """Record the clips that never started, so the manifest stays complete."""
    for index in range(start, len(sources)):
        source = sources[index]
        manifest["clips"].append(
            {
                "output_id": "clip-%04d" % (index + 1),
                "source_id": source["source_id"],
                "order": index + 1,
                "source_filename": source["filename"],
                "source_duration_seconds": source["duration_seconds"],
                "filename": None,
                "relative_path": None,
                "status": cutting.CLIP_SKIPPED,
                "duration_seconds": None,
                "size_bytes": None,
                "video": None,
                "audio": None,
                "removed_seconds": None,
                "error": "The run stopped before this clip was processed.",
                "log_path": None,
                "started_at": None,
                "finished_at": None,
            }
        )


def _combine(
    context: jobs.JobContext,
    manifest: dict,
    directory: Path,
    produced: list[dict],
    unit: int,
    total_units: int,
) -> None:
    """Join the produced clips, in the submitted order."""
    excluded = [
        {"output_id": clip["output_id"], "source_filename": clip["source_filename"]}
        for clip in manifest["clips"]
        if clip["status"] == cutting.CLIP_EMPTY
    ]

    output_path = directory / cutting.COMBINED_DIRECTORY / cutting.COMBINED_FILE_NAME
    output_path.parent.mkdir(parents=True, exist_ok=True)

    combined: dict = {
        "output_id": cutting.COMBINED_OUTPUT_ID,
        "requested": True,
        "filename": cutting.COMBINED_FILE_NAME,
        "relative_path": "%s/%s" % (cutting.COMBINED_DIRECTORY, cutting.COMBINED_FILE_NAME),
        "status": cutting.CLIP_FAILED,
        "strategy": None,
        "clip_output_ids": [clip["output_id"] for clip in produced],
        # False when clips were legitimately produced but left out, so the
        # interface can never present this as "all of your footage, joined".
        "complete": not excluded,
        "excluded_clips": excluded,
        "duration_seconds": None,
        "expected_duration_seconds": round(
            sum(clip["duration_seconds"] or 0.0 for clip in produced), 3
        ),
        "size_bytes": None,
        "error": None,
        "log_path": None,
    }
    manifest["combined"] = combined
    cutting.write_manifest(manifest)

    context.progress("Preparing to join %d clips…" % len(produced))

    # Dimensions first. FFmpeg will stream-copy clips of different sizes, exit
    # zero and hand back a file whose duration looks right and whose picture is
    # broken — so this is checked rather than attempted.
    geometries = {media.video_geometry(clip): clip for clip in produced}
    if len(geometries) > 1:
        listing = "; ".join(
            "%s → %s" % (clip["source_filename"], media.geometry_label(clip))
            for clip in produced
        )
        message = (
            "Clips with different dimensions cannot be joined without stretching or "
            "distorting the picture, and this milestone does not resize. The clips: "
            "%s. Run again with files at the same resolution and orientation, or "
            "choose separate-clips output only."
            % listing
        )
        combined["status"] = cutting.CLIP_FAILED
        combined["strategy"] = "unsupported_mixed_dimensions"
        combined["error"] = message
        cutting.write_manifest(manifest)
        raise jobs.JobFailed(message)

    clip_paths = [str(directory / clip["relative_path"]) for clip in produced]

    # Black frames Auto-Editor left at the end of a clip would show as a blip
    # at every join. Cutting them needs the re-encoding join; a stream copy
    # cannot drop them (see media.trailing_black_start).
    context.progress("Checking the clips' endings…")
    ends = [
        media.trailing_black_start(path, clip.get("duration_seconds") or 0.0)
        for path, clip in zip(clip_paths, produced)
    ]
    trimmed = sum(
        (clip.get("duration_seconds") or 0.0) - end
        for end, clip in zip(ends, produced)
        if end is not None
    )
    combined["trimmed_black_seconds"] = round(trimmed, 3)
    combined["expected_duration_seconds"] = round(
        combined["expected_duration_seconds"] - trimmed, 3
    )
    expected = combined["expected_duration_seconds"]

    signatures = {media.stream_signature(clip) for clip in produced}
    strategies: list[tuple[str, list[str]]] = []

    if len(signatures) == 1 and not trimmed:
        list_path = directory / cutting.CONCAT_LIST_FILE
        list_path.write_text(
            "".join(cutting.concat_list_entry(path) for path in clip_paths),
            encoding="utf-8",
            newline="\n",
        )
        strategies.append(
            (
                cutting.COMBINE_STREAM_COPY,
                cutting.build_concat_copy_command(str(list_path), str(output_path)),
            )
        )

    frame_rate = max(
        ((clip.get("video") or {}).get("frame_rate") or 0.0) for clip in produced
    )
    strategies.append(
        (
            cutting.COMBINE_REENCODE,
            cutting.build_concat_filter_command(
                clip_paths, str(output_path), frame_rate, ends
            ),
        )
    )

    last_error = ""

    for attempt, (strategy, command) in enumerate(strategies):
        context.raise_if_cancelled()

        label = (
            "Joining the clips without re-encoding…"
            if strategy == cutting.COMBINE_STREAM_COPY
            else "Joining the clips with re-encoding (same quality, slower)…"
        )
        context.progress(
            label, percent=100.0 * unit / total_units
        )

        throttle = _Throttle()

        def on_output(
            chunk: str, _label: str = label, _throttle: _Throttle = throttle
        ) -> None:
            fraction = cutting.parse_ffmpeg_progress(chunk, expected)
            if fraction is None or not _throttle.ready():
                return
            context.progress(
                _label, percent=100.0 * (unit + fraction) / total_units
            )

        try:
            result = processes.run(
                command, cancelled=lambda: context.cancelled, on_output=on_output
            )
        except processes.ProcessCancelled:
            combined["status"] = cutting.CLIP_CANCELLED
            combined["error"] = "Joining was cancelled at your request."
            _discard(output_path)
            cutting.write_manifest(manifest)
            raise jobs.JobCancelled() from None
        except processes.ProcessStartFailed as error:
            combined["error"] = error.message
            cutting.write_manifest(manifest)
            raise jobs.JobFailed(error.message) from error

        combined["log_path"] = _write_log(
            directory, "combined-%s.log" % strategy, command, result.output
        )

        problem = None
        if not result.ok:
            problem = "FFmpeg returned exit code %d.\n%s" % (
                result.exit_code,
                result.last_lines(),
            )
        else:
            try:
                described = media.verify_output(str(output_path), require_audio=True)
            except media.MediaError as error:
                problem = error.message
            else:
                actual = described["duration_seconds"]
                tolerance = JOIN_DURATION_TOLERANCE_SECONDS + (
                    expected * JOIN_DURATION_TOLERANCE_RATIO
                )
                if abs(actual - expected) > tolerance:
                    # The join "succeeded" and produced the wrong thing. Seen
                    # in practice with the concat demuxer and mismatched
                    # sample rates; the re-encoding strategy handles it.
                    problem = (
                        "The combined file's duration (%.2f s) does not match the "
                        "sum of the clips (%.2f s)." % (actual, expected)
                    )
                else:
                    combined["status"] = cutting.CLIP_SUCCEEDED
                    combined["strategy"] = strategy
                    combined["duration_seconds"] = round(actual, 3)
                    combined["size_bytes"] = os.path.getsize(output_path)
                    combined["video"] = described["video"]
                    combined["audio"] = described["audio"]
                    combined["error"] = None
                    cutting.write_manifest(manifest)
                    return

        last_error = problem or ""
        _discard(output_path)

        if attempt < len(strategies) - 1:
            manifest["notes"].append(
                "Joining without re-encoding failed (%s); falling back to re-encoding." % last_error
            )
            cutting.write_manifest(manifest)

    message = "Joining the clips failed. %s" % last_error
    combined["status"] = cutting.CLIP_FAILED
    combined["error"] = message
    cutting.write_manifest(manifest)
    raise jobs.JobFailed(message)
