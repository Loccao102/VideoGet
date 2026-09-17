#!/usr/bin/env python3
"""Local OCR -> Vietnamese subtitles -> burn into the original video/audio.

This mode reuses the OCR/translation primitives from localize_ocr_music but stops
before music replacement. It never loads Whisper or TTS and preserves the source
audio track while covering the OCR-detected source-caption footprint and burning
Vietnamese subtitles.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import localize as base
import localize_fast as fast  # noqa: F401 - installs optimized translation overrides on base
import localize_ocr_music as ocr


def main() -> None:
    parser = argparse.ArgumentParser(description="OCR source captions, translate to Vietnamese, keep original audio")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    stem = input_path.stem
    original_srt = output_dir / f"{stem}.ocr.original.srt"
    vi_srt = output_dir / f"{stem}.ocr.vi.srt"
    output_video = output_dir / f"{stem}.ocr-vi-subbed.mp4"
    metadata_path = output_dir / f"{stem}.ocr-subtitles.json"
    timings: dict[str, float] = {"transcribe": 0.0, "tts": 0.0, "music": 0.0}
    started = time.perf_counter()

    ocr_started = time.perf_counter()
    segments, boxes, video_info = ocr.extract_segments(input_path, output_dir)
    timings["ocr"] = round(time.perf_counter() - ocr_started, 3)
    if not segments:
        raise RuntimeError(
            "OCR did not detect timed subtitle text. Try lowering OCR_MIN_CONFIDENCE, "
            "increasing OCR_FPS, or set OCR_SUBTITLE_REGION=x,y,w,h."
        )
    base.write_srt(original_srt, segments, "text")

    translate_started = time.perf_counter()
    if ocr.env_bool("OCR_TRANSLATE", True):
        base.translate_segments(segments, os.getenv("OCR_SOURCE_LANGUAGE", "zh"))
    else:
        for segment in segments:
            segment["vi"] = segment["text"]
    timings["translate"] = round(time.perf_counter() - translate_started, 3)
    base.write_srt(vi_srt, segments, "vi")

    fallback_region = tuple(float(value) for value in video_info["region"])
    text_region = ocr.aggregate_text_region(boxes, fallback_region)
    render_started = time.perf_counter()
    ocr.render_ocr_subtitles(input_path, vi_srt, output_video, text_region)
    timings["render"] = round(time.perf_counter() - render_started, 3)
    timings["total"] = round(time.perf_counter() - started, 3)

    metadata = {
        "input": str(input_path),
        "mode": "ocr_subtitles",
        "detectedLanguage": os.getenv("OCR_SOURCE_LANGUAGE", "zh"),
        "segments": segments,
        "originalSubtitle": str(original_srt),
        "vietnameseSubtitle": str(vi_srt),
        "outputVideo": str(output_video),
        "preserveOriginalAudio": True,
        "ocr": {
            **video_info,
            "modelSize": os.getenv("OCR_MODEL_SIZE", "small"),
            "minConfidence": ocr.env_float("OCR_MIN_CONFIDENCE", 0.65),
            "textRegion": text_region,
        },
        "timings": timings,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "originalSubtitle": str(original_srt),
        "vietnameseSubtitle": str(vi_srt),
        "outputVideo": str(output_video),
        "detectedLanguage": os.getenv("OCR_SOURCE_LANGUAGE", "zh"),
        "segments": len(segments),
        "timings": timings,
        "worker": False,
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        base.log(f"ERROR: {error}")
        raise SystemExit(1)
