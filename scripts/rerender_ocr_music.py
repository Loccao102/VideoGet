#!/usr/bin/env python3
"""Re-render edited OCR+Music subtitles with the shared OCR Overlay/brand policy.

This path deliberately skips OCR, translation, Whisper and TTS. It reuses the
metadata emitted by the OCR+Music pipeline, overlays the edited Vietnamese SRT,
replaces detected Bilibili branding when possible, then rebuilds the music mix.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path

import localize as base
import localize_ocr_music as ocr
import render_ocr_overlay
import smart_render as smart


def probe_duration(path: Path) -> float:
    process = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "ffprobe duration failed")
    try:
        duration = float((process.stdout or "0").strip())
    except ValueError as error:
        raise RuntimeError("invalid video duration") from error
    if duration <= 0:
        raise RuntimeError("video duration is unavailable")
    return duration


def metadata_path(input_path: Path, output_dir: Path) -> Path:
    return output_dir / f"{input_path.stem}.ocr-music.json"


def load_metadata(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def resolve_text_region(input_path: Path, output_dir: Path, metadata: dict) -> tuple[float, float, float, float]:
    raw = ((metadata.get("ocr") or {}).get("textRegion") or []) if isinstance(metadata, dict) else []
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        try:
            return tuple(float(item) for item in raw)  # type: ignore[return-value]
        except (TypeError, ValueError):
            pass
    width, height = smart.video_size(input_path)
    return ocr.detect_ocr_region(input_path, output_dir, width, height)


def resolve_music(input_path: Path, metadata: dict) -> Path | None:
    raw = str(metadata.get("musicTrack") or "").strip() if isinstance(metadata, dict) else ""
    if raw:
        path = Path(raw)
        if path.is_file():
            return path
    return ocr.choose_music(input_path)


def infer_platform(input_path: Path, metadata: dict) -> str:
    raw = str(metadata.get("sourcePlatform") or "").strip().lower()
    if raw:
        return raw
    if re.search(r"\[bv[0-9a-z]+\]", input_path.name, flags=re.IGNORECASE):
        return "bilibili"
    if "bilibili" in input_path.name.lower():
        return "bilibili"
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-render edited OCR+Music subtitles with brand replacement")
    parser.add_argument("--input", required=True)
    parser.add_argument("--subtitle", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--aspect", default="original")
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    subtitle_path = Path(args.subtitle).resolve()
    output_path = Path(args.output).resolve()
    output_dir = output_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    if not subtitle_path.is_file():
        raise FileNotFoundError(subtitle_path)

    meta_path = metadata_path(input_path, output_dir)
    metadata = load_metadata(meta_path)
    text_region = resolve_text_region(input_path, output_dir, metadata)
    music = resolve_music(input_path, metadata)
    duration = float(((metadata.get("ocr") or {}).get("duration") or 0)) if metadata else 0.0
    if duration <= 0:
        duration = probe_duration(input_path)
    platform = infer_platform(input_path, metadata)
    aspect = args.aspect.strip().lower() or "original"

    with tempfile.TemporaryDirectory(prefix="videoget-ocr-rerender-") as temp:
        subbed = Path(temp) / "ocr-subbed.mp4"
        if meta_path.is_file():
            try:
                render_ocr_overlay.render(
                    input_path, subtitle_path, meta_path, subbed, platform, aspect=aspect
                )
            except Exception as error:
                base.log(f"OCR+Music overlay re-render failed; falling back to legacy render: {error}")
                ocr.render_ocr_subtitles(input_path, subtitle_path, subbed, text_region, None, aspect)
        else:
            ocr.render_ocr_subtitles(input_path, subtitle_path, subbed, text_region, None, aspect)
        ocr.mix_music(subbed, music, output_path, duration)

    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise RuntimeError("OCR music re-render produced no usable video")
    base.log(f"OCR+Music re-render complete: {output_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        base.log(f"ERROR: {error}")
        raise SystemExit(1)
