#!/usr/bin/env python3
"""Localization V2.3: role separation + human-approved context/ASR locks."""
from __future__ import annotations

import contextvars
import json
import os
from pathlib import Path

import context_overrides
import contextual_translate as v3
import utterance_translate_v22 as v22

TRANSLATION_PROMPT_VERSION = 7
_CONTEXT_VERSION = 5
_runtime_source_corrections: contextvars.ContextVar[list[dict]] = contextvars.ContextVar(
    "videoget_approved_source_corrections", default=[]
)

_original_compact_story_context = v22.v3._compact_story_context
_original_build_story_context = v22.build_story_context


def _compact_story_context_with_approved(context: dict) -> dict:
    compact = _original_compact_story_context(context)
    approved = context_overrides.compact_approved_facts(context)
    has_approved = any(
        approved.get(key)
        for key in ("characters", "relationships", "terms", "sourceCorrections", "notes")
    )
    if has_approved:
        compact["APPROVED_LOCKED_FACTS"] = approved
        compact["APPROVED_FACTS_RULE"] = (
            "APPROVED_LOCKED_FACTS do người dùng xác nhận và có ưu tiên cao nhất. "
            "Không được đổi tên, quan hệ, thuật ngữ hoặc suy luận trái với chúng. "
            "Mỗi sourceCorrections item chứa sourceIds + correctedText là bản ASR người dùng đã xác nhận; "
            "khi dịch các sourceIds đó phải hiểu correctedText thay cho câu Whisper gốc, nhưng vẫn giữ timing/id gốc."
        )
    return compact


# V2.2 translation/reviewer prompts call this helper through the shared module.
v22.v3._compact_story_context = _compact_story_context_with_approved


def build_story_context(
    segments,
    *,
    title: str = "",
    profile: str = "auto",
    instruction: str = "",
) -> dict:
    context = _original_build_story_context(
        segments, title=title, profile=profile, instruction=instruction
    )
    context = context_overrides.apply_overrides(context)
    context = context_overrides.attach_source_corrections(
        context, _runtime_source_corrections.get()
    )
    context["translationContextVersion"] = _CONTEXT_VERSION
    context["approvedOverridesFingerprint"] = context_overrides.overrides_fingerprint()
    return context


# V2.2 translate_contextual resolves build_story_context from its module globals.
v22.build_story_context = build_story_context


def translation_signature(
    transcript_signature: dict, *, profile: str | None = None, instruction: str | None = None
) -> dict:
    signature = v22.translation_signature(
        transcript_signature, profile=profile, instruction=instruction
    )
    signature.update(
        {
            "version": 7,
            "promptVersion": TRANSLATION_PROMPT_VERSION,
            "approvedContextFacts": True,
            "approvedSourceCorrections": True,
            "contextOverridesFingerprint": context_overrides.overrides_fingerprint(),
        }
    )
    return signature


def _approved_source_corrections(existing_segments: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    for item in existing_segments or []:
        if not isinstance(item, dict) or not bool(item.get("sourceCorrectionApproved")):
            continue
        corrected = " ".join(str(item.get("sourceCorrected") or "").split()).strip()
        if not corrected:
            continue
        source_ids: list[int] = []
        values = item.get("sourceSegmentIds") or [item.get("id")]
        for raw in values:
            try:
                source_ids.append(int(raw))
            except (TypeError, ValueError):
                continue
        if not source_ids:
            continue
        out.append(
            {
                "sourceIds": source_ids,
                "originalText": " ".join(str(item.get("text") or item.get("sourceText") or "").split()).strip(),
                "correctedText": corrected,
            }
        )
    return out


def _preserve_approved_source_corrections(
    translated: list[dict], corrections: list[dict]
) -> list[dict]:
    correction_sets = [
        (set(int(value) for value in item.get("sourceIds") or []), item)
        for item in corrections
        if item.get("sourceIds") and item.get("correctedText")
    ]
    out: list[dict] = []
    for raw in translated:
        item = dict(raw)
        raw_ids = item.get("sourceSegmentIds") or [item.get("id")]
        ids: set[int] = set()
        for value in raw_ids:
            try:
                ids.add(int(value))
            except (TypeError, ValueError):
                continue
        for correction_ids, correction in correction_sets:
            if ids and correction_ids and (ids == correction_ids or correction_ids.issubset(ids)):
                item["sourceCorrected"] = str(correction.get("correctedText") or "").strip()
                item["sourceCorrectionApproved"] = True
                break
        out.append(item)
    return out


def translate_contextual(*args, **kwargs):
    corrections = _approved_source_corrections(kwargs.get("existing_segments"))
    token = _runtime_source_corrections.set(corrections)
    try:
        translated, story = v22.translate_contextual(*args, **kwargs)
    finally:
        _runtime_source_corrections.reset(token)
    translated = _preserve_approved_source_corrections(translated, corrections)
    translated = context_overrides.apply_voice_hints(translated, story)
    story["translationPromptVersion"] = TRANSLATION_PROMPT_VERSION
    story["approvedOverridesFingerprint"] = context_overrides.overrides_fingerprint()
    story["approvedSourceCorrections"] = len(corrections)
    return translated, story


def translate_segments(segments: list[dict], detected_language: str) -> dict:
    profile = os.getenv("TRANSLATE_PROFILE", "auto").strip().lower() or "auto"
    instruction = os.getenv("TRANSLATE_CONTEXT_HINT", "").strip()
    translated, story = translate_contextual(
        [dict(item) for item in segments],
        detected_language,
        profile=profile,
        instruction=instruction,
    )
    segments[:] = translated
    return story


def write_context_artifact(output_dir: Path, stem: str, context: dict) -> str:
    path = output_dir / f"{stem}.translation-context.json"
    payload = {"version": _CONTEXT_VERSION, **context}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)
