"""One long-lived Whisper process, so a model is not reloaded for every job.

Loading the Hebrew large-v3-turbo model takes seconds and over a gigabyte of
memory. The captions module therefore keeps a single worker process
(`backend/whisper_worker.py --serve`) alive after a transcription, keyed by
everything that decides how the model was loaded: model, device, compute type
and CPU threads. The next transcription with the same key reuses it; a
different key replaces it. After `IDLE_SECONDS` without work the process is
stopped and its memory returned.

Cancellation still kills the process tree — that is the only way to stop
CTranslate2 mid-segment — at the price of reloading the model next time.

The job queue runs one job at a time, so there is never more than one
request in flight; the lock is there for the idle timer and shutdown.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import processes

WORKER_SCRIPT = Path(__file__).with_name("whisper_worker.py")

IDLE_ENV_VAR = "VIDEO_FACTORY_WHISPER_IDLE_SECONDS"
DEFAULT_IDLE_SECONDS = 600.0

# How often the supervising loop checks for cancellation.
POLL_SECONDS = 0.15

# Retained worker output (stderr: Hugging Face and CTranslate2 warnings) for
# the log. Bounded so a chatty library cannot grow memory without limit.
MAX_LOG_CHARACTERS = 200_000


class WhisperCancelled(Exception):
    pass


class WhisperFailed(Exception):
    """`kind` is one of: model_missing, device, load, no_audio, failed, crashed."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        # The worker's stderr up to the failure, for the job's log file.
        self.log = ""


def idle_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get(IDLE_ENV_VAR, DEFAULT_IDLE_SECONDS)))
    except ValueError:
        return DEFAULT_IDLE_SECONDS


class _Worker:
    def __init__(self, key: tuple, command: list[str]) -> None:
        self.key = key
        self.command = command
        self.events: queue.Queue = queue.Queue()
        self.log: list[str] = []
        self._log_length = 0
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        self.ready = False
        self.load_seconds: float | None = None
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _remember(self, text: str) -> None:
        if self._log_length < MAX_LOG_CHARACTERS:
            self.log.append(text)
            self._log_length += len(text)

    def _read_stdout(self) -> None:
        stream = self.process.stdout
        assert stream is not None
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line.startswith("@@"):
                try:
                    self.events.put(json.loads(line[2:]))
                    continue
                except ValueError:
                    pass
            self._remember(line + "\n")
        self.events.put({"event": "_exit"})

    def _read_stderr(self) -> None:
        stream = self.process.stderr
        assert stream is not None
        for raw in iter(stream.readline, b""):
            self._remember(raw.decode("utf-8", errors="replace"))

    def alive(self) -> bool:
        return self.process.poll() is None

    def send(self, request: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def kill(self) -> None:
        processes.terminate_tree(self.process)

    def log_text(self) -> str:
        return "".join(self.log)


class WhisperService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._worker: _Worker | None = None
        self._idle_timer: threading.Timer | None = None
        self._python = sys.executable

    # -- lifecycle --

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _schedule_idle_stop(self) -> None:
        self._cancel_idle_timer()
        delay = idle_seconds()
        if delay <= 0:
            self.stop()
            return
        self._idle_timer = threading.Timer(delay, self.stop)
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def stop(self) -> None:
        with self._lock:
            self._cancel_idle_timer()
            worker, self._worker = self._worker, None
        if worker is not None:
            try:
                worker.send({"command": "exit"})
                worker.process.wait(timeout=3)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            worker.kill()

    def status(self) -> dict:
        with self._lock:
            worker = self._worker
            if worker is None or not worker.alive():
                return {"loaded": False, "model": None}
            return {
                "loaded": worker.ready,
                "model": worker.key[0],
                "device": worker.key[1],
                "compute_type": worker.key[2],
                "load_seconds": worker.load_seconds,
            }

    # -- work --

    def _command(self, key: tuple) -> list[str]:
        model, device, compute_type, cpu_threads = key
        return [
            self._python, "-u", str(WORKER_SCRIPT), "--serve",
            "--model", model,
            "--device", device,
            "--compute-type", compute_type,
            "--cpu-threads", str(cpu_threads),
        ]

    def transcribe(
        self,
        *,
        model: str,
        device: str,
        compute_type: str,
        cpu_threads: int,
        request: dict,
        cancelled: Callable[[], bool],
        on_event: Callable[[dict], None],
    ) -> dict:
        """Transcribe one file. Returns `{"output", "reused_model", "load_seconds", "log"}`.

        `on_event` receives `loading`, `ready`, `info` and `progress` events.
        Raises `WhisperCancelled` or `WhisperFailed`.
        """
        key = (model, device, compute_type, int(cpu_threads))

        with self._lock:
            self._cancel_idle_timer()
            worker = self._worker
            reused = worker is not None and worker.key == key and worker.alive() and worker.ready
            if not reused:
                if worker is not None:
                    worker.kill()
                try:
                    worker = _Worker(key, self._command(key))
                except OSError as error:
                    self._worker = None
                    raise WhisperFailed("load", "The Whisper worker could not be started: %s" % error) from error
                self._worker = worker

        request_id = request["id"]
        try:
            if reused:
                on_event({"event": "ready", "load_seconds": 0.0, "reused": True})
                # A fresh worker is sent the request when it reports `ready`.
                worker.send(request)

            while True:
                if cancelled():
                    raise WhisperCancelled()
                try:
                    event = worker.events.get(timeout=POLL_SECONDS)
                except queue.Empty:
                    continue

                kind = event.get("event")
                if kind == "loading":
                    on_event(event)
                elif kind == "ready":
                    worker.ready = True
                    worker.load_seconds = event.get("load_seconds")
                    on_event(event)
                    worker.send(request)
                elif kind == "fatal":
                    raise WhisperFailed(event.get("kind") or "load", event.get("message") or "")
                elif kind == "_exit":
                    raise WhisperFailed(
                        "crashed",
                        "The Whisper worker stopped unexpectedly (exit code %s)."
                        % worker.process.poll(),
                    )
                elif event.get("id") != request_id:
                    continue  # a stale event from an earlier request
                elif kind in ("info", "progress"):
                    on_event(event)
                elif kind == "error":
                    raise WhisperFailed(event.get("kind") or "failed", event.get("message") or "")
                elif kind == "done":
                    break
        except WhisperCancelled:
            self._discard(worker)
            raise
        except WhisperFailed as failure:
            log = worker.log_text()
            # A failed request on a healthy worker keeps the model loaded; a
            # failed load or a crash does not leave anything worth keeping.
            if failure.kind not in ("no_audio", "failed") or not worker.alive():
                self._discard(worker)
            else:
                with self._lock:
                    self._schedule_idle_stop()
            failure.log = log
            raise
        except (OSError, ValueError) as error:
            self._discard(worker)
            raise WhisperFailed("crashed", "The Whisper worker could not be reached: %s" % error) from error

        with self._lock:
            self._schedule_idle_stop()
        return {
            "output": request["output"],
            "reused_model": reused,
            "load_seconds": None if reused else worker.load_seconds,
            "log": worker.log_text(),
        }

    def _discard(self, worker: _Worker) -> None:
        worker.kill()
        with self._lock:
            if self._worker is worker:
                self._worker = None


service = WhisperService()
