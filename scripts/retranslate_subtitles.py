#!/usr/bin/env python3
"""Re-translate an existing VideoGet transcript without running Whisper again.

The command is used by the Subtitle Editor. It rebuilds Vietnamese subtitles with
Localization V2 contextual translation, writes a reviewable draft, invalidates TTS,
and keeps the current rendered MP4 untouched until the user explicitly regenerates
voice/render.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import contextual_translate
import localization_v2_quality as quality
import localize as base


def load_json(path: Path, required: bool = False) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        if required:
            raise RuntimeError(f"required localization file not found: {path}")
        return {}
    except json.JSONDecodeError as error:
        if required:
            raise RuntimeError(f"invalid JSON file {path}: {error}") from error
        return {}


def parse_ids(value: str) -> set[int] | None:
    value = str(value or "").strip()
    if not value:
        return None
    out: set[int] = set()
    for raw in value.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.add(int(raw))
        except ValueError as error:
            raise RuntimeError(f"invalid segment id: {raw}") from error
    return out or None


def main() -> None:
    parser = argparse.ArgumentParser(description="Contextually re-translate VideoGet subtitles")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--profile", default="auto")
    parser.add_argument("--instruction", default="")
    parser.add_argument("--segment-ids", default="")
    args = parser.parse_args()

    started = time.perf_counter()
    input_path = Path(args.input).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    stem = input_path.stem
    transcript_path = output_dir / f"{stem}.transcript.json"
    translated_path = output_dir / f"{stem}.translated.json"
    draft_path = output_dir / f"{stem}.subtitle-edit.json"
    vi_srt = output_dir / f"{stem}.vi.srt"
    metadata_path = output_dir / f"{stem}.localization.json"
    tts_cache = output_dir / f"{stem}.tts.json"
    voice_track = output_dir / f"{stem}.vi-voice.wav"

    transcript = load_json(transcript_path, required=True)
    source_segments = transcript.get("segments") or []
    if not isinstance(source_segments, list) or not source_segments:
        raise RuntimeError("transcript cache has no source segments")
    detected_language = str(transcript.get("detectedLanguage") or "zh")

    existing_draft = load_json(draft_path)
    existing_translated = load_json(translated_path)
    existing_segments = existing_draft.get("segments") or existing_translated.get("segments") or []
    if not isinstance(existing_segments, list):
        existing_segments = []

    target_ids = parse_ids(args.segment_ids)
    profile = str(args.profile or "auto").strip().lower() or "auto"
    instruction = str(args.instruction or "").strip()

    translated, story = contextual_translate.translate_contextual(
        source_segments,
        detected_language,
        existing_segments=existing_segments,
        target_ids=target_ids,
        profile=profile,
        instruction=instruction,
        title=input_path.stem,
    )
    for item in translated:
        item.pop("_videoTitle", None)

    signature = contextual_translate.translation_signature(
        transcript.get("signature") or {}, profile=profile, instruction=instruction
    )
    translated_payload = {
        "signature": signature,
        "contextualTranslation": True,
        "segments": translated,
    }
    translated_path.write_text(json.dumps(translated_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    base.write_srt(vi_srt, translated, "vi")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    draft_payload = {
        "version": 2,
        "jobId": str(existing_draft.get("jobId") or ""),
        "editedAt": now,
        "retranslatedAt": now,
        "profile": profile,
        "instruction": instruction,
        "segments": translated,
    }
    draft_path.write_text(json.dumps(draft_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    context_path = contextual_translate.write_context_artifact(output_dir, stem, story)
    report = quality.write_quality_artifacts(output_dir, stem, translated)

    # Translation changed: generated voice is now stale. Keep the old MP4 until
    # the user explicitly requests TTS/render so before/after remains comparable.
    tts_cache.unlink(missing_ok=True)
    voice_track.unlink(missing_ok=True)

    metadata = load_json(metadata_path)
    metadata.update({
        "segments": translated,
        "vietnameseSubtitle": str(vi_srt),
        "subtitleDraftPending": True,
        "subtitleEditedAt": now,
        "subtitleRetranslatedAt": now,
        "translationProfile": profile,
        "translationContextFile": context_path,
        "translationQA": {
            "status": report["qa"].get("status", "pass"),
            "errors": report["qa"].get("errors", 0),
            "warnings": report["qa"].get("warnings", 0),
            "segments": report["qa"].get("segments", len(translated)),
        },
        "translationQAFile": report["qaFile"],
        "semanticBlocks": report["semanticBlocks"],
        "semanticBlocksFile": report["semanticBlocksFile"],
    })
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {
        "segments": len(translated),
        "targetSegments": sorted(target_ids) if target_ids is not None else "all",
        "profile": profile,
        "translatedFile": str(translated_path),
        "draftFile": str(draft_path),
        "vietnameseSubtitle": str(vi_srt),
        "translationContextFile": context_path,
        "translationQAFile": report["qaFile"],
        "translationQA": metadata["translationQA"],
        "semanticBlocks": report["semanticBlocks"],
        "elapsedSeconds": round(time.perf_counter() - started, 3),
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        base.log(f"ERROR: {error}")
        raise SystemExit(1)
