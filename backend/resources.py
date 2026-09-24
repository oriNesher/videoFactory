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

from . import storage

RESOURCE_CATALOG_VERSION = 1

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
    }


def input_snapshot(project: dict) -> dict:
    """The subset of project state a plan depends on.

    The project *name* is deliberately absent: renaming a project does not
    change what a plan would do, so it must not invalidate one.
    """
    catalog = build_catalog(project)

    return {
        "resources": [
            {
                "id": resource["id"],
                "filename": resource["filename"],
                "available": resource["available"],
            }
            for resource in catalog["resources"]
        ],
        "settings": project.get("settings", {}),
    }


def fingerprint(project: dict) -> str:
    """A stable hash of `input_snapshot`, used to detect outdated plans."""
    canonical = json.dumps(
        input_snapshot(project), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return "sha256:%s" % digest
