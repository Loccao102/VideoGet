#!/usr/bin/env python3
"""Burn an edited Vietnamese SRT into a downloaded video without TTS."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import localize as base
import smart_render as smart


def render(input_path: Path, subtitle_path: Path, output_path: Path) -> None:
    if not input_path.exists():
        raise FileNotFoundError(input_path)
    if not subtitle_path.exists():
        raise FileNotFoundError(subtitle_path)

    width, height = smart.video_size(input_path)
    mode = smart.cleanup_mode()
    layout = smart.analyze_or_load(input_path, output_path, width, height, mode)
    filters: list[str] = []
    video_label = "0:v"
    cleanup_index = 0

    source_subtitle = layout.get("sourceSubtitle") or None
    if (
        mode != "safe"
        and smart.env_bool("VIDEO_CLEANUP_SOURCE_SUBTITLES", True)
        and source_subtitle
    ):
        confidence = float(source_subtitle.get("confidence", 1.0))
        threshold = 0.55 if mode in {"aggressive", "legacy"} else 0.66
        if confidence >= threshold:
            active = smart.timeline_enable(smart.parse_srt_intervals(subtitle_path))
            video_label = smart.add_blur_region(
                filters,
                video_label,
                cleanup_index,
                source_subtitle,
                smart.env_int(
                    "VIDEO_SOURCE_SUBTITLE_BLUR",
                    8 if mode == "smart" else 11,
                    2,
                ),
                active,
            )
            cleanup_index += 1

    if mode != "safe" and smart.env_bool("VIDEO_CLEANUP_LOGOS", True):
        min_conf = 0.55 if mode in {"aggressive", "legacy"} else 0.72
        for region in list(layout.get("watermarks") or []):
            if float(region.get("confidence", 1.0)) < min_conf:
                continue
            video_label = smart.add_delogo(
                filters,
                video_label,
                cleanup_index,
                region,
                width,
                height,
            )
            cleanup_index += 1

    if smart.env_bool("VIDEO_COLOR_GRADE", True):
        contrast = smart.env_float("VIDEO_CONTRAST", 1.03, 0.1)
        saturation = smart.env_float("VIDEO_SATURATION", 1.05, 0.0)
        brightness = float(os.getenv("VIDEO_BRIGHTNESS", "0.005"))
        filters.append(
            f"[{video_label}]eq=contrast={contrast}:saturation={saturation}:"
            f"brightness={brightness},unsharp=5:5:0.25:5:5:0.0[vgraded]"
        )
        video_label = "vgraded"

    ass_path = output_path.parent / f"{output_path.stem}.ass"
    smart.build_ass_from_srt(subtitle_path, ass_path, layout, width, height)
    escaped = base.escape_subtitle_path(ass_path)
    filters.append(f"[{video_label}]subtitles='{escaped}'[vout]")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-i", str(input_path)]
    if filters:
        cmd += ["-filter_complex", ";".join(filters)]
    cmd += ["-map", "[vout]"]
    if base.has_audio_stream(input_path):
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "192k"]
    cmd += [
        "-c:v",
        "libx264",
        "-preset",
        os.getenv("VIDEO_PRESET", "veryfast"),
        "-crf",
        os.getenv("VIDEO_CRF", "21"),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    base.log("Đang render subtitle-only: giữ audio gốc, không TTS")
    base.run(cmd)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--subtitle", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    render(Path(args.input).resolve(), Path(args.subtitle).resolve(), Path(args.output).resolve())


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        base.log(f"ERROR: {error}")
        raise SystemExit(1)
