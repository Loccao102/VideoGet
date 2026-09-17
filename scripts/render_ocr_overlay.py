#!/usr/bin/env python3
"""Render edited Vietnamese OCR subtitles back onto the source video.

Overlay rules:
- OCR text footprint in the lower part of the frame: cover the original caption with
  a dark box and place white Vietnamese text on top of that footprint.
- OCR text footprint in the upper/middle part: blur the original text footprint and
  place Vietnamese text directly over the blurred text.
- Bilibili: optionally blur a conservative channel/watermark strip near the top.

The renderer reuses OCR metadata written by localize_ocr_subtitles.py. It does not
run OCR, translation, Whisper or TTS again.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

import localize as base
import smart_render as smart


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def metadata_region(metadata: dict) -> tuple[float, float, float, float]:
    ocr = metadata.get("ocr") or {}
    raw = ocr.get("textRegion") or ocr.get("region") or [0.06, 0.72, 0.88, 0.18]
    try:
        x, y, w, h = [float(value) for value in raw]
    except (TypeError, ValueError):
        x, y, w, h = 0.06, 0.72, 0.88, 0.18
    x = clamp(x, 0.0, 0.98)
    y = clamp(y, 0.0, 0.98)
    w = clamp(w, 0.02, 1.0 - x)
    h = clamp(h, 0.02, 1.0 - y)
    pad_x = smart.env_float("OCR_OVERLAY_REGION_PAD_X", 0.018, 0.0)
    pad_y = smart.env_float("OCR_OVERLAY_REGION_PAD_Y", 0.012, 0.0)
    nx = clamp(x - pad_x, 0.0, 0.98)
    ny = clamp(y - pad_y, 0.0, 0.98)
    right = clamp(x + w + pad_x, nx + 0.02, 1.0)
    bottom = clamp(y + h + pad_y, ny + 0.02, 1.0)
    return nx, ny, right - nx, bottom - ny


def parse_srt(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig")
    blocks = re.split(r"\n\s*\n", text.strip())
    pattern = re.compile(
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s+-->\s+"
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
    )
    out: list[dict] = []
    for block in blocks:
        lines = [line.rstrip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        timing_index = 1 if re.fullmatch(r"\d+", lines[0].strip()) else 0
        if timing_index >= len(lines):
            continue
        match = pattern.search(lines[timing_index])
        if not match:
            continue
        values = [int(value) for value in match.groups()]
        start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000.0
        end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000.0
        if end <= start:
            continue
        body = "\\N".join(smart.ass_escape(line) for line in lines[timing_index + 1 :]).strip()
        if not body:
            continue
        out.append({"start": start, "end": end, "text": body})
    return out


def ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def build_positioned_ass(
    subtitle_path: Path,
    ass_path: Path,
    width: int,
    height: int,
    region: tuple[float, float, float, float],
    bottom_mode: bool,
) -> None:
    entries = parse_srt(subtitle_path)
    x, y, w, h = region
    anchor_x = int((x + w / 2.0) * width)
    anchor_y = int((y + h / 2.0) * height)
    font = os.getenv("VIDEO_SUBTITLE_FONT", "Noto Sans")
    default_size = max(18, min(44, int(min(width, height) * (0.050 if bottom_mode else 0.044))))
    font_size = smart.env_int("OCR_OVERLAY_FONT_SIZE", default_size, 12)
    outline = smart.env_float("OCR_OVERLAY_TEXT_OUTLINE", 1.6 if bottom_mode else 2.2, 0.0)
    margin_lr = max(12, int(width * 0.05))
    header = f"""[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: OCR,{font},{font_size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H00000000,-1,0,0,0,100,100,0,0,1,{outline:.1f},0,5,{margin_lr},{margin_lr},0,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"""
    events = []
    for entry in entries:
        text = entry["text"]
        events.append(
            "Dialogue: 0,{start},{end},OCR,,0,0,0,,"
            "{{\\an5\\pos({x},{y})\\q2}}{text}".format(
                start=ass_time(float(entry["start"])),
                end=ass_time(float(entry["end"])),
                x=anchor_x,
                y=anchor_y,
                text=text,
            )
        )
    ass_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def add_bilibili_cleanup(
    filters: list[str],
    video_label: str,
    start_index: int,
    platform: str,
) -> tuple[str, int]:
    if platform.lower().strip() != "bilibili" or not smart.env_bool("OCR_OVERLAY_HIDE_BILIBILI", True):
        return video_label, start_index
    raw = os.getenv("OCR_OVERLAY_BILIBILI_REGIONS", "0.012,0.012,0.38,0.085")
    radius = smart.env_int("OCR_OVERLAY_BILIBILI_BLUR", 13, 2)
    index = start_index
    for region in smart.parse_regions(raw, "bilibili_watermark"):
        video_label = smart.add_blur_region(filters, video_label, index, region, radius)
        index += 1
    return video_label, index


def render(
    input_path: Path,
    subtitle_path: Path,
    metadata_path: Path,
    output_path: Path,
    platform: str,
) -> None:
    for path in (input_path, subtitle_path, metadata_path):
        if not path.exists():
            raise FileNotFoundError(path)

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    region = metadata_region(metadata)
    width, height = smart.video_size(input_path)
    active = smart.timeline_enable(smart.parse_srt_intervals(subtitle_path))
    bottom_threshold = smart.env_float("OCR_OVERLAY_BOTTOM_THRESHOLD", 0.68, 0.0)
    bottom_mode = region[1] + region[3] >= bottom_threshold

    filters: list[str] = []
    video_label = "0:v"
    cleanup_index = 0

    video_label, cleanup_index = add_bilibili_cleanup(
        filters, video_label, cleanup_index, platform
    )

    if bottom_mode:
        x, y, w, h = region
        alpha = clamp(smart.env_float("OCR_OVERLAY_BOTTOM_BOX_ALPHA", 0.88, 0.0), 0.0, 1.0)
        enable = f":enable='{active}'" if active else ""
        out = f"ocrbox{cleanup_index}"
        filters.append(
            f"[{video_label}]drawbox=x=iw*{x:.5f}:y=ih*{y:.5f}:"
            f"w=iw*{w:.5f}:h=ih*{h:.5f}:color=black@{alpha:.3f}:t=fill{enable}[{out}]"
        )
        video_label = out
        cleanup_index += 1
        base.log("OCR overlay: lower caption -> black box + white Vietnamese text")
    else:
        blur = smart.env_int("OCR_OVERLAY_SOURCE_BLUR", 11, 2)
        region_dict = {
            "x": region[0],
            "y": region[1],
            "w": region[2],
            "h": region[3],
            "confidence": 1.0,
        }
        video_label = smart.add_blur_region(
            filters, video_label, cleanup_index, region_dict, blur, active
        )
        cleanup_index += 1
        base.log("OCR overlay: upper/middle caption -> blur source text + overlay Vietnamese text")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path = output_path.parent / f"{output_path.stem}.ass"
    build_positioned_ass(subtitle_path, ass_path, width, height, region, bottom_mode)
    escaped = base.escape_subtitle_path(ass_path)
    filters.append(f"[{video_label}]subtitles='{escaped}'[vout]")

    cmd = ["ffmpeg", "-y", "-i", str(input_path), "-filter_complex", ";".join(filters), "-map", "[vout]"]
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
    base.log(f"OCR overlay render: platform={platform or 'unknown'}, region={region}")
    base.run(cmd)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--subtitle", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--platform", default="")
    args = parser.parse_args()
    render(
        Path(args.input).resolve(),
        Path(args.subtitle).resolve(),
        Path(args.metadata).resolve(),
        Path(args.output).resolve(),
        args.platform,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        base.log(f"ERROR: {error}")
        raise SystemExit(1)
