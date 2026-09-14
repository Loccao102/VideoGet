#!/usr/bin/env python3
"""Persistent localization worker with smart preserve-frame rendering."""
import json
import os
from pathlib import Path

import adaptive_tts
import localization_v2_quality as quality
import localize_worker as worker
import smart_render

# localize_worker imports localize_fast as `fast`; replace the final renderer and
# TTS stage while keeping the persistent Whisper/cache pipeline intact.
worker.fast.render_video = smart_render.render_video
worker.fast.synthesize_segments = adaptive_tts.synthesize_segments

_original_tts_signature = worker.tts_signature


def adaptive_tts_signature(translation_sig: dict) -> dict:
    signature = _original_tts_signature(translation_sig)
    signature["adaptiveSegments"] = True
    signature["targetCharsPerSec"] = os.getenv("TTS_TARGET_CHARS_PER_SEC", "14")
    signature["editorMaxRatePercent"] = os.getenv("TTS_EDITOR_MAX_RATE_PERCENT", "70")
    signature["editorPostMaxSpeed"] = os.getenv("TTS_EDITOR_POST_MAX_SPEED", "1.35")
    return signature


worker.tts_signature = adaptive_tts_signature
_original_process_job = worker.process_job


def _read_segments(path: Path) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("segments") or []
        return rows if isinstance(rows, list) else []
    except (OSError, json.JSONDecodeError, TypeError):
        return []


def _attach_quality(input_path: Path, output_dir: Path, result: dict) -> None:
    stem = input_path.stem
    segments = _read_segments(output_dir / f"{stem}.translated.json")
    if not segments:
        segments = _read_segments(output_dir / f"{stem}.localization.json")
    if not segments:
        return

    report = quality.write_quality_artifacts(output_dir, stem, segments)
    qa = report["qa"]
    summary = {
        "status": qa.get("status", "pass"),
        "errors": qa.get("errors", 0),
        "warnings": qa.get("warnings", 0),
        "segments": qa.get("segments", len(segments)),
    }
    result["translationQA"] = summary
    result["translationQAFile"] = report["qaFile"]
    result["semanticBlocks"] = report["semanticBlocks"]
    result["semanticBlocksFile"] = report["semanticBlocksFile"]

    metadata_path = output_dir / f"{stem}.localization.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
        metadata["translationQA"] = summary
        metadata["translationQAFile"] = report["qaFile"]
        metadata["semanticBlocks"] = report["semanticBlocks"]
        metadata["semanticBlocksFile"] = report["semanticBlocksFile"]
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, json.JSONDecodeError, TypeError):
        pass


def process_job(model, input_value: str, output_value: str) -> dict:
    result = _original_process_job(model, input_value, output_value)
    input_path = Path(input_value).resolve()
    output_dir = Path(output_value).resolve()
    layout_path = output_dir / f"{input_path.stem}.layout.json"
    ass_path = output_dir / f"{input_path.stem}.vi.ass"
    if layout_path.exists():
        result["layoutFile"] = str(layout_path)
        try:
            payload = json.loads(layout_path.read_text(encoding="utf-8"))
            layout = payload.get("layout") or {}
            result["renderProfile"] = str(layout.get("mode") or "")
            result["renderLayout"] = {
                "sourceSubtitle": layout.get("sourceSubtitle"),
                "watermarks": layout.get("watermarks") or [],
                "subtitlePlacement": layout.get("subtitlePlacement") or {},
                "strategy": layout.get("strategy") or "preserve_frame_no_crop",
            }
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    if ass_path.exists():
        result["assSubtitle"] = str(ass_path)
    _attach_quality(input_path, output_dir, result)
    return result


worker.process_job = process_job

if __name__ == "__main__":
    worker.main()
