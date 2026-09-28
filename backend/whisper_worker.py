"""Transcribe one media file with faster-whisper. Runs as its own process.

Started by `backend/subtitle_runner.py` through `processes.run`, never imported
by the backend. Keeping Whisper out of the server process is deliberate:

- **cancellation works** — the job kills this process tree, which stops the
  model mid-sentence instead of waiting for the next segment;
- **memory comes back** — the medium model holds well over a gigabyte, and it
  is released when this process exits rather than living in the server;
- **a native crash stays here** — CTranslate2 is C++, and if it falls over, the
  job fails with a message while the backend keeps serving.

This file depends only on the standard library and `faster_whisper`, so it is
run by path and needs no package context.

Protocol. Lines starting with `@@` on stdout are for the runner; everything
else (Hugging Face download bars, warnings) is only kept in the log:

    @@loading                      the model is being loaded or downloaded
    @@info <language> <prob> <duration>
    @@progress <seconds>           transcribed up to this point of the media

The transcript itself goes to `--output` as JSON: segments with word-level
timestamps. Splitting that into subtitle lines happens in the backend, where it
is tested without a model.
"""

import argparse
import json
import os
import sys


def main() -> int:
    # A pipe on Windows defaults to the ANSI code page; the runner reads UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:  # pragma: no cover - not a text stream
        pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--language", required=True, help='A code such as "he", or "auto"')
    parser.add_argument("--device", default="cpu", choices=("cpu", "cuda"))
    args = parser.parse_args()

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print(
            "faster-whisper is not installed in this Python environment. Run: "
            ".venv\\Scripts\\python.exe -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 2

    print("@@loading", flush=True)

    model = WhisperModel(
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


if __name__ == "__main__":
    raise SystemExit(main())
