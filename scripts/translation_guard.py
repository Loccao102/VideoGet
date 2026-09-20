#!/usr/bin/env python3
"""Lightweight guards against accidentally burning untranslated Chinese as Vietnamese."""
from __future__ import annotations

import re


HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def _han_count(value: str) -> int:
    return len(HAN_RE.findall(str(value or "")))


def looks_untranslated_chinese(source: str, target: str) -> bool:
    """Return True when a translated caption still looks like copied Chinese text.

    This is intentionally conservative so short Chinese proper names can remain intact.
    """
    source = str(source or "").strip()
    target = str(target or "").strip()
    source_han = _han_count(source)
    target_han = _han_count(target)

    if source_han < 3 or not target:
        return False

    if _compact(source) == _compact(target):
        return True

    # Only reject Han-dominant outputs for sentence-like source text. A few Han
    # characters inside an otherwise Vietnamese line may be a proper name/brand.
    if source_han < 4 or target_han < 4:
        return False

    alphabetic = sum(1 for ch in target if ch.isalpha())
    han_ratio = target_han / max(1, alphabetic)
    return han_ratio >= 0.55


def assert_vietnamese_translation(
    batch: list[dict],
    translated: list[dict],
    detected_language: str = "",
) -> None:
    """Fail translation retries when Chinese source was copied instead of translated."""
    if str(detected_language or "").strip().lower().startswith("vi"):
        return

    source_by_id = {
        int(item["id"]): str(item.get("text", ""))
        for item in batch
        if "id" in item
    }
    bad_ids: list[int] = []
    for item in translated:
        if "id" not in item:
            continue
        item_id = int(item["id"])
        source = source_by_id.get(item_id, "")
        target = str(item.get("text", ""))
        if looks_untranslated_chinese(source, target):
            bad_ids.append(item_id)

    if bad_ids:
        raise RuntimeError(
            "translator returned untranslated Chinese for segment ids: "
            + ",".join(str(value) for value in bad_ids)
        )
