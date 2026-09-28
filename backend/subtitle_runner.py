"""Executing one subtitle job: Whisper on each chosen clip, then an SRT.

Runs on the job queue's worker thread. Whisper itself runs in a child process
(`backend/whisper_worker.py`) so that cancelling kills it outright and its
memory is returned when it finishes; this module supervises it, turns its
transcript into subtitle lines, and writes the files.

Guarantees, per clip:

- the attempt is recorded before Whisper starts, so an interruption is visible;
- the SRT on disk is replaced only by a complete new one — a failed or
  cancelled attempt leaves the previous subtitles untouched;
- Whisper's own output is kept in the run's `logs` directory.
"""

import json
import sys
import time
from pathlib import Path

from . import cut_runner, cutting, jobs, processes, storage, subtitles

WORKER_SCRIPT = Path(__file__).with_name("whisper_worker.py")

PROGRESS_INTERVAL_SECONDS = 0.4


def build_command(input_path: str, output_path: str, model: str, language: str) -> list[str]:
    """The worker invocation. Same interpreter as the backend, so same packages."""
    return [
        sys.executable,
        "-u",
        str(WORKER_SCRIPT),
        "--input",
        input_path,
        "--output",
        output_path,
        "--model",
        model,
        "--language",
        language,
    ]


class _ProtocolReader:
    """Collect the worker's `@@` lines from raw, arbitrarily split chunks."""

    def __init__(self) -> None:
        self._pending = ""
        self.loading = False
        self.duration: float | None = None
        self.language: str | None = None
        self.position = 0.0

    def feed(self, chunk: str) -> bool:
        """Returns True when something the progress message shows changed."""
        self._pending += chunk.replace("\r", "\n")
        *complete, self._pending = self._pending.split("\n")
        changed = False

        for line in complete:
            parts = line.strip().split()
            if not parts or not parts[0].startswith("@@"):
                continue
            try:
                if parts[0] == "@@loading":
                    self.loading = True
                elif parts[0] == "@@info" and len(parts) >= 4:
                    self.loading = False
                    self.language = parts[1]
                    self.duration = float(parts[3])
                elif parts[0] == "@@progress" and len(parts) >= 2:
                    self.position = float(parts[1])
                else:
                    continue
            except ValueError:
                continue
            changed = True

        return changed

    def fraction(self) -> float:
        if not self.duration or self.duration <= 0:
            return 0.0
        return max(0.0, min(1.0, self.position / self.duration))


def _write_log(project_id: str, run_id: str, output_id: str, command: list[str], output: str) -> str:
    directory = cutting.run_directory(project_id, run_id)
    path = directory / cutting.LOGS_DIRECTORY / ("subtitles-%s.log" % output_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "$ %s\n\n%s" % (processes.describe_command(command), output),
            encoding="utf-8",
            errors="replace",
            newline="\n",
        )
    except OSError:
        return ""
    return str(path.relative_to(directory)).replace("\\", "/")


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def run_subtitles(context: jobs.JobContext) -> dict:
    project_id = context.project_id
    job_input = context.input
    run_id = job_input["run_id"]
    outputs = job_input["outputs"]
    model = job_input["model"]
    language = job_input["language"]
    settings = job_input["settings"]

    if job_input.get("target") == subtitles.TARGET_MERGED:
        # A run cut before merging was automatic has only clips: merge them
        # now. Does nothing when the merged video already exists, which is
        # also what makes a retry of this job safe.
        context.progress("Merging the clips into one video…", percent=0.0)
        cut_runner.merge_run(context, run_id)
        try:
            outputs = [
                subtitles.describe_output(project_id, run_id, cutting.COMBINED_OUTPUT_ID)
            ]
        except storage.ProjectError as error:
            raise jobs.JobFailed(error.message) from error

    if not outputs:
        raise jobs.JobFailed("No clips were chosen for subtitles.")

    directory = subtitles.subtitles_directory(project_id, run_id)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise jobs.JobFailed("The subtitles folder could not be created: %s" % error) from error

    total = len(outputs)
    produced: list[dict] = []

    for index, output in enumerate(outputs):
        context.raise_if_cancelled()

        output_id = output["output_id"]
        name = output.get("source_filename") or output["filename"]
        label = "Subtitles for clip %d of %d: %s" % (index + 1, total, name)

        # A retry re-runs this snapshot; the clip may have been deleted since.
        if not Path(output["path"]).is_file():
            raise jobs.JobFailed(
                'The clip "%s" is no longer in its run folder, so it cannot be '
                "transcribed." % output["filename"]
            )

        record = subtitles.read_record(project_id, run_id, output_id)
        attempt = {
            "status": subtitles.ATTEMPT_RUNNING,
            "job_id": context.job_id,
            "model": model,
            "language": language,
            "started_at": subtitles.now(),
            "finished_at": None,
            "error": None,
            "log_path": None,
        }
        record["attempt"] = attempt
        subtitles.write_record(project_id, run_id, record)

        def finish_attempt(status: str, error: str | None = None) -> None:
            attempt["status"] = status
            attempt["error"] = error
            attempt["finished_at"] = subtitles.now()
            subtitles.write_record(project_id, run_id, record)

        transcript_temporary = directory / (".%s.whisper.json" % output_id)
        command = build_command(output["path"], str(transcript_temporary), model, language)

        reader = _ProtocolReader()
        last_report = [0.0]

        def report(force: bool = False) -> None:
            moment = time.monotonic()
            if not force and moment - last_report[0] < PROGRESS_INTERVAL_SECONDS:
                return
            last_report[0] = moment
            if reader.loading:
                message = "%s — loading the Whisper model (the first use downloads it)…" % label
            else:
                message = label
            context.progress(message, percent=100.0 * (index + reader.fraction()) / total)

        def on_output(chunk: str) -> None:
            if reader.feed(chunk):
                report(force=reader.loading)

        report(force=True)

        try:
            result = processes.run(command, cancelled=lambda: context.cancelled, on_output=on_output)
        except processes.ProcessCancelled:
            _discard(transcript_temporary)
            finish_attempt(subtitles.ATTEMPT_CANCELLED, "Cancelled at your request.")
            raise jobs.JobCancelled() from None
        except processes.ProcessStartFailed as error:
            finish_attempt(subtitles.ATTEMPT_FAILED, error.message)
            raise jobs.JobFailed(error.message) from error

        attempt["log_path"] = _write_log(project_id, run_id, output_id, command, result.output)

        if not result.ok or not transcript_temporary.is_file():
            _discard(transcript_temporary)
            message = 'Whisper failed on "%s" (exit code %d).\n%s' % (
                name,
                result.exit_code,
                result.last_lines(),
            )
            finish_attempt(subtitles.ATTEMPT_FAILED, message)
            raise jobs.JobFailed(message)

        try:
            transcript = storage.read_json(transcript_temporary)
        except (OSError, ValueError) as error:
            _discard(transcript_temporary)
            message = "Whisper's transcript could not be read: %s" % error
            finish_attempt(subtitles.ATTEMPT_FAILED, message)
            raise jobs.JobFailed(message) from error

        lines = subtitles.build_lines(transcript, settings)

        try:
            # UTF-8 with a BOM, as the legacy script wrote: Premiere reads
            # Hebrew correctly only with it.
            subtitles.write_text_atomic(
                subtitles.srt_path(project_id, run_id, output_id),
                subtitles.render_srt(lines),
                "utf-8-sig",
            )
            subtitles.write_text_atomic(
                subtitles.transcript_path(project_id, run_id, output_id),
                json.dumps(transcript, ensure_ascii=False, indent=1),
                "utf-8",
            )
        except OSError as error:
            message = "The subtitle file could not be written: %s" % error
            finish_attempt(subtitles.ATTEMPT_FAILED, message)
            raise jobs.JobFailed(message) from error
        finally:
            _discard(transcript_temporary)

        record["current"] = {
            "job_id": context.job_id,
            "created_at": subtitles.now(),
            "model": model,
            "language": language,
            "detected_language": transcript.get("language"),
            "language_probability": transcript.get("language_probability"),
            "settings": settings,
            "line_count": len(lines),
            "filename": "%s.srt" % Path(output["filename"]).stem,
        }
        finish_attempt(subtitles.ATTEMPT_SUCCEEDED)
        produced.append({"output_id": output_id, "line_count": len(lines)})

    line_total = sum(entry["line_count"] for entry in produced)
    return {
        "run_id": run_id,
        "clips": produced,
        "summary": "Created subtitles for %d video%s (%d lines)."
        % (len(produced), "" if len(produced) == 1 else "s", line_total),
    }
