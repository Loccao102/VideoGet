#!/usr/bin/env python3
"""OCR+Music entrypoint that reuses the OCR Overlay/brand renderer.

The legacy OCR+Music pipeline still owns OCR, translation and music mixing. This
wrapper replaces only its intermediate subtitle-render step so OCR+Music and
OCR->Sub Việt share the same per-segment placement and Bilibili brand policy.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

import localize as base
import localize_ocr_music as legacy
import ocr_segment_regions
import render_ocr_overlay


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def infer_platform(input_path: Path) -> str:
    explicit = os.getenv("VIDEOGET_SOURCE_PLATFORM", "").strip().lower()
    if explicit:
        return explicit
    if re.search(r"\[bv[0-9a-z]+\]", input_path.name, flags=re.IGNORECASE):
        return "bilibili"
    if "bilibili" in input_path.name.lower():
        return "bilibili"
    return ""


def _srt_time(value: str) -> float:
    hours, minutes, rest = value.replace(".", ",").split(":", 2)
    seconds, millis = rest.split(",", 1)
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis[:3].ljust(3, "0")) / 1000.0


def parse_source_srt(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    content = path.read_text(encoding="utf-8-sig")
    segments: list[dict] = []
    timing = re.compile(
        r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2}[,.]\d{3})"
    )
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        timing_index = 1 if lines[0].isdigit() else 0
        if timing_index >= len(lines):
            continue
        match = timing.search(lines[timing_index])
        if not match:
            continue
        start = _srt_time(match.group(1))
        end = _srt_time(match.group(2))
        text = " ".join(lines[timing_index + 1 :]).strip()
        if text and end > start:
            segments.append({"id": len(segments), "start": start, "end": end, "text": text})
    return segments


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--aspect", default="original")
    known, _ = parser.parse_known_args()

    input_path = Path(known.input).resolve()
    output_dir = Path(known.output_dir).resolve()
    stem = input_path.stem
    platform = infer_platform(input_path)
    aspect = known.aspect.strip().lower() or "original"
    original_render = legacy.render_ocr_subtitles
    captured: dict = {}
    temp_metadata = output_dir / f".{stem}.ocr-music-overlay.json"

    def branded_render(
        source: Path,
        subtitle: Path,
        output: Path,
        text_region: tuple[float, float, float, float],
        segments: list[dict] | None = None,
        render_aspect: str = "original",
    ) -> None:
        if not segments:
            original_srt = output_dir / f"{stem}.ocr.original.srt"
            segments = parse_source_srt(original_srt)
        bbox_count = sum(1 for segment in (segments or []) if segment.get("bbox"))

        metadata = {
            "input": str(source),
            "mode": "ocr_music",
            "sourcePlatform": platform,
            "outputAspect": render_aspect or aspect,
            "segments": segments or [],
            "ocr": {
                "region": list(text_region),
                "textRegion": list(text_region),
                "segmentBBoxCount": bbox_count,
            },
        }
        temp_metadata.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            render_ocr_overlay.render(
                source,
                subtitle,
                temp_metadata,
                output,
                platform,
                aspect=render_aspect or aspect,
            )
            captured.update(json.loads(temp_metadata.read_text(encoding="utf-8")))
        except Exception as error:
            if not env_bool("OCR_MUSIC_OVERLAY_FALLBACK", True):
                raise
            base.log(f"OCR+Music overlay render failed; falling back to legacy render: {error}")
            original_render(source, subtitle, output, text_region, segments, render_aspect or aspect)

    legacy.render_ocr_subtitles = branded_render
    try:
        legacy.main()
    finally:
        legacy.render_ocr_subtitles = original_render

    final_metadata = output_dir / f"{stem}.ocr-music.json"
    if final_metadata.is_file() and captured:
        try:
            current = json.loads(final_metadata.read_text(encoding="utf-8"))
            current["sourcePlatform"] = platform
            current["initialRenderStyle"] = "ocr_overlay"
            if captured.get("segments"):
                current["segments"] = captured["segments"]
            if captured.get("bilibiliBrand"):
                current["bilibiliBrand"] = captured["bilibiliBrand"]
            current_ocr = current.setdefault("ocr", {})
            captured_ocr = captured.get("ocr") or {}
            if "segmentBBoxCount" in captured_ocr:
                current_ocr["segmentBBoxCount"] = captured_ocr["segmentBBoxCount"]
            final_metadata.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as error:
            base.log(f"Could not enrich OCR+Music metadata: {error}")

    try:
        temp_metadata.unlink(missing_ok=True)
    except OSError:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        base.log(f"ERROR: {error}")
        raise SystemExit(1)
