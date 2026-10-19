"""The final export after the music stage (stage 4).

`music_export` renders the composed result — base video, the approved B-roll,
captions and, unless the user continued without it, the background music —
with the same Remotion composition, render script and checks as the B-roll
export (`broll_jobs.render_export`):

    exports/final/<export-id>/
      export.json      status, what it was made from, verification
      snapshot.json    base, B-roll, captions, track, props
      <name>.final.mp4
      logs/render.log

The snapshot is frozen when the export is requested, from the music decision
the user made, so changing the music settings or the library afterwards
renders nothing different.
"""

import os
from typing import Any

from . import broll_jobs, caption_inputs, caption_jobs, jobs, music

EXPORT_JOB = "music_export"
EXPORTS_SUBDIRECTORY = "final"


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
    """Freeze the decided result now. Refuses anything that was not decided as it is."""
    if not isinstance(raw, dict):
        raise music.MusicError("The export request is invalid.")
    composed = music.compose(project_id, raw.get("input_key"))
    review = music.review_state(project_id, composed)
    if not (review["approved"] or review["skipped"]):
        raise music.MusicError("Approve the music, or continue without it, before exporting.", status_code=409)
    expected = raw.get("composition_digest")
    if expected is not None and expected != composed["digest"]:
        raise music.MusicError("The result changed since it was reviewed. Review it again.", status_code=409)
    props = composed["props"] if review["approved"] else music.without_music(composed["props"])
    if not music.has_overlays(props):
        raise music.MusicError("There is nothing to add to this video: the video itself is the finished file.", status_code=409)
    status = caption_jobs.renderer_status()
    if not status["ready"]:
        raise music.MusicError(" ".join(status["problems"]), status_code=503)
    if active_job(project_id, composed["base"]["key"]) is not None:
        raise music.MusicError("This video is already being exported.", status_code=409)

    described = composed["described"]
    path = caption_inputs.resolve(project_id, described["key"])["path"]
    broll_assets = composed["broll"]["assets"] if props["broll"]["placements"] else {}
    track = composed["track"] if props["music"] else None
    return {
        "requested_at": music.now(),
        "input": {
            "key": described["key"],
            "label": described["label"],
            "filename": described["filename"],
            "path": path,
            "fingerprint": described["fingerprint"],
            "metadata": described["metadata"],
        },
        "decision": review["decision"]["decision"],
        "plan": composed["broll"]["plan_reference"] if props["broll"]["placements"] else None,
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
            for asset_id, entry in sorted(broll_assets.items())
        ],
        "music": (
            {
                "id": track["id"],
                "path": track["path"],
                "filename": track.get("filename") or os.path.basename(track["path"]),
                "digest": track["fingerprint"]["digest"],
            }
            if track else None
        ),
        "props": props,
        "composition": described["composition"],
        "export_settings": dict(caption_jobs.EXPORT_SETTINGS),
    }


def run_export(context: jobs.JobContext) -> dict:
    return broll_jobs.render_export(
        context, subdirectory=EXPORTS_SUBDIRECTORY, suffix=".final.mp4", kind="video_factory.final_export"
    )


def read_export(project_id: str, export_id: Any) -> dict:
    return broll_jobs.read_export(project_id, export_id, EXPORTS_SUBDIRECTORY)


def list_exports(project_id: str) -> list[dict]:
    return broll_jobs.list_exports(project_id, EXPORTS_SUBDIRECTORY)


def resolve_export_file(project_id: str, export_id: Any):
    return broll_jobs.resolve_export_file(project_id, export_id, EXPORTS_SUBDIRECTORY)


jobs.register_job_type(EXPORT_JOB, "Final export", run_export, build_export_input)
