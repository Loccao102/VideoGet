#!/usr/bin/env python3
"""Transcribe + translate + burn Vietnamese subtitles without generating TTS."""
from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
import time
from pathlib import Path

from faster_whisper import WhisperModel

import localize as base
import localize_fast as fast  # installs faster translation overrides on base
import render_subtitles


def main() -> None:
    parser = argparse.ArgumentParser(description="Create Vietnamese subtitles without TTS")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    stem = input_path.stem
    original_srt = output_dir / f"{stem}.original.srt"
    vi_srt = output_dir / f"{stem}.vi.srt"
    output_video = output_dir / f"{stem}.vi-subbed.mp4"
    metadata_path = output_dir / f"{stem}.subtitle.json"
    timings: dict[str, float] = {}
    started = time.perf_counter()

    with tempfile.TemporaryDirectory(prefix="videoget-subtitles-") as temp:
        workdir = Path(temp)
        audio_path = workdir / "source.wav"
        base.log("Đang tách audio cho subtitle-only...")
        base.run([
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(audio_path),
        ])

        transcribe_started = time.perf_counter()
        model_name = os.getenv("WHISPER_MODEL", "small")
        device = os.getenv("WHISPER_DEVICE", "cpu")
        compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "int8" if device == "cpu" else "float16")
        model = WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
            cpu_threads=int(os.getenv("WHISPER_CPU_THREADS", "8")),
            num_workers=int(os.getenv("WHISPER_NUM_WORKERS", "1")),
        )
        raw_segments, info = model.transcribe(
            str(audio_path),
            beam_size=int(os.getenv("WHISPER_BEAM_SIZE", "1")),
            vad_filter=True,
            condition_on_previous_text=os.getenv("WHISPER_CONDITION_PREVIOUS_TEXT", "true").lower() not in {"0", "false", "no", "off"},
            language=(os.getenv("WHISPER_LANGUAGE", "").strip() or None),
        )
        segments: list[dict] = []
        for index, segment in enumerate(raw_segments):
            text = segment.text.strip()
            if not text:
                continue
            segments.append({
                "id": index,
                "start": float(segment.start),
                "end": float(segment.end),
                "text": text,
            })
        timings["transcribe"] = round(time.perf_counter() - transcribe_started, 3)
        if not segments:
            raise RuntimeError("Whisper did not detect any speech")

        detected_language = getattr(info, "language", "") or ""
        base.write_srt(original_srt, segments, "text")

        translate_started = time.perf_counter()
        base.translate_segments(segments, detected_language)
        timings["translate"] = round(time.perf_counter() - translate_started, 3)
        base.write_srt(vi_srt, segments, "vi")

        render_started = time.perf_counter()
        render_subtitles.render(input_path, vi_srt, output_video)
        timings["render"] = round(time.perf_counter() - render_started, 3)

    timings["total"] = round(time.perf_counter() - started, 3)
    timings["tts"] = 0.0
    metadata = {
        "input": str(input_path),
        "mode": "subtitles",
        "detectedLanguage": detected_language,
        "segments": segments,
        "originalSubtitle": str(original_srt),
        "vietnameseSubtitle": str(vi_srt),
        "outputVideo": str(output_video),
        "timings": timings,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "originalSubtitle": str(original_srt),
        "vietnameseSubtitle": str(vi_srt),
        "outputVideo": str(output_video),
        "detectedLanguage": detected_language,
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
