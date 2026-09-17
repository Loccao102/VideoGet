#!/usr/bin/env python3
"""Render edited Vietnamese OCR subtitles back onto the source video.

New OCR jobs store one normalized bbox per timed OCR segment. This renderer matches
edited SRT entries back to those OCR segments by timestamp, so each Vietnamese line
can cover and occupy the exact source-caption area that produced it.

Rules:
- lower caption: black box over that segment's source bbox + white Vietnamese text;
- upper/middle caption: blur that segment's source bbox + white Vietnamese text;
- Bilibili: optionally blur a conservative channel/watermark strip near the top;
- old jobs or heavily retimed SRT entries fall back to the video-level OCR region.
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


def normalize_region(raw, fallback=(0.06, 0.72, 0.88, 0.18), *, pad: bool = True) -> tuple[float, float, float, float]:
    try:
        x, y, w, h = [float(value) for value in raw]
    except (TypeError, ValueError):
        x, y, w, h = fallback
    x = clamp(x, 0.0, 0.98)
    y = clamp(y, 0.0, 0.98)
    w = clamp(w, 0.02, 1.0 - x)
    h = clamp(h, 0.02, 1.0 - y)
    if not pad:
        return x, y, w, h
    pad_x = smart.env_float("OCR_OVERLAY_REGION_PAD_X", 0.018, 0.0)
    pad_y = smart.env_float("OCR_OVERLAY_REGION_PAD_Y", 0.012, 0.0)
    nx = clamp(x - pad_x, 0.0, 0.98)
    ny = clamp(y - pad_y, 0.0, 0.98)
    right = clamp(x + w + pad_x, nx + 0.02, 1.0)
    bottom = clamp(y + h + pad_y, ny + 0.02, 1.0)
    return nx, ny, right - nx, bottom - ny


def metadata_region(metadata: dict) -> tuple[float, float, float, float]:
    ocr = metadata.get("ocr") or {}
    raw = ocr.get("textRegion") or ocr.get("region") or [0.06, 0.72, 0.88, 0.18]
    return normalize_region(raw)


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


def metadata_segments(metadata: dict) -> list[dict]:
    out = []
    for raw in metadata.get("segments") or []:
        if not isinstance(raw, dict) or not raw.get("bbox"):
            continue
        try:
            start = float(raw.get("start", 0.0))
            end = float(raw.get("end", start))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        out.append({
            "start": start,
            "end": end,
            "bbox": normalize_region(raw.get("bbox")),
            "placement": str(raw.get("placement", "")).strip().lower(),
            "confidence": float(raw.get("bboxConfidence", 0.0) or 0.0),
        })
    return out


def _overlap(left: dict, right: dict) -> float:
    return max(0.0, min(float(left["end"]), float(right["end"])) - max(float(left["start"]), float(right["start"])))


def _center(item: dict) -> float:
    return (float(item["start"]) + float(item["end"])) / 2.0


def attach_regions(entries: list[dict], metadata: dict, fallback_region: tuple[float, float, float, float]) -> int:
    segments = metadata_segments(metadata)
    threshold = smart.env_float("OCR_OVERLAY_BOTTOM_THRESHOLD", 0.68, 0.0)
    max_gap = smart.env_float("OCR_OVERLAY_MATCH_MAX_GAP_SEC", 1.5, 0.0)
    matched = 0

    for entry in entries:
        best = None
        best_overlap = 0.0
        for segment in segments:
            overlap = _overlap(entry, segment)
            if overlap > best_overlap:
                best_overlap = overlap
                best = segment

        if best is None and segments:
            candidate = min(segments, key=lambda item: abs(_center(entry) - _center(item)))
            if abs(_center(entry) - _center(candidate)) <= max_gap:
                best = candidate

        if best is not None:
            region = best["bbox"]
            placement = best.get("placement", "")
            entry["bboxMatched"] = True
            entry["bboxConfidence"] = best.get("confidence", 0.0)
            matched += 1
        else:
            region = fallback_region
            placement = ""
            entry["bboxMatched"] = False
            entry["bboxConfidence"] = 0.0

        entry["region"] = region
        if placement in {"bottom", "upper"}:
            entry["bottom"] = placement == "bottom"
        else:
            entry["bottom"] = region[1] + region[3] >= threshold

    return matched


def build_positioned_ass(entries: list[dict], ass_path: Path, width: int, height: int) -> None:
    font = os.getenv("VIDEO_SUBTITLE_FONT", "Noto Sans")
    bottom_size = smart.env_int(
        "OCR_OVERLAY_BOTTOM_FONT_SIZE",
        smart.env_int("OCR_OVERLAY_FONT_SIZE", max(18, min(44, int(min(width, height) * 0.050))), 12),
        12,
    )
    upper_size = smart.env_int(
        "OCR_OVERLAY_UPPER_FONT_SIZE",
        smart.env_int("OCR_OVERLAY_FONT_SIZE", max(18, min(40, int(min(width, height) * 0.044))), 12),
        12,
    )
    bottom_outline = smart.env_float("OCR_OVERLAY_TEXT_OUTLINE", 1.6, 0.0)
    upper_outline = smart.env_float("OCR_OVERLAY_UPPER_TEXT_OUTLINE", 2.2, 0.0)
    margin_lr = max(12, int(width * 0.04))
    header = f"""[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: OCRBottom,{font},{bottom_size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H00000000,-1,0,0,0,100,100,0,0,1,{bottom_outline:.1f},0,5,{margin_lr},{margin_lr},0,1\nStyle: OCRUpper,{font},{upper_size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H00000000,-1,0,0,0,100,100,0,0,1,{upper_outline:.1f},0,5,{margin_lr},{margin_lr},0,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"""
    events = []
    for entry in entries:
        x, y, w, h = entry["region"]
        anchor_x = int((x + w / 2.0) * width)
        anchor_y = int((y + h / 2.0) * height)
        style = "OCRBottom" if entry.get("bottom") else "OCRUpper"
        events.append(
            "Dialogue: 0,{start},{end},{style},,0,0,0,,"
            "{{\\an5\\pos({x},{y})\\q2}}{text}".format(
                start=ass_time(float(entry["start"])),
                end=ass_time(float(entry["end"])),
                style=style,
                x=anchor_x,
                y=anchor_y,
                text=entry["text"],
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
    fallback_region = metadata_region(metadata)
    entries = parse_srt(subtitle_path)
    if not entries:
        raise RuntimeError("subtitle file has no usable timed entries")
    matched = attach_regions(entries, metadata, fallback_region)

    width, height = smart.video_size(input_path)
    filters: list[str] = []
    video_label = "0:v"
    cleanup_index = 0

    video_label, cleanup_index = add_bilibili_cleanup(
        filters, video_label, cleanup_index, platform
    )

    alpha = clamp(smart.env_float("OCR_OVERLAY_BOTTOM_BOX_ALPHA", 0.88, 0.0), 0.0, 1.0)
    blur = smart.env_int("OCR_OVERLAY_SOURCE_BLUR", 11, 2)
    max_segments = smart.env_int("OCR_OVERLAY_MAX_SEGMENTS", 240, 10)

    for entry in entries[:max_segments]:
        x, y, w, h = entry["region"]
        enable = f"between(t,{float(entry['start']):.3f},{float(entry['end']):.3f})"
        if entry.get("bottom"):
            out = f"ocrbox{cleanup_index}"
            filters.append(
                f"[{video_label}]drawbox=x=iw*{x:.5f}:y=ih*{y:.5f}:"
                f"w=iw*{w:.5f}:h=ih*{h:.5f}:color=black@{alpha:.3f}:t=fill:"
                f"enable='{enable}'[{out}]"
            )
            video_label = out
            cleanup_index += 1
        else:
            region_dict = {"x": x, "y": y, "w": w, "h": h, "confidence": 1.0}
            video_label = smart.add_blur_region(
                filters, video_label, cleanup_index, region_dict, blur, enable
            )
            cleanup_index += 1

    if len(entries) > max_segments:
        base.log(f"OCR overlay: cleanup limited to first {max_segments}/{len(entries)} subtitle entries")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path = output_path.parent / f"{output_path.stem}.ass"
    build_positioned_ass(entries, ass_path, width, height)
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
    base.log(
        f"OCR overlay render: platform={platform or 'unknown'}, "
        f"segment bbox matched={matched}/{len(entries)}"
    )
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
