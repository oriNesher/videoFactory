"""The project resource catalog: what a plan is allowed to refer to.

A plan never contains a file path. It refers to stable resource ids, which are
the source ids milestone 0A already assigns, and the backend resolves those ids
to real files at execution time. That keeps local absolute paths — which say
where a person keeps their footage and often who they are — out of plans and out
of anything sent to an external provider.

The catalog also carries the project's *input fingerprint*: a hash of exactly
those project inputs a plan depends on. If sources are added, removed,
reordered or go missing, the fingerprint changes and plans made against the old
one are outdated.
"""

import hashlib
import json

from . import cutting, storage

RESOURCE_CATALOG_VERSION = 2

# Settings blocks that belong to a module's own form rather than to what a plan
# would do. A plan carries its own parameters, so nudging the cutting threshold
# in the interface must not mark every existing plan outdated.
FINGERPRINT_EXCLUDED_SETTINGS = frozenset({cutting.SETTINGS_KEY})

MEDIA_TYPE_VIDEO = "video"

# Stated in the catalog itself so the model is never tempted to guess content
# from a filename, and so the user can see that it was told not to.
CONTENT_NOTE = (
    "שמות הקבצים אינם מעידים על תוכן הווידאו. המערכת אינה מנתחת, מתמללת או "
    "צופה בקבצים, ולכן אין מידע על מה שמופיע או נאמר בהם."
)


def build_catalog(project: dict) -> dict:
    """The resource catalog of one project: ids, filenames, availability."""
    resources = []

    for index, source in enumerate(storage.describe_sources(project["sources"])):
        resources.append(
            {
                "id": source["id"],
                "filename": source["filename"],
                "media_type": MEDIA_TYPE_VIDEO,
                "order": index + 1,
                "available": source["exists"],
                "size_bytes": source["size_bytes"],
                "added_at": source["added_at"],
                "description": (
                    "חומר גלם מס' %d בפרויקט. %s"
                    % (
                        index + 1,
                        "הקובץ זמין."
                        if source["exists"]
                        else "הקובץ אינו נמצא כרגע במיקומו.",
                    )
                ),
            }
        )

    return {
        "project_id": project["id"],
        "catalog_version": RESOURCE_CATALOG_VERSION,
        "note": CONTENT_NOTE,
        "resources": resources,
        # Files this application produced, listed separately from the footage
        # the user supplied. Keeping them apart is the point: a source is
        # something to edit, a generated file is a result, and a cutting run
        # must never quietly take the previous run's output as its input.
        "generated": cutting.generated_resources(project["id"]),
    }


def input_snapshot(project: dict) -> dict:
    """The subset of project state a plan depends on.

    The project *name* is deliberately absent: renaming a project does not
    change what a plan would do, so it must not invalidate one. Generated
    outputs are absent for the same reason, and because reading them means
    scanning every run directory — work a fingerprint should never do.
    """
    return {
        "resources": [
            {
                "id": source["id"],
                "filename": source["filename"],
                "available": source["exists"],
            }
            for source in storage.describe_sources(project["sources"])
        ],
        "settings": {
            key: value
            for key, value in project.get("settings", {}).items()
            if key not in FINGERPRINT_EXCLUDED_SETTINGS
        },
    }


def fingerprint(project: dict) -> str:
    """A stable hash of `input_snapshot`, used to detect outdated plans."""
    canonical = json.dumps(
        input_snapshot(project), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return "sha256:%s" % digest
