#!/usr/bin/env python3
"""Smart final-video renderer for VideoGet.

Conservative by default: never crops the frame, only cleans regions that visual
analysis identifies with enough confidence, and reuses the source subtitle footprint
when possible. SAFE/SMART/AGGRESSIVE modes are selected by VIDEO_CLEANUP_MODE.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import localize as base

try:
    import analyze_layout
except Exception:
    analyze_layout = None

RENDER_VERSION = 1


def env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return max(minimum, default)


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except ValueError:
        return max(minimum, default)


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def cleanup_mode() -> str:
    if not env_bool("VIDEO_CLEANUP", True):
        return "safe"
    value = os.getenv("VIDEO_CLEANUP_MODE", "smart").strip().lower()
    return value if value in {"safe", "smart", "aggressive", "legacy"} else "smart"


def video_size(path: Path) -> tuple[int, int]:
    process = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "ffprobe video size failed")
    data = json.loads(process.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError("video stream not found")
    return int(streams[0]["width"]), int(streams[0]["height"])


def source_signature(path: Path, mode: str) -> dict:
    stat = path.stat()
    return {
        "renderVersion": RENDER_VERSION,
        "analyzerVersion": getattr(analyze_layout, "ANALYZER_VERSION", 0) if analyze_layout else 0,
        "size": stat.st_size,
        "mtimeNs": stat.st_mtime_ns,
        "mode": mode,
        "samples": env_int("VIDEO_ANALYSIS_SAMPLES", 12, 3),
    }


def default_layout(width: int, height: int, mode: str, reason: str) -> dict:
    return {
        "version": 0,
        "mode": mode,
        "width": width,
        "height": height,
        "sourceSubtitle": None,
        "watermarks": [],
        "protectedRegions": [],
        "subtitlePlacement": {
            "anchorX": 0.5,
            "anchorY": 0.86 if height >= width else 0.90,
            "alignment": 2,
            "reason": reason,
        },
        "strategy": "preserve_frame_no_crop",
    }


def manual_region(value: str, kind: str) -> dict | None:
    try:
        x, y, w, h = [float(item.strip()) for item in value.split(",")]
    except (ValueError, TypeError):
        return None
    x, y = max(0.0, min(0.99, x)), max(0.0, min(0.99, y))
    w, h = max(0.01, min(1.0 - x, w)), max(0.01, min(1.0 - y, h))
    return {"x": x, "y": y, "w": w, "h": h, "confidence": 1.0, "kind": kind}


def parse_regions(value: str, kind: str) -> list[dict]:
    out = []
    for raw in value.split(";"):
        if region := manual_region(raw.strip(), kind):
            out.append(region)
    return out


def analyze_or_load(
    input_path: Path, output_path: Path, width: int, height: int, mode: str
) -> dict:
    cache_path = output_path.parent / f"{input_path.stem}.layout.json"
    signature = source_signature(input_path, mode)
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("signature") == signature and isinstance(cached.get("layout"), dict):
            base.log("Layout analysis cache hit")
            return cached["layout"]
    except (OSError, json.JSONDecodeError, TypeError):
        pass

    if mode == "legacy":
        layout = default_layout(width, height, mode, "legacy_bottom")
        layout["sourceSubtitle"] = manual_region(
            ",".join(
                [
                    os.getenv("VIDEO_SUBTITLE_MASK_X", "0.02"),
                    os.getenv("VIDEO_SUBTITLE_MASK_Y", "0.72"),
                    os.getenv("VIDEO_SUBTITLE_MASK_W", "0.96"),
                    os.getenv("VIDEO_SUBTITLE_MASK_H", "0.24"),
                ]
            ),
            "source_subtitle",
        )
        layout["watermarks"] = parse_regions(os.getenv("VIDEO_LOGO_MASKS", ""), "watermark")
    elif mode == "safe" or analyze_layout is None:
        layout = default_layout(
            width,
            height,
            mode,
            "safe_default" if mode == "safe" else "analysis_unavailable",
        )
    else:
        try:
            layout = analyze_layout.analyze(
                input_path,
                mode=mode,
                samples=env_int("VIDEO_ANALYSIS_SAMPLES", 12, 3),
            )
        except Exception as error:
            base.log(f"Layout analysis lỗi; giữ nguyên frame và chỉ burn sub: {error}")
            layout = default_layout(width, height, mode, "analysis_failed")

    override = os.getenv("VIDEO_SOURCE_SUBTITLE_REGION", "").strip()
    if override:
        if region := manual_region(override, "source_subtitle"):
            layout["sourceSubtitle"] = region
            layout["subtitlePlacement"] = {
                "anchorX": 0.5,
                "anchorY": min(0.92, region["y"] + region["h"] * 0.56),
                "alignment": 2,
                "reason": "manual_source_subtitle",
            }

    manual_logos = parse_regions(
        os.getenv("VIDEO_MANUAL_LOGO_REGIONS", ""), "watermark"
    )
    if manual_logos:
        layout["watermarks"] = manual_logos + list(layout.get("watermarks") or [])

    try:
        temp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        temp.write_text(
            json.dumps({"signature": signature, "layout": layout}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(cache_path)
    except OSError:
        pass
    return layout


def parse_srt_intervals(path: Path) -> list[tuple[float, float]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return []
    pattern = re.compile(
        r"(?m)^(\d{2}):(\d{2}):(\d{2}),(\d{3})\s+-->\s+"
        r"(\d{2}):(\d{2}):(\d{2}),(\d{3})"
    )
    intervals = []
    for match in pattern.finditer(text):
        values = [int(value) for value in match.groups()]
        start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000
        end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000
        if end > start:
            intervals.append((start, end))
    if not intervals:
        return []
    merged = [list(intervals[0])]
    for start, end in intervals[1:]:
        if start - merged[-1][1] <= 0.30:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(float(a), float(b)) for a, b in merged]


def timeline_enable(intervals: list[tuple[float, float]]) -> str:
    if not intervals:
        return ""
    limited = intervals[:220]
    return "+".join(
        f"between(t,{start:.3f},{end:.3f})" for start, end in limited
    )


def add_blur_region(
    filters: list[str],
    input_label: str,
    index: int,
    region: dict,
    radius: int,
    enable: str = "",
) -> str:
    x, y, w, h = region["x"], region["y"], region["w"], region["h"]
    base_label, crop_label = f"cleanbase{index}", f"cleancrop{index}"
    blur_label, output_label = f"cleanblur{index}", f"cleanout{index}"
    filters.append(f"[{input_label}]split=2[{base_label}][{crop_label}]")
    filters.append(
        f"[{crop_label}]crop=w=iw*{w:.5f}:h=ih*{h:.5f}:x=iw*{x:.5f}:y=ih*{y:.5f},"
        f"boxblur=luma_radius={radius}:luma_power=1:"
        f"chroma_radius={max(1, radius // 2)}:chroma_power=1[{blur_label}]"
    )
    enable_opt = f":enable='{enable}'" if enable else ""
    filters.append(
        f"[{base_label}][{blur_label}]overlay=x=main_w*{x:.5f}:"
        f"y=main_h*{y:.5f}{enable_opt}[{output_label}]"
    )
    return output_label


def add_delogo(
    filters: list[str],
    input_label: str,
    index: int,
    region: dict,
    width: int,
    height: int,
) -> str:
    pad = 3 if min(width, height) >= 480 else 2
    x = max(0, int(region["x"] * width) - pad)
    y = max(0, int(region["y"] * height) - pad)
    w = min(width - x, int(region["w"] * width) + pad * 2)
    h = min(height - y, int(region["h"] * height) + pad * 2)
    if w < 8 or h < 6:
        return input_label
    output = f"delogo{index}"
    filters.append(
        f"[{input_label}]delogo=x={x}:y={y}:w={w}:h={h}:show=0[{output}]"
    )
    return output


def srt_time_to_ass(value: str) -> str:
    match = re.match(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", value.strip())
    if not match:
        return "0:00:00.00"
    h, m, s, ms = [int(x) for x in match.groups()]
    return f"{h}:{m:02}:{s:02}.{int(ms / 10):02}"


def ass_escape(text: str) -> str:
    text = text.replace("\\", "／").replace("{", "(").replace("}", ")")
    text = "".join(ch for ch in text if ch == "\n" or ord(ch) >= 32)
    return text.strip()


def wrap_text(text: str, width_chars: int, max_lines: int = 2) -> str:
    text = " ".join(text.replace("\n", " ").split())
    if len(text) <= width_chars:
        return text
    words = text.split()
    lines: list[str] = []
    current = ""
    consumed = 0
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > width_chars:
            lines.append(current)
            consumed += len(current.split())
            current = word
            if len(lines) >= max_lines - 1:
                break
        else:
            current = candidate
    if len(lines) < max_lines:
        remaining = words[consumed:]
        final = " ".join(remaining) if remaining else current
        if final:
            lines.append(final)
    return "\\N".join(lines[:max_lines])


def build_ass_from_srt(
    srt_path: Path,
    ass_path: Path,
    layout: dict,
    width: int,
    height: int,
) -> None:
    text = srt_path.read_text(encoding="utf-8-sig")
    blocks = re.split(r"\n\s*\n", text.strip())
    anchor = layout.get("subtitlePlacement") or {}
    anchor_x = int(clamp(float(anchor.get("anchorX", 0.5)), 0.08, 0.92) * width)
    anchor_y = int(clamp(float(anchor.get("anchorY", 0.86)), 0.10, 0.94) * height)
    font = os.getenv("VIDEO_SUBTITLE_FONT", "Noto Sans")
    font_size = env_int(
        "VIDEO_SUBTITLE_FONT_SIZE",
        max(20, min(40, int(min(width, height) * 0.052))),
        12,
    )
    box_pad = env_float("VIDEO_SUBTITLE_BOX_PADDING", 7.0, 0.0)
    opacity = int(
        clamp(env_float("VIDEO_SUBTITLE_BOX_ALPHA", 0.58, 0.0), 0.0, 0.95) * 255
    )
    ass_alpha = 255 - opacity
    back = f"&H{ass_alpha:02X}000000"
    max_chars = env_int(
        "VIDEO_SUBTITLE_WRAP_CHARS", 32 if height >= width else 46, 12
    )
    max_lines = env_int("VIDEO_SUBTITLE_MAX_LINES", 2, 1)
    header = f"""[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: VG,{font},{font_size},&H00FFFFFF,&H00FFFFFF,&H00101010,{back},-1,0,0,0,100,100,0,0,3,{box_pad:.1f},0,2,{int(width*0.07)},{int(width*0.07)},0,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"""
    events = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        timing_index = 1 if re.fullmatch(r"\d+", lines[0]) else 0
        if timing_index >= len(lines) or "-->" not in lines[timing_index]:
            continue
        start_raw, end_raw = [
            part.strip() for part in lines[timing_index].split("-->", 1)
        ]
        caption = ass_escape(" ".join(lines[timing_index + 1 :]))
        if not caption:
            continue
        caption = wrap_text(caption, max_chars, max_lines)
        plain_len = len(caption.replace("\\N", " "))
        fs = max(16, int(font_size * 0.88)) if plain_len > max_chars * max_lines else font_size
        override = f"{{\\an2\\pos({anchor_x},{anchor_y})\\fs{fs}}}"
        events.append(
            f"Dialogue: 0,{srt_time_to_ass(start_raw)},{srt_time_to_ass(end_raw)},"
            f"VG,,0,0,0,,{override}{caption}"
        )
    ass_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def render_video(
    input_path: Path,
    voice_track: Path,
    vi_srt: Path,
    output_path: Path,
) -> None:
    burn_subtitles = env_bool("BURN_SUBTITLES", True)
    mode = cleanup_mode()
    cleanup_source_subtitles = env_bool("VIDEO_CLEANUP_SOURCE_SUBTITLES", True)
    cleanup_logos = env_bool("VIDEO_CLEANUP_LOGOS", True)
    color_grade = env_bool("VIDEO_COLOR_GRADE", True)
    original_volume = max(
        0.0, env_float("ORIGINAL_AUDIO_VOLUME", 0.08, 0.0)
    )
    source_has_audio = base.has_audio_stream(input_path)
    width, height = video_size(input_path)
    layout = analyze_or_load(input_path, output_path, width, height, mode)

    subtitle = layout.get("sourceSubtitle") or None
    watermarks = list(layout.get("watermarks") or [])
    placement = layout.get("subtitlePlacement") or {}
    base.log(
        f"Render profile={mode}; sourceSub={'yes' if subtitle else 'no'}; "
        f"watermarks={len(watermarks)}; subtitle="
        f"{placement.get('reason','default')}@{placement.get('anchorY','?')}"
    )

    cmd = ["ffmpeg", "-y", "-i", str(input_path), "-i", str(voice_track)]
    filters: list[str] = []
    if source_has_audio and original_volume > 0:
        filters.append(f"[0:a]volume={original_volume}[original]")
        filters.append(
            "[original][1:a]amix=inputs=2:duration=longest:normalize=0[aout]"
        )
        audio_map = "[aout]"
    else:
        audio_map = "1:a:0"

    video_label = "0:v"
    cleanup_index = 0
    if mode != "safe" and cleanup_source_subtitles and subtitle:
        confidence = float(subtitle.get("confidence", 1.0))
        threshold = 0.55 if mode in {"aggressive", "legacy"} else 0.66
        if confidence >= threshold:
            active = timeline_enable(parse_srt_intervals(vi_srt))
            video_label = add_blur_region(
                filters,
                video_label,
                cleanup_index,
                subtitle,
                env_int(
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
            video_label = add_delogo(
                filters,
                video_label,
                cleanup_index,
                region,
                width,
                height,
            )
            cleanup_index += 1

    if color_grade:
        graded = "vgraded"
        contrast = env_float("VIDEO_CONTRAST", 1.03, 0.1)
        saturation = env_float("VIDEO_SATURATION", 1.05, 0.0)
        brightness = float(os.getenv("VIDEO_BRIGHTNESS", "0.005"))
        filters.append(
            f"[{video_label}]eq=contrast={contrast}:saturation={saturation}:"
            f"brightness={brightness},unsharp=5:5:0.25:5:5:0.0[vgraded]"
        )
        video_label = graded

    if burn_subtitles:
        ass_path = output_path.parent / f"{input_path.stem}.vi.ass"
        build_ass_from_srt(vi_srt, ass_path, layout, width, height)
        escaped = base.escape_subtitle_path(ass_path)
        filters.append(f"[{video_label}]subtitles='{escaped}'[vout]")
        video_map = "[vout]"
    else:
        video_map = f"[{video_label}]" if video_label != "0:v" else "0:v:0"

    if filters:
        cmd += ["-filter_complex", ";".join(filters)]
    cmd += ["-map", video_map, "-map", audio_map]
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
    cmd += [
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-shortest",
        str(output_path),
    ]
    base.log(
        "Đang render: preserve-frame smart cleanup -> adaptive ASS subtitle -> final video"
    )
    base.run(cmd)
