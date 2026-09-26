"""Audio-driven cutting: settings, command construction, runs and manifests.

This is the working manual version of `legacy/AutoEditor/RUN_EDIT.bat`, which
trimmed each take with Auto-Editor and then joined the trimmed clips with
FFmpeg. What is kept from it: the five cut settings and their defaults, the
per-take-then-join shape, and the stream-copy-first join with a re-encoding
fallback. What is deliberately *not* kept:

- **Its cleanup.** The BAT begins by deleting the whole trimmed-output
  directory. Here every run writes to its own new directory, so a run can never
  destroy an earlier one, and a failed run leaves its partial output in place
  to be looked at rather than silently removed.
- **Its file discovery.** The BAT globbed `takes\\*.mkv` and sorted by filename.
  Here the inputs are project sources chosen by the user, in an order the user
  set, resolved from the project manifest by id. The frontend never names a
  file, a flag or an output path.
- **Its `dir /b` ordering for the join.** Clip order comes from the submitted
  snapshot, not from whatever the filesystem lists.
- **Its trust in exit codes.** Every produced file is probed with FFprobe.

Verified against Auto-Editor 31.3.2 on this machine (see docs/PROGRESS.md):
`--edit audio:threshold=X` is the modern spelling of the BAT's `--edit audio:X`;
`--margin BEFORE,AFTER`; and `--smooth MINCUT,MINCLIP`, where MINCUT is the
shortest silence that will actually be removed and MINCLIP the shortest speech
segment that will be kept.
"""

import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import media, processes, storage

RUN_SCHEMA_VERSION = 1

CUTS_DIRECTORY = "cuts"
MANIFEST_FILE_NAME = "manifest.json"
CLIPS_DIRECTORY = "clips"
COMBINED_DIRECTORY = "combined"
LOGS_DIRECTORY = "logs"
CONCAT_LIST_FILE = "concat-list.txt"

COMBINED_OUTPUT_ID = "combined"
COMBINED_FILE_NAME = "combined.mp4"

# --- run and clip states -----------------------------------------------------

RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"

CLIP_SUCCEEDED = "succeeded"
CLIP_EMPTY = "empty"
CLIP_FAILED = "failed"
CLIP_SKIPPED = "skipped"
CLIP_CANCELLED = "cancelled"

# --- output modes ------------------------------------------------------------

MODE_CLIPS = "clips"
MODE_COMBINED = "combined"
MODE_BOTH = "both"

OUTPUT_MODES = {
    MODE_CLIPS: {
        "id": MODE_CLIPS,
        "label": "Separate clips only",
        "description": "One trimmed MP4 per source, in the order you chose.",
    },
    MODE_COMBINED: {
        "id": MODE_COMBINED,
        "label": "Combined video only",
        "description": (
            "A single MP4 joining every trimmed clip in order. The separate clips are "
            "produced as an intermediate step and kept, but only the combined "
            "file is recorded as a project output."
        ),
    },
    MODE_BOTH: {
        "id": MODE_BOTH,
        "label": "Both separate clips and a combined video",
        "description": "Both kinds of output are recorded as project outputs.",
    },
}

DEFAULT_OUTPUT_MODE = MODE_BOTH

# --- the five cut settings ---------------------------------------------------
#
# Defaults are the BAT's, which is where they earned their place: they are a
# sensible starting point for this user's talking-head takes, not a universal
# recommendation. Ranges are wide enough to be useful and narrow enough that a
# typo cannot produce a nonsense command.

SETTINGS_SPEC: dict[str, dict] = {
    "audio_threshold": {
        "type": "number",
        "default": 0.04,
        "min": 0.0,
        "max": 1.0,
        "step": 0.005,
        "unit": "loudness ratio (0-1)",
        "label": "Audio threshold",
        "description": (
            "How loud audio has to be to count as speech. 0.04 = 4% of peak "
            "loudness. A lower value keeps more audio; a higher value cuts more."
        ),
        "maps_to": "--edit audio:threshold=",
    },
    "margin_before_seconds": {
        "type": "number",
        "default": 0.0,
        "min": 0.0,
        "max": 10.0,
        "step": 0.05,
        "unit": "seconds",
        "label": "Margin before speech",
        "description": (
            "How much to keep before detected speech starts, so the first syllable "
            "is not clipped."
        ),
        "maps_to": "--margin (first value)",
    },
    "margin_after_seconds": {
        "type": "number",
        "default": 0.5,
        "min": 0.0,
        "max": 10.0,
        "step": 0.05,
        "unit": "seconds",
        "label": "Margin after speech",
        "description": (
            "How much to keep after detected speech ends. A generous value here "
            "keeps sentences from feeling chopped off."
        ),
        "maps_to": "--margin (second value)",
    },
    "min_silence_seconds": {
        "type": "number",
        "default": 0.1,
        "min": 0.0,
        "max": 30.0,
        "step": 0.05,
        "unit": "seconds",
        "label": "Minimum silence to cut",
        "description": (
            "Silence shorter than this is never cut. A lower value means more "
            "aggressive cutting, because short pauses go too."
        ),
        "maps_to": "--smooth (MINCUT)",
    },
    "min_speech_seconds": {
        "type": "number",
        "default": 0.6,
        "min": 0.0,
        "max": 30.0,
        "step": 0.05,
        "unit": "seconds",
        "label": "Minimum speech to keep",
        "description": (
            "A speech segment shorter than this is treated as noise and removed. "
            "A higher value means more aggressive cutting."
        ),
        "maps_to": "--smooth (MINCLIP)",
    },
}

SETTINGS_KEY = "cutting"

MAX_SOURCES_PER_RUN = 50

# The combined re-encoding recipe, kept in one place so the manifest, the README
# and the command cannot drift apart.
REENCODE_VIDEO = ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
                  "-pix_fmt", "yuv420p"]
REENCODE_AUDIO = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]

COMBINE_STREAM_COPY = "stream_copy"
COMBINE_REENCODE = "re_encode"

# Run ids are short on purpose. Windows still limits a full path to 260
# characters, and the workspace already nests
# projects/<32-hex>/intermediates/cuts/<run>/clips/<name>; a 32-character run id
# spent a fifth of the budget on nothing. Twelve hex characters is still 48 bits
# — far more than enough to keep one project's runs apart — and the wider
# pattern keeps ids written by an earlier version readable.
RUN_ID_LENGTH = 12
_RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{%d,32}$" % RUN_ID_LENGTH)

# The real ceiling is 260 including the terminating NUL; leaving a little room
# means the check fires before the filesystem does.
WINDOWS_MAX_PATH = 255
_OUTPUT_ID_PATTERN = re.compile(r"^(combined|clip-[0-9]{4})$")

# Auto-Editor's `--progress machine` line: "…~done~total~eta".
_MACHINE_PROGRESS = re.compile(r"~(\d+(?:\.\d+)?)~(\d+(?:\.\d+)?)~")

# FFmpeg's `-progress pipe:1` key/value output.
_FFMPEG_PROGRESS = re.compile(r"out_time_us=(\d+)")

# Auto-Editor's wording when everything was cut away. Matched on purpose: it is
# not a crash, it is a legitimate outcome that has to be reported per clip.
_EMPTY_TIMELINE_MARKERS = ("timeline is empty", "nothing to do")


class CuttingError(storage.ProjectError):
    """A cutting problem the user can act on."""


class RunNotFound(CuttingError):
    status_code = 404


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# Public alias: the run executor stamps manifests with the same clock.
now = _now


# --- settings ---------------------------------------------------------------


def default_settings() -> dict:
    return {name: spec["default"] for name, spec in SETTINGS_SPEC.items()}


def settings_catalog() -> dict:
    """What the interface needs to render the form, straight from the spec."""
    return {
        "defaults": default_settings(),
        "default_output_mode": DEFAULT_OUTPUT_MODE,
        "parameters": [
            {"name": name, **{k: v for k, v in spec.items()}}
            for name, spec in SETTINGS_SPEC.items()
        ],
        "output_modes": [dict(mode) for mode in OUTPUT_MODES.values()],
        "max_sources_per_run": MAX_SOURCES_PER_RUN,
    }


def validate_settings(raw: Any) -> dict:
    """Check the five parameters against the spec. Unknown keys are refused."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise CuttingError("The cutting settings are invalid.")

    for name in raw:
        if name not in SETTINGS_SPEC:
            allowed = ", ".join(SETTINGS_SPEC)
            raise CuttingError(
                'Setting "%s" is not recognised. Available settings: %s.' % (name, allowed)
            )

    validated: dict = {}
    for name, spec in SETTINGS_SPEC.items():
        if name not in raw or raw[name] is None:
            validated[name] = spec["default"]
            continue

        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CuttingError('Setting "%s" must be a number.' % spec["label"])

        value = float(value)
        if value != value:  # NaN
            raise CuttingError('Setting "%s" must be a number.' % spec["label"])
        if value < spec["min"] or value > spec["max"]:
            raise CuttingError(
                'Setting "%s" must be between %s and %s (%s).'
                % (spec["label"], spec["min"], spec["max"], spec["unit"])
            )

        validated[name] = round(value, 4)

    return validated


def validate_output_mode(raw: Any) -> str:
    if raw is None:
        return DEFAULT_OUTPUT_MODE
    if not isinstance(raw, str) or raw not in OUTPUT_MODES:
        allowed = ", ".join(OUTPUT_MODES)
        raise CuttingError("Unsupported output mode. Available modes: %s." % allowed)
    return raw


def read_settings(project: dict) -> dict:
    """The project's saved cutting settings, falling back to the defaults."""
    stored = project.get("settings", {}).get(SETTINGS_KEY)
    if not isinstance(stored, dict):
        return {
            "settings": default_settings(),
            "output_mode": DEFAULT_OUTPUT_MODE,
            "source_ids": [],
        }

    try:
        settings = validate_settings(stored.get("settings"))
        output_mode = validate_output_mode(stored.get("output_mode"))
    except CuttingError:
        # A hand-edited or older settings block must never stop a project from
        # opening: fall back to the defaults and let the user save over them.
        settings, output_mode = default_settings(), DEFAULT_OUTPUT_MODE

    known = {source["id"] for source in project.get("sources", [])}
    raw_ids = stored.get("source_ids")
    source_ids = [
        value
        for value in (raw_ids if isinstance(raw_ids, list) else [])
        if isinstance(value, str) and value in known
    ]

    return {
        "settings": settings,
        "output_mode": output_mode,
        "source_ids": source_ids,
    }


def save_settings(project_id: str, raw: Any) -> dict:
    """Persist the cutting form with the project, validated first."""
    if not isinstance(raw, dict):
        raise CuttingError("The cutting settings are invalid.")

    project = storage.read_project(project_id)
    settings = validate_settings(raw.get("settings"))
    output_mode = validate_output_mode(raw.get("output_mode"))
    source_ids = _validate_source_ids(project, raw.get("source_ids"), allow_empty=True)

    project["settings"][SETTINGS_KEY] = {
        "settings": settings,
        "output_mode": output_mode,
        "source_ids": source_ids,
        "saved_at": _now(),
    }
    saved = storage.write_project(project)
    return read_settings(saved)


# --- input selection --------------------------------------------------------


def _validate_source_ids(
    project: dict, raw: Any, *, allow_empty: bool = False
) -> list[str]:
    """Resolve ids against the project manifest. Paths are never accepted."""
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise CuttingError("The list of files to cut is invalid.")

    known = {source["id"] for source in project.get("sources", [])}
    ordered: list[str] = []

    for value in raw:
        if not isinstance(value, str) or value not in known:
            raise CuttingError(
                "One of the selected files is not in the project. Refresh the page and select again."
            )
        if value in ordered:
            raise CuttingError("The same file was selected more than once.")
        ordered.append(value)

    if not ordered and not allow_empty:
        raise CuttingError("Select at least one file to cut.")
    if len(ordered) > MAX_SOURCES_PER_RUN:
        raise CuttingError(
            "Up to %d files can be cut in one run." % MAX_SOURCES_PER_RUN
        )

    return ordered


def build_job_input(project_id: str, raw: Any) -> dict:
    """Validate a cut request and freeze everything the run will need.

    The result is the job's input snapshot. It holds resolved paths, measured
    durations and file fingerprints taken *now*, so a run keeps processing what
    was submitted even if the project is edited while it waits in the queue,
    and so a retry re-runs the same thing.
    """
    if not isinstance(raw, dict):
        raise CuttingError("The cut request is invalid.")

    project = storage.read_project(project_id)
    source_ids = _validate_source_ids(project, raw.get("source_ids"))
    settings = validate_settings(raw.get("settings"))
    output_mode = validate_output_mode(raw.get("output_mode"))

    by_id = {source["id"]: source for source in project["sources"]}
    snapshot: list[dict] = []

    for index, source_id in enumerate(source_ids):
        path = by_id[source_id]["path"]
        filename = os.path.basename(path)

        if not os.path.isfile(path):
            raise CuttingError(
                'File "%s" is no longer where it was (%s). Put it back or remove it '
                "from the selection." % (filename, path)
            )

        try:
            described = media.describe_input(path, require_audio=True)
            marks = media.fingerprint(path)
        except media.MediaError as error:
            raise CuttingError(error.message) from error

        snapshot.append(
            {
                "source_id": source_id,
                "order": index + 1,
                "path": path,
                "filename": filename,
                "fingerprint": marks,
                "duration_seconds": described["duration_seconds"],
                "video": described["video"],
                "audio": described["audio"],
            }
        )

    return {
        "settings": settings,
        "output_mode": output_mode,
        "source_ids": source_ids,
        "sources": snapshot,
    }


# --- command construction ---------------------------------------------------


def format_seconds(value: float) -> str:
    """Auto-Editor's duration spelling: a plain number with an `s` suffix."""
    text = ("%.3f" % float(value)).rstrip("0").rstrip(".")
    return "%ss" % (text or "0")


def format_threshold(value: float) -> str:
    text = ("%.4f" % float(value)).rstrip("0").rstrip(".")
    return text or "0"


def build_cut_command(input_path: str, output_path: str, settings: dict) -> list[str]:
    """The Auto-Editor invocation for one source. An argument list, never text.

    `--edit audio:threshold=X` is the explicit spelling of the legacy
    `--edit audio:X`; both were checked against 31.3.2 and produce an identical
    timeline. The explicit form is used because it says what the number means.
    """
    return [
        "auto-editor.exe",
        input_path,
        "--edit",
        "audio:threshold=%s" % format_threshold(settings["audio_threshold"]),
        "--margin",
        "%s,%s"
        % (
            format_seconds(settings["margin_before_seconds"]),
            format_seconds(settings["margin_after_seconds"]),
        ),
        "--smooth",
        "%s,%s"
        % (
            format_seconds(settings["min_silence_seconds"]),
            format_seconds(settings["min_speech_seconds"]),
        ),
        # Machine-readable progress instead of the animated bar, so the job can
        # report a percentage that is a real ratio of rendered frames.
        "--progress",
        "machine",
        "--faststart",
        "-o",
        output_path,
    ]


def concat_list_entry(path: str) -> str:
    """One line of an FFmpeg concat list, with the demuxer's own escaping."""
    return "file '%s'\n" % str(path).replace("'", "'\\''")


def build_concat_copy_command(list_path: str, output_path: str) -> list[str]:
    """Join without re-encoding. Only valid when every stream property matches."""
    return [
        "ffmpeg.exe",
        "-y",
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-progress",
        "pipe:1",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_path,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        output_path,
    ]


def build_concat_filter_command(
    clip_paths: list[str], output_path: str, frame_rate: float
) -> list[str]:
    """Join by re-encoding through the concat *filter*.

    Not the concat demuxer: with inputs that differ in frame rate or sample
    rate the demuxer does not resample, and the joined file drifts — measured
    at 17.56s for material that is 14.63s long. The filter normalises frame
    rate and audio format per input first, which is what makes the result
    correct. Dimensions are *not* normalised here on purpose; clips whose
    dimensions differ are refused earlier rather than stretched.
    """
    command = ["ffmpeg.exe", "-y", "-hide_banner", "-nostdin", "-nostats",
               "-progress", "pipe:1"]

    for path in clip_paths:
        command += ["-i", path]

    rate = "%.6g" % frame_rate if frame_rate and frame_rate > 0 else "25"
    parts = []
    for index in range(len(clip_paths)):
        parts.append("[%d:v:0]fps=%s,setsar=1[v%d]" % (index, rate, index))
        parts.append(
            "[%d:a:0]aresample=48000,aformat=sample_fmts=fltp:"
            "channel_layouts=stereo[a%d]" % (index, index)
        )

    streams = "".join("[v%d][a%d]" % (i, i) for i in range(len(clip_paths)))
    parts.append("%sconcat=n=%d:v=1:a=1[v][a]" % (streams, len(clip_paths)))

    command += [
        "-filter_complex",
        ";".join(parts),
        "-map",
        "[v]",
        "-map",
        "[a]",
        *REENCODE_VIDEO,
        *REENCODE_AUDIO,
        "-movflags",
        "+faststart",
        output_path,
    ]
    return command


# --- run directories and manifests ------------------------------------------


def cuts_root(project_id: str) -> Path:
    return storage.project_directory(project_id) / "intermediates" / CUTS_DIRECTORY


def run_directory(project_id: str, run_id: str) -> Path:
    return cuts_root(project_id) / run_id


def validate_run_id(raw: Any) -> str:
    if not isinstance(raw, str) or not _RUN_ID_PATTERN.match(raw):
        raise RunNotFound("Invalid run id.")
    return raw


def validate_output_id(raw: Any) -> str:
    if not isinstance(raw, str) or not _OUTPUT_ID_PATTERN.match(raw):
        raise RunNotFound("Invalid output id.")
    return raw


def safe_stem(filename: str, fallback: str) -> str:
    """A Windows-safe file stem derived from the source name.

    Hebrew, spaces and most punctuation are kept — only what Windows actually
    forbids is replaced, so an output file still looks like the take it came
    from.
    """
    # Separators are split by hand rather than with `os.path.basename`: on
    # Windows that reads a leading "a:" as a drive letter and would silently
    # drop the first character of a name that merely contains a colon.
    tail = re.split(r"[\\/]", str(filename))[-1]
    stem = tail.rsplit(".", 1)[0] if "." in tail[1:] else tail
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")
    stem = stem[:60].strip(" .")
    return stem or fallback


def manifest_path(project_id: str, run_id: str) -> Path:
    return run_directory(project_id, run_id) / MANIFEST_FILE_NAME


def write_manifest(manifest: dict) -> None:
    path = manifest_path(manifest["project_id"], manifest["run_id"])
    try:
        storage.write_json_atomic(path, manifest)
    except OSError as error:
        raise CuttingError(
            "Saving the run details failed: %s" % error, status_code=500
        ) from error


def read_manifest(project_id: str, run_id: str) -> dict:
    project_id = storage.validate_project_id(project_id)
    run_id = validate_run_id(run_id)

    try:
        data = storage.read_json(manifest_path(project_id, run_id))
    except FileNotFoundError as error:
        raise RunNotFound("Run not found.") from error
    except (OSError, ValueError) as error:
        raise CuttingError(
            "The run details cannot be read: %s" % error, status_code=422
        ) from error

    if not isinstance(data, dict):
        raise CuttingError("The run file is corrupt.", status_code=422)

    version = data.get("schema_version")
    if isinstance(version, int) and version > RUN_SCHEMA_VERSION:
        raise CuttingError(
            "The run file was written by a newer version (%d) and is not supported (%d)."
            % (version, RUN_SCHEMA_VERSION),
            status_code=422,
        )

    data["project_id"] = project_id
    data["run_id"] = run_id
    return data


def list_runs(project_id: str) -> list[dict]:
    """Every cutting run of a project, newest first."""
    project_id = storage.validate_project_id(project_id)
    root = cuts_root(project_id)

    if not root.is_dir():
        return []

    runs: list[dict] = []
    for directory in root.iterdir():
        if not directory.is_dir() or not _RUN_ID_PATTERN.match(directory.name):
            continue
        try:
            runs.append(read_manifest(project_id, directory.name))
        except storage.ProjectError:
            continue

    runs.sort(key=lambda run: run.get("created_at") or "", reverse=True)
    return runs


def new_manifest(
    project_id: str, run_id: str, job_id: str, job_input: dict, tool_versions: dict
) -> dict:
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "project_id": project_id,
        "job_id": job_id,
        "created_at": _now(),
        "finished_at": None,
        "status": RUN_RUNNING,
        "settings": job_input["settings"],
        "output_mode": job_input["output_mode"],
        "tool_versions": tool_versions,
        # The exact inputs this run was handed, including their fingerprints.
        # A later edit to the project does not change what this run processed.
        "sources": job_input["sources"],
        "clips": [],
        "combined": None,
        "error": None,
        "notes": [],
    }


# --- resolving an output for playback and download --------------------------


def describe_run(manifest: dict) -> dict:
    """The API shape of a run: the manifest plus derived, never-stored fields."""
    described = dict(manifest)

    clips = [dict(clip) for clip in manifest.get("clips") or []]
    for clip in clips:
        clip["playable"] = bool(
            clip.get("status") == CLIP_SUCCEEDED and clip.get("output_id")
        )
    described["clips"] = clips

    combined = manifest.get("combined")
    if isinstance(combined, dict):
        combined = dict(combined)
        combined["playable"] = combined.get("status") == CLIP_SUCCEEDED
        described["combined"] = combined

    source_total = sum(
        source.get("duration_seconds") or 0.0 for source in manifest.get("sources") or []
    )
    clip_total = sum(
        clip.get("duration_seconds") or 0.0
        for clip in clips
        if clip.get("status") == CLIP_SUCCEEDED
    )

    described["source_duration_seconds"] = round(source_total, 3)
    described["clip_duration_seconds"] = round(clip_total, 3)
    described["removed_duration_seconds"] = round(max(source_total - clip_total, 0), 3)
    described["is_complete_result"] = manifest.get("status") == RUN_SUCCEEDED
    return described


def resolve_output(project_id: str, run_id: str, output_id: str) -> tuple[Path, dict]:
    """Turn a run id and an output id into a real file, or refuse.

    The client never supplies a path. The id is looked up in this run's
    manifest, and the resulting path is then checked to be inside this run's
    directory — belt and braces, so that even a hand-edited manifest cannot
    make the server read somewhere else.
    """
    manifest = read_manifest(project_id, run_id)
    output_id = validate_output_id(output_id)

    entry: dict | None = None
    if output_id == COMBINED_OUTPUT_ID:
        candidate = manifest.get("combined")
        if isinstance(candidate, dict):
            entry = candidate
    else:
        for clip in manifest.get("clips") or []:
            if isinstance(clip, dict) and clip.get("output_id") == output_id:
                entry = clip
                break

    if entry is None or entry.get("status") != CLIP_SUCCEEDED:
        raise RunNotFound("That output does not exist in this run.")

    relative = entry.get("relative_path")
    if not isinstance(relative, str) or not relative:
        raise RunNotFound("That output does not exist in this run.")

    directory = run_directory(project_id, run_id).resolve()
    resolved = (directory / relative).resolve()

    if not resolved.is_relative_to(directory):
        raise RunNotFound("That output does not exist in this run.")
    if not resolved.is_file():
        raise RunNotFound(
            "The output file is no longer in the run directory. It may have been deleted by hand."
        )

    return resolved, entry


def generated_resources(project_id: str) -> list[dict]:
    """Outputs that were produced *and verified*, as project resources.

    Derived from the run manifests rather than copied into a second store, so
    a resource cannot outlive the file it names or disagree with its run.

    A failed run still contributes the clips it finished before it stopped:
    they were rendered and probed, and throwing away real work because a later
    step failed would be its own kind of data loss. The run itself is still
    reported as failed — `run_status` travels with every entry — so nothing
    here presents an incomplete run as a finished one. A *cancelled* run
    contributes nothing: the user asked for it to be abandoned.

    These are *products*, not sources. They are not offered as cutting inputs:
    the cutting module selects from the project's source manifest only, so a
    run can never silently feed on the output of the previous one.
    """
    entries: list[dict] = []

    for manifest in list_runs(project_id):
        if manifest.get("status") not in (RUN_SUCCEEDED, RUN_FAILED):
            continue

        run_id = manifest["run_id"]
        mode = manifest.get("output_mode", DEFAULT_OUTPUT_MODE)

        if mode in (MODE_CLIPS, MODE_BOTH):
            for clip in manifest.get("clips") or []:
                if clip.get("status") != CLIP_SUCCEEDED:
                    continue
                entries.append(
                    {
                        "id": "%s:%s" % (run_id, clip["output_id"]),
                        "run_id": run_id,
                        "run_status": manifest.get("status"),
                        "output_id": clip["output_id"],
                        "kind": "trimmed_clip",
                        "filename": clip.get("filename", ""),
                        "media_type": "video",
                        "produced_by": "edit.cut_silence",
                        "from_source_id": clip.get("source_id"),
                        "duration_seconds": clip.get("duration_seconds"),
                        "size_bytes": clip.get("size_bytes"),
                        "created_at": manifest.get("finished_at")
                        or manifest.get("created_at"),
                    }
                )

        combined = manifest.get("combined")
        if (
            mode in (MODE_COMBINED, MODE_BOTH)
            and isinstance(combined, dict)
            and combined.get("status") == CLIP_SUCCEEDED
        ):
            entries.append(
                {
                    "id": "%s:%s" % (run_id, COMBINED_OUTPUT_ID),
                    "run_id": run_id,
                    "run_status": manifest.get("status"),
                    "output_id": COMBINED_OUTPUT_ID,
                    "kind": "combined_video",
                    "filename": combined.get("filename", COMBINED_FILE_NAME),
                    "media_type": "video",
                    "produced_by": "edit.cut_silence",
                    "from_source_id": None,
                    "duration_seconds": combined.get("duration_seconds"),
                    "size_bytes": combined.get("size_bytes"),
                    "created_at": manifest.get("finished_at")
                    or manifest.get("created_at"),
                }
            )

    return entries


def collect_tool_versions() -> dict:
    """Record which tools produced a run, so a result stays reproducible."""
    return {
        "auto_editor": processes.tool_version(["auto-editor.exe", "--version"]),
        "ffmpeg": processes.tool_version(["ffmpeg.exe", "-version"]),
        "ffprobe": processes.tool_version(["ffprobe.exe", "-version"]),
    }


def is_empty_timeline(output: str) -> bool:
    """Did Auto-Editor report that nothing survived the cut?"""
    lowered = output.lower()
    return all(marker in lowered for marker in _EMPTY_TIMELINE_MARKERS)


def parse_machine_progress(chunk: str) -> float | None:
    """Fraction rendered, from Auto-Editor's `--progress machine` output."""
    matches = _MACHINE_PROGRESS.findall(chunk)
    if not matches:
        return None

    done, total = matches[-1]
    try:
        done_value, total_value = float(done), float(total)
    except ValueError:  # pragma: no cover - the regex already guarantees digits
        return None

    if total_value <= 0:
        return None
    return max(0.0, min(1.0, done_value / total_value))


def parse_ffmpeg_progress(chunk: str, total_seconds: float) -> float | None:
    """Fraction encoded, from FFmpeg's `-progress pipe:1` output."""
    if total_seconds <= 0:
        return None

    matches = _FFMPEG_PROGRESS.findall(chunk)
    if not matches:
        return None

    try:
        seconds = int(matches[-1]) / 1_000_000
    except ValueError:  # pragma: no cover
        return None

    return max(0.0, min(1.0, seconds / total_seconds))


def new_run_id() -> str:
    return uuid.uuid4().hex[:RUN_ID_LENGTH]


def clip_file_name(order: int, source_filename: str) -> str:
    """The deterministic name of one trimmed clip, from the legacy scheme."""
    return "%04d_%s_trimmed.mp4" % (order, safe_stem(source_filename, "clip"))


def check_path_budget(directory: Path, sources: list[dict]) -> None:
    """Refuse a run whose output paths would exceed the Windows path limit.

    Found by running it: with a deep workspace, Auto-Editor exits **zero** and
    simply writes no file. FFprobe catches that afterwards, but the resulting
    message blames the tool for something the path caused. Checking first turns
    a baffling failure into one sentence naming the fix.
    """
    if os.name != "nt":
        return

    longest = ""
    for index, source in enumerate(sources):
        candidate = str(
            directory
            / CLIPS_DIRECTORY
            / clip_file_name(index + 1, source["filename"])
        )
        if len(candidate) > len(longest):
            longest = candidate

    if len(longest) <= WINDOWS_MAX_PATH:
        return

    raise CuttingError(
        "The output path is too long for Windows (%d characters, limit %d). "
        "Shorten the file name, or move the workspace somewhere shorter with "
        "the VIDEO_FACTORY_WORKSPACE environment variable (for example D:\\VF). "
        "The offending path: %s"
        % (len(longest), WINDOWS_MAX_PATH, longest)
    )
