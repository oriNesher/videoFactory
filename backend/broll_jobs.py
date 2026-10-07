"""The B-roll export job (stage 3A).

`broll_export` renders the composed result — base video, B-roll, captions —
with the same Remotion composition and render script as the captions export,
from an immutable snapshot taken when the export was requested:

    exports/broll/<export-id>/
      export.json      status, what it was made from, verification
      snapshot.json    base, plan revision, asset fingerprints, captions, props
      <name>.broll.mp4
      logs/render.log

Nothing in the snapshot is looked up again while rendering: the props, the
file paths and every fingerprint are in the job record, so editing the plan,
the captions or the library during an export, or retrying the job later,
renders exactly what was asked for. Before rendering the base video and
every asset are fingerprinted again, and once more afterwards; if a file
changed in between, the result is discarded rather than offered.
"""

import os
import uuid
from pathlib import Path
from typing import Any

from . import broll, caption_inputs, caption_jobs, cutting, jobs, media, storage

EXPORT_JOB = "broll_export"
EXPORT_SCHEMA_VERSION = 1
EXPORTS_SUBDIRECTORY = "broll"


def exports_root(project_id: str) -> Path:
    return storage.project_directory(project_id) / "exports" / EXPORTS_SUBDIRECTORY


def export_directory(project_id: str, export_id: Any) -> Path:
    if not isinstance(export_id, str) or len(export_id) != 12 or not all(c in "0123456789abcdef" for c in export_id):
        raise broll.BrollError("Export not found.", status_code=404)
    return exports_root(project_id) / export_id


def active_job(project_id: str, key: str) -> dict | None:
    for record in jobs.list_jobs(project_id):
        if (
            record["type"] == EXPORT_JOB
            and record["status"] in jobs.ACTIVE_STATUSES
            and (record["input"].get("input") or {}).get("key") == key
        ):
            return record
    return None


def build_export_input(project_id: str, raw: Any) -> dict:
    """Freeze the composed result now. Refuses anything that is not reviewable."""
    if not isinstance(raw, dict):
        raise broll.BrollError("The export request is invalid.")
    composed = broll.compose(project_id, raw.get("input_key"))
    expected = raw.get("composition_digest")
    if expected is not None and expected != composed["digest"]:
        raise broll.BrollError("The result changed since it was reviewed. Review it again.", status_code=409)
    if composed["issues"]:
        raise broll.BrollError("Fix the B-roll problems first: %s" % composed["issues"][0]["message"], status_code=409)
    if not composed["placements"]:
        raise broll.BrollError("There is no B-roll to export.", status_code=409)
    status = caption_jobs.renderer_status()
    if not status["ready"]:
        raise broll.BrollError(" ".join(status["problems"]), status_code=503)
    if active_job(project_id, composed["base"]["key"]) is not None:
        raise broll.BrollError("This video is already being exported with B-roll.", status_code=409)

    described = composed["described"]
    path = caption_inputs.resolve(project_id, described["key"])["path"]
    return {
        "requested_at": broll.now(),
        "input": {
            "key": described["key"],
            "label": described["label"],
            "filename": described["filename"],
            "path": path,
            "fingerprint": described["fingerprint"],
            "metadata": described["metadata"],
        },
        "plan": composed["plan_reference"],
        "composition_digest": composed["digest"],
        "captions": composed["captions_reference"],
        "assets": [
            {
                "id": asset_id,
                "path": entry["path"],
                "filename": entry.get("filename") or os.path.basename(entry["path"]),
                "media_type": entry["media_type"],
                "digest": entry["fingerprint"]["digest"],
            }
            for asset_id, entry in sorted(composed["assets"].items())
        ],
        "props": composed["props"],
        "composition": described["composition"],
        "export_settings": dict(caption_jobs.EXPORT_SETTINGS),
    }


def _changed_files(snapshot: dict) -> list[str]:
    """Names of snapshot files whose bytes are no longer what was frozen."""
    changed = []
    files = [(snapshot["input"]["path"], snapshot["input"]["fingerprint"]["digest"], snapshot["input"]["filename"])]
    files += [(asset["path"], asset["digest"], asset["filename"]) for asset in snapshot["assets"]]
    for path, digest, name in files:
        try:
            current = caption_inputs.fingerprint(path) if os.path.isfile(path) else None
        except storage.ProjectError:
            current = None
        if current is None or current["digest"] != digest:
            changed.append(name)
    return changed


def _write_record(directory: Path, record: dict) -> None:
    try:
        storage.write_json_atomic(directory / "export.json", record)
    except OSError as error:
        raise jobs.JobFailed("The export record could not be saved: %s" % error) from error


def run_export(context: jobs.JobContext) -> dict:
    project_id = context.project_id
    snapshot = context.input
    export_id = uuid.uuid4().hex[:12]
    directory = export_directory(project_id, export_id)
    directory.mkdir(parents=True, exist_ok=False)
    final = directory / ("%s.broll.mp4" % cutting.safe_stem(snapshot["input"]["filename"], "video"))
    partial = directory / ".partial.mp4"

    record = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "kind": "video_factory.broll_export",
        "export_id": export_id,
        "project_id": project_id,
        "job_id": context.job_id,
        "status": caption_jobs.EXPORT_RUNNING,
        "created_at": broll.now(),
        "finished_at": None,
        "input_key": snapshot["input"]["key"],
        "input_label": snapshot["input"]["label"],
        "input_fingerprint": snapshot["input"]["fingerprint"]["digest"],
        "plan": snapshot["plan"],
        "composition_digest": snapshot["composition_digest"],
        "captions": snapshot["captions"],
        "asset_digests": {asset["id"]: asset["digest"] for asset in snapshot["assets"]},
        "placement_count": len(snapshot["props"]["broll"]["placements"]),
        "composition": snapshot["composition"],
        "export_settings": snapshot["export_settings"],
        "output": None,
        "verification": None,
        "error": None,
        "log_path": None,
    }

    def finish(status: str, error: str | None = None) -> None:
        partial.unlink(missing_ok=True)
        record.update(status=status, error=error, finished_at=broll.now())
        _write_record(directory, record)

    if len(str(final)) > cutting.WINDOWS_MAX_PATH:
        finish(caption_jobs.EXPORT_FAILED, "The export path is too long for Windows: %s. Use a shorter workspace path." % final)
        raise jobs.JobFailed(record["error"])
    try:
        storage.write_json_atomic(directory / "snapshot.json", snapshot)
    except OSError as error:
        raise jobs.JobFailed("The export snapshot could not be saved: %s" % error) from error
    _write_record(directory, record)

    context.progress("Checking the video and the B-roll files…")
    changed = _changed_files(snapshot)
    if changed:
        message = (
            "These files changed or disappeared after the export was requested, so nothing was "
            "exported: %s. Review the result again." % ", ".join(changed)
        )
        finish(caption_jobs.EXPORT_FAILED, message)
        raise jobs.JobFailed(message)

    settings = snapshot["export_settings"]
    caption_jobs.render_composition(
        context,
        directory,
        {
            "props": snapshot["props"],
            "input_path": snapshot["input"]["path"],
            "assets": [{"id": asset["id"], "path": asset["path"]} for asset in snapshot["assets"]],
            "output_path": str(partial),
            "bundle_cache_dir": str(caption_jobs.bundle_cache_directory()),
            "crf": settings["crf"],
            "x264_preset": settings["x264_preset"],
            "concurrency": None,
        },
        snapshot["composition"]["duration_in_frames"],
        record,
        finish,
    )

    context.progress("Checking the exported file with FFprobe…", percent=100.0)
    try:
        verification = caption_jobs.verify_render(
            str(partial), snapshot["composition"], snapshot["input"]["metadata"]["duration_seconds"]
        )
    except (media.MediaError, OSError, ValueError) as error:
        message = "The rendered file failed verification and was discarded: %s" % getattr(error, "message", error)
        finish(caption_jobs.EXPORT_FAILED, message)
        raise jobs.JobFailed(message) from error
    changed = _changed_files(snapshot)
    if changed:
        message = "These files changed while rendering, so the result was discarded: %s." % ", ".join(changed)
        finish(caption_jobs.EXPORT_FAILED, message)
        raise jobs.JobFailed(message)
    try:
        os.replace(partial, final)
    except OSError as error:
        finish(caption_jobs.EXPORT_FAILED, "The export could not be saved: %s" % error)
        raise jobs.JobFailed(record["error"]) from error

    record["output"] = {"filename": final.name, "size_bytes": final.stat().st_size, "media_type": "video/mp4"}
    record["verification"] = verification
    finish(caption_jobs.EXPORT_SUCCEEDED)
    return {
        "export_id": export_id,
        "summary": "Exported %s with %d B-roll placement(s) (%.1f s, %d×%d)."
        % (final.name, record["placement_count"], verification["duration_seconds"], verification["width"], verification["height"]),
    }


def read_export(project_id: str, export_id: Any) -> dict:
    directory = export_directory(project_id, export_id)
    try:
        record = storage.read_json(directory / "export.json")
    except FileNotFoundError as error:
        raise broll.BrollError("Export not found.", status_code=404) from error
    except (OSError, ValueError) as error:
        raise broll.BrollError("The export record cannot be read: %s" % error, status_code=422) from error
    if record.get("status") == caption_jobs.EXPORT_RUNNING and not caption_jobs._job_active(project_id, record.get("job_id")):  # noqa: SLF001
        record = {**record, "status": caption_jobs.EXPORT_INTERRUPTED, "error": "The backend stopped while this export was running."}
    record["downloadable"] = record.get("status") == caption_jobs.EXPORT_SUCCEEDED and bool(record.get("output"))
    return record


def list_exports(project_id: str) -> list[dict]:
    root = exports_root(project_id)
    if not root.is_dir():
        return []
    records = []
    for directory in root.iterdir():
        try:
            records.append(read_export(project_id, directory.name))
        except storage.ProjectError:
            continue
    records.sort(key=lambda record: record.get("created_at") or "", reverse=True)
    return records


def resolve_export_file(project_id: str, export_id: Any) -> tuple[Path, dict]:
    record = read_export(project_id, export_id)
    if not record["downloadable"]:
        raise broll.BrollError("This export has no finished file.", status_code=404)
    directory = export_directory(project_id, record["export_id"]).resolve()
    path = (directory / record["output"]["filename"]).resolve()
    if not path.is_relative_to(directory) or not path.is_file():
        raise broll.BrollError("The exported file is no longer in its folder.", status_code=404)
    return path, record


jobs.register_job_type(EXPORT_JOB, "B-roll export", run_export, build_export_input)
