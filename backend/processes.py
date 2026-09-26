"""Running external tools: argument lists, live output, real cancellation.

Three properties matter here and none of them are free on Windows.

**No shell.** Every command is an argument list handed straight to
`CreateProcess`. Nothing is ever interpolated into a command string, so a path
containing a space, a quote, an ampersand or Hebrew text is data, not syntax.
The frontend cannot influence the executable or the flags at all — it names
project resources, and the backend builds the command.

**Live output.** Auto-Editor writes machine-readable progress to stdout using
carriage returns rather than newlines, and writes errors to stderr. Both are
merged into one pipe and drained by a reader thread, so the process can never
block on a full pipe buffer while the job thread is checking for cancellation.

**Cancellation that actually stops work.** Auto-Editor spawns its own encoder
child processes. Killing only the process we started would leave those
rendering in the background, burning CPU and writing to a file the user
believes was abandoned — so the whole tree is killed with `taskkill /T`, and
the plain `kill()` is only the fallback for when that is unavailable.
"""

import os
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

# How often the supervising loop looks at the cancellation flag.
POLL_INTERVAL_SECONDS = 0.15

# How long a killed process is given to actually disappear.
KILL_GRACE_SECONDS = 5.0

# Retained output per process. Enough to explain any failure; bounded so a
# chatty tool cannot grow the job record without limit. The full log is written
# to the run directory separately.
MAX_CAPTURED_CHARACTERS = 200_000

# Windows-only flags; absent elsewhere, which keeps the tests portable.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


class ProcessCancelled(Exception):
    """The supervising job asked for cancellation and the tree was killed."""


class ProcessStartFailed(Exception):
    """The executable could not be launched at all."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ProcessResult:
    def __init__(self, exit_code: int, output: str, truncated: bool) -> None:
        self.exit_code = exit_code
        self.output = output
        self.truncated = truncated

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def last_lines(self, count: int = 12) -> str:
        """The tail of the output, for an error message a person will read."""
        lines = [line.strip() for line in self.output.replace("\r", "\n").splitlines()]
        meaningful = [line for line in lines if line]
        return "\n".join(meaningful[-count:])


def terminate_tree(process: subprocess.Popen) -> None:
    """Kill the process and everything it started.

    `taskkill /T` is the only reliable way on Windows to reach grandchildren:
    Auto-Editor's encoder is a separate process, and stopping the parent alone
    leaves it running.
    """
    if process.poll() is not None:
        return

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill.exe", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=KILL_GRACE_SECONDS,
                creationflags=_CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass

    try:
        process.wait(timeout=KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover - the OS gave up too
        pass


def run(
    argv: list[str],
    *,
    cancelled: Callable[[], bool] | None = None,
    on_output: Callable[[str], None] | None = None,
    cwd: str | None = None,
    timeout_seconds: float | None = None,
) -> ProcessResult:
    """Run `argv` to completion, streaming its output.

    `on_output` is called with each raw chunk as it arrives — it must not
    raise. `cancelled` is polled between chunks; when it becomes true the whole
    process tree is killed and `ProcessCancelled` is raised, so the caller
    cannot mistake an abandoned run for a finished one.
    """
    if not argv or not isinstance(argv[0], str):
        raise ProcessStartFailed("פקודת הרצה לא תקינה.")

    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            creationflags=_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
        )
    except FileNotFoundError as error:
        raise ProcessStartFailed(
            'הכלי "%s" לא נמצא ב־PATH. בדוק את לשונית "כלים".' % argv[0]
        ) from error
    except OSError as error:
        raise ProcessStartFailed(
            'לא ניתן להריץ את "%s": %s' % (argv[0], error)
        ) from error

    captured: list[str] = []
    captured_length = 0
    truncated = False
    lock = threading.Lock()

    def drain() -> None:
        """Read until EOF in a thread so the pipe can never fill up."""
        nonlocal captured_length, truncated
        stream = process.stdout
        if stream is None:  # pragma: no cover - PIPE was requested
            return

        while True:
            raw = stream.read(4096)
            if not raw:
                break

            text = raw.decode("utf-8", errors="replace")

            with lock:
                if captured_length < MAX_CAPTURED_CHARACTERS:
                    captured.append(text)
                    captured_length += len(text)
                else:
                    truncated = True

            if on_output is not None:
                try:
                    on_output(text)
                except Exception:  # noqa: BLE001 - progress must never kill a run
                    pass

        try:
            stream.close()
        except OSError:  # pragma: no cover
            pass

    reader = threading.Thread(target=drain, name="process-output", daemon=True)
    reader.start()

    started = time.monotonic()
    was_cancelled = False
    timed_out = False

    while process.poll() is None:
        if cancelled is not None and cancelled():
            was_cancelled = True
            terminate_tree(process)
            break
        if timeout_seconds is not None and time.monotonic() - started > timeout_seconds:
            timed_out = True
            terminate_tree(process)
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    # The reader owns the pipe; waiting for it guarantees the captured output is
    # complete before anyone reads it.
    reader.join(timeout=KILL_GRACE_SECONDS)
    exit_code = process.wait()

    with lock:
        output = "".join(captured)
        output_truncated = truncated

    if was_cancelled:
        raise ProcessCancelled()

    if timed_out:
        raise ProcessStartFailed(
            'ההרצה של "%s" נמשכה מעבר למגבלת הזמן ונעצרה.' % argv[0]
        )

    return ProcessResult(exit_code, output, output_truncated)


def describe_command(argv: list[str]) -> str:
    """A readable rendering of an argument list, for logs and the manifest.

    Display only. Nothing ever parses this back into a command — the list is
    always the real thing.
    """
    parts = []
    for argument in argv:
        text = str(argument)
        parts.append('"%s"' % text if " " in text or "\t" in text else text)
    return " ".join(parts)


def tool_version(argv: list[str]) -> str | None:
    """First line of a tool's version output, or None if it cannot be read."""
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    output: Any = (result.stdout or result.stderr or "").strip()
    return output.splitlines()[0] if output else None
