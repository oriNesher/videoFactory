"""A small application-managed job queue.

Long operations must not run inside an HTTP request handler, but this is a
single-user local application: a database, a broker and a separate worker
process would all be infrastructure the user has to install and keep running.
So the queue is one background thread inside the backend process, executing one
job at a time, with every job record persisted as its own JSON file:

    <workspace>/projects/<project_id>/jobs/<job_id>.json

**Disk is the source of truth.** Memory holds only the pending queue and the
cancellation flags of jobs that are actually running, so listing and inspecting
a job always reflects what survived a restart. On startup, any record still
marked queued or running belongs to a process that is gone: it is marked
`interrupted` rather than replayed, and the user decides whether to retry.

The single worker is deliberate for this milestone. It also means the queue
assumes a single backend process; two `uvicorn` processes over one workspace
would each run their own worker.
"""

import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Queue
from typing import Any

from . import storage
from .config import get_projects_root

JOB_SCHEMA_VERSION = 1

JOBS_DIRECTORY = "jobs"

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"

ACTIVE_STATUSES = frozenset({QUEUED, RUNNING})
FINISHED_STATUSES = frozenset({SUCCEEDED, FAILED, CANCELLED, INTERRUPTED})

_JOB_ID_PATTERN_LENGTH = 32

# How long `stop()` waits for the running job to notice cancellation.
SHUTDOWN_TIMEOUT_SECONDS = 5.0


class JobError(storage.ProjectError):
    """A job problem the user can act on."""


class JobNotFound(JobError):
    status_code = 404


class JobConflict(JobError):
    status_code = 409


class JobCancelled(Exception):
    """Raised inside a handler when the user asked to cancel."""


class JobFailed(Exception):
    """A handler failure with a message meant for the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


_clock_lock = threading.Lock()
_last_timestamp = datetime.min.replace(tzinfo=timezone.utc)


def _now() -> str:
    """A strictly increasing timestamp.

    The listing is ordered by `created_at`, and Windows' clock granularity is
    coarse enough that two jobs submitted in quick succession can otherwise
    share a timestamp and come back in an arbitrary order. Nudging by a
    microsecond keeps ordering unambiguous at a cost nobody can observe.
    """
    global _last_timestamp

    with _clock_lock:
        value = datetime.now(timezone.utc)
        if value <= _last_timestamp:
            value = _last_timestamp + timedelta(microseconds=1)
        _last_timestamp = value

    return value.isoformat(timespec="microseconds")


# --- job type registry ------------------------------------------------------

JobHandler = Callable[["JobContext"], dict]
InputValidator = Callable[[str, Any], dict]

JOB_TYPES: dict[str, dict] = {}


def register_job_type(
    job_type: str,
    label: str,
    handler: JobHandler,
    validate_input: InputValidator | None = None,
) -> None:
    """Register an executable job type. Only registered types can be submitted."""
    JOB_TYPES[job_type] = {
        "label": label,
        "handler": handler,
        "validate_input": validate_input,
    }


def job_type_label(job_type: str) -> str:
    definition = JOB_TYPES.get(job_type)
    return definition["label"] if definition else job_type


# --- persistence ------------------------------------------------------------


def jobs_directory(project_id: str) -> Path:
    return storage.project_directory(project_id) / JOBS_DIRECTORY


def job_file(project_id: str, job_id: str) -> Path:
    return jobs_directory(project_id) / ("%s.json" % job_id)


def _validate_job_id(raw_id: Any) -> str:
    if (
        not isinstance(raw_id, str)
        or len(raw_id) != _JOB_ID_PATTERN_LENGTH
        or not all(character in "0123456789abcdef" for character in raw_id)
    ):
        raise JobNotFound("מזהה משימה לא חוקי.")
    return raw_id


def _blank_record(project_id: str, job_type: str, job_input: dict) -> dict:
    return {
        "schema_version": JOB_SCHEMA_VERSION,
        "id": uuid.uuid4().hex,
        "project_id": project_id,
        "type": job_type,
        "status": QUEUED,
        # The snapshot of everything the job needs. A retry re-runs *this*,
        # not whatever the project looks like later.
        "input": job_input,
        "created_at": _now(),
        "started_at": None,
        "finished_at": None,
        "progress_message": "בהמתנה בתור.",
        # Only set when it is genuinely measurable.
        "progress_percent": None,
        "result": None,
        "error": None,
        "cancel_requested": False,
        "retry_of": None,
    }


def _parse_record(data: Any, project_id: str, job_id: str) -> dict:
    if not isinstance(data, dict):
        raise JobError("קובץ המשימה פגום.", status_code=422)

    schema_version = data.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise JobError("קובץ המשימה פגום: חסרה גרסת סכמה.", status_code=422)
    if schema_version > JOB_SCHEMA_VERSION:
        raise JobError(
            "קובץ המשימה נוצר בגרסה חדשה יותר (%d) ואינו נתמך (%d)."
            % (schema_version, JOB_SCHEMA_VERSION),
            status_code=422,
        )

    status = data.get("status")
    if status not in ACTIVE_STATUSES | FINISHED_STATUSES:
        raise JobError("קובץ המשימה פגום: סטטוס לא מוכר.", status_code=422)

    record = _blank_record(project_id, data.get("type") or "", {})
    record.update(data)
    record["id"] = job_id
    record["project_id"] = project_id
    if not isinstance(record.get("input"), dict):
        record["input"] = {}
    return record


def read_job(project_id: str, job_id: str) -> dict:
    project_id = storage.validate_project_id(project_id)
    job_id = _validate_job_id(job_id)

    path = job_file(project_id, job_id)

    try:
        data = storage.read_json(path)
    except FileNotFoundError as error:
        raise JobNotFound("המשימה לא נמצאה.") from error
    except (OSError, ValueError) as error:
        raise JobError("לא ניתן לקרוא את קובץ המשימה: %s" % error, status_code=422) from error

    return _parse_record(data, project_id, job_id)


def list_jobs(project_id: str) -> list[dict]:
    """Every job of a project, newest first. Unreadable files are skipped."""
    project_id = storage.validate_project_id(project_id)
    directory = jobs_directory(project_id)

    if not directory.is_dir():
        return []

    records: list[dict] = []
    for path in directory.iterdir():
        if path.suffix != ".json" or path.name.startswith("."):
            continue
        try:
            records.append(read_job(project_id, path.stem))
        except (JobError, ValueError):
            continue

    records.sort(key=lambda record: record["created_at"], reverse=True)
    return records


def describe_job(record: dict) -> dict:
    """The API shape: the stored record plus its translated type label."""
    described = dict(record)
    described["type_label"] = job_type_label(record["type"])
    return described


# --- the worker -------------------------------------------------------------


class JobContext:
    """What a handler is given: its input snapshot, progress and cancellation."""

    def __init__(self, manager: "JobManager", record: dict) -> None:
        self._manager = manager
        self.job_id = record["id"]
        self.project_id = record["project_id"]
        self.job_type = record["type"]
        self.input = record["input"]

    def progress(self, message: str, percent: float | None = None) -> None:
        """Report a readable message, and a percentage only when measurable."""
        self._manager.update_progress(
            self.project_id, self.job_id, message, percent
        )

    @property
    def cancelled(self) -> bool:
        return self._manager.cancel_requested(self.job_id)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise JobCancelled()


class JobManager:
    def __init__(self) -> None:
        self._queue: Queue = Queue()
        self._lock = threading.RLock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._thread: threading.Thread | None = None
        self._stopping = False

    # -- lifecycle --

    def start(self) -> list[dict]:
        """Mark leftovers as interrupted, then start the worker."""
        interrupted = self.recover_interrupted()
        self._ensure_worker()
        return interrupted

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(
                target=self._work, name="video-factory-jobs", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """Ask the running job to stop and let the worker thread finish."""
        with self._lock:
            self._stopping = True
            thread = self._thread
            for event in self._cancel_events.values():
                event.set()

        self._queue.put(None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)

        with self._lock:
            self._thread = None

    def recover_interrupted(self) -> list[dict]:
        """Any job still queued or running belongs to a process that is gone."""
        recovered: list[dict] = []
        projects_root = get_projects_root()

        for project_directory in projects_root.iterdir():
            jobs_path = project_directory / JOBS_DIRECTORY
            if not project_directory.is_dir() or not jobs_path.is_dir():
                continue

            for path in jobs_path.iterdir():
                if path.suffix != ".json" or path.name.startswith("."):
                    continue
                try:
                    record = read_job(project_directory.name, path.stem)
                except (JobError, ValueError):
                    continue

                if record["status"] not in ACTIVE_STATUSES:
                    continue

                record["status"] = INTERRUPTED
                record["finished_at"] = _now()
                record["progress_message"] = "המשימה נקטעה."
                record["error"] = (
                    "השרת הופעל מחדש בזמן שהמשימה הייתה פעילה, ולכן היא סומנה "
                    "כנקטעה. אפשר להריץ אותה שוב."
                )
                self._write(record)
                recovered.append(record)

        return recovered

    # -- submission --

    def submit(
        self,
        project_id: str,
        job_type: Any,
        raw_input: Any,
        retry_of: str | None = None,
    ) -> dict:
        # Existence and id validation come from the project layer.
        storage.read_project(project_id)
        project_id = storage.validate_project_id(project_id)

        if not isinstance(job_type, str) or job_type not in JOB_TYPES:
            known = ", ".join(sorted(JOB_TYPES))
            raise JobError("סוג משימה לא נתמך. הסוגים הנתמכים: %s" % known)

        validator = JOB_TYPES[job_type]["validate_input"]
        job_input = validator(project_id, raw_input) if validator else {}

        record = _blank_record(project_id, job_type, job_input)
        record["retry_of"] = retry_of
        self._write(record)

        self._ensure_worker()
        self._queue.put((project_id, record["id"]))
        return record

    def retry(self, project_id: str, job_id: str) -> dict:
        """Run the same input snapshot again as a brand-new job."""
        record = read_job(project_id, job_id)

        if record["status"] in ACTIVE_STATUSES:
            raise JobConflict("המשימה עדיין פעילה; אי אפשר להריץ אותה שוב כעת.")
        if record["type"] not in JOB_TYPES:
            raise JobError("סוג המשימה אינו נתמך עוד ולכן אי אפשר להריץ אותה שוב.")

        retried = _blank_record(record["project_id"], record["type"], record["input"])
        retried["retry_of"] = record["id"]
        self._write(retried)

        self._ensure_worker()
        self._queue.put((retried["project_id"], retried["id"]))
        return retried

    # -- cancellation --

    def cancel(self, project_id: str, job_id: str) -> dict:
        """Cancel honestly: queued stops now, running is *asked* to stop."""
        with self._lock:
            record = read_job(project_id, job_id)

            if record["status"] in FINISHED_STATUSES:
                raise JobConflict("המשימה כבר הסתיימה ואי אפשר לבטל אותה.")

            event = self._cancel_events.setdefault(record["id"], threading.Event())
            event.set()
            record["cancel_requested"] = True

            if record["status"] == QUEUED:
                # Nothing has started, so this is immediate and truthful.
                record["status"] = CANCELLED
                record["finished_at"] = _now()
                record["progress_message"] = "בוטלה לפני שהתחילה."
                self._write(record)
                return record

            # Running: the handler stops at its next cancellation check. The
            # status stays `running` until it actually does.
            record["progress_message"] = "התקבלה בקשת ביטול; ממתין לעצירה…"
            self._write(record)
            return record

    def cancel_requested(self, job_id: str) -> bool:
        with self._lock:
            event = self._cancel_events.get(job_id)
            return event is not None and event.is_set()

    # -- progress --

    def update_progress(
        self,
        project_id: str,
        job_id: str,
        message: str,
        percent: float | None = None,
    ) -> None:
        with self._lock:
            try:
                record = read_job(project_id, job_id)
            except JobError:
                return

            if record["status"] != RUNNING:
                return

            record["progress_message"] = message
            if percent is not None:
                record["progress_percent"] = max(0.0, min(100.0, round(percent, 1)))
            self._write(record)

    # -- internals --

    def _write(self, record: dict) -> None:
        with self._lock:
            path = job_file(record["project_id"], record["id"])
            try:
                storage.write_json_atomic(path, record)
            except OSError as error:
                raise JobError(
                    "שמירת מצב המשימה נכשלה: %s" % error, status_code=500
                ) from error

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    # The shutdown sentinel. If the manager was restarted in
                    # the meantime (tests do this), keep working instead.
                    if self._stopping:
                        return
                    continue
                project_id, job_id = item
                self._run(project_id, job_id)
            except Exception:  # noqa: BLE001 - the worker must never die
                pass
            finally:
                self._queue.task_done()

    def _run(self, project_id: str, job_id: str) -> None:
        with self._lock:
            try:
                record = read_job(project_id, job_id)
            except JobError:
                return

            # Cancelled while it sat in the queue, or already handled.
            if record["status"] != QUEUED:
                return

            event = self._cancel_events.setdefault(job_id, threading.Event())
            if event.is_set() or self._stopping:
                record["status"] = CANCELLED
                record["finished_at"] = _now()
                record["progress_message"] = "בוטלה לפני שהתחילה."
                self._write(record)
                return

            record["status"] = RUNNING
            record["started_at"] = _now()
            record["progress_message"] = "מתחיל…"
            self._write(record)

        context = JobContext(self, record)
        handler = JOB_TYPES[record["type"]]["handler"]

        try:
            result = handler(context)
            outcome = (SUCCEEDED, result if isinstance(result, dict) else {}, None)
        except JobCancelled:
            outcome = (CANCELLED, None, None)
        except JobFailed as error:
            outcome = (FAILED, None, error.message)
        except Exception as error:  # noqa: BLE001 - reported, never swallowed
            outcome = (FAILED, None, "המשימה נכשלה: %s" % error)

        status, result, error_message = outcome

        with self._lock:
            try:
                record = read_job(project_id, job_id)
            except JobError:
                return

            record["status"] = status
            record["finished_at"] = _now()
            record["result"] = result
            record["error"] = error_message

            if status == SUCCEEDED:
                record["progress_message"] = "הסתיימה בהצלחה."
                record["progress_percent"] = 100.0
            elif status == CANCELLED:
                record["progress_message"] = "בוטלה."
            else:
                record["progress_message"] = "נכשלה."

            self._write(record)
            self._cancel_events.pop(job_id, None)


# One manager per backend process.
manager = JobManager()


def start() -> list[dict]:
    return manager.start()


def stop() -> None:
    manager.stop()


def submit(project_id: str, job_type: Any, raw_input: Any) -> dict:
    return manager.submit(project_id, job_type, raw_input)


def cancel(project_id: str, job_id: str) -> dict:
    return manager.cancel(project_id, job_id)


def retry(project_id: str, job_id: str) -> dict:
    return manager.retry(project_id, job_id)
