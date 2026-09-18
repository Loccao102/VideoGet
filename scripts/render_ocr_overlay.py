#!/usr/bin/env python3
"""Render Vietnamese OCR subtitles at source positions and replace Bilibili branding.

Overlay styles:
- clean (default): blur source text, then place bold Vietnamese text with outline/shadow;
- capsule: clean + a small translucent backing around the translated line;
- box: legacy heavy black box for lower captions, blur for upper captions.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

import bilibili_brand
import brand_badge
import localize as base
import smart_render as smart


STYLE_ALIASES = {
    "": "clean",
    "clean": "clean",
    "ocr_clean": "clean",
    "ocr_overlay": "clean",
    "capsule": "capsule",
    "ocr_capsule": "capsule",
    "box": "box",
    "ocr_box": "box",
}


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def normalize_style(value: str | None) -> str:
    raw = (value or os.getenv("OCR_OVERLAY_STYLE", "clean")).strip().lower()
    style = STYLE_ALIASES.get(raw)
    if style is None:
        raise RuntimeError("OCR overlay style must be clean, capsule, or box")
    return style


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


def expand_min_region(region: tuple[float, float, float, float], min_width: float, min_height: float) -> tuple[float, float, float, float]:
    x, y, w, h = region
    target_w = clamp(max(w, min_width), 0.02, 0.96)
    target_h = clamp(max(h, min_height), 0.02, 0.30)
    cx = x + w / 2.0
    cy = y + h / 2.0
    nx = clamp(cx - target_w / 2.0, 0.0, 1.0 - target_w)
    ny = clamp(cy - target_h / 2.0, 0.0, 1.0 - target_h)
    return nx, ny, target_w, target_h


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
        if body:
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
        detail_regions = []
        for detail in raw.get("bboxRegions") or []:
            try:
                detail_regions.append(normalize_region(detail, pad=False))
            except (TypeError, ValueError):
                continue
        out.append({
            "start": start,
            "end": end,
            # bbox from ocr_segment_regions is already padded for placement.
            # Do not expand it a second time here.
            "bbox": normalize_region(raw.get("bbox"), pad=False),
            "regions": detail_regions,
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
    min_bottom_width = smart.env_float("OCR_OVERLAY_BOTTOM_MIN_WIDTH", 0.24, 0.02)
    min_bottom_height = smart.env_float("OCR_OVERLAY_BOTTOM_MIN_HEIGHT", 0.050, 0.02)
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
            source_region = best["bbox"]
            source_regions = best.get("regions") or [source_region]
            placement = best.get("placement", "")
            entry["bboxMatched"] = True
            entry["bboxConfidence"] = best.get("confidence", 0.0)
            matched += 1
        else:
            source_region = fallback_region
            source_regions = [source_region]
            placement = ""
            entry["bboxMatched"] = False
            entry["bboxConfidence"] = 0.0

        if placement in {"bottom", "upper"}:
            is_bottom = placement == "bottom"
        else:
            is_bottom = source_region[1] + source_region[3] >= threshold
        entry["sourceRegion"] = source_region
        entry["sourceRegions"] = source_regions
        entry["bottom"] = is_bottom
        entry["region"] = expand_min_region(source_region, min_bottom_width, min_bottom_height) if is_bottom else source_region
    return matched


def build_positioned_ass(entries: list[dict], ass_path: Path, width: int, height: int, render_style: str) -> None:
    font = os.getenv("VIDEO_SUBTITLE_FONT", "Noto Sans")
    bottom_size = smart.env_int("OCR_OVERLAY_BOTTOM_FONT_SIZE", smart.env_int("OCR_OVERLAY_FONT_SIZE", max(18, min(42, int(min(width, height) * 0.047))), 12), 12)
    upper_size = smart.env_int("OCR_OVERLAY_UPPER_FONT_SIZE", smart.env_int("OCR_OVERLAY_FONT_SIZE", max(18, min(38, int(min(width, height) * 0.042))), 12), 12)

    if render_style == "clean":
        bottom_outline = smart.env_float("OCR_OVERLAY_CLEAN_OUTLINE", 2.8, 0.0)
        upper_outline = smart.env_float("OCR_OVERLAY_CLEAN_UPPER_OUTLINE", 2.8, 0.0)
        shadow = smart.env_float("OCR_OVERLAY_CLEAN_SHADOW", 1.1, 0.0)
    elif render_style == "capsule":
        bottom_outline = smart.env_float("OCR_OVERLAY_CAPSULE_OUTLINE", 2.2, 0.0)
        upper_outline = smart.env_float("OCR_OVERLAY_CAPSULE_UPPER_OUTLINE", 2.2, 0.0)
        shadow = smart.env_float("OCR_OVERLAY_CAPSULE_SHADOW", 0.9, 0.0)
    else:
        bottom_outline = smart.env_float("OCR_OVERLAY_TEXT_OUTLINE", 1.6, 0.0)
        upper_outline = smart.env_float("OCR_OVERLAY_UPPER_TEXT_OUTLINE", 2.2, 0.0)
        shadow = smart.env_float("OCR_OVERLAY_BOX_SHADOW", 0.0, 0.0)

    margin_lr = max(12, int(width * 0.04))
    header = f"""[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: OCRBottom,{font},{bottom_size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H00000000,-1,0,0,0,100,100,0,0,1,{bottom_outline:.1f},{shadow:.1f},5,{margin_lr},{margin_lr},0,1\nStyle: OCRUpper,{font},{upper_size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H00000000,-1,0,0,0,100,100,0,0,1,{upper_outline:.1f},{shadow:.1f},5,{margin_lr},{margin_lr},0,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"""
    events = []
    for entry in entries:
        x, y, w, h = entry["region"]
        anchor_x = int((x + w / 2.0) * width)
        anchor_y = int((y + h / 2.0) * height)
        style = "OCRBottom" if entry.get("bottom") else "OCRUpper"
        events.append(
            "Dialogue: 0,{start},{end},{style},,0,0,0,,{{\\an5\\pos({x},{y})\\q2}}{text}".format(
                start=ass_time(float(entry["start"])), end=ass_time(float(entry["end"])),
                style=style, x=anchor_x, y=anchor_y, text=entry["text"],
            )
        )
    ass_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def _region_dict(raw) -> dict:
    x, y, w, h = normalize_region(raw, pad=False)
    return {"x": x, "y": y, "w": w, "h": h, "confidence": 1.0}


def resolve_bilibili_brand(input_path: Path, metadata: dict, platform: str) -> tuple[list[dict], str, dict | None]:
    if platform.lower().strip() != "bilibili" or not smart.env_bool("OCR_OVERLAY_HIDE_BILIBILI", True):
        return [], "", None

    manual_raw = os.getenv("OCR_OVERLAY_BILIBILI_MANUAL_REGIONS", "").strip()
    if manual_raw:
        regions = smart.parse_regions(manual_raw, "bilibili_watermark")
        side = bilibili_brand.side_from_regions(regions)
        return regions, side, {"source": "manual", "side": side}

    cached = metadata.get("bilibiliBrand") if isinstance(metadata.get("bilibiliBrand"), dict) else None
    if cached and not smart.env_bool("OCR_OVERLAY_BILIBILI_REDETECT", False):
        raw = cached.get("region")
        if isinstance(raw, (list, tuple)) and len(raw) == 4 and cached.get("side") in {"left", "right"}:
            return [_region_dict(raw)], str(cached["side"]), cached

    detected = bilibili_brand.detect(input_path)
    if detected and detected.get("region") and detected.get("side") in {"left", "right"}:
        metadata["bilibiliBrand"] = detected
        return [_region_dict(detected["region"])], str(detected["side"]), detected

    fallback_raw = os.getenv("OCR_OVERLAY_BILIBILI_REGIONS", "").strip()
    if fallback_raw:
        regions = smart.parse_regions(fallback_raw, "bilibili_watermark_fallback")
        side = bilibili_brand.side_from_regions(regions)
        return regions, side, {"source": "fallback", "side": side}
    return [], "", None


def add_bilibili_cleanup(filters: list[str], video_label: str, start_index: int, regions: list[dict]) -> tuple[str, int]:
    radius = smart.env_int("OCR_OVERLAY_BILIBILI_BLUR", 17, 2)
    index = start_index
    for region in regions:
        video_label = smart.add_blur_region(filters, video_label, index, region, radius)
        index += 1
    return video_label, index


def render(
    input_path: Path,
    subtitle_path: Path,
    metadata_path: Path,
    output_path: Path,
    platform: str,
    style: str | None = None,
) -> None:
    for path in (input_path, subtitle_path, metadata_path):
        if not path.exists():
            raise FileNotFoundError(path)

    render_style = normalize_style(style)
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

    brand_regions, brand_side, brand_detection = resolve_bilibili_brand(input_path, metadata, platform)
    video_label, cleanup_index = add_bilibili_cleanup(filters, video_label, cleanup_index, brand_regions)

    source_blur = smart.env_int("OCR_OVERLAY_SOURCE_BLUR", 20, 2)
    source_blur_power = smart.env_int("OCR_OVERLAY_SOURCE_BLUR_POWER", 2, 1)
    capsule_alpha = clamp(smart.env_float("OCR_OVERLAY_CAPSULE_ALPHA", 0.24, 0.0), 0.0, 1.0)
    box_alpha = clamp(smart.env_float("OCR_OVERLAY_BOTTOM_BOX_ALPHA", 0.88, 0.0), 0.0, 1.0)
    max_segments = smart.env_int("OCR_OVERLAY_MAX_SEGMENTS", 240, 10)

    for entry in entries[:max_segments]:
        x, y, w, h = entry["region"]
        source_x, source_y, source_w, source_h = entry.get("sourceRegion", entry["region"])
        source_region = {"x": source_x, "y": source_y, "w": source_w, "h": source_h, "confidence": 1.0}
        source_regions = []
        for raw_region in entry.get("sourceRegions") or [entry.get("sourceRegion", entry["region"])]:
            rx, ry, rw, rh = raw_region
            source_regions.append({"x": rx, "y": ry, "w": rw, "h": rh, "confidence": 1.0})
        enable = f"between(t,{float(entry['start']):.3f},{float(entry['end']):.3f})"

        if render_style == "box" and entry.get("bottom"):
            out = f"ocrbox{cleanup_index}"
            filters.append(
                f"[{video_label}]drawbox=x=iw*{x:.5f}:y=ih*{y:.5f}:w=iw*{w:.5f}:h=ih*{h:.5f}:"
                f"color=black@{box_alpha:.3f}:t=fill:enable='{enable}'[{out}]"
            )
            video_label = out
            cleanup_index += 1
        else:
            # Clean and capsule always remove the original characters first. Box mode
            # does the same for upper/middle captions; only lower box mode fully covers.
            # Prefer the tight OCR line/word regions. Older metadata falls back
            # to the single union bbox, but new jobs no longer blur one oversized block.
            for tight_region in source_regions:
                video_label = smart.add_blur_region(
                    filters,
                    video_label,
                    cleanup_index,
                    tight_region,
                    source_blur,
                    enable,
                    source_blur_power,
                )
                cleanup_index += 1

            if render_style == "capsule":
                out = f"ocrcapsule{cleanup_index}"
                filters.append(
                    f"[{video_label}]drawbox=x=iw*{x:.5f}:y=ih*{y:.5f}:w=iw*{w:.5f}:h=ih*{h:.5f}:"
                    f"color=black@{capsule_alpha:.3f}:t=fill:enable='{enable}'[{out}]"
                )
                video_label = out
                cleanup_index += 1

    if len(entries) > max_segments:
        base.log(f"OCR overlay: cleanup limited to first {max_segments}/{len(entries)} subtitle entries")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path = output_path.parent / f"{output_path.stem}.ass"
    build_positioned_ass(entries, ass_path, width, height, render_style)
    escaped = base.escape_subtitle_path(ass_path)
    subtitle_label = "ocrsubbed"
    filters.append(f"[{video_label}]subtitles='{escaped}'[{subtitle_label}]")
    video_label = subtitle_label

    badge_path = None
    if platform.lower().strip() == "bilibili" and brand_side in {"left", "right"}:
        badge_path = brand_badge.ensure(output_path.parent)
        if badge_path is not None:
            badge_width = max(120, int(width * smart.env_float("BRAND_WATERMARK_WIDTH", 0.20, 0.08)))
            margin_x = max(4, int(width * smart.env_float("BRAND_WATERMARK_MARGIN_X", 0.012, 0.0)))
            margin_y = max(4, int(height * smart.env_float("BRAND_WATERMARK_MARGIN_Y", 0.012, 0.0)))
            filters.append(f"[1:v]scale={badge_width}:-1[brandbadge]")
            overlay_x = str(margin_x) if brand_side == "left" else f"W-w-{margin_x}"
            branded = "brandout"
            filters.append(
                f"[{video_label}][brandbadge]overlay=x={overlay_x}:y={margin_y}:format=auto:eof_action=repeat[{branded}]"
            )
            video_label = branded

    if video_label != "vout":
        filters.append(f"[{video_label}]null[vout]")

    cmd = ["ffmpeg", "-y", "-i", str(input_path)]
    if badge_path is not None:
        cmd += ["-i", str(badge_path)]
    cmd += ["-filter_complex", ";".join(filters), "-map", "[vout]"]
    if base.has_audio_stream(input_path):
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "192k"]
    cmd += [
        "-c:v", "libx264", "-preset", os.getenv("VIDEO_PRESET", "veryfast"),
        "-crf", os.getenv("VIDEO_CRF", "21"), "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(output_path),
    ]

    metadata["lastOverlayStyle"] = render_style
    if brand_detection is not None or metadata.get("lastOverlayStyle") != render_style:
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    base.log(
        f"OCR overlay render: style={render_style}, platform={platform or 'unknown'}, "
        f"segment bbox matched={matched}/{len(entries)}, sourceBlur={source_blur}x{source_blur_power}, "
        f"brandSide={brand_side or 'none'}, "
        f"brandDetect={(brand_detection or {}).get('source', 'none')}"
    )
    base.run(cmd)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--subtitle", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--platform", default="")
    parser.add_argument("--style", default="")
    args = parser.parse_args()
    render(
        Path(args.input).resolve(),
        Path(args.subtitle).resolve(),
        Path(args.metadata).resolve(),
        Path(args.output).resolve(),
        args.platform,
        args.style,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        base.log(f"ERROR: {error}")
        raise SystemExit(1)
