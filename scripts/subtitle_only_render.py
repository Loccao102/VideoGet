#!/usr/bin/env python3
"""Render Vietnamese subtitles while preserving the original audio untouched by TTS.

This deliberately reuses Smart Render's visual analysis/cleanup helpers, but never
mixes, lowers, replaces, or synthesizes audio. It is the renderer used by the
per-video `subtitles_only` localization mode.
"""
from __future__ import annotations

import os
from pathlib import Path

import localize as base
import smart_render as smart


def render_video(input_path: Path, vi_srt: Path, output_path: Path) -> None:
    burn_subtitles = smart.env_bool("BURN_SUBTITLES", True)
    mode = smart.cleanup_mode()
    cleanup_source_subtitles = smart.env_bool("VIDEO_CLEANUP_SOURCE_SUBTITLES", True)
    cleanup_logos = smart.env_bool("VIDEO_CLEANUP_LOGOS", True)
    color_grade = smart.env_bool("VIDEO_COLOR_GRADE", True)
    source_has_audio = base.has_audio_stream(input_path)
    width, height = smart.video_size(input_path)
    layout = smart.analyze_or_load(input_path, output_path, width, height, mode)

    subtitle = layout.get("sourceSubtitle") or None
    watermarks = list(layout.get("watermarks") or [])
    placement = layout.get("subtitlePlacement") or {}
    base.log(
        f"Subtitle-only render profile={mode}; sourceSub={'yes' if subtitle else 'no'}; "
        f"watermarks={len(watermarks)}; subtitle="
        f"{placement.get('reason','default')}@{placement.get('anchorY','?')}"
    )

    cmd = ["ffmpeg", "-y", "-i", str(input_path)]
    filters: list[str] = []
    video_label = "0:v"
    cleanup_index = 0

    if mode != "safe" and cleanup_source_subtitles and subtitle:
        confidence = float(subtitle.get("confidence", 1.0))
        threshold = 0.55 if mode in {"aggressive", "legacy"} else 0.66
        if confidence >= threshold:
            active = smart.timeline_enable(smart.parse_srt_intervals(vi_srt))
            video_label = smart.add_blur_region(
                filters,
                video_label,
                cleanup_index,
                subtitle,
                smart.env_int(
                    "VIDEO_SOURCE_SUBTITLE_BLUR",
                    8 if mode == "smart" else 11,
                    2,
                ),
                active,
            )
            cleanup_index += 1

    if mode != "safe" and cleanup_logos:
        min_conf = 0.55 if mode in {"aggressive", "legacy"} else 0.72
        for region in watermarks:
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

    if color_grade:
        contrast = smart.env_float("VIDEO_CONTRAST", 1.03, 0.1)
        saturation = smart.env_float("VIDEO_SATURATION", 1.05, 0.0)
        brightness = float(os.getenv("VIDEO_BRIGHTNESS", "0.005"))
        filters.append(
            f"[{video_label}]eq=contrast={contrast}:saturation={saturation}:"
            f"brightness={brightness},unsharp=5:5:0.25:5:5:0.0[vgraded]"
        )
        video_label = "vgraded"

    if burn_subtitles:
        ass_path = output_path.parent / f"{input_path.stem}.vi.ass"
        smart.build_ass_from_srt(vi_srt, ass_path, layout, width, height)
        escaped = base.escape_subtitle_path(ass_path)
        filters.append(f"[{video_label}]subtitles='{escaped}'[vout]")
        video_map = "[vout]"
    else:
        video_map = f"[{video_label}]" if video_label != "0:v" else "0:v:0"

    if filters:
        cmd += ["-filter_complex", ";".join(filters)]
    cmd += ["-map", video_map]
    if source_has_audio:
        cmd += ["-map", "0:a:0"]
    if filters:
        cmd += [
            "-c:v",
            "libx264",
            "-preset",
            os.getenv("VIDEO_PRESET", "veryfast"),
            "-crf",
            os.getenv("VIDEO_CRF", "21"),
            "-pix_fmt",
            "yuv420p",
        ]
    else:
        cmd += ["-c:v", "copy"]
    if source_has_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", str(output_path)]
    base.log("Đang render subtitle-only: giữ nguyên audio gốc, không chạy/mix TTS")
    base.run(cmd)
