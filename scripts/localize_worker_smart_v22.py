#!/usr/bin/env python3
"""Persistent localization worker with V2.2 role-separated translation."""
import json
import os
from pathlib import Path

import adaptive_tts
import utterance_translate_v22 as contextual_translate
import localization_v2_quality as quality
import localize_worker as worker
import smart_render
import translation_endpoint

worker.fast.render_video = smart_render.render_video
worker.fast.synthesize_segments = adaptive_tts.synthesize_segments
worker.fast.translate_segments = contextual_translate.translate_segments
worker.fast.TRANSLATION_PROMPT_VERSION = contextual_translate.TRANSLATION_PROMPT_VERSION


def contextual_translation_signature(transcript_sig: dict) -> dict:
    return contextual_translate.translation_signature(transcript_sig)


worker.translation_signature = contextual_translation_signature
_original_tts_signature = worker.tts_signature


def adaptive_tts_signature(translation_sig: dict) -> dict:
    signature = _original_tts_signature(translation_sig)
    signature["adaptiveSegments"] = True
    signature["contextualUtteranceGrouping"] = os.getenv("TTS_GROUP_CONTEXTUAL_UTTERANCES", "true")
    signature["targetCharsPerSec"] = os.getenv("TTS_TARGET_CHARS_PER_SEC", "14")
    signature["editorMaxRatePercent"] = os.getenv("TTS_EDITOR_MAX_RATE_PERCENT", "70")
    signature["editorPostMaxSpeed"] = os.getenv("TTS_EDITOR_POST_MAX_SPEED", "1.25")
    signature["utteranceMaxDurationSec"] = os.getenv("TTS_UTTERANCE_MAX_DURATION_SEC", "8")
    signature["utteranceMaxChars"] = os.getenv("TTS_UTTERANCE_MAX_CHARS", "180")
    signature["utteranceMaxGapSec"] = os.getenv("TTS_UTTERANCE_MAX_GAP_SEC", "0.45")
    return signature


worker.tts_signature = adaptive_tts_signature


def contextual_translate_stage(
    segments: list[dict], detected_language: str, output_dir: Path, stem: str, transcript_sig: dict
):
    signature = worker.translation_signature(transcript_sig)
    cache_path = output_dir / f"{stem}.translated.json"
    vi_srt = output_dir / f"{stem}.vi.srt"
    cached = worker.read_cache(cache_path, signature)
    if cached and cached.get("segments"):
        translated = cached["segments"]
        if not vi_srt.exists():
            worker.base.write_srt(vi_srt, translated, "vi")
        worker.base.log(f"V2.2 translation cache hit: {len(translated)} utterances")
        return translated, vi_srt, signature, True

    # A provider/network outage is a job-level configuration failure, not a
    # scene-level translation failure. Check it once before expensive retries.
    info = translation_endpoint.require_translation_endpoint()
    worker.base.log(
        f"Translation endpoint ready: provider={info.get('provider')} base={info.get('baseUrl')}"
    )

    translated_source = [dict(item) for item in segments]
    video_title = worker.clean_video_title(stem)
    for item in translated_source:
        item["_videoTitle"] = video_title

    translated, story = contextual_translate.translate_contextual(
        translated_source,
        detected_language,
        profile=os.getenv("TRANSLATE_PROFILE", "auto").strip().lower() or "auto",
        instruction=os.getenv("TRANSLATE_CONTEXT_HINT", "").strip(),
        title=video_title,
    )
    for item in translated:
        item.pop("_videoTitle", None)

    worker.base.write_srt(vi_srt, translated, "vi")
    worker.write_cache(
        cache_path,
        signature,
        contextualTranslation=True,
        utteranceFirst=True,
        roleSeparated=True,
        segments=translated,
    )
    contextual_translate.write_context_artifact(output_dir, stem, story)
    return translated, vi_srt, signature, False


worker.translate = contextual_translate_stage
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

    context_path = output_dir / f"{stem}.translation-context.json"
    if context_path.exists():
        result["translationContextFile"] = str(context_path)

    metadata_path = output_dir / f"{stem}.localization.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
        metadata["translationQA"] = summary
        metadata["translationQAFile"] = report["qaFile"]
        metadata["semanticBlocks"] = report["semanticBlocks"]
        metadata["semanticBlocksFile"] = report["semanticBlocksFile"]
        if context_path.exists():
            metadata["translationContextFile"] = str(context_path)
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
