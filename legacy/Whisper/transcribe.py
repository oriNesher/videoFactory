from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from faster_whisper import WhisperModel


@dataclass
class Subtitle:
    start: float
    end: float
    text: str


def srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1_000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def clean_text(text: str) -> str:
    return " ".join(text.strip().split())


def ends_sentence(text: str) -> bool:
    """Return True when a word ends a natural phrase or sentence."""
    return text.rstrip().endswith((".", "!", "?", "…", ",", ":", ";"))


def split_segment_into_subtitles(
    segment,
    *,
    max_words: int,
    max_characters: int,
    pause_threshold: float,
    min_words: int,
) -> Iterator[Subtitle]:
    """
    Split one Whisper segment into shorter subtitle blocks.

    A block is closed when:
    - it reaches max_words;
    - adding another word would exceed max_characters;
    - there is a noticeable pause before the next word;
    - punctuation creates a natural split point.
    """
    words = segment.words or []

    # Fallback for unexpected segments without word timestamps.
    if not words:
        text = clean_text(segment.text)
        if text:
            yield Subtitle(
                start=segment.start,
                end=segment.end,
                text=text,
            )
        return

    current_words = []
    current_start: float | None = None
    current_end: float | None = None

    def flush() -> Subtitle | None:
        nonlocal current_words, current_start, current_end

        if not current_words or current_start is None or current_end is None:
            return None

        subtitle = Subtitle(
            start=current_start,
            end=current_end,
            text=clean_text(" ".join(current_words)),
        )

        current_words = []
        current_start = None
        current_end = None

        return subtitle

    for index, word in enumerate(words):
        word_text = clean_text(word.word)

        if not word_text:
            continue

        next_word = words[index + 1] if index + 1 < len(words) else None

        proposed_words = current_words + [word_text]
        proposed_text = clean_text(" ".join(proposed_words))

        # If adding this word makes the block too long,
        # close the existing block first.
        would_exceed_words = (
            bool(current_words) and len(proposed_words) > max_words
        )
        would_exceed_characters = (
            bool(current_words) and len(proposed_text) > max_characters
        )

        if would_exceed_words or would_exceed_characters:
            subtitle = flush()
            if subtitle:
                yield subtitle

        if current_start is None:
            current_start = word.start

        current_words.append(word_text)
        current_end = word.end

        reached_word_limit = len(current_words) >= max_words
        reached_character_limit = (
            len(clean_text(" ".join(current_words))) >= max_characters
        )

        pause_after_word = False
        if next_word is not None:
            pause_after_word = (
                next_word.start - word.end
            ) >= pause_threshold

        natural_break = (
            len(current_words) >= min_words
            and ends_sentence(word_text)
        )

        is_last_word = next_word is None

        if (
            reached_word_limit
            or reached_character_limit
            or pause_after_word
            or natural_break
            or is_last_word
        ):
            subtitle = flush()
            if subtitle:
                yield subtitle


def generate_subtitles(
    segments: Iterable,
    *,
    max_words: int,
    max_characters: int,
    pause_threshold: float,
    min_words: int,
) -> Iterator[Subtitle]:
    for segment in segments:
        yield from split_segment_into_subtitles(
            segment,
            max_words=max_words,
            max_characters=max_characters,
            pause_threshold=pause_threshold,
            min_words=min_words,
        )


def write_srt(
    subtitles: Iterable[Subtitle],
    output_path: Path,
) -> int:
    count = 0

    with output_path.open(
        "w",
        encoding="utf-8-sig",
        newline="\n",
    ) as file:
        for subtitle in subtitles:
            text = clean_text(subtitle.text)

            if not text:
                continue

            count += 1

            file.write(f"{count}\n")
            file.write(
                f"{srt_timestamp(subtitle.start)} --> "
                f"{srt_timestamp(subtitle.end)}\n"
            )
            file.write(f"{text}\n\n")

    return count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create short Hebrew SRT subtitles "
            "from a video or audio file."
        )
    )

    parser.add_argument(
        "input_file",
        help="Path to the video/audio file",
    )

    parser.add_argument(
        "--model",
        default="medium",
        help=(
            "Whisper model: small, medium, large-v3 "
            "(default: medium)"
        ),
    )

    parser.add_argument(
        "--device",
        choices=("cpu", "cuda"),
        default="cpu",
        help="Processing device (default: cpu)",
    )

    parser.add_argument(
        "--language",
        default="he",
        help="Language code. Hebrew is he (default: he)",
    )

    parser.add_argument(
        "--max-words",
        type=int,
        default=5,
        help=(
            "Maximum words in each subtitle "
            "(default: 5)"
        ),
    )

    parser.add_argument(
        "--max-characters",
        type=int,
        default=30,
        help=(
            "Approximate maximum characters in each subtitle "
            "(default: 30)"
        ),
    )

    parser.add_argument(
        "--min-words",
        type=int,
        default=2,
        help=(
            "Minimum words before splitting on punctuation "
            "(default: 2)"
        ),
    )

    parser.add_argument(
        "--pause-threshold",
        type=float,
        default=0.45,
        help=(
            "Split after a pause of this many seconds "
            "(default: 0.45)"
        ),
    )

    return parser


def main() -> int:
    args = build_parser().parse_args()
    input_path = Path(args.input_file).expanduser().resolve()

    if not input_path.is_file():
        print(
            f"ERROR: File not found: {input_path}",
            file=sys.stderr,
        )
        return 1

    if args.max_words < 1:
        print(
            "ERROR: --max-words must be at least 1.",
            file=sys.stderr,
        )
        return 1

    if args.max_characters < 1:
        print(
            "ERROR: --max-characters must be at least 1.",
            file=sys.stderr,
        )
        return 1

    output_path = input_path.with_suffix(".srt")
    compute_type = "float16" if args.device == "cuda" else "int8"

    print(f"Input:          {input_path}")
    print(f"Output:         {output_path}")
    print(f"Model:          {args.model}")
    print(f"Device:         {args.device} ({compute_type})")
    print(f"Maximum words:  {args.max_words}")
    print(f"Maximum chars:  {args.max_characters}")
    print(f"Pause split:    {args.pause_threshold} seconds")
    print("Loading model. The first run downloads it automatically...")

    try:
        model = WhisperModel(
            args.model,
            device=args.device,
            compute_type=compute_type,
        )

        segments, info = model.transcribe(
            str(input_path),
            language=args.language,
            task="transcribe",
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=True,
            word_timestamps=True,
        )

        print(
            f"Detected language: {info.language} "
            f"(probability {info.language_probability:.2f})"
        )

        subtitles = generate_subtitles(
            segments,
            max_words=args.max_words,
            max_characters=args.max_characters,
            pause_threshold=args.pause_threshold,
            min_words=args.min_words,
        )

        count = write_srt(subtitles, output_path)

    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)

        if args.device == "cuda":
            print(
                "\nCUDA failed. Try running with --device cpu.",
                file=sys.stderr,
            )

        return 1

    print(f"\nDone. Created {count} subtitles:")
    print(output_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())