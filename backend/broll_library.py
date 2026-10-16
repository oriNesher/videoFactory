"""The local B-roll asset libraries (stage 3A).

Two kinds of library, with the same layout:

    <workspace>/library/broll/           the general library, shared by every project
    <workspace>/projects/<id>/broll_library/   one project's own library
      library.json                  the entries (versioned, written atomically)
      thumbnails/<id>-<digest>.jpg  small previews, regenerated when a file changes

**A project sees its own library and the general one, never another
project's.** Every lookup made on behalf of a project goes through
`visible_entries` / `read_entry(..., project_id)`, so a project's assets
cannot be listed, placed, planned with or served from another project. Ids
are unique across all libraries, so an asset keeps its id when it is moved
between the general library and a project's (`move`), and plans that use it
keep working. Each public entry says which library it is in (`library`).

**Files are referenced, never copied** — the same rule as project footage. An
entry records the file's absolute path, and the original stays exactly where
it is: importing reads it (FFprobe, a fingerprint, one frame for the
thumbnail) and writes nothing next to it. The consequences are explicit in
each entry's derived `status`:

- `available` — the file is there and has the bytes that were measured;
- `missing`   — nothing is at the path any more (moved, renamed, deleted);
- `changed`   — something is there, but not the measured bytes. It cannot be
  placed or rendered until it is measured again (**Re-measure**), because its
  size or length may no longer fit the placements that use it;
- `unsupported` — FFprobe reads it, but the preview browser and the renderer
  cannot decode it (for example HEVC or ProRes video). The reason says what
  to convert it to.

**Measured vs. described.** Everything under `measured` comes from the file:
dimensions after rotation, aspect ratio, duration and audio for videos, codec.
`description`, `tags` and `moments` (stage 3B: "from 0:05 to 0:12 the
dashboard is shown" — optional, videos only) are only ever what the user
typed; nothing is derived from a filename or guessed from the picture. The
library never claims to know what an asset shows.

Placements live in each project's B-roll plans (`broll.py`) and refer to an
entry by its stable `id`. Removing an entry from the library never touches the
file; plans that used it then report the asset as gone.
"""

import json
import os
import re
import subprocess
import threading
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import caption_inputs, folders, media, storage
from .config import get_projects_root, get_workspace_root

LIBRARY_SCHEMA_VERSION = 1
LIBRARY_KIND = "video_factory.broll_library"

MEDIA_IMAGE = "image"
MEDIA_VIDEO = "video"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTENSIONS = set(storage.VIDEO_EXTENSIONS)

# What Chrome (the preview, and Remotion's headless renderer) decodes reliably.
PLAYABLE_VIDEO_CODECS = {"h264", "vp8", "vp9", "av1"}
PLAYABLE_VIDEO_CONTAINERS = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}
PLAYABLE_IMAGE_CODECS = {"png", "mjpeg", "webp"}

LIBRARY_GENERAL = "general"
LIBRARY_PROJECT = "project"

STATUS_AVAILABLE = "available"
STATUS_MISSING = "missing"
STATUS_CHANGED = "changed"
STATUS_UNSUPPORTED = "unsupported"

MAX_ASSETS = 1000
MAX_DISPLAY_NAME = 120
MAX_DESCRIPTION = 1000
MAX_TAGS = 20
MAX_TAG_LENGTH = 40
MAX_MOMENTS = 20
MAX_MOMENT_DESCRIPTION = 300
MIN_MOMENT_SECONDS = 0.2

THUMBNAIL_WIDTH = 320

_ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")
_lock = threading.RLock()


class LibraryError(storage.ProjectError):
    """A library problem the user can act on."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# --- locations ------------------------------------------------------------------


def library_root(project_id: str | None = None) -> Path:
    """The general library, or with `project_id` that project's own."""
    if project_id is None:
        return get_workspace_root() / "library" / "broll"
    project_id = storage.validate_project_id(project_id)
    if not storage.project_file(project_id).is_file():
        raise storage.ProjectNotFound("Video not found.")
    return storage.project_directory(project_id) / "broll_library"


def library_file(project_id: str | None = None) -> Path:
    return library_root(project_id) / "library.json"


def thumbnails_directory(project_id: str | None = None) -> Path:
    return library_root(project_id) / "thumbnails"


def validate_asset_id(raw: Any) -> str:
    if not isinstance(raw, str) or not _ID_PATTERN.match(raw):
        raise LibraryError("B-roll asset not found.", status_code=404)
    return raw


# --- the document -----------------------------------------------------------------
#
# Every entry read carries `library` (general or project) and, for a project's
# own assets, `project_id`. Both say where the entry was read from; neither is
# written to the file.

_LOCATION_KEYS = ("library", "project_id")


def _read_document(project_id: str | None = None) -> dict:
    try:
        document = storage.read_json(library_file(project_id))
    except FileNotFoundError:
        document = {"schema_version": LIBRARY_SCHEMA_VERSION, "kind": LIBRARY_KIND, "assets": []}
    except (OSError, ValueError) as error:
        raise LibraryError("The B-roll library file cannot be read: %s" % error, status_code=422) from error
    if not isinstance(document, dict) or document.get("kind") != LIBRARY_KIND:
        raise LibraryError("The B-roll library file is not a library.", status_code=422)
    if (document.get("schema_version") or 0) > LIBRARY_SCHEMA_VERSION:
        raise LibraryError("The B-roll library was written by a newer version.", status_code=422)
    assets = document.get("assets")
    document["assets"] = [entry for entry in assets if _well_formed(entry)] if isinstance(assets, list) else []
    for entry in document["assets"]:
        _locate(entry, project_id)
    return document


def _locate(entry: dict, project_id: str | None) -> dict:
    entry["library"] = LIBRARY_GENERAL if project_id is None else LIBRARY_PROJECT
    entry["project_id"] = project_id
    return entry


def _well_formed(entry: Any) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("id"), str)
        and _ID_PATTERN.match(entry["id"]) is not None
        and isinstance(entry.get("path"), str)
        and entry.get("media_type") in (MEDIA_IMAGE, MEDIA_VIDEO)
        and isinstance(entry.get("measured"), dict)
        and isinstance(entry.get("fingerprint"), dict)
    )


def _write_document(document: dict, project_id: str | None = None) -> None:
    stored = {
        **document,
        "assets": [
            {key: value for key, value in entry.items() if key not in _LOCATION_KEYS}
            for entry in document["assets"]
        ],
    }
    try:
        storage.write_json_atomic(library_file(project_id), stored)
    except OSError as error:
        raise LibraryError("Saving the B-roll library failed: %s" % error, status_code=500) from error


def read_entries(project_id: str | None = None) -> list[dict]:
    """One library's raw entries, paths included. For the backend only."""
    with _lock:
        return _read_document(project_id)["assets"]


def visible_entries(project_id: str | None) -> list[dict]:
    """Everything a project may use: its own assets, then the general ones.

    Without a project, only the general library.
    """
    with _lock:
        own = read_entries(project_id) if project_id is not None else []
        return own + read_entries()


def read_entry(asset_id: Any, project_id: str | None = None) -> dict:
    """An asset visible to `project_id` (or a general asset, without one)."""
    asset_id = validate_asset_id(asset_id)
    for entry in visible_entries(project_id):
        if entry["id"] == asset_id:
            return entry
    raise LibraryError("That B-roll asset is not in the library.", status_code=404)


def _find(asset_id: Any, project_id: str | None) -> tuple[dict, dict, str | None]:
    """`(document, entry, owner)` for an asset visible to `project_id`, where
    `owner` is the project whose library holds it (None: the general one).
    Call with `_lock` held; write the document back with `owner`."""
    asset_id = validate_asset_id(asset_id)
    owners = ([project_id] if project_id is not None else []) + [None]
    for owner in owners:
        document = _read_document(owner)
        entry = next((item for item in document["assets"] if item["id"] == asset_id), None)
        if entry is not None:
            return document, entry, owner
    raise LibraryError("That B-roll asset is not in the library.", status_code=404)


# --- measuring --------------------------------------------------------------------


def media_type_for(path: str) -> str | None:
    extension = Path(path).suffix.lower()
    if extension in IMAGE_EXTENSIONS:
        return MEDIA_IMAGE
    if extension in VIDEO_EXTENSIONS:
        return MEDIA_VIDEO
    return None


def validate_asset_path(raw: Any) -> str:
    """An absolute path to an existing image or video file."""
    if not isinstance(raw, str):
        raise LibraryError("The file path must be text.")
    text = raw.strip()
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        text = text[1:-1].strip()
    if not text:
        raise LibraryError("Enter a file path.")
    if len(text) > storage.MAX_PATH_LENGTH or "\x00" in text:
        raise LibraryError("The file path is not valid.")
    if not Path(text).is_absolute():
        raise LibraryError("Enter a full path, for example C:\\B-roll\\screen recording.mp4")
    if media_type_for(text) is None:
        allowed = ", ".join(sorted(IMAGE_EXTENSIONS | VIDEO_EXTENSIONS))
        raise LibraryError("Unsupported file type. Images and videos are accepted: %s" % allowed)
    path = os.path.normpath(text)
    if not os.path.isfile(path):
        raise LibraryError("File not found: %s" % path)
    return path


def _probe_image(path: str) -> dict:
    try:
        raw = media._ffprobe(  # noqa: SLF001 - the shared FFprobe runner
            path,
            [
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,width,height:stream_side_data=rotation:stream_tags=rotate",
                "-of", "json",
            ],
        )
        streams = json.loads(raw).get("streams") or []
    except media.MediaError as error:
        raise LibraryError(error.message) from error
    except ValueError as error:
        raise LibraryError("FFprobe returned an unreadable answer for this image.") from error
    if not streams:
        raise LibraryError("This file holds no picture FFprobe can read.")
    stream = streams[0]
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise LibraryError("The picture size of this image cannot be read.")
    rotation = caption_inputs._rotation(stream)  # noqa: SLF001 - the shared rotation reader
    if abs(rotation) % 180 == 90:
        width, height = height, width
    codec = stream.get("codec_name") or ""
    supported = codec in PLAYABLE_IMAGE_CODECS
    return {
        "width": width,
        "height": height,
        "codec": codec,
        "duration_seconds": None,
        "fps": None,
        "has_audio": False,
        "supported": supported,
        "unsupported_reason": None if supported else (
            "The image is encoded as %s, which the preview cannot show. Save it as PNG, JPEG or WebP." % codec
        ),
    }


def _probe_video(path: str) -> dict:
    try:
        metadata = caption_inputs.probe_metadata(path)
    except storage.ProjectError as error:
        raise LibraryError(error.message) from error
    codec = metadata["video_codec"]
    extension = Path(path).suffix.lower()
    reason = None
    if codec not in PLAYABLE_VIDEO_CODECS:
        reason = (
            "The video is encoded as %s, which the preview browser and the renderer cannot decode "
            "reliably. Convert it to H.264 MP4 (for example with HandBrake or FFmpeg) and import that."
            % (codec or "an unknown codec")
        )
    elif extension not in PLAYABLE_VIDEO_CONTAINERS:
        reason = "%s files cannot be played by the preview browser. Convert it to MP4." % extension
    return {
        "width": metadata["display_width"],
        "height": metadata["display_height"],
        "codec": codec,
        "duration_seconds": metadata["duration_seconds"],
        "fps": metadata["fps"],
        "has_audio": metadata["has_audio"],
        "audio_codec": metadata["audio_codec"],
        "variable_frame_rate": metadata["variable_frame_rate"],
        "supported": reason is None,
        "unsupported_reason": reason,
    }


def measure(path: str, media_type: str) -> dict:
    """Technical metadata only, read from the file."""
    measured = _probe_image(path) if media_type == MEDIA_IMAGE else _probe_video(path)
    measured["aspect_ratio"] = round(measured["width"] / measured["height"], 6)
    ratio = measured["aspect_ratio"]
    measured["orientation"] = "landscape" if ratio > 1.02 else "portrait" if ratio < 0.98 else "square"
    return measured


def _thumbnail_name(asset_id: str, digest: str) -> str:
    return "%s-%s.jpg" % (asset_id, digest.split(":")[-1][:12])


def make_thumbnail(
    path: str, media_type: str, measured: dict, asset_id: str, digest: str, project_id: str | None = None,
) -> str | None:
    """A small JPEG of one frame. None if FFmpeg cannot make one (shown as a placeholder)."""
    directory = thumbnails_directory(project_id)
    directory.mkdir(parents=True, exist_ok=True)
    name = _thumbnail_name(asset_id, digest)
    target = directory / name
    command = ["ffmpeg.exe", "-hide_banner", "-loglevel", "error", "-y", "-nostdin"]
    if media_type == MEDIA_VIDEO and measured.get("duration_seconds"):
        command += ["-ss", "%.3f" % min(1.0, measured["duration_seconds"] * 0.25)]
    command += [
        "-i", path, "-frames:v", "1",
        "-vf", "scale='min(%d,iw)':-2" % THUMBNAIL_WIDTH,
        "-q:v", "4", str(target),
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return name if result.returncode == 0 and target.is_file() else None


_THUMBNAIL_PATTERN = re.compile(r"^[0-9a-f]{12}-[0-9a-f]{12}\.jpg$")


def _remove_thumbnail(name: Any, project_id: str | None = None) -> None:
    if isinstance(name, str) and _THUMBNAIL_PATTERN.match(name):
        (thumbnails_directory(project_id) / name).unlink(missing_ok=True)


def _move_thumbnail(name: Any, source: str | None, target: str | None) -> str | None:
    """Carry a thumbnail to another library's folder. None if there was none to carry."""
    if not isinstance(name, str) or not _THUMBNAIL_PATTERN.match(name):
        return None
    origin = thumbnails_directory(source) / name
    if not origin.is_file():
        return None
    destination = thumbnails_directory(target)
    destination.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(origin, destination / name)
    except OSError:
        return None
    return name


# --- status ----------------------------------------------------------------------


def current_fingerprint(path: str) -> dict | None:
    if not os.path.isfile(path):
        return None
    try:
        return caption_inputs.fingerprint(path)
    except storage.ProjectError:
        return None


def status_of(entry: dict) -> tuple[str, str | None]:
    """Derived on every read, never stored."""
    path = entry["path"]
    if not os.path.isfile(path):
        return STATUS_MISSING, "The file is no longer at %s. Move it back, or remove this asset." % path
    current = current_fingerprint(path)
    if current is None or current["digest"] != entry["fingerprint"].get("digest"):
        return STATUS_CHANGED, (
            "The file has changed since it was measured. Press Re-measure before using it; "
            "placements that use it are blocked until then."
        )
    if not entry["measured"].get("supported", True):
        return STATUS_UNSUPPORTED, entry["measured"].get("unsupported_reason")
    return STATUS_AVAILABLE, None


def public_entry(entry: dict) -> dict:
    """An entry for the interface: no path, plus its derived status."""
    status, reason = status_of(entry)
    return {
        "id": entry["id"],
        "library": entry.get("library", LIBRARY_GENERAL),
        "media_type": entry["media_type"],
        "filename": entry.get("filename") or os.path.basename(entry["path"]),
        "display_name": entry.get("display_name") or entry.get("filename") or entry["id"],
        "display_name_source": "user" if entry.get("display_name") else "filename",
        "description": entry.get("description"),
        "tags": list(entry.get("tags") or []),
        "moments": valid_moments(entry),
        "described": is_described(entry),
        "measured": entry["measured"],
        "fingerprint": entry["fingerprint"],
        "imported_at": entry.get("imported_at"),
        "measured_at": entry.get("measured_at"),
        "storage": "referenced",
        "has_thumbnail": bool(entry.get("thumbnail")),
        "status": status,
        "status_reason": reason,
    }


def list_public(project_id: str | None = None, *, own_only: bool = False) -> list[dict]:
    """The general library; with `project_id`, what that project sees (its
    own assets and the general ones), or with `own_only` its own alone."""
    if project_id is None:
        entries = read_entries()
    elif own_only:
        entries = read_entries(project_id)
    else:
        entries = visible_entries(project_id)
    entries.sort(key=lambda entry: entry.get("imported_at") or "", reverse=True)
    return [public_entry(entry) for entry in entries]


# --- changes ----------------------------------------------------------------------


def choose_and_import(project_id: str | None = None) -> dict:
    library_root(project_id)  # an unknown project fails before the dialog opens
    chosen = folders.choose_broll_file()
    if chosen["cancelled"]:
        return {"cancelled": True, "asset": None, "duplicate": False}
    return import_asset(chosen["path"], project_id)


def _all_ids() -> set[str]:
    """Ids in every library, so a new id is unique wherever the asset moves."""
    ids = {entry["id"] for entry in read_entries()}
    root = get_projects_root()
    for directory in root.glob("*/broll_library") if root.is_dir() else []:
        try:
            ids |= {entry["id"] for entry in read_entries(directory.parent.name)}
        except storage.ProjectError:
            continue
    return ids


def import_asset(raw_path: Any, project_id: str | None = None) -> dict:
    """Measure a file and add it to a library, referenced where it is: the
    general one, or with `project_id` that project's own.

    A file the project can already see (in its library or the general one)
    is reported as a duplicate rather than imported twice.
    """
    path = validate_asset_path(raw_path)
    media_type = media_type_for(path)
    fingerprint = current_fingerprint(path)
    if fingerprint is None:
        raise LibraryError("The file cannot be read: %s" % path)

    with _lock:
        for entry in visible_entries(project_id):
            same_path = os.path.normcase(entry["path"]) == os.path.normcase(path)
            if same_path or entry["fingerprint"].get("digest") == fingerprint["digest"]:
                return {"cancelled": False, "asset": public_entry(entry), "duplicate": True}
        if len(read_entries(project_id)) >= MAX_ASSETS:
            raise LibraryError("The library holds up to %d assets." % MAX_ASSETS)

    measured = measure(path, media_type)
    with _lock:
        taken = _all_ids()
    asset_id = uuid.uuid4().hex[:12]
    while asset_id in taken:
        asset_id = uuid.uuid4().hex[:12]
    entry = {
        "id": asset_id,
        "media_type": media_type,
        "path": path,
        "filename": os.path.basename(path),
        "display_name": None,
        "description": None,
        "tags": [],
        "imported_at": _now(),
        "measured_at": _now(),
        "measured": measured,
        "fingerprint": fingerprint,
        "thumbnail": make_thumbnail(path, media_type, measured, asset_id, fingerprint["digest"], project_id),
    }
    with _lock:
        document = _read_document(project_id)
        document["assets"].append(_locate(entry, project_id))
        _write_document(document, project_id)
    return {"cancelled": False, "asset": public_entry(entry), "duplicate": False}


def _clean_text(raw: Any, limit: int, what: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise LibraryError("%s must be text." % what)
    text = unicodedata.normalize("NFC", raw).strip()
    if any(ord(character) < 32 and character not in "\n\t" for character in text):
        raise LibraryError("%s contains invalid characters." % what)
    if len(text) > limit:
        raise LibraryError("%s is too long (up to %d characters)." % (what, limit))
    return text or None


def _clean_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise LibraryError("Tags must be a list.")
    tags: list[str] = []
    for item in raw:
        tag = _clean_text(item, MAX_TAG_LENGTH, "A tag")
        if tag and "\n" not in tag and tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise LibraryError("Up to %d tags." % MAX_TAGS)
    return tags


def _finite_seconds(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")


def _clean_moments(raw: Any, entry: dict) -> list[dict]:
    """User-described stretches inside a video asset, checked against its length."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise LibraryError("Moments must be a list.")
    if raw and entry["media_type"] != MEDIA_VIDEO:
        raise LibraryError("Only a video has moments; describe an image as a whole.")
    if len(raw) > MAX_MOMENTS:
        raise LibraryError("Up to %d described moments." % MAX_MOMENTS)
    duration = float(entry["measured"].get("duration_seconds") or 0.0)
    moments = []
    for number, item in enumerate(raw, start=1):
        if not isinstance(item, dict) or set(item) - {"start", "end", "description"}:
            raise LibraryError("Moment %d must have a start, an end and a description." % number)
        start, end = item.get("start"), item.get("end")
        if not _finite_seconds(start) or not _finite_seconds(end):
            raise LibraryError("Moment %d: start and end must be numbers of seconds." % number)
        start, end = float(start), float(end)
        if start < 0 or end > duration + 0.001 or end - start < MIN_MOMENT_SECONDS:
            raise LibraryError(
                "Moment %d (%.2f–%.2f s) must lie inside the video (0–%.2f s) and last at least %.1f s."
                % (number, start, end, duration, MIN_MOMENT_SECONDS)
            )
        description = _clean_text(item.get("description"), MAX_MOMENT_DESCRIPTION, "Moment %d's description" % number)
        if not description:
            raise LibraryError("Moment %d needs a description of what it shows." % number)
        moments.append({"start": round(start, 3), "end": round(min(end, duration), 3), "description": description})
    moments.sort(key=lambda moment: (moment["start"], moment["end"]))
    return moments


def valid_moments(entry: dict) -> list[dict]:
    """Stored moments that still fit the asset as measured now (a re-measured,
    shorter file drops the ones past its end rather than inventing new times)."""
    if entry["media_type"] != MEDIA_VIDEO:
        return []
    duration = float(entry["measured"].get("duration_seconds") or 0.0)
    return [
        dict(moment) for moment in entry.get("moments") or []
        if isinstance(moment, dict) and _finite_seconds(moment.get("start")) and _finite_seconds(moment.get("end"))
        and 0 <= moment["start"] < moment["end"] <= duration + 0.001 and isinstance(moment.get("description"), str)
    ]


def is_described(entry: dict) -> bool:
    """Whether the user said what the asset shows: a description, tags or moments.

    A name alone is not enough — it is often a file name retyped — and nothing
    measured counts. Undescribed assets stay usable by hand; the automatic
    planner does not choose them.
    """
    return bool(entry.get("description") or entry.get("tags") or valid_moments(entry))


def update_details(asset_id: Any, changes: dict, project_id: str | None = None) -> dict:
    """The user's own words: display name, description, tags, moments. Nothing measured."""
    allowed = {"display_name", "description", "tags", "moments"}
    unknown = set(changes) - allowed
    if unknown:
        raise LibraryError("Only the name, description, tags and moments can be edited.")
    with _lock:
        document, entry, owner = _find(asset_id, project_id)
        if "display_name" in changes:
            name = _clean_text(changes["display_name"], MAX_DISPLAY_NAME, "The name")
            entry["display_name"] = name.replace("\n", " ") if name else None
        if "description" in changes:
            entry["description"] = _clean_text(changes["description"], MAX_DESCRIPTION, "The description")
        if "tags" in changes:
            entry["tags"] = _clean_tags(changes["tags"])
        if "moments" in changes:
            entry["moments"] = _clean_moments(changes["moments"], entry)
        _write_document(document, owner)
        return public_entry(entry)


def remeasure(asset_id: Any, project_id: str | None = None) -> dict:
    """Measure a changed file again. Plans that used it must be reviewed again."""
    with _lock:
        _document, entry, owner = _find(asset_id, project_id)
    path = entry["path"]
    fingerprint = current_fingerprint(path)
    if fingerprint is None:
        raise LibraryError("The file is missing: %s" % path, status_code=409)
    measured = measure(path, entry["media_type"])
    thumbnail = make_thumbnail(path, entry["media_type"], measured, entry["id"], fingerprint["digest"], owner)
    with _lock:
        document = _read_document(owner)
        stored = next((item for item in document["assets"] if item["id"] == entry["id"]), None)
        if stored is None:
            raise LibraryError("That B-roll asset is not in the library.", status_code=404)
        if stored.get("thumbnail") != thumbnail:
            _remove_thumbnail(stored.get("thumbnail"), owner)
        stored.update(measured=measured, fingerprint=fingerprint, measured_at=_now(), thumbnail=thumbnail)
        _write_document(document, owner)
        return public_entry(stored)


def remove(asset_id: Any, project_id: str | None = None) -> dict:
    """Remove the library entry only. The file on disk is never touched."""
    with _lock:
        document, entry, owner = _find(asset_id, project_id)
        document["assets"] = [item for item in document["assets"] if item["id"] != entry["id"]]
        _write_document(document, owner)
    _remove_thumbnail(entry.get("thumbnail"), owner)
    return {"removed": entry["id"]}


def move(asset_id: Any, project_id: str, to: Any) -> dict:
    """Move an asset between the general library and `project_id`'s own.

    `to="general"` shares one of the project's assets with every project;
    `to="project"` makes a general asset this project's alone — other
    projects' plans that used it then report the asset as gone. The id stays
    the same, so this project's plans keep working either way.
    """
    if to not in (LIBRARY_GENERAL, LIBRARY_PROJECT):
        raise LibraryError("An asset moves to the general library or to a video's own.")
    target = None if to == LIBRARY_GENERAL else project_id
    library_root(project_id)
    with _lock:
        source_document, entry, owner = _find(asset_id, project_id)
        if owner == target:
            return public_entry(entry)
        target_document = _read_document(target)
        if len(target_document["assets"]) >= MAX_ASSETS:
            raise LibraryError("The library holds up to %d assets." % MAX_ASSETS)
        for other in target_document["assets"]:
            if other["fingerprint"].get("digest") == entry["fingerprint"].get("digest"):
                raise LibraryError(
                    "The same file is already there as \"%s\"." % public_entry(other)["display_name"], status_code=409,
                )
        moved = {key: value for key, value in entry.items() if key not in _LOCATION_KEYS}
        moved["thumbnail"] = _move_thumbnail(entry.get("thumbnail"), owner, target)
        target_document["assets"].append(_locate(moved, target))
        # Written to its new library first: if the second write fails, the
        # asset is in both rather than in neither.
        _write_document(target_document, target)
        source_document["assets"] = [item for item in source_document["assets"] if item["id"] != entry["id"]]
        _write_document(source_document, owner)
        return public_entry(moved)


# --- serving -----------------------------------------------------------------------

CONTENT_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".mkv": "video/x-matroska",
}


def media_file(asset_id: Any, project_id: str | None = None) -> tuple[Path, str]:
    entry = read_entry(asset_id, project_id)
    path = Path(entry["path"])
    if not path.is_file():
        raise LibraryError("The asset's file is missing.", status_code=404)
    return path, CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def thumbnail_file(asset_id: Any, project_id: str | None = None) -> Path:
    entry = read_entry(asset_id, project_id)
    name = entry.get("thumbnail")
    if not isinstance(name, str):
        raise LibraryError("This asset has no thumbnail.", status_code=404)
    directory = thumbnails_directory(entry["project_id"]).resolve()
    path = (directory / name).resolve()
    if not path.is_relative_to(directory) or not path.is_file():
        raise LibraryError("This asset has no thumbnail.", status_code=404)
    return path


# --- the model-facing view -----------------------------------------------------------


def catalog_entry(entry: dict) -> dict:
    """What a future planner may know about an asset: no path, no filename.

    The filename is left out on purpose: it is neither a description nor
    safe to send anywhere, and a model must not read meaning into it. The
    user's own words are passed on and labelled as such; their absence is
    stated, not filled in.
    """
    status, _ = status_of(entry)
    measured = entry["measured"]
    has_description = bool(entry.get("description"))
    return {
        "asset_id": entry["id"],
        "media_type": entry["media_type"],
        "usable": status == STATUS_AVAILABLE,
        "status": status,
        "user_label": entry.get("display_name"),
        "user_description": entry.get("description"),
        "user_tags": list(entry.get("tags") or []),
        "user_moments": valid_moments(entry),
        "semantic_description": "user_provided" if has_description else "none",
        "width": measured["width"],
        "height": measured["height"],
        "aspect_ratio": measured["aspect_ratio"],
        "orientation": measured["orientation"],
        "duration_seconds": measured.get("duration_seconds"),
        "has_audio": bool(measured.get("has_audio")),
    }


CATALOG_NOTE = (
    "Technical fields (size, aspect ratio, duration, audio) are measured from the file "
    "and say nothing about what an asset shows. Only user_label, user_description, "
    "user_tags and user_moments were written by a person; when semantic_description is "
    "\"none\", nothing is known about the asset's content and none may be assumed."
)
