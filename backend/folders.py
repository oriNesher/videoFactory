"""The native folder picker, and scanning a chosen folder for video files.

A browser never hands a web page an absolute disk path: `<input webkitdirectory>`
gives file *contents* and relative names, which would mean uploading gigabytes of
footage into the workspace. This application deliberately leaves footage where it
was recorded and only references it by path, so the picker runs on the backend —
the same machine as the browser — and opens the real Windows folder dialog
through Tk. What comes back is a true absolute path, and nothing is copied.

The dialog is modal to the machine, not to the request: only one may be open at
a time, and a second attempt is refused rather than queued behind it.
"""

import os
import re
import threading
from pathlib import Path
from typing import Any

from . import storage

# One dialog at a time. `blocking=False` so a second request fails fast with a
# message instead of hanging until the first is answered.
_dialog_lock = threading.Lock()

DIALOG_TITLE = "Choose a folder of video clips"

# Scanning is shallow and bounded: a folder of takes, not a drive.
MAX_FILES_PER_DIRECTORY = 500


class FolderError(storage.ProjectError):
    """A problem choosing or reading a folder."""


class DialogUnavailable(FolderError):
    """Tk is missing or no desktop session is available to show a dialog."""

    status_code = 503


def _ask_for_directory() -> str | None:
    """Open the native folder dialog. Returns the path, or None if cancelled.

    Tk is created and destroyed inside this call. It runs on a thread of its
    own (see `choose_directory`) because a Tk interpreter must live entirely on
    one thread, and the server's worker threads are pooled and reused.
    """
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    try:
        root.withdraw()
        # Without this the dialog can open behind the browser window, which
        # looks exactly like the button doing nothing.
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(title=DIALOG_TITLE, mustexist=True)
    finally:
        root.destroy()

    return chosen or None


def choose_directory() -> dict:
    """Show the folder dialog and report what the user picked."""
    if not _dialog_lock.acquire(blocking=False):
        raise FolderError(
            "A folder dialog is already open. Finish or cancel it first.",
            status_code=409,
        )

    result: dict[str, Any] = {}

    def run() -> None:
        try:
            result["path"] = _ask_for_directory()
        except Exception as error:  # noqa: BLE001 - reported, never raised here
            result["error"] = error

    try:
        # A fresh thread per dialog: a Tk interpreter cannot be created twice on
        # the same pooled thread reliably, and this one dies with the dialog.
        thread = threading.Thread(target=run, name="video-factory-folder-dialog")
        thread.start()
        thread.join()
    finally:
        _dialog_lock.release()

    error = result.get("error")
    if isinstance(error, ImportError):
        raise DialogUnavailable(
            "The folder dialog is not available on this machine (Tk is missing). "
            "Paste the folder path instead."
        ) from error
    if error is not None:
        raise DialogUnavailable(
            "The folder dialog could not be opened: %s. Paste the folder path "
            "instead." % error
        ) from error

    path = result.get("path")
    if path is None:
        return {"cancelled": True, "path": None}

    return {"cancelled": False, "path": os.path.normpath(path)}


def validate_directory(raw_path: Any) -> str:
    """Normalise and check a path that is meant to be an existing folder."""
    if not isinstance(raw_path, str):
        raise FolderError("The folder path must be text.")

    path_text = raw_path.strip()
    # Windows Explorer's "Copy as path" wraps the path in double quotes.
    if len(path_text) >= 2 and path_text.startswith('"') and path_text.endswith('"'):
        path_text = path_text[1:-1].strip()

    if not path_text:
        raise FolderError("Choose a folder first.")
    if len(path_text) > storage.MAX_PATH_LENGTH:
        raise FolderError("The folder path is too long.")
    if "\x00" in path_text:
        raise FolderError("The folder path contains invalid characters.")
    if not Path(path_text).is_absolute():
        raise FolderError("Enter a full path, for example C:\\Videos\\shoot-01")

    normalised = os.path.normpath(path_text)

    if not os.path.exists(normalised):
        raise FolderError("Folder not found: " + normalised)
    if not os.path.isdir(normalised):
        raise FolderError("That path is not a folder: " + normalised)

    return normalised


def _name_order(filename: str) -> list:
    """Sort key matching what Explorer shows, so takes cut in the order seen.

    Runs of digits compare as numbers, which is the whole point: a plain string
    sort puts `take10` before `take2`, and the join order would silently be
    wrong. Case is ignored so `Take1` and `take2` do not separate into groups.

    Each part is tagged before it is compared, because `1.mp4` and `take1.mp4`
    line a number up against text otherwise, and that raises rather than sorts.
    """
    return [
        (0, int(part), "") if part.isdigit() else (1, 0, part.lower())
        for part in re.split(r"(\d+)", filename)
        if part
    ]


def scan_directory(directory: str) -> dict:
    """List the video files directly inside `directory`, sorted by name.

    Only the top level is read: a folder of takes is what this is for, and
    recursing would quietly pull in exports, proxies and backups. Anything that
    is not a video file is reported as ignored rather than treated as an error —
    thumbnails, notes and project files live happily beside footage.
    """
    try:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
    except OSError as error:
        raise FolderError("The folder could not be read: %s" % error) from error

    videos: list[str] = []
    ignored: list[str] = []

    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue

        if Path(entry.name).suffix.lower() in storage.VIDEO_EXTENSIONS:
            videos.append(os.path.normpath(entry.path))
        else:
            ignored.append(entry.name)

    videos.sort(key=lambda path: _name_order(os.path.basename(path)))

    if len(videos) > MAX_FILES_PER_DIRECTORY:
        raise FolderError(
            "That folder holds %d video files; up to %d can be added at once. "
            "Split it into smaller folders."
            % (len(videos), MAX_FILES_PER_DIRECTORY)
        )

    return {"directory": directory, "videos": videos, "ignored": ignored}


def add_directory(project_id: str, raw_path: Any) -> dict:
    """Add every video file in a folder to the project, ordered by name.

    Partial success is the normal outcome and is reported, not raised: a folder
    is very often added twice, or holds one file that has gone unreadable. What
    could be added is added, and the rest comes back as a per-file reason.
    """
    directory = validate_directory(raw_path)
    scan = scan_directory(directory)

    if not scan["videos"]:
        raise FolderError(
            "No video files were found in %s. Supported extensions: %s."
            % (directory, ", ".join(sorted(storage.VIDEO_EXTENSIONS)))
        )

    # One write for the whole folder: adding twelve takes should leave one
    # entry in the project's history, not twelve.
    outcome = storage.add_sources(project_id, scan["videos"])

    return {
        "project": outcome["project"],
        "directory": directory,
        "added": outcome["added"],
        "duplicates": outcome["duplicates"],
        "ignored": scan["ignored"],
        "failed": outcome["failed"],
    }
