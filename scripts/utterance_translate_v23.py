#!/usr/bin/env python3
"""Localization V2.3: V2.2 role separation + human-approved context locks.

The generated story context remains editable/advisory. Facts saved by Subtitle
Studio are overlaid after AI context analysis and injected explicitly into every
translation/review prompt as hard constraints.
"""
from __future__ import annotations

from pathlib import Path

import context_overrides
import contextual_translate as v3
import utterance_translate_v22 as v22

TRANSLATION_PROMPT_VERSION = 6
_CONTEXT_VERSION = 4

_original_compact_story_context = v22.v3._compact_story_context
_original_build_story_context = v22.build_story_context


def _compact_story_context_with_approved(context: dict) -> dict:
    compact = _original_compact_story_context(context)
    approved = context_overrides.compact_approved_facts(context)
    if approved.get("characters") or approved.get("relationships") or approved.get("terms") or approved.get("notes"):
        compact["APPROVED_LOCKED_FACTS"] = approved
        compact["APPROVED_FACTS_RULE"] = (
            "Các fact trong APPROVED_LOCKED_FACTS do người dùng xác nhận. "
            "Không được đổi tên, quan hệ, thuật ngữ hoặc suy luận trái với chúng."
        )
    return compact


# V2.2 translation/reviewer prompts call this helper through the shared v3 module.
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
    context["translationContextVersion"] = _CONTEXT_VERSION
    context["approvedOverridesFingerprint"] = context_overrides.overrides_fingerprint()
    return context


# V2.2 translate_contextual resolves build_story_context from its own module globals.
v22.build_story_context = build_story_context


def translation_signature(
    transcript_signature: dict, *, profile: str | None = None, instruction: str | None = None
) -> dict:
    signature = v22.translation_signature(
        transcript_signature, profile=profile, instruction=instruction
    )
    signature.update(
        {
            "version": 6,
            "promptVersion": TRANSLATION_PROMPT_VERSION,
            "approvedContextFacts": True,
            "contextOverridesFingerprint": context_overrides.overrides_fingerprint(),
        }
    )
    return signature


def translate_contextual(*args, **kwargs):
    translated, story = v22.translate_contextual(*args, **kwargs)
    translated = context_overrides.apply_voice_hints(translated, story)
    story["translationPromptVersion"] = TRANSLATION_PROMPT_VERSION
    story["approvedOverridesFingerprint"] = context_overrides.overrides_fingerprint()
    return translated, story


def translate_segments(segments: list[dict], detected_language: str) -> dict:
    profile = v3.os.getenv("TRANSLATE_PROFILE", "auto").strip().lower() or "auto"
    instruction = v3.os.getenv("TRANSLATE_CONTEXT_HINT", "").strip()
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
    path.write_text(v3.json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)
