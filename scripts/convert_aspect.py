#!/usr/bin/env python3
"""Convert a rendered/source video to a requested social aspect ratio.

The source file is never modified. "auto" uses a center crop when enough of the
source frame can be retained; otherwise it preserves the entire frame over a
blurred fill background. This keeps 16:9 -> 3:4 useful while avoiding destructive
16:9 -> 9:16 crops by default.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


TARGETS = {
    "16:9": (1920, 1080),
    "3:4": (1080, 1440),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
}


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except ValueError:
        return max(minimum, default)


def probe_size(path: Path) -> tuple[int, int]:
    process = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "json", str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "ffprobe failed")
    data = json.loads(process.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError("video stream not found")
    width = int(streams[0]["width"])
    height = int(streams[0]["height"])
    if width <= 0 or height <= 0:
        raise RuntimeError("invalid source dimensions")
    return width, height


def retention_ratio(source_w: int, source_h: int, target_w: int, target_h: int) -> float:
    source_aspect = source_w / source_h
    target_aspect = target_w / target_h
    return min(source_aspect, target_aspect) / max(source_aspect, target_aspect)


def crop_geometry(source_w: int, source_h: int, target_w: int, target_h: int) -> tuple[int, int, int, int]:
    target_aspect = target_w / target_h
    source_aspect = source_w / source_h
    if source_aspect > target_aspect:
        crop_h = source_h
        crop_w = min(source_w, int(round(crop_h * target_aspect)))
        crop_w -= crop_w % 2
        x = max(0, (source_w - crop_w) // 2)
        x -= x % 2
        return crop_w, crop_h - (crop_h % 2), x, 0
    crop_w = source_w
    crop_h = min(source_h, int(round(crop_w / target_aspect)))
    crop_h -= crop_h % 2
    y = max(0, (source_h - crop_h) // 2)
    y -= y % 2
    return crop_w - (crop_w % 2), crop_h, 0, y


def choose_mode(source_w: int, source_h: int, target_w: int, target_h: int) -> str:
    raw = os.getenv("ASPECT_CONVERT_MODE", "auto").strip().lower()
    if raw in {"crop", "blur_fill"}:
        return raw
    threshold = env_float("ASPECT_CROP_MIN_RETAIN", 0.40, 0.05)
    return "crop" if retention_ratio(source_w, source_h, target_w, target_h) >= threshold else "blur_fill"


def build_filter(source_w: int, source_h: int, target_w: int, target_h: int, mode: str) -> str:
    if mode == "crop":
        crop_w, crop_h, x, y = crop_geometry(source_w, source_h, target_w, target_h)
        return (
            f"[0:v]crop={crop_w}:{crop_h}:{x}:{y},"
            f"scale={target_w}:{target_h}:flags=lanczos,setsar=1[vout]"
        )

    blur = max(4, int(env_float("ASPECT_BLUR_RADIUS", 24, 4)))
    return (
        f"[0:v]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
        f"crop={target_w}:{target_h},boxblur=luma_radius={blur}:luma_power=1[bg];"
        f"[0:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[vout]"
    )


def convert(input_path: Path, output_path: Path, aspect: str) -> dict:
    if aspect not in TARGETS:
        raise RuntimeError("aspect must be one of: 16:9, 3:4, 9:16, 1:1")
    if not input_path.is_file():
        raise FileNotFoundError(input_path)

    source_w, source_h = probe_size(input_path)
    target_w, target_h = TARGETS[aspect]
    source_aspect = source_w / source_h
    target_aspect = target_w / target_h

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if abs(source_aspect - target_aspect) <= 0.002:
        shutil.copy2(input_path, output_path)
        return {
            "aspect": aspect,
            "mode": "copy",
            "source": [source_w, source_h],
            "output": [source_w, source_h],
        }

    mode = choose_mode(source_w, source_h, target_w, target_h)
    filter_complex = build_filter(source_w, source_h, target_w, target_h, mode)
    preset = os.getenv("ASPECT_OUTPUT_PRESET", "").strip() or os.getenv("VIDEO_PRESET", "veryfast").strip() or "veryfast"
    crf = os.getenv("ASPECT_OUTPUT_CRF", "18").strip() or "18"

    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", preset, "-crf", crf,
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ]
    process = subprocess.run(cmd)
    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg aspect conversion failed with exit code {process.returncode}")
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise RuntimeError("aspect conversion produced no usable video")

    return {
        "aspect": aspect,
        "mode": mode,
        "source": [source_w, source_h],
        "output": [target_w, target_h],
        "retained": round(retention_ratio(source_w, source_h, target_w, target_h), 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--aspect", required=True)
    args = parser.parse_args()
    result = convert(Path(args.input).resolve(), Path(args.output).resolve(), args.aspect.strip())
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", flush=True)
        raise SystemExit(1)
