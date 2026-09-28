"""Opening a project's footage in VLC, and its folders in Explorer, on this machine.

The backend runs on the user's own computer, so it can start a desktop player
where the browser cannot. Only files already in the project are ever opened:
the request names a source id, never a path.
"""

import os
import shutil
import subprocess

from . import storage

VLC_LOCATIONS = (
    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
)


def find_vlc() -> str | None:
    found = shutil.which("vlc")
    if found:
        return found
    return next((path for path in VLC_LOCATIONS if os.path.isfile(path)), None)


def open_source(project_id: str, source_id: str) -> None:
    project = storage.read_project(project_id)
    source = next((s for s in project["sources"] if s["id"] == source_id), None)
    if source is None:
        raise storage.ProjectError("That file is not in this video.", 404)

    path = source["path"]
    if not os.path.isfile(path):
        raise storage.ProjectError("File not found: %s" % path, 404)

    vlc = find_vlc()
    if vlc is None:
        raise storage.ProjectError("VLC was not found. Install it from videolan.org.")

    try:
        launch([vlc, path])
    except OSError as error:
        raise storage.ProjectError("VLC could not be started: %s" % error) from error


def launch(command: list[str]) -> None:
    """Start the player detached: it outlives the request and is never waited on."""
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def open_folder(path: str) -> None:
    """Show a folder in the system file manager (Explorer on Windows)."""
    os.startfile(path)  # noqa: S606 - a directory this app wrote, never user input
