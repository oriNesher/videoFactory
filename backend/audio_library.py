"""The audio library: music tracks shared by every video (stage 4).

One general library, like the B-roll library's general half, and referenced
the same way — a file is measured where it is and never copied or modified:

    workspace/library/audio/library.json

    {"schema_version": 1, "kind": "video_factory.audio_library",
     "assets": [{"id": "<12 hex>", "kind": "music", "path": "C:\\Music\\calm.mp3",
                 "filename": "calm.mp3", "display_name": null,
                 "imported_at": "...", "measured_at": "...",
                 "measured": {"duration_seconds": 182.4, "codec": "mp3",
                              "sample_rate": 44100, "channels": 2},
                 "fingerprint": {...}}]}

`kind` is "music" today. Sound effects (a later stage) are the same kind of
entry with `kind: "sfx"`, so they can live here without a second library.

The status of an entry (available, missing, changed) is derived on every read,
exactly like the B-roll library: a re-recorded or moved file is noticed, and a
music decision that named its old fingerprint becomes outdated by itself.
"""

import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import caption_inputs, folders, media, storage
from .config import get_workspace_root

LIBRARY_SCHEMA_VERSION = 1
LIBRARY_KIND = "video_factory.audio_library"

KIND_MUSIC = "music"
KINDS = (KIND_MUSIC,)  # "sfx" joins when sound effects do

EXTENSIONS = set(folders.AUDIO_EXTENSIONS)

STATUS_AVAILABLE = "available"
STATUS_MISSING = "missing"
STATUS_CHANGED = "changed"

MAX_ASSETS = 1000
MAX_DISPLAY_NAME = 120
# Shorter than this is a sound, not a music bed.
MIN_DURATION_SECONDS = 1.0

_ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")
_lock = threading.RLock()

CONTENT_TYPES = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".ogg": "audio/ogg", ".opus": "audio/ogg", ".flac": "audio/flac",
}


class AudioLibraryError(storage.ProjectError):
    """An audio library problem the user can act on."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def library_file() -> Path:
    return get_workspace_root() / "library" / "audio" / "library.json"


def validate_asset_id(raw: Any) -> str:
    if not isinstance(raw, str) or not _ID_PATTERN.match(raw):
        raise AudioLibraryError("That track is not in the music library.", status_code=404)
    return raw


def validate_kind(raw: Any) -> str:
    if raw not in KINDS:
        raise AudioLibraryError("Unknown audio kind. Choose one of: %s." % ", ".join(KINDS))
    return raw


# --- the document -----------------------------------------------------------------


def _well_formed(entry: Any) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("id"), str)
        and _ID_PATTERN.match(entry["id"]) is not None
        and isinstance(entry.get("path"), str)
        and entry.get("kind") in KINDS
        and isinstance(entry.get("measured"), dict)
        and isinstance(entry.get("fingerprint"), dict)
    )


def _read_document() -> dict:
    try:
        document = storage.read_json(library_file())
    except FileNotFoundError:
        document = {"schema_version": LIBRARY_SCHEMA_VERSION, "kind": LIBRARY_KIND, "assets": []}
    except (OSError, ValueError) as error:
        raise AudioLibraryError("The music library file cannot be read: %s" % error, status_code=422) from error
    if not isinstance(document, dict) or document.get("kind") != LIBRARY_KIND:
        raise AudioLibraryError("The music library file is not a library.", status_code=422)
    if (document.get("schema_version") or 0) > LIBRARY_SCHEMA_VERSION:
        raise AudioLibraryError("The music library was written by a newer version.", status_code=422)
    assets = document.get("assets")
    document["assets"] = [entry for entry in assets if _well_formed(entry)] if isinstance(assets, list) else []
    return document


def _write_document(document: dict) -> None:
    try:
        storage.write_json_atomic(library_file(), document)
    except OSError as error:
        raise AudioLibraryError("Saving the music library failed: %s" % error, status_code=500) from error


def read_entries() -> list[dict]:
    """Raw entries, paths included. For the backend only."""
    with _lock:
        return _read_document()["assets"]


def read_entry(asset_id: Any) -> dict:
    asset_id = validate_asset_id(asset_id)
    entry = next((item for item in read_entries() if item["id"] == asset_id), None)
    if entry is None:
        raise AudioLibraryError("That track is not in the music library.", status_code=404)
    return entry


def find_entry(asset_id: Any) -> dict | None:
    try:
        return read_entry(asset_id)
    except AudioLibraryError:
        return None


# --- measuring ----------------------------------------------------------------------


def validate_asset_path(raw: Any) -> str:
    """An absolute path to an existing audio file."""
    if not isinstance(raw, str):
        raise AudioLibraryError("The file path must be text.")
    text = raw.strip()
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        text = text[1:-1].strip()
    if not text:
        raise AudioLibraryError("Enter a file path.")
    if len(text) > storage.MAX_PATH_LENGTH or "\x00" in text:
        raise AudioLibraryError("The file path is not valid.")
    if not Path(text).is_absolute():
        raise AudioLibraryError("Enter a full path, for example C:\\Music\\calm.mp3")
    if Path(text).suffix.lower() not in EXTENSIONS:
        raise AudioLibraryError("Unsupported file type. Audio files are accepted: %s" % ", ".join(sorted(EXTENSIONS)))
    path = os.path.normpath(text)
    if not os.path.isfile(path):
        raise AudioLibraryError("File not found: %s" % path)
    return path


def measure(path: str) -> dict:
    """Technical metadata only, read from the file with FFprobe."""
    try:
        described = media.probe(path)
    except media.MediaError as error:
        raise AudioLibraryError("The file cannot be read as audio: %s" % error.message) from error
    audio = described.get("audio")
    duration = described.get("duration_seconds")
    if audio is None:
        raise AudioLibraryError("The file has no audio track: %s" % described["filename"])
    if duration is None or duration < MIN_DURATION_SECONDS:
        raise AudioLibraryError("The track is too short to use as background music (at least %.0f s)." % MIN_DURATION_SECONDS)
    return {
        "duration_seconds": round(float(duration), 3),
        "codec": audio.get("codec_name") or None,
        "sample_rate": audio.get("sample_rate"),
        "channels": audio.get("channels"),
    }


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
        return STATUS_MISSING, "The file is no longer at %s. Move it back, or remove this track." % path
    current = current_fingerprint(path)
    if current is None or current["digest"] != entry["fingerprint"].get("digest"):
        return STATUS_CHANGED, "The file has changed since it was measured. Press Re-measure before using it."
    return STATUS_AVAILABLE, None


def public_entry(entry: dict) -> dict:
    """An entry for the interface: no path, plus its derived status."""
    status, reason = status_of(entry)
    return {
        "id": entry["id"],
        "kind": entry["kind"],
        "filename": entry.get("filename") or os.path.basename(entry["path"]),
        "display_name": entry.get("display_name") or entry.get("filename") or entry["id"],
        "measured": entry["measured"],
        "fingerprint": entry["fingerprint"],
        "imported_at": entry.get("imported_at"),
        "storage": "referenced",
        "status": status,
        "status_reason": reason,
    }


def list_public(kind: str | None = None) -> list[dict]:
    entries = [entry for entry in read_entries() if kind is None or entry["kind"] == kind]
    entries.sort(key=lambda entry: entry.get("imported_at") or "", reverse=True)
    return [public_entry(entry) for entry in entries]


# --- changes -------------------------------------------------------------------------


def choose_and_import(kind: Any = KIND_MUSIC) -> dict:
    kind = validate_kind(kind)
    chosen = folders.choose_audio_file()
    if chosen["cancelled"]:
        return {"cancelled": True, "asset": None, "duplicate": False}
    return import_asset(chosen["path"], kind)


def import_asset(raw_path: Any, kind: Any = KIND_MUSIC) -> dict:
    """Measure a file and add it, referenced where it is. A file already in
    the library (same path or same bytes) is reported as a duplicate."""
    kind = validate_kind(kind)
    path = validate_asset_path(raw_path)
    fingerprint = current_fingerprint(path)
    if fingerprint is None:
        raise AudioLibraryError("The file cannot be read: %s" % path)
    with _lock:
        for entry in read_entries():
            same_path = os.path.normcase(entry["path"]) == os.path.normcase(path)
            if same_path or entry["fingerprint"].get("digest") == fingerprint["digest"]:
                return {"cancelled": False, "asset": public_entry(entry), "duplicate": True}
        if len(read_entries()) >= MAX_ASSETS:
            raise AudioLibraryError("The music library holds up to %d tracks." % MAX_ASSETS)

    measured = measure(path)
    with _lock:
        document = _read_document()
        taken = {entry["id"] for entry in document["assets"]}
        asset_id = uuid.uuid4().hex[:12]
        while asset_id in taken:
            asset_id = uuid.uuid4().hex[:12]
        entry = {
            "id": asset_id,
            "kind": kind,
            "path": path,
            "filename": os.path.basename(path),
            "display_name": None,
            "imported_at": _now(),
            "measured_at": _now(),
            "measured": measured,
            "fingerprint": fingerprint,
        }
        document["assets"].append(entry)
        _write_document(document)
    return {"cancelled": False, "asset": public_entry(entry), "duplicate": False}


def rename(asset_id: Any, display_name: Any) -> dict:
    asset_id = validate_asset_id(asset_id)
    if display_name is not None and not isinstance(display_name, str):
        raise AudioLibraryError("The name must be text.")
    name = " ".join((display_name or "").split())
    if len(name) > MAX_DISPLAY_NAME:
        raise AudioLibraryError("The name is too long (at most %d characters)." % MAX_DISPLAY_NAME)
    with _lock:
        document = _read_document()
        entry = next((item for item in document["assets"] if item["id"] == asset_id), None)
        if entry is None:
            raise AudioLibraryError("That track is not in the music library.", status_code=404)
        entry["display_name"] = name or None
        _write_document(document)
        return public_entry(entry)


def remeasure(asset_id: Any) -> dict:
    """Measure a changed file again. A music decision that used it must be reviewed again."""
    entry = read_entry(asset_id)
    fingerprint = current_fingerprint(entry["path"])
    if fingerprint is None:
        raise AudioLibraryError("The file is missing: %s" % entry["path"], status_code=409)
    measured = measure(entry["path"])
    with _lock:
        document = _read_document()
        stored = next((item for item in document["assets"] if item["id"] == entry["id"]), None)
        if stored is None:
            raise AudioLibraryError("That track is not in the music library.", status_code=404)
        stored.update(measured=measured, fingerprint=fingerprint, measured_at=_now())
        _write_document(document)
        return public_entry(stored)


def remove(asset_id: Any) -> dict:
    """Remove the library entry only. The file on disk is never touched."""
    asset_id = validate_asset_id(asset_id)
    with _lock:
        document = _read_document()
        if not any(item["id"] == asset_id for item in document["assets"]):
            raise AudioLibraryError("That track is not in the music library.", status_code=404)
        document["assets"] = [item for item in document["assets"] if item["id"] != asset_id]
        _write_document(document)
    return {"removed": asset_id}


def media_file(asset_id: Any) -> tuple[Path, str]:
    entry = read_entry(asset_id)
    path = Path(entry["path"])
    if not path.is_file():
        raise AudioLibraryError("The track's file is missing.", status_code=404)
    return path, CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
