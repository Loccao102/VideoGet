#!/usr/bin/env python3
"""Persistent localization worker with contextual translation and per-job output mode."""
import json
import os
import sys
import time
from pathlib import Path

import adaptive_tts
import utterance_translate_v22 as contextual_translate
import localization_v2_quality as quality
import localize_worker as worker
import smart_render
import subtitle_only_render
import translation_endpoint

MODE_SUBTITLES_TTS = "subtitles_tts"
MODE_SUBTITLES_ONLY = "subtitles_only"


def normalize_mode(value: object) -> str:
    raw = str(value or "").strip().lower()
    return MODE_SUBTITLES_ONLY if raw in {MODE_SUBTITLES_ONLY, "subtitles", "sub_only", "subtitle_only"} else MODE_SUBTITLES_TTS


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

    # Provider/network failures are job-level failures, not scene-level translation
    # failures. Preflight once before the translator starts retrying scenes.
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


def _decorate_result(input_path: Path, output_dir: Path, result: dict) -> dict:
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


def _process_subtitles_only(model, input_value: str, output_value: str) -> dict:
    started_total = time.perf_counter()
    timings: dict[str, float] = {}
    cache_hits: list[str] = []

    input_path = Path(input_value).resolve()
    output_dir = Path(output_value).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not input_path.exists():
        raise FileNotFoundError(input_path)
    if not worker.base.has_audio_stream(input_path):
        raise RuntimeError("input video has no audio stream")

    stem = input_path.stem
    transcript_sig = worker.source_signature(input_path)

    stage = time.perf_counter()
    segments, detected_language, original_srt, hit = worker.transcribe(
        model, input_path, output_dir, stem
    )
    timings["transcribe"] = round(time.perf_counter() - stage, 3)
    if hit:
        cache_hits.append("transcript")
    if not segments:
        return {
            "outputVideo": str(input_path),
            "detectedLanguage": detected_language,
            "localizationMode": MODE_SUBTITLES_ONLY,
            "segments": 0,
            "skippedReason": "no_speech",
            "timings": {**timings, "tts": 0.0, "total": round(time.perf_counter() - started_total, 3)},
            "cacheHits": cache_hits,
            "worker": True,
        }

    stage = time.perf_counter()
    translated, vi_srt, _, hit = worker.translate(
        segments, detected_language, output_dir, stem, transcript_sig
    )
    timings["translate"] = round(time.perf_counter() - stage, 3)
    if hit:
        cache_hits.append("translation")

    output_video = output_dir / f"{stem}.vi-subbed.mp4"
    stage = time.perf_counter()
    subtitle_only_render.render_video(input_path, vi_srt, output_video)
    timings["render"] = round(time.perf_counter() - stage, 3)
    timings["tts"] = 0.0
    timings["total"] = round(time.perf_counter() - started_total, 3)

    metadata_path = output_dir / f"{stem}.localization.json"
    metadata = {
        "input": str(input_path),
        "detectedLanguage": detected_language,
        "localizationMode": MODE_SUBTITLES_ONLY,
        "segments": translated,
        "originalSubtitle": str(original_srt),
        "vietnameseSubtitle": str(vi_srt),
        "voiceTrack": "",
        "outputVideo": str(output_video),
        "timings": timings,
        "cacheHits": cache_hits,
        "worker": True,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "originalSubtitle": str(original_srt),
        "vietnameseSubtitle": str(vi_srt),
        "voiceTrack": "",
        "outputVideo": str(output_video),
        "detectedLanguage": detected_language,
        "localizationMode": MODE_SUBTITLES_ONLY,
        "segments": len(translated),
        "timings": timings,
        "cacheHits": cache_hits,
        "worker": True,
    }


def process_job(model, input_value: str, output_value: str, mode: str = MODE_SUBTITLES_TTS) -> dict:
    mode = normalize_mode(mode)
    input_path = Path(input_value).resolve()
    output_dir = Path(output_value).resolve()
    if mode == MODE_SUBTITLES_ONLY:
        result = _process_subtitles_only(model, input_value, output_value)
    else:
        result = _original_process_job(model, input_value, output_value)
        result["localizationMode"] = MODE_SUBTITLES_TTS
        metadata_path = output_dir / f"{input_path.stem}.localization.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
            metadata["localizationMode"] = MODE_SUBTITLES_TTS
            metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return _decorate_result(input_path, output_dir, result)


worker.process_job = lambda model, input_value, output_value: process_job(
    model, input_value, output_value, MODE_SUBTITLES_TTS
)


def main() -> None:
    model, model_info = worker.build_model()
    worker.emit({"type": "ready", **model_info, "localizationModes": [MODE_SUBTITLES_TTS, MODE_SUBTITLES_ONLY]})

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        request_id = ""
        try:
            request = json.loads(raw)
            request_id = str(request.get("id", ""))
            input_value = str(request.get("input", "")).strip()
            output_value = str(request.get("outputDir", "")).strip()
            mode = normalize_mode(request.get("mode"))
            if not input_value or not output_value:
                raise ValueError("input and outputDir are required")
            result = process_job(model, input_value, output_value, mode)
            worker.emit({"id": request_id, "ok": True, "result": result})
        except Exception as error:
            worker.base.log(f"WORKER ERROR: {error}")
            worker.emit({"id": request_id, "ok": False, "error": str(error)})


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        worker.base.log(f"WORKER FATAL: {error}")
        sys.exit(1)
