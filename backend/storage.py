"""Reading, validating and atomically writing project files.

A project is a single versioned JSON file inside the workspace:

    <workspace>/projects/<project_id>/project.json
    <workspace>/projects/<project_id>/intermediates/
    <workspace>/projects/<project_id>/exports/

Storage paths are derived from a generated project id, never from the
user-supplied project name. Source footage stays where the user recorded it and
is only referenced by absolute path.
"""

import json
import os
import re
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import get_projects_root

SCHEMA_VERSION = 1

PROJECT_FILE_NAME = "project.json"
PROJECT_SUBDIRECTORIES = ("intermediates", "exports")

MAX_NAME_LENGTH = 100
MAX_PATH_LENGTH = 4000

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".avi",
    ".m4v",
    ".webm",
    ".mts",
    ".m2ts",
    ".wmv",
    ".flv",
    ".mpg",
    ".mpeg",
}

_PROJECT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class ProjectError(Exception):
    """A problem the user can act on. `status_code` is used by the API layer."""

    status_code = 400

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code


class ProjectNotFound(ProjectError):
    status_code = 404


class ProjectFileInvalid(ProjectError):
    """The project file exists but cannot be understood."""

    status_code = 422


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- shared JSON helpers ----------------------------------------------------
#
# Jobs and plans are stored with the same discipline as project.json: a
# versioned JSON document written through a temp file in the same directory and
# then `os.replace`-d, which is atomic on one volume.


# A reader can collide with `os.replace` on Windows: while the swap happens the
# target is briefly unopenable (sharing violation) or momentarily absent. The
# job worker writes while the interface polls, so reads retry for a few
# milliseconds before believing the error.
READ_ATTEMPTS = 6
READ_RETRY_SECONDS = 0.02

WRITE_ATTEMPTS = 6
WRITE_RETRY_SECONDS = 0.02


def replace_with_retry(temporary: Path, path: Path) -> None:
    """`os.replace`, retried briefly.

    On Windows the swap fails with a sharing violation if anything — a reader
    in another thread, an indexer, an antivirus scan — has the target open for
    a moment. Retrying turns a spurious failure into a short wait.
    """
    for attempt in range(WRITE_ATTEMPTS):
        try:
            os.replace(temporary, path)
            return
        except OSError:
            if attempt == WRITE_ATTEMPTS - 1:
                raise
            time.sleep(WRITE_RETRY_SECONDS)


def write_json_atomic(path: Path, data: Any) -> None:
    """Write `data` as JSON to `path` atomically. Raises OSError on failure."""
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(data, ensure_ascii=False, indent=2)
    # A short temp name on purpose: Windows still limits a path to 260
    # characters, and a deep workspace plus a long name is enough to fail.
    temporary = directory / (".%s.tmp" % uuid.uuid4().hex[:12])

    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def read_text_resilient(path: Path) -> str:
    for attempt in range(READ_ATTEMPTS):
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # A missing file is usually simply missing; retry once in case a
            # replace was in flight, then believe it.
            if attempt >= 1:
                raise
        except OSError:
            if attempt == READ_ATTEMPTS - 1:
                raise
        time.sleep(READ_RETRY_SECONDS)

    raise FileNotFoundError(path)  # pragma: no cover - loop always returns/raises


def read_json(path: Path) -> Any:
    """Read a JSON document. Raises OSError or json.JSONDecodeError."""
    return json.loads(read_text_resilient(path))


# --- validation -------------------------------------------------------------


def validate_project_name(raw_name: Any) -> str:
    if not isinstance(raw_name, str):
        raise ProjectError("The project name must be text.")

    name = unicodedata.normalize("NFC", raw_name).strip()

    if not name:
        raise ProjectError("The project name cannot be empty.")
    if len(name) > MAX_NAME_LENGTH:
        raise ProjectError("The project name is too long (up to %d characters)." % MAX_NAME_LENGTH)
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise ProjectError("The project name contains invalid characters.")

    return name


def validate_project_id(raw_id: Any) -> str:
    if not isinstance(raw_id, str) or not _PROJECT_ID_PATTERN.match(raw_id):
        raise ProjectNotFound("Invalid project id.")
    return raw_id


def validate_source_path(raw_path: Any) -> str:
    """Normalise and check a user-entered absolute path to a local video file."""
    if not isinstance(raw_path, str):
        raise ProjectError("The file path must be text.")

    path_text = raw_path.strip()
    # Windows Explorer's "Copy as path" wraps the path in double quotes.
    if len(path_text) >= 2 and path_text.startswith('"') and path_text.endswith('"'):
        path_text = path_text[1:-1].strip()

    if not path_text:
        raise ProjectError("Enter a file path.")
    if len(path_text) > MAX_PATH_LENGTH:
        raise ProjectError("The file path is too long.")
    if "\x00" in path_text:
        raise ProjectError("The file path contains invalid characters.")

    candidate = Path(path_text)
    if not candidate.is_absolute():
        raise ProjectError("Enter a full path, for example C:\\Videos\\take1.mp4")

    if candidate.suffix.lower() not in VIDEO_EXTENSIONS:
        allowed = ", ".join(sorted(VIDEO_EXTENSIONS))
        raise ProjectError("Unsupported file type. Supported extensions: " + allowed)

    normalised = os.path.normpath(path_text)

    if not os.path.exists(normalised):
        raise ProjectError("File not found: " + normalised)
    if not os.path.isfile(normalised):
        raise ProjectError("The path does not point to a file: " + normalised)

    return normalised


# --- paths ------------------------------------------------------------------


def project_directory(project_id: str) -> Path:
    return get_projects_root() / project_id


def project_file(project_id: str) -> Path:
    return project_directory(project_id) / PROJECT_FILE_NAME


# --- persistence ------------------------------------------------------------


def _parse_project(data: Any, project_id: str) -> dict:
    """Validate the on-disk shape and return a normalised project dict."""
    if not isinstance(data, dict):
        raise ProjectFileInvalid("The project file is corrupt: its structure is invalid.")

    schema_version = data.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise ProjectFileInvalid("The project file is corrupt: the schema version is missing.")
    if schema_version > SCHEMA_VERSION:
        raise ProjectFileInvalid(
            "The project file was written by a newer version (%d) and is not supported by this one (%d)."
            % (schema_version, SCHEMA_VERSION)
        )

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ProjectFileInvalid("The project file is corrupt: the project name is missing.")

    raw_sources = data.get("sources", [])
    if not isinstance(raw_sources, list):
        raise ProjectFileInvalid("The project file is corrupt: the source list is invalid.")

    sources = []
    for entry in raw_sources:
        if not isinstance(entry, dict):
            raise ProjectFileInvalid("The project file is corrupt: a source entry is invalid.")

        source_id = entry.get("id")
        path = entry.get("path")
        if not isinstance(source_id, str) or not source_id:
            raise ProjectFileInvalid("The project file is corrupt: a source entry has no id.")
        if not isinstance(path, str) or not path:
            raise ProjectFileInvalid("The project file is corrupt: a source entry has no path.")

        sources.append(
            {
                "id": source_id,
                "path": path,
                "added_at": entry.get("added_at") or "",
            }
        )

    settings = data.get("settings")
    if not isinstance(settings, dict):
        settings = {}

    return {
        "schema_version": schema_version,
        # The directory name is authoritative: it is what the API addresses.
        "id": project_id,
        "name": name,
        "created_at": data.get("created_at") or "",
        "updated_at": data.get("updated_at") or "",
        "sources": sources,
        "settings": settings,
    }


def read_project(project_id: str) -> dict:
    project_id = validate_project_id(project_id)
    path = project_file(project_id)

    try:
        raw = read_text_resilient(path)
    except FileNotFoundError as error:
        raise ProjectNotFound("Project not found.") from error
    except OSError as error:
        raise ProjectFileInvalid("The project file cannot be read: %s" % error) from error

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ProjectFileInvalid(
            "The project file is not valid JSON (line %d)." % error.lineno
        ) from error

    return _parse_project(data, project_id)


def write_project(project: dict) -> dict:
    """Write project.json atomically: temp file in the same directory, then replace.

    os.replace is atomic on the same volume, so a failure mid-write leaves the
    previous project.json intact.
    """
    project_id = validate_project_id(project["id"])
    directory = project_directory(project_id)
    directory.mkdir(parents=True, exist_ok=True)

    updated_at = _now()
    document = {
        "schema_version": SCHEMA_VERSION,
        "id": project_id,
        "name": project["name"],
        "created_at": project.get("created_at") or updated_at,
        "updated_at": updated_at,
        "sources": project.get("sources", []),
        "settings": project.get("settings", {}),
    }

    try:
        write_json_atomic(directory / PROJECT_FILE_NAME, document)
    except OSError as error:
        raise ProjectError("Saving the project failed: %s" % error, status_code=500) from error

    return read_project(project_id)


def create_project(name: Any) -> dict:
    validated_name = validate_project_name(name)
    project_id = uuid.uuid4().hex

    directory = project_directory(project_id)
    directory.mkdir(parents=True, exist_ok=False)
    for subdirectory in PROJECT_SUBDIRECTORIES:
        (directory / subdirectory).mkdir(exist_ok=True)

    return write_project(
        {
            "id": project_id,
            "name": validated_name,
            "created_at": _now(),
            "sources": [],
            "settings": {},
        }
    )


def list_projects() -> list[dict]:
    """List every project directory in the workspace.

    A project whose file is missing or malformed is reported with an `error`
    instead of breaking the whole listing.
    """
    entries: list[dict] = []

    for directory in sorted(get_projects_root().iterdir()):
        if not directory.is_dir() or not _PROJECT_ID_PATTERN.match(directory.name):
            continue

        try:
            project = read_project(directory.name)
        except ProjectError as error:
            entries.append(
                {
                    "id": directory.name,
                    "name": directory.name,
                    "created_at": "",
                    "updated_at": "",
                    "source_count": 0,
                    "missing_source_count": 0,
                    "error": error.message,
                }
            )
            continue

        sources = describe_sources(project["sources"])
        entries.append(
            {
                "id": project["id"],
                "name": project["name"],
                "created_at": project["created_at"],
                "updated_at": project["updated_at"],
                "source_count": len(sources),
                "missing_source_count": sum(1 for s in sources if not s["exists"]),
                "error": None,
            }
        )

    entries.sort(key=lambda entry: entry["updated_at"], reverse=True)
    return entries


# --- mutations --------------------------------------------------------------


def add_source(project_id: str, raw_path: Any) -> dict:
    project = read_project(project_id)
    path = validate_source_path(raw_path)

    for existing in project["sources"]:
        if os.path.normcase(existing["path"]) == os.path.normcase(path):
            raise ProjectError("That file is already in the project.", status_code=409)

    project["sources"].append(
        {"id": uuid.uuid4().hex, "path": path, "added_at": _now()}
    )
    return write_project(project)


def add_sources(project_id: str, paths: list[str]) -> dict:
    """Add several files in one write, keeping the given order.

    Used by the folder picker, where a dozen takes arrive at once. A file that
    is already in the project, or that fails validation, is reported rather
    than aborting the rest: a folder added twice should quietly add what is new.
    """
    project = read_project(project_id)

    seen = {os.path.normcase(source["path"]) for source in project["sources"]}
    added: list[str] = []
    duplicates: list[str] = []
    failed: list[dict] = []

    for raw_path in paths:
        try:
            path = validate_source_path(raw_path)
        except ProjectError as error:
            failed.append(
                {"filename": os.path.basename(str(raw_path)), "error": error.message}
            )
            continue

        key = os.path.normcase(path)
        if key in seen:
            duplicates.append(os.path.basename(path))
            continue

        seen.add(key)
        project["sources"].append(
            {"id": uuid.uuid4().hex, "path": path, "added_at": _now()}
        )
        added.append(os.path.basename(path))

    # Nothing new: leave the file alone rather than bumping `updated_at`, which
    # would needlessly mark every existing plan as outdated.
    saved = write_project(project) if added else project

    return {
        "project": describe_project(saved),
        "added": added,
        "duplicates": duplicates,
        "failed": failed,
    }


def save_project(project_id: str, name: Any, source_ids: Any) -> dict:
    """Save a new project name and a new source order.

    `source_ids` is the complete ordered list of sources to keep. Ids left out
    are removed from the project; the files themselves are never touched.
    """
    project = read_project(project_id)
    validated_name = validate_project_name(name)

    if not isinstance(source_ids, list):
        raise ProjectError("The source list is invalid.")

    known = {source["id"]: source for source in project["sources"]}
    seen: set[str] = set()
    ordered = []

    for source_id in source_ids:
        if not isinstance(source_id, str) or source_id not in known:
            raise ProjectError("The source list refers to a file that is not in the project.")
        if source_id in seen:
            raise ProjectError("The source list contains duplicates.")
        seen.add(source_id)
        ordered.append(known[source_id])

    project["name"] = validated_name
    project["sources"] = ordered
    return write_project(project)


# --- presentation -----------------------------------------------------------


def describe_sources(sources: list[dict]) -> list[dict]:
    """Add derived, never-persisted fields: filename, existence, size."""
    described = []

    for source in sources:
        path = source["path"]
        try:
            exists = os.path.isfile(path)
            size = os.path.getsize(path) if exists else None
        except OSError:
            exists, size = False, None

        described.append(
            {
                "id": source["id"],
                "path": path,
                "filename": os.path.basename(path) or path,
                "added_at": source.get("added_at", ""),
                "exists": exists,
                "size_bytes": size,
            }
        )

    return described


def describe_project(project: dict) -> dict:
    return {
        "schema_version": project["schema_version"],
        "id": project["id"],
        "name": project["name"],
        "created_at": project["created_at"],
        "updated_at": project["updated_at"],
        "settings": project["settings"],
        "sources": describe_sources(project["sources"]),
        "directory": str(project_directory(project["id"])),
    }
