#!/usr/bin/env python3
"""Localization V2 quality helpers.

This module is intentionally dependency-free. It provides two safe building blocks
that can run after translation without changing the rendered result:

1. semantic block construction over raw ASR/subtitle segments, so later retrieval
   and Translation Memory can work on meaningful chunks instead of tiny Whisper
   fragments;
2. deterministic translation QA for empty text, untranslated Chinese, lost numbers,
   lost Latin/model tokens, and reading-speed pressure.

The QA report is advisory for now: it surfaces quality issues to the editor and
metadata without automatically rewriting approved/user-edited text.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterable

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_NUMBER_RE = re.compile(r"(?<![\w])\d+(?:[.,]\d+)?(?:%|℃|°C)?")
_LATIN_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+._-]{1,24}\b")
_TERMINAL_RE = re.compile(r"[。！？!?；;：:]\s*$")


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except ValueError:
        return max(minimum, default)


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return max(minimum, default)


def _clean(value: object) -> str:
    return " ".join(str(value or "").replace("\u200b", " ").split()).strip()


def _source_text(segment: dict) -> str:
    return _clean(segment.get("text") or segment.get("sourceText"))


def _vi_text(segment: dict) -> str:
    return _clean(segment.get("vi") or segment.get("translation") or segment.get("targetText"))


def _duration(segment: dict) -> float:
    try:
        return max(0.001, float(segment.get("end", 0)) - float(segment.get("start", 0)))
    except (TypeError, ValueError):
        return 0.001


def _visible_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def _normalized_numbers(text: str) -> set[str]:
    return {value.replace(",", ".").lower() for value in _NUMBER_RE.findall(text)}


def _latin_tokens(text: str) -> set[str]:
    # Only preserve source tokens that look like brands/models/technical names.
    # Common English glue words are intentionally ignored.
    stop = {"the", "and", "for", "with", "this", "that", "from", "you", "your"}
    return {
        token.lower()
        for token in _LATIN_TOKEN_RE.findall(text)
        if token.lower() not in stop and (any(ch.isdigit() for ch in token) or len(token) >= 3)
    }


def analyze_segments(segments: Iterable[dict]) -> dict:
    warn_cps = _env_float("TRANSLATION_QA_WARN_CPS", 18.0, 1.0)
    error_cps = _env_float("TRANSLATION_QA_ERROR_CPS", 28.0, warn_cps)
    max_cjk_ratio = _env_float("TRANSLATION_QA_MAX_CJK_RATIO", 0.18, 0.0)

    issues: list[dict] = []
    rows = list(segments)
    previous_vi = ""
    previous_source = ""

    for index, segment in enumerate(rows):
        seg_id = segment.get("id", index)
        source = _source_text(segment)
        vi = _vi_text(segment)
        duration = _duration(segment)
        cps = _visible_chars(vi) / duration if vi else 0.0

        def add(severity: str, code: str, detail: str) -> None:
            issues.append({
                "segmentId": seg_id,
                "severity": severity,
                "code": code,
                "detail": detail,
            })

        if not vi:
            add("error", "empty_translation", "Đoạn dịch tiếng Việt đang trống.")
            continue

        cjk_count = len(_CJK_RE.findall(vi))
        visible = max(1, _visible_chars(vi))
        cjk_ratio = cjk_count / visible
        if cjk_count >= 2 and cjk_ratio > max_cjk_ratio:
            add(
                "warning",
                "untranslated_chinese",
                f"Còn nhiều ký tự Trung trong bản dịch ({cjk_count} ký tự, {cjk_ratio:.0%}).",
            )

        source_numbers = _normalized_numbers(source)
        target_numbers = _normalized_numbers(vi)
        missing_numbers = sorted(source_numbers - target_numbers)
        if missing_numbers:
            add(
                "warning",
                "number_changed_or_missing",
                "Số/đơn vị nguồn không còn trong bản dịch: " + ", ".join(missing_numbers[:8]),
            )

        source_tokens = _latin_tokens(source)
        target_lower = vi.lower()
        missing_tokens = sorted(token for token in source_tokens if token not in target_lower)
        if missing_tokens:
            add(
                "warning",
                "brand_or_model_missing",
                "Brand/model/thuật ngữ Latin có thể bị mất: " + ", ".join(missing_tokens[:8]),
            )

        if cps > error_cps:
            add(
                "error",
                "reading_speed_critical",
                f"Phụ đề quá dày: {cps:.1f} ký tự/giây trong {duration:.2f}s.",
            )
        elif cps > warn_cps:
            add(
                "warning",
                "reading_speed_high",
                f"Phụ đề khá nhanh: {cps:.1f} ký tự/giây trong {duration:.2f}s.",
            )

        if previous_vi and vi == previous_vi and source != previous_source:
            add(
                "warning",
                "duplicate_translation",
                "Hai đoạn nguồn khác nhau đang có cùng một bản dịch liên tiếp.",
            )
        previous_vi = vi
        previous_source = source

    errors = sum(1 for item in issues if item["severity"] == "error")
    warnings = sum(1 for item in issues if item["severity"] == "warning")
    status = "error" if errors else "warning" if warnings else "pass"
    return {
        "version": 1,
        "status": status,
        "segments": len(rows),
        "errors": errors,
        "warnings": warnings,
        "issues": issues,
        "settings": {
            "warnCharsPerSec": warn_cps,
            "errorCharsPerSec": error_cps,
            "maxChineseRatio": max_cjk_ratio,
        },
    }


def build_semantic_blocks(segments: Iterable[dict]) -> list[dict]:
    max_duration = _env_float("SEMANTIC_BLOCK_MAX_DURATION_SEC", 12.0, 1.0)
    max_chars = _env_int("SEMANTIC_BLOCK_MAX_SOURCE_CHARS", 140, 20)
    max_gap = _env_float("SEMANTIC_BLOCK_MAX_GAP_SEC", 1.0, 0.0)

    blocks: list[dict] = []
    current: dict | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        current["sourceText"] = " ".join(current.pop("sourceParts")).strip()
        current["viText"] = " ".join(current.pop("viParts")).strip()
        blocks.append(current)
        current = None

    for index, segment in enumerate(segments):
        source = _source_text(segment)
        vi = _vi_text(segment)
        if not source and not vi:
            continue
        try:
            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", start))
        except (TypeError, ValueError):
            continue
        seg_id = segment.get("id", index)

        if current is None:
            current = {
                "id": len(blocks),
                "segmentIds": [seg_id],
                "start": start,
                "end": end,
                "sourceParts": [source] if source else [],
                "viParts": [vi] if vi else [],
            }
            continue

        gap = max(0.0, start - float(current["end"]))
        proposed_duration = end - float(current["start"])
        proposed_chars = sum(len(value) for value in current["sourceParts"]) + len(source)
        previous_source = current["sourceParts"][-1] if current["sourceParts"] else ""
        strong_boundary = bool(_TERMINAL_RE.search(previous_source)) and gap >= 0.25

        if gap > max_gap or proposed_duration > max_duration or proposed_chars > max_chars or strong_boundary:
            flush()
            current = {
                "id": len(blocks),
                "segmentIds": [seg_id],
                "start": start,
                "end": end,
                "sourceParts": [source] if source else [],
                "viParts": [vi] if vi else [],
            }
            continue

        current["segmentIds"].append(seg_id)
        current["end"] = end
        if source:
            current["sourceParts"].append(source)
        if vi:
            current["viParts"].append(vi)

    flush()
    return blocks


def write_quality_artifacts(output_dir: Path, stem: str, segments: Iterable[dict]) -> dict:
    rows = list(segments)
    qa = analyze_segments(rows)
    blocks = build_semantic_blocks(rows)

    qa_path = output_dir / f"{stem}.translation-qa.json"
    blocks_path = output_dir / f"{stem}.semantic-blocks.json"
    qa_path.write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8")
    blocks_path.write_text(
        json.dumps({"version": 1, "blocks": blocks}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "qa": qa,
        "qaFile": str(qa_path),
        "semanticBlocks": len(blocks),
        "semanticBlocksFile": str(blocks_path),
    }
