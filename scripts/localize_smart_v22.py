#!/usr/bin/env python3
"""One-shot localization entrypoint using V2.2 translation roles."""
import json
import os
import sys
import time
from pathlib import Path

import adaptive_tts
import utterance_translate_v22 as contextual_translate
import localize as base
import localize_fast as fast
import localize_worker as worker
import smart_render
import subtitle_only_render
import translation_endpoint

MODE_SUBTITLES_TTS = "subtitles_tts"
MODE_SUBTITLES_ONLY = "subtitles_only"

fast.TRANSLATION_PROMPT_VERSION = contextual_translate.TRANSLATION_PROMPT_VERSION


def checked_translate_segments(segments: list[dict], detected_language: str):
    # Fail once with an actionable provider/network error instead of letting the
    # scene translator repeatedly retry an unreachable endpoint.
    translation_endpoint.require_translation_endpoint()
    return contextual_translate.translate_segments(segments, detected_language)


fast.translate_segments = checked_translate_segments
base.translate_segments = checked_translate_segments
fast.synthesize_segments = adaptive_tts.synthesize_segments
base.synthesize_segments = adaptive_tts.synthesize_segments
base.render_video = smart_render.render_video


def arg_value(name: str, default: str = "") -> str:
    try:
        index = sys.argv.index(name)
        return str(sys.argv[index + 1]).strip()
    except (ValueError, IndexError):
        return default


def input_arg() -> str:
    value = arg_value("--input")
    return str(Path(value).resolve()) if value else ""


def normalize_mode(value: str) -> str:
    raw = str(value or "").strip().lower()
    return MODE_SUBTITLES_ONLY if raw in {MODE_SUBTITLES_ONLY, "subtitles", "sub_only", "subtitle_only"} else MODE_SUBTITLES_TTS


def strip_mode_args() -> None:
    while "--mode" in sys.argv:
        index = sys.argv.index("--mode")
        del sys.argv[index:index + 2]


def run_subtitles_only() -> None:
    input_value = arg_value("--input")
    output_value = arg_value("--output-dir")
    if not input_value or not output_value:
        raise ValueError("--input and --output-dir are required")

    input_path = Path(input_value).resolve()
    output_dir = Path(output_value).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not input_path.exists():
        raise FileNotFoundError(input_path)
    if not base.has_audio_stream(input_path):
        raise RuntimeError("input video has no audio stream")

    started = time.perf_counter()
    model, _ = worker.build_model()
    stem = input_path.stem
    segments, detected_language, original_srt, _ = worker.transcribe(
        model, input_path, output_dir, stem
    )
    if not segments:
        print(json.dumps({
            "outputVideo": str(input_path),
            "detectedLanguage": detected_language,
            "localizationMode": MODE_SUBTITLES_ONLY,
            "segments": 0,
            "skippedReason": "no_speech",
        }, ensure_ascii=False))
        return

    translation_endpoint.require_translation_endpoint()
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

    vi_srt = output_dir / f"{stem}.vi.srt"
    base.write_srt(vi_srt, translated, "vi")
    contextual_translate.write_context_artifact(output_dir, stem, story)

    output_video = output_dir / f"{stem}.vi-subbed.mp4"
    subtitle_only_render.render_video(input_path, vi_srt, output_video)
    metadata = {
        "input": str(input_path),
        "detectedLanguage": detected_language,
        "localizationMode": MODE_SUBTITLES_ONLY,
        "segments": translated,
        "originalSubtitle": str(original_srt),
        "vietnameseSubtitle": str(vi_srt),
        "voiceTrack": "",
        "outputVideo": str(output_video),
    }
    (output_dir / f"{stem}.localization.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "originalSubtitle": str(original_srt),
        "vietnameseSubtitle": str(vi_srt),
        "voiceTrack": "",
        "outputVideo": str(output_video),
        "detectedLanguage": detected_language,
        "localizationMode": MODE_SUBTITLES_ONLY,
        "segments": len(translated),
        "timings": {"total": round(time.perf_counter() - started, 3), "tts": 0.0},
    }, ensure_ascii=False))


if __name__ == "__main__":
    mode = normalize_mode(arg_value("--mode", MODE_SUBTITLES_TTS))
    try:
        if mode == MODE_SUBTITLES_ONLY:
            run_subtitles_only()
        else:
            # localize.py predates the per-job mode flag; remove it before its
            # argparse parser runs. Empty/legacy mode remains Sub + TTS.
            strip_mode_args()
            base.main()
    except RuntimeError as error:
        if "Whisper did not detect any speech" in str(error):
            print(
                json.dumps(
                    {
                        "outputVideo": input_arg(),
                        "localizationMode": mode,
                        "segments": 0,
                        "skippedReason": "no_speech",
                    },
                    ensure_ascii=False,
                )
            )
        else:
            base.log(f"ERROR: {error}")
            sys.exit(1)
    except Exception as error:
        base.log(f"ERROR: {error}")
        sys.exit(1)
