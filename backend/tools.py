"""Availability and version checks for the external processing tools.

These are the checks milestone 0A exposed through `/tools` and `/tools/versions`,
moved here unchanged so that the HTTP endpoints and the `tool_check` job run the
exact same code instead of two drifting copies.
"""

import shutil
import subprocess

TOOL_LABELS: dict[str, str] = {
    "ffmpeg": "FFmpeg",
    "ffprobe": "FFprobe",
    "auto_editor": "Auto-Editor",
}

EXECUTABLES: dict[str, str] = {
    "ffmpeg": "ffmpeg.exe",
    "ffprobe": "ffprobe.exe",
    "auto_editor": "auto-editor.exe",
}

VERSION_COMMANDS: dict[str, list[str]] = {
    "ffmpeg": ["ffmpeg.exe", "-version"],
    "ffprobe": ["ffprobe.exe", "-version"],
    "auto_editor": ["auto-editor.exe", "--version"],
}

TOOL_NAMES: tuple[str, ...] = tuple(EXECUTABLES)

VERSION_TIMEOUT_SECONDS = 10


def check_availability(name: str) -> dict:
    """Is the executable reachable through PATH?"""
    path = shutil.which(EXECUTABLES[name])
    return {"available": path is not None, "path": path}


def check_version(name: str) -> dict:
    """Run the tool's version command. Never raises: failures are reported."""
    try:
        result = subprocess.run(
            VERSION_COMMANDS[name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=VERSION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"working": False, "error": str(error)}

    output = (result.stdout or result.stderr).strip()

    return {
        "working": result.returncode == 0,
        "version": output.splitlines()[0] if output else "",
        "exit_code": result.returncode,
    }


def check_tool(name: str) -> dict:
    """One tool: availability plus, when found, its reported version."""
    status = check_availability(name)
    version = check_version(name) if status["available"] else {"working": False}

    return {
        "tool": name,
        "label": TOOL_LABELS.get(name, name),
        "available": status["available"],
        "path": status["path"],
        "working": version.get("working", False),
        "version": version.get("version", ""),
        "error": version.get("error"),
    }


def availability_report() -> dict:
    """The `/tools` payload."""
    return {name: check_availability(name) for name in TOOL_NAMES}


def version_report() -> dict:
    """The `/tools/versions` payload."""
    return {name: check_version(name) for name in TOOL_NAMES}
