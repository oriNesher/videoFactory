from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


PRESET_ZOOM = {
    "NORMAL": 1.00,
    "PUNCH_LIGHT": 1.08,
    "PUNCH_MEDIUM": 1.12,
    "PUNCH_STRONG": 1.20,
}

EPSILON = 0.002


def fail(message: str) -> int:
    print(f"\nERROR: {message}", file=sys.stderr)
    return 1


def find_executable(name: str, script_dir: Path) -> Path | None:
    candidates = [
        script_dir / f"{name}.exe",
        script_dir.parent / "AutoEditor" / f"{name}.exe",
    ]

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    auto_editor = script_dir.parent / "AutoEditor"
    if auto_editor.is_dir():
        matches = list(auto_editor.rglob(f"{name}.exe"))
        if matches:
            return matches[0]

    path_match = shutil.which(name)
    return Path(path_match) if path_match else None


def probe_media(ffprobe: Path, input_file: Path) -> dict[str, Any]:
    command = [
        str(ffprobe),
        "-v", "error",
        "-show_entries",
        "stream=index,codec_type,width,height:format=duration",
        "-of", "json",
        str(input_file),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed")

    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    if not video:
        raise RuntimeError("No video stream found")

    duration = float(data["format"]["duration"])
    return {
        "width": int(video["width"]),
        "height": int(video["height"]),
        "duration": duration,
        "has_audio": has_audio,
    }


def validate_plan(plan: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    if plan.get("schema_version") != "1.0":
        errors.append("schema_version must be 1.0")

    clips = plan.get("clips")
    if not isinstance(clips, list) or not clips:
        errors.append("The plan contains no clips")
        return errors

    seen_filenames: set[str] = set()

    for clip in clips:
        filename = clip.get("filename")
        effects = clip.get("effects")

        if not isinstance(filename, str) or not filename:
            errors.append("A clip has no valid filename")
            continue

        if filename in seen_filenames:
            errors.append(f"Duplicate clip filename: {filename}")
        seen_filenames.add(filename)

        if not isinstance(effects, list) or not effects:
            errors.append(f"{filename}: no effects")
            continue

        expected_start = 0.0
        for index, effect in enumerate(effects, 1):
            preset = effect.get("preset")
            start = effect.get("start")
            end = effect.get("end")

            if preset not in PRESET_ZOOM:
                errors.append(f"{filename}, effect {index}: invalid preset {preset}")

            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
                errors.append(f"{filename}, effect {index}: invalid timestamps")
                continue

            start = float(start)
            end = float(end)

            if start < 0 or end <= start:
                errors.append(f"{filename}, effect {index}: invalid time range")

            if abs(start - expected_start) > EPSILON:
                errors.append(
                    f"{filename}, effect {index}: gap or overlap at {start:.3f}s "
                    f"(expected {expected_start:.3f}s)"
                )

            expected_start = end

    return errors


def even_crop_dimension(original: int, zoom: float) -> int:
    value = int(original / zoom)
    value -= value % 2
    return max(2, min(original, value))


def make_filter(
    effects: list[dict[str, Any]],
    width: int,
    height: int,
    has_audio: bool,
) -> tuple[str, list[str]]:
    filters: list[str] = []
    video_labels: list[str] = []
    audio_labels: list[str] = []

    for index, effect in enumerate(effects):
        start = float(effect["start"])
        end = float(effect["end"])
        zoom = PRESET_ZOOM[effect["preset"]]

        video_label = f"v{index}"
        base = (
            f"[0:v]trim=start={start:.3f}:end={end:.3f},"
            f"setpts=PTS-STARTPTS"
        )

        if zoom > 1.0:
            crop_w = even_crop_dimension(width, zoom)
            crop_h = even_crop_dimension(height, zoom)
            # FACE_CENTER currently means a fixed center crop.
            base += (
                f",crop={crop_w}:{crop_h}:(iw-{crop_w})/2:(ih-{crop_h})/2"
                f",scale={width}:{height}:flags=lanczos"
            )

        base += f",setsar=1,format=yuv420p[{video_label}]"
        filters.append(base)
        video_labels.append(f"[{video_label}]")

        if has_audio:
            audio_label = f"a{index}"
            filters.append(
                f"[0:a]atrim=start={start:.3f}:end={end:.3f},"
                f"asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0"
                f"[{audio_label}]"
            )
            audio_labels.append(f"[{audio_label}]")

    if has_audio:
        concat_inputs = "".join(
            video_labels[i] + audio_labels[i] for i in range(len(video_labels))
        )
        filters.append(
            f"{concat_inputs}concat=n={len(video_labels)}:v=1:a=1[vout][aout]"
        )
        maps = ["-map", "[vout]", "-map", "[aout]"]
    else:
        concat_inputs = "".join(video_labels)
        filters.append(
            f"{concat_inputs}concat=n={len(video_labels)}:v=1:a=0[vout]"
        )
        maps = ["-map", "[vout]"]

    return ";".join(filters), maps


def render_clip(
    ffmpeg: Path,
    ffprobe: Path,
    input_file: Path,
    output_file: Path,
    effects: list[dict[str, Any]],
) -> None:
    info = probe_media(ffprobe, input_file)
    planned_duration = float(effects[-1]["end"])

    if abs(info["duration"] - planned_duration) > 0.12:
        raise RuntimeError(
            f"Plan duration is {planned_duration:.3f}s, "
            f"but file duration is {info['duration']:.3f}s"
        )

    filter_complex, maps = make_filter(
        effects,
        info["width"],
        info["height"],
        info["has_audio"],
    )

    command = [
        str(ffmpeg),
        "-y",
        "-hide_banner",
        "-i", str(input_file),
        "-filter_complex", filter_complex,
        *maps,
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
    ]

    if info["has_audio"]:
        command += ["-c:a", "aac", "-b:a", "192k"]

    command += [
        "-movflags", "+faststart",
        str(output_file),
    ]

    result = subprocess.run(command)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed with exit code {result.returncode}")


def resolve_input(target: Path) -> tuple[Path, Path]:
    if target.is_file() and target.suffix.lower() == ".json":
        return target, target.parent

    if target.is_dir():
        plan_path = target / "zoom_plan.json"
        if not plan_path.is_file():
            json_files = list(target.glob("*zoom*plan*.json"))
            if len(json_files) == 1:
                plan_path = json_files[0]
            else:
                raise RuntimeError(
                    "Could not find zoom_plan.json inside the dragged folder"
                )
        return plan_path, target

    raise RuntimeError("Drag either zoom_plan.json or the clips folder")


def main() -> int:
    if len(sys.argv) < 2:
        return fail(
            "Drag the clips folder or zoom_plan.json onto render_zoom_plan.bat"
        )

    target = Path(sys.argv[1]).expanduser().resolve()
    script_dir = Path(__file__).resolve().parent

    try:
        plan_path, clips_folder = resolve_input(target)
    except Exception as exc:
        return fail(str(exc))

    ffmpeg = find_executable("ffmpeg", script_dir)
    ffprobe = find_executable("ffprobe", script_dir)

    if not ffmpeg or not ffprobe:
        return fail(
            "FFmpeg/FFprobe were not found.\n"
            "Put ffmpeg.exe and ffprobe.exe inside ZoomRenderer, "
            "or keep them inside the sibling AutoEditor folder."
        )

    try:
        with plan_path.open("r", encoding="utf-8") as file:
            plan = json.load(file)
    except Exception as exc:
        return fail(f"Could not read JSON: {exc}")

    errors = validate_plan(plan)
    if errors:
        print("\nPLAN VALIDATION FAILED:")
        for error in errors:
            print(f" - {error}")
        return 1

    output_folder = clips_folder / "ZOOMED_OUTPUT"
    output_folder.mkdir(exist_ok=True)

    clips = plan["clips"]
    print(f"Plan: {plan_path}")
    print(f"Clips folder: {clips_folder}")
    print(f"Output folder: {output_folder}")
    print(f"FFmpeg: {ffmpeg}")
    print(f"\nRendering {len(clips)} clips...\n")

    failures: list[str] = []

    for index, clip in enumerate(clips, 1):
        filename = clip["filename"]
        input_file = clips_folder / filename
        output_file = output_folder / filename

        print(f"[{index}/{len(clips)}] {filename}")

        if not input_file.is_file():
            message = f"Missing input file: {input_file}"
            print(f"  ERROR: {message}")
            failures.append(message)
            continue

        try:
            render_clip(
                ffmpeg,
                ffprobe,
                input_file,
                output_file,
                clip["effects"],
            )
            print(f"  Created: {output_file.name}")
        except Exception as exc:
            message = f"{filename}: {exc}"
            print(f"  ERROR: {exc}")
            failures.append(message)

    print("\n----------------------------------------")
    if failures:
        print(f"Finished with {len(failures)} error(s):")
        for failure in failures:
            print(f" - {failure}")
        return 1

    print("All clips rendered successfully.")
    print(f"Output: {output_folder}")
    print("\nImport the files from ZOOMED_OUTPUT into Premiere.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
