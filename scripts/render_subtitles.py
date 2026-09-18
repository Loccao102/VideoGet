#!/usr/bin/env python3
"""Burn an edited Vietnamese SRT into a downloaded video without TTS."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import convert_aspect
import localize as base
import smart_render as smart


def append_aspect_filter(
    filters: list[str],
    input_label: str,
    source_w: int,
    source_h: int,
    aspect: str,
) -> str:
    aspect = (aspect or "original").strip().lower()
    if aspect in {"", "original"}:
        return input_label
    if aspect not in convert_aspect.TARGETS:
        raise RuntimeError("aspect must be original, 16:9, 3:4, 9:16, or 1:1")

    target_w, target_h = convert_aspect.TARGETS[aspect]
    source_aspect = source_w / source_h
    target_aspect = target_w / target_h
    if abs(source_aspect - target_aspect) <= 0.002:
        filters.append(
            f"[{input_label}]scale={target_w}:{target_h}:flags=lanczos,setsar=1[aspectout]"
        )
        return "aspectout"

    mode = convert_aspect.choose_mode(source_w, source_h, target_w, target_h)
    if mode == "crop":
        crop_w, crop_h, x, y = convert_aspect.crop_geometry(
            source_w, source_h, target_w, target_h
        )
        filters.append(
            f"[{input_label}]crop={crop_w}:{crop_h}:{x}:{y},"
            f"scale={target_w}:{target_h}:flags=lanczos,setsar=1[aspectout]"
        )
        return "aspectout"

    blur = max(4, int(convert_aspect.env_float("ASPECT_BLUR_RADIUS", 24, 4)))
    filters.append(f"[{input_label}]split=2[aspectbgsrc][aspectfgsrc]")
    filters.append(
        f"[aspectbgsrc]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
        f"crop={target_w}:{target_h},boxblur=luma_radius={blur}:luma_power=1[aspectbg]"
    )
    filters.append(
        f"[aspectfgsrc]scale={target_w}:{target_h}:"
        "force_original_aspect_ratio=decrease:flags=lanczos[aspectfg]"
    )
    filters.append(
        "[aspectbg][aspectfg]overlay=(W-w)/2:(H-h)/2,setsar=1[aspectout]"
    )
    return "aspectout"


def render(input_path: Path, subtitle_path: Path, output_path: Path, aspect: str = "original") -> None:
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
    filters.append(f"[{video_label}]subtitles='{escaped}'[subbed]")
    video_label = append_aspect_filter(filters, "subbed", width, height, aspect)
    if video_label != "vout":
        filters.append(f"[{video_label}]null[vout]")

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
    parser.add_argument("--aspect", default="original")
    args = parser.parse_args()
    render(
        Path(args.input).resolve(),
        Path(args.subtitle).resolve(),
        Path(args.output).resolve(),
        args.aspect,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        base.log(f"ERROR: {error}")
        raise SystemExit(1)
