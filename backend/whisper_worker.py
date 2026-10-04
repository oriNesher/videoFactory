"""Transcribe media with faster-whisper. Runs as its own process.

Never imported by the backend; started by path with the backend's own
interpreter. Keeping Whisper out of the server process is deliberate:

- **cancellation works** — the job kills this process tree, which stops the
  model mid-sentence instead of waiting for the next segment;
- **memory comes back** — a large model holds well over a gigabyte, released
  when this process exits rather than living in the server;
- **a native crash stays here** — CTranslate2 is C++, and if it falls over, the
  job fails with a message while the backend keeps serving.

This file depends only on the standard library and `faster_whisper`.

Three modes.

**One-shot** (`--input --output --model --language`), used by the older
per-run subtitles job (`backend/subtitle_runner.py`). Unchanged protocol:

    @@loading
    @@info <language> <prob> <duration>
    @@progress <seconds>

**Serve** (`--serve --model --device --compute-type --cpu-threads`), used by
the captions module (`backend/whisper_service.py`). The model is loaded once,
from local files only, and then any number of requests are read from stdin,
one JSON object per line. Every event is one stdout line, `@@` + JSON:

    {"event": "loading"}
    {"event": "ready", "load_seconds": 3.1}
    {"event": "fatal", "kind": "model_missing" | "device" | "load", "message": ...}
    {"event": "info", "id": ..., "language": "he", "language_probability": 1.0,
     "duration": 61.2}
    {"event": "progress", "id": ..., "position": 12.4}
    {"event": "done", "id": ..., "output": "C:\\...\\transcript.json"}
    {"event": "error", "id": ..., "kind": "no_audio" | "failed", "message": ...}

A request: `{"id", "input", "output", "language", "beam_size", "vad_filter",
"condition_on_previous_text"}`. `language` may be "auto".

**Download** (`--download --model`): fetches a model into the Hugging Face cache
and reports how many bytes have arrived:

    {"event": "download", "bytes": 123456789}
    {"event": "done", "output": "C:\\...\\snapshots\\<hash>"}

Downloading is the *only* mode that touches the network. Serve mode passes
`local_files_only=True`, so a missing model is an error that names the
download button, never a silent multi-gigabyte download in the middle of a
transcription.
"""

import argparse
import json
import os
import sys
import threading
import time


def _configure_streams() -> None:
    # A pipe on Windows defaults to the ANSI code page; the backend reads UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:  # pragma: no cover - not a text stream
        pass


def emit(event: dict) -> None:
    sys.stdout.write("@@" + json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _import_whisper():
    try:
        from faster_whisper import WhisperModel  # noqa: F401
    except ImportError:
        return None
    import faster_whisper

    return faster_whisper


NOT_INSTALLED = (
    "faster-whisper is not installed in this Python environment. Run: "
    ".venv\\Scripts\\python.exe -m pip install -r requirements.txt"
)


# --- one-shot (legacy subtitles job) -----------------------------------------


def one_shot(args: argparse.Namespace) -> int:
    faster_whisper = _import_whisper()
    if faster_whisper is None:
        print(NOT_INSTALLED, file=sys.stderr)
        return 2

    print("@@loading", flush=True)

    model = faster_whisper.WhisperModel(
        args.model,
        device=args.device,
        compute_type="float16" if args.device == "cuda" else "int8",
    )

    # The legacy script's recipe, unchanged: it is what produced subtitles the
    # user was already happy with.
    segments, info = model.transcribe(
        args.input,
        language=None if args.language == "auto" else args.language,
        task="transcribe",
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=True,
        word_timestamps=True,
    )

    print(
        "@@info %s %.4f %.3f"
        % (info.language, info.language_probability, info.duration),
        flush=True,
    )

    collected = []
    for segment in segments:  # a generator: this loop is the transcription
        collected.append(
            {
                "start": segment.start,
                "end": segment.end,
                "text": segment.text,
                "words": [
                    {"start": word.start, "end": word.end, "word": word.word}
                    for word in (segment.words or [])
                ],
            }
        )
        print("@@progress %.3f" % segment.end, flush=True)

    document = {
        "language": info.language,
        "language_probability": info.language_probability,
        "duration_seconds": info.duration,
        "model": args.model,
        "segments": collected,
    }

    temporary = args.output + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False)
    os.replace(temporary, args.output)
    return 0


# --- serve (captions) ---------------------------------------------------------


def _classify_load_error(error: Exception, device: str) -> tuple[str, str]:
    text = str(error)
    lowered = text.lower()
    name = type(error).__name__
    if (
        "localentrynotfound" in name.lower()
        or "local_files_only" in lowered
        or "cannot find the requested files" in lowered
        or "outgoing traffic has been disabled" in lowered
        or "snapshot folder" in lowered
    ):
        return "model_missing", text
    if device == "cuda" and ("cuda" in lowered or "cudnn" in lowered or "cublas" in lowered):
        return "device", text
    return "load", "%s: %s" % (name, text)


def serve(args: argparse.Namespace) -> int:
    faster_whisper = _import_whisper()
    if faster_whisper is None:
        emit({"event": "fatal", "kind": "load", "message": NOT_INSTALLED})
        return 2

    emit({"event": "loading"})
    started = time.monotonic()
    try:
        model = faster_whisper.WhisperModel(
            args.model,
            device=args.device,
            compute_type=args.compute_type,
            cpu_threads=args.cpu_threads,
            local_files_only=True,
        )
    except Exception as error:  # noqa: BLE001 - reported to the backend, then exit
        kind, message = _classify_load_error(error, args.device)
        emit({"event": "fatal", "kind": kind, "message": message})
        return 2

    emit({"event": "ready", "load_seconds": round(time.monotonic() - started, 3)})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            emit({"event": "error", "id": None, "kind": "failed", "message": "Unreadable request."})
            continue
        if request.get("command") == "exit":
            return 0
        _serve_one(model, request)

    return 0


def _serve_one(model, request: dict) -> None:
    request_id = request.get("id")
    language = request.get("language") or "auto"
    try:
        segments, info = model.transcribe(
            request["input"],
            language=None if language == "auto" else language,
            task="transcribe",
            beam_size=int(request.get("beam_size", 5)),
            vad_filter=bool(request.get("vad_filter", True)),
            condition_on_previous_text=bool(request.get("condition_on_previous_text", True)),
            word_timestamps=True,
        )
        emit(
            {
                "event": "info",
                "id": request_id,
                "language": info.language,
                "language_probability": info.language_probability,
                "duration": info.duration,
            }
        )

        collected = []
        for segment in segments:  # a generator: this loop is the transcription
            collected.append(
                {
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text,
                    "avg_logprob": segment.avg_logprob,
                    "no_speech_prob": segment.no_speech_prob,
                    "words": [
                        {
                            "start": word.start,
                            "end": word.end,
                            "word": word.word,
                            "probability": word.probability,
                        }
                        for word in (segment.words or [])
                    ],
                }
            )
            emit({"event": "progress", "id": request_id, "position": segment.end})

        document = {
            "language": info.language,
            "language_probability": info.language_probability,
            "duration_seconds": info.duration,
            "duration_after_vad_seconds": getattr(info, "duration_after_vad", None),
            "segments": collected,
        }
        temporary = request["output"] + ".tmp"
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False)
        os.replace(temporary, request["output"])
        emit({"event": "done", "id": request_id, "output": request["output"]})
    except Exception as error:  # noqa: BLE001 - one bad request must not end the service
        text = str(error)
        lowered = text.lower()
        kind = "failed"
        # PyAV's messages when the container holds no audio stream.
        if "audio" in lowered and ("stream" in lowered or "no " in lowered):
            kind = "no_audio"
        emit({"event": "error", "id": request_id, "kind": kind, "message": "%s: %s" % (type(error).__name__, text)})


# --- download -----------------------------------------------------------------


def _cache_root() -> str:
    explicit = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    if explicit:
        return explicit
    home = os.environ.get("HF_HOME")
    if home:
        return os.path.join(home, "hub")
    return os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")


def _folder_bytes(folder: str) -> int:
    total = 0
    for root, _directories, files in os.walk(folder):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def download(args: argparse.Namespace) -> int:
    faster_whisper = _import_whisper()
    if faster_whisper is None:
        emit({"event": "error", "id": None, "kind": "failed", "message": NOT_INSTALLED})
        return 2

    folder = os.path.join(_cache_root(), "models--" + args.repository.replace("/", "--"))
    finished = threading.Event()

    def report() -> None:
        while not finished.wait(1.0):
            emit({"event": "download", "bytes": _folder_bytes(folder)})

    reporter = threading.Thread(target=report, daemon=True)
    reporter.start()
    try:
        path = faster_whisper.download_model(args.model)
    except Exception as error:  # noqa: BLE001
        finished.set()
        emit({"event": "error", "id": None, "kind": "failed", "message": "%s: %s" % (type(error).__name__, error)})
        return 1
    finished.set()
    emit({"event": "download", "bytes": _folder_bytes(folder)})
    emit({"event": "done", "id": None, "output": path})
    return 0


def main() -> int:
    _configure_streams()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--input")
    parser.add_argument("--output")
    parser.add_argument("--model", required=True)
    parser.add_argument("--repository", help="The Hugging Face repository, for download progress")
    parser.add_argument("--language", default="he", help='A code such as "he", or "auto"')
    parser.add_argument("--device", default="cpu", choices=("cpu", "cuda"))
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--cpu-threads", type=int, default=0)
    args = parser.parse_args()

    if args.serve:
        return serve(args)
    if args.download:
        if not args.repository:
            args.repository = args.model
        return download(args)
    if not args.input or not args.output:
        parser.error("--input and --output are required in one-shot mode")
    return one_shot(args)


if __name__ == "__main__":
    raise SystemExit(main())
