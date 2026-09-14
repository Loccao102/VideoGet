#!/usr/bin/env python3
"""Re-render a localized VideoGet job from manually edited subtitle segments.

This path intentionally skips Whisper and translation. It reads the subtitle edit
draft, regenerates Vietnamese TTS with adaptive per-segment rates, then runs the
same smart preserve-frame renderer used by normal localization.
"""
import argparse
import asyncio
import json
import math
import shutil
import sys
import tempfile
import time
from pathlib import Path

import adaptive_tts
import localize as base
import smart_render


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeError(f"subtitle edit draft not found: {path}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"invalid subtitle edit draft: {error}") from error


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-render manually edited VideoGet subtitles")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    started = time.perf_counter()
    input_path = Path(args.input).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    stem = input_path.stem
    edit_path = output_dir / f"{stem}.subtitle-edit.json"
    metadata_path = output_dir / f"{stem}.localization.json"
    edit = load_json(edit_path)
    segments = edit.get("segments") or []
    if not isinstance(segments, list) or not segments:
        raise RuntimeError("subtitle edit draft has no segments")

    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise RuntimeError(f"invalid subtitle segment at index {index}")
        segment.setdefault("id", index)
        segment["start"] = float(segment.get("start", 0.0))
        segment["end"] = float(segment.get("end", 0.0))
        segment["text"] = str(segment.get("text", "")).strip()
        segment["vi"] = str(segment.get("vi", "")).strip()
        segment["speechRate"] = str(segment.get("speechRate", "auto") or "auto")
        if segment["end"] <= segment["start"]:
            raise RuntimeError(f"segment {segment['id']} has invalid timing")

    vi_srt = output_dir / f"{stem}.vi.srt"
    base.write_srt(vi_srt, segments, "vi")

    total_ms = max(1000, int(math.ceil(base.ffprobe_duration(input_path) * 1000)))
    stage = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="videoget-subtitle-rerender-") as temp:
        workdir = Path(temp)
        voice_temp = asyncio.run(adaptive_tts.synthesize_segments(segments, workdir, total_ms))
        voice_track = output_dir / f"{stem}.vi-voice.wav"
        shutil.copy2(voice_temp, voice_track)
    tts_seconds = round(time.perf_counter() - stage, 3)

    output_video = output_dir / f"{stem}.vi-dubbed.mp4"
    stage = time.perf_counter()
    smart_render.render_video(input_path, voice_track, vi_srt, output_video)
    render_seconds = round(time.perf_counter() - stage, 3)

    metadata = {}
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            metadata = {}
    metadata.update(
        {
            "input": str(input_path),
            "segments": segments,
            "vietnameseSubtitle": str(vi_srt),
            "voiceTrack": str(voice_track),
            "outputVideo": str(output_video),
            "subtitleEdited": True,
            "subtitleEditFile": str(edit_path),
            "lastRerenderAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {
        "originalSubtitle": str(metadata.get("originalSubtitle", "")),
        "vietnameseSubtitle": str(vi_srt),
        "voiceTrack": str(voice_track),
        "outputVideo": str(output_video),
        "detectedLanguage": str(metadata.get("detectedLanguage", "")),
        "segments": len(segments),
        "timings": {
            "tts": tts_seconds,
            "render": render_seconds,
            "total": round(time.perf_counter() - started, 3),
        },
        "cacheHits": ["transcript", "translation", "manual_subtitle_edit"],
        "worker": False,
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        base.log(f"ERROR: {error}")
        sys.exit(1)
