#!/usr/bin/env python3
"""Re-render an edited subtitle draft while preserving the original audio.

This is intentionally separate from rerender_subtitles.py: it does not import or
invoke Edge TTS and never mixes a generated voice track into the source video.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import localization_v2_quality as quality
import localize as base
import subtitle_only_render

MODE = "subtitles_only"


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeError(f"subtitle edit draft not found: {path}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"invalid subtitle edit draft: {error}") from error


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-render edited Vietnamese subtitles while keeping source audio"
    )
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
        if segment["end"] <= segment["start"]:
            raise RuntimeError(f"segment {segment['id']} has invalid timing")
        if not segment["vi"]:
            raise RuntimeError(f"segment {segment['id']} has empty Vietnamese text")

    vi_srt = output_dir / f"{stem}.vi.srt"
    base.write_srt(vi_srt, segments, "vi")

    quality_report = quality.write_quality_artifacts(output_dir, stem, segments)
    qa = quality_report["qa"]
    qa_summary = {
        "status": qa.get("status", "pass"),
        "errors": qa.get("errors", 0),
        "warnings": qa.get("warnings", 0),
        "segments": qa.get("segments", len(segments)),
    }

    output_video = output_dir / f"{stem}.vi-subbed.mp4"
    stage = time.perf_counter()
    subtitle_only_render.render_video(input_path, vi_srt, output_video)
    render_seconds = round(time.perf_counter() - stage, 3)

    rendered_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    edit["segments"] = segments
    edit["renderedAt"] = rendered_at
    edit["localizationMode"] = MODE
    edit_path.write_text(json.dumps(edit, ensure_ascii=False, indent=2), encoding="utf-8")

    metadata: dict = {}
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
            "voiceTrack": "",
            "outputVideo": str(output_video),
            "localizationMode": MODE,
            "subtitleEdited": True,
            "subtitleDraftPending": False,
            "subtitleEditFile": str(edit_path),
            "lastRerenderAt": rendered_at,
            "translationQA": qa_summary,
            "translationQAFile": quality_report["qaFile"],
            "semanticBlocks": quality_report["semanticBlocks"],
            "semanticBlocksFile": quality_report["semanticBlocksFile"],
        }
    )
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {
        "originalSubtitle": str(metadata.get("originalSubtitle", "")),
        "vietnameseSubtitle": str(vi_srt),
        "voiceTrack": "",
        "outputVideo": str(output_video),
        "detectedLanguage": str(metadata.get("detectedLanguage", "")),
        "localizationMode": MODE,
        "segments": len(segments),
        "translationQA": qa_summary,
        "translationQAFile": quality_report["qaFile"],
        "semanticBlocks": quality_report["semanticBlocks"],
        "semanticBlocksFile": quality_report["semanticBlocksFile"],
        "timings": {
            "tts": 0.0,
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
