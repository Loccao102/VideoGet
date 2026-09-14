#!/usr/bin/env python3
"""Localization V2.1: scene/utterance-first Chinese -> Vietnamese translation.

V3 already provides provider handling and whole-video context analysis. This layer
changes the important unit of translation: a model returns natural utterances that
may own multiple raw Whisper ids, then one subtitle/TTS cue is created per utterance.
A second reviewer pass checks meaning, names, kinship and spoken Vietnamese.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Iterable

import contextual_translate as v3
import localization_v2_quality as quality
import localize as base

TRANSLATION_PROMPT_VERSION = 4
_CONTEXT_VERSION = 2
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def translation_signature(transcript_signature: dict, *, profile: str | None = None, instruction: str | None = None) -> dict:
    signature = v3.translation_signature(transcript_signature, profile=profile, instruction=instruction)
    signature.update({
        "version": 4,
        "promptVersion": TRANSLATION_PROMPT_VERSION,
        "utteranceFirst": True,
        "reviewEnabled": v3._env_bool("TRANSLATE_REVIEW_ENABLED", True),
        "reviewModel": os.getenv("TRANSLATE_REVIEW_MODEL", "").strip() or signature.get("model", ""),
        "sceneMaxDurationSec": v3._env_float("TRANSLATE_SCENE_MAX_DURATION_SEC", 45.0, 8.0),
        "sceneMaxSourceChars": v3._env_int("TRANSLATE_SCENE_MAX_SOURCE_CHARS", 900, 200),
    })
    return signature


def build_translation_scenes(source_segments: Iterable[dict]) -> list[dict]:
    source = v3._source_rows(source_segments)
    if not source:
        return []
    by_id = {int(row["id"]): row for row in source}
    blocks = quality.build_semantic_blocks(source)
    max_duration = v3._env_float("TRANSLATE_SCENE_MAX_DURATION_SEC", 45.0, 8.0)
    max_chars = v3._env_int("TRANSLATE_SCENE_MAX_SOURCE_CHARS", 900, 200)
    max_gap = v3._env_float("TRANSLATE_SCENE_MAX_GAP_SEC", 2.0, 0.3)
    scenes: list[dict] = []
    current: list[dict] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        ids: list[int] = []
        for block in current:
            ids.extend(int(value) for value in (block.get("segmentIds") or []) if int(value) in by_id)
        ids = list(dict.fromkeys(ids))
        rows = [by_id[value] for value in ids]
        if rows:
            scenes.append({
                "id": len(scenes),
                "start": rows[0]["start"],
                "end": rows[-1]["end"],
                "segmentIds": ids,
                "sourceText": " ".join(row["text"] for row in rows),
            })
        current = []

    for block in blocks:
        if not current:
            current = [block]
            continue
        proposed = current + [block]
        start = float(proposed[0].get("start", 0.0))
        end = float(proposed[-1].get("end", start))
        gap = max(0.0, float(block.get("start", 0.0)) - float(current[-1].get("end", 0.0)))
        chars = sum(len(v3._clean(item.get("sourceText"))) for item in proposed)
        if end - start > max_duration or chars > max_chars or gap > max_gap:
            flush()
            current = [block]
        else:
            current.append(block)
    flush()
    return scenes


def _scene_rows(scene: dict, source_by_id: dict[int, dict]) -> list[dict]:
    return [source_by_id[int(value)] for value in (scene.get("segmentIds") or []) if int(value) in source_by_id]


def _compact_history(items: list[dict], limit: int = 8) -> list[dict]:
    return [
        {
            "speaker": v3._clean(item.get("speaker")),
            "source": v3._clean(item.get("text")),
            "vi": v3._clean(item.get("vi")),
        }
        for item in items[-limit:]
    ]


def _normalize_utterances(values: object, expected_ids: list[int]) -> list[dict]:
    if not isinstance(values, list) or not values:
        raise RuntimeError("translator returned no utterances")
    expected = [int(value) for value in expected_ids]
    expected_set = set(expected)
    used: list[int] = []
    normalized: list[dict] = []
    positions = {item_id: index for index, item_id in enumerate(expected)}

    for raw in values:
        if not isinstance(raw, dict):
            continue
        raw_ids = raw.get("sourceIds") or []
        if not isinstance(raw_ids, list):
            continue
        ids: list[int] = []
        for value in raw_ids:
            try:
                item_id = int(value)
            except (TypeError, ValueError):
                continue
            if item_id in expected_set and item_id not in ids:
                ids.append(item_id)
        vi = v3._clean(raw.get("vi"))
        if not ids or not vi:
            continue
        idx = [positions[item_id] for item_id in ids]
        if idx != list(range(min(idx), max(idx) + 1)):
            raise RuntimeError(f"utterance sourceIds must be contiguous: {ids}")
        if any(item_id in used for item_id in ids):
            raise RuntimeError(f"utterance sourceIds overlap: {ids}")
        used.extend(ids)
        try:
            confidence = round(max(0.0, min(1.0, float(raw.get("confidence", 0.7)))), 3)
        except (TypeError, ValueError):
            confidence = 0.7
        normalized.append({
            "sourceIds": ids,
            "vi": vi,
            "sourceCorrected": v3._clean(raw.get("sourceCorrected")),
            "speaker": v3._clean(raw.get("speaker")),
            "confidence": confidence,
        })

    if used != expected:
        missing = [value for value in expected if value not in used]
        raise RuntimeError(f"utterance coverage mismatch; missing ids={missing[:20]}")
    return normalized


def _system_for_profile(profile: str) -> str:
    if profile in {"drama", "short_drama"}:
        return (
            "Bạn là senior subtitle editor Việt hóa phim ngắn Trung Quốc. Dịch theo cảnh và câu thoại, không theo mảnh ASR. "
            "Xưng hô phải đúng quan hệ, tên phải nhất quán, tiếng Việt phải nghe như thoại thật. Không sáng tác. Return strict JSON only."
        )
    if profile == "affiliate":
        return (
            "Bạn là senior subtitle editor video review/affiliate Trung Quốc. Dịch tự nhiên nhưng giữ chính xác model, số liệu và claim. "
            "Không phóng đại. Return strict JSON only."
        )
    if profile == "tutorial":
        return "Bạn là senior subtitle editor video hướng dẫn Trung-Việt. Ưu tiên rõ nghĩa và thuật ngữ nhất quán. Return strict JSON only."
    return "Bạn là senior subtitle editor Trung-Việt. Hiểu toàn cảnh trước khi dịch. Return strict JSON only."


def _translate_scene_once(rows: list[dict], *, scene_id: int, story: dict, previous: list[dict], next_rows: list[dict]) -> list[dict]:
    profile = v3._clean(story.get("profile") or story.get("contentType") or "general")
    prompt = f"""
Dịch SCENE #{scene_id + 1} theo CÂU THOẠI tự nhiên, không dịch từng fragment Whisper.

CONTEXT BIBLE:
{json.dumps(v3._compact_story_context(story), ensure_ascii=False)}

THOẠI TRƯỚC:
{json.dumps(_compact_history(previous), ensure_ascii=False)}

CURRENT RAW ASR CUES:
{json.dumps(rows, ensure_ascii=False)}

NGUỒN NGAY SAU:
{json.dumps(next_rows, ensure_ascii=False)}

QUY TẮC BẮT BUỘC:
1. Gom các source id LIỀN NHAU thành utterance đúng câu/ý. Một utterance có thể chứa nhiều sourceIds.
2. Mỗi source id CURRENT xuất hiện đúng một lần, đúng thứ tự, không overlap.
3. `vi` là toàn bộ câu thoại Việt của utterance. Dịch theo Ý và tình huống, không literal từng cụm.
4. Với phim: xác định ai nói với ai trước khi chọn tôi/cậu/anh/chị/cô/chú/bác/con/cháu. Không dịch 大伯/表叔/伯父 bằng một từ cố định nếu quan hệ trong cảnh khác.
5. Tên nhân vật phải nhất quán với context. Không tự đổi cùng một tên thành nhiều cách đọc.
6. Thành ngữ/khẩu ngữ phải dịch theo nghĩa: ví dụ 往死路上逼 là kiểu "dồn vào đường cùng", không phải "lên đường chết".
7. Nếu ASR nghe sai gần như chắc chắn, ghi câu nguồn đã sửa vào `sourceCorrected`; chưa chắc thì để trống.
8. Không bịa tình tiết/quan hệ/cảm xúc nguồn không có. Không bỏ số, model, thương hiệu.
9. `speaker` chỉ ghi khi đủ chắc; `confidence` là độ chắc khi hiểu SOURCE 0..1.
10. `vi` không được chứa chữ Trung.

JSON:
{{"utterances":[{{"sourceIds":[1,2],"vi":"...","sourceCorrected":"","speaker":"","confidence":0.9}}]}}
""".strip()
    payload = v3._chat_json(_system_for_profile(profile), prompt, temperature=0.07)
    return _normalize_utterances(payload.get("utterances"), [int(row["id"]) for row in rows])


def _review_scene(rows: list[dict], draft: list[dict], *, scene_id: int, story: dict, previous: list[dict]) -> list[dict]:
    if not v3._env_bool("TRANSLATE_REVIEW_ENABLED", True):
        return draft
    prompt = f"""
REVIEW SCENE #{scene_id + 1}.

CONTEXT:
{json.dumps(v3._compact_story_context(story), ensure_ascii=False)}
SOURCE:
{json.dumps(rows, ensure_ascii=False)}
DRAFT:
{json.dumps(draft, ensure_ascii=False)}
PREVIOUS:
{json.dumps(_compact_history(previous), ensure_ascii=False)}

Chỉ sửa khi cần. Kiểm tra: chủ thể/đối tượng, quan hệ và xưng hô, tên riêng, mạch nguyên nhân-kết quả, thành ngữ, ý bị sót, chữ Trung còn sót và câu Việt nghe như dịch máy. Không làm văn hoa, không bịa.
Có thể gộp/tách utterance nếu cần nhưng sourceIds vẫn phải phủ đủ, liên tiếp, đúng thứ tự và không overlap.

JSON:
{{"utterances":[{{"sourceIds":[1,2],"vi":"...","sourceCorrected":"","speaker":"","confidence":0.9}}]}}
""".strip()
    system = (
        "Bạn là reviewer Trung-Việt khó tính. Mục tiêu là đúng nghĩa và đúng quan hệ nhân vật trước, tự nhiên sau. "
        "Nếu draft đúng thì giữ, không rewrite vô ích. Return strict JSON only."
    )
    model = os.getenv("TRANSLATE_REVIEW_MODEL", "").strip() or None
    try:
        payload = v3._chat_json(system, prompt, temperature=0.01, model_override=model)
        return _normalize_utterances(payload.get("utterances"), [int(row["id"]) for row in rows])
    except Exception as error:
        base.log(f"Translation reviewer fallback on scene {scene_id + 1}: {error}")
        return draft


def _translate_rows_resilient(rows: list[dict], *, scene_id: int, story: dict, previous: list[dict], next_rows: list[dict]) -> list[dict]:
    retries = v3._env_int("TRANSLATE_RETRIES", 2, 0)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            draft = _translate_scene_once(rows, scene_id=scene_id, story=story, previous=previous, next_rows=next_rows)
            return _review_scene(rows, draft, scene_id=scene_id, story=story, previous=previous)
        except Exception as error:
            last_error = error
            if attempt < retries:
                wait = min(8, 2**attempt)
                base.log(f"Scene translation retry {attempt + 1}/{retries}: {error}")
                time.sleep(wait)
    if len(rows) > 3:
        middle = len(rows) // 2
        left_rows, right_rows = rows[:middle], rows[middle:]
        left = _translate_rows_resilient(left_rows, scene_id=scene_id, story=story, previous=previous, next_rows=right_rows + next_rows)
        left_history = previous + [
            {"text": "", "vi": item["vi"], "speaker": item.get("speaker", "")}
            for item in left
        ]
        right = _translate_rows_resilient(right_rows, scene_id=scene_id, story=story, previous=left_history, next_rows=next_rows)
        return left + right
    raise RuntimeError(f"contextual scene translation failed for scene {scene_id + 1}: {last_error}")


def _utterances_to_segments(utterances: list[dict], *, scene_id: int, source_by_id: dict[int, dict]) -> list[dict]:
    out: list[dict] = []
    for index, utterance in enumerate(utterances):
        ids = [int(value) for value in (utterance.get("sourceIds") or []) if int(value) in source_by_id]
        if not ids:
            continue
        rows = [source_by_id[value] for value in ids]
        source_text = " ".join(row["text"] for row in rows).strip()
        segment = {
            "id": ids[0],
            "sourceSegmentIds": ids,
            "start": float(rows[0]["start"]),
            "end": float(rows[-1]["end"]),
            "text": source_text,
            "vi": v3._clean(utterance.get("vi")),
            "utteranceId": f"s{scene_id + 1}:u{index + 1}",
            "sceneId": scene_id + 1,
            "speaker": v3._clean(utterance.get("speaker")),
            "translationConfidence": float(utterance.get("confidence", 0.7)),
        }
        corrected = v3._clean(utterance.get("sourceCorrected"))
        if corrected and corrected != source_text:
            segment["sourceCorrected"] = corrected
        out.append(segment)
    return out


def _existing_source_ids(segment: dict) -> list[int]:
    raw = segment.get("sourceSegmentIds")
    if isinstance(raw, list) and raw:
        out: list[int] = []
        for value in raw:
            try:
                out.append(int(value))
            except (TypeError, ValueError):
                pass
        if out:
            return out
    try:
        return [int(segment.get("id"))]
    except (TypeError, ValueError):
        return []


def _expand_targets(target_ids: set[int] | None, existing: list[dict]) -> set[int] | None:
    if target_ids is None:
        return None
    expanded = set(target_ids)
    for item in existing:
        try:
            item_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if item_id in target_ids:
            expanded.update(_existing_source_ids(item))
    return expanded


def _existing_for_scene(existing: list[dict], scene_ids: set[int]) -> list[dict]:
    out = [dict(item) for item in existing if any(value in scene_ids for value in _existing_source_ids(item))]
    out.sort(key=lambda item: float(item.get("start", 0.0)))
    return out


def translate_contextual(
    source_segments: list[dict],
    detected_language: str,
    *,
    existing_segments: list[dict] | None = None,
    target_ids: set[int] | None = None,
    profile: str = "auto",
    instruction: str = "",
    title: str = "",
) -> tuple[list[dict], dict]:
    if detected_language.lower().startswith("vi"):
        translated = [dict(item, vi=v3._clean(item.get("text"))) for item in source_segments]
        return translated, v3._fallback_context(profile, instruction, title)

    title = title or (v3._clean(source_segments[0].get("_videoTitle")) if source_segments else "")
    story = v3.build_story_context(source_segments, title=title, profile=profile, instruction=instruction)
    source = v3._source_rows(source_segments)
    source_by_id = {int(item["id"]): item for item in source}
    scenes = build_translation_scenes(source_segments)
    existing = [dict(item) for item in (existing_segments or [])]
    target_source_ids = _expand_targets(target_ids, existing)
    rolling = v3._env_int("TRANSLATE_ROLLING_CONTEXT_SEGMENTS", 8, 0)
    result: list[dict] = []
    history: list[dict] = []

    for scene_index, scene in enumerate(scenes):
        scene_ids = {int(value) for value in scene.get("segmentIds") or []}
        selected = target_source_ids is None or any(value in target_source_ids for value in scene_ids)
        if not selected:
            preserved = _existing_for_scene(existing, scene_ids)
            if preserved:
                result.extend(preserved)
                history.extend(preserved)
                continue

        rows = _scene_rows(scene, source_by_id)
        next_rows = _scene_rows(scenes[scene_index + 1], source_by_id)[:rolling] if scene_index + 1 < len(scenes) else []
        utterances = _translate_rows_resilient(
            rows,
            scene_id=int(scene.get("id", scene_index)),
            story=story,
            previous=history[-rolling:] if rolling else [],
            next_rows=next_rows,
        )
        translated_scene = _utterances_to_segments(utterances, scene_id=int(scene.get("id", scene_index)), source_by_id=source_by_id)
        if not translated_scene:
            raise RuntimeError(f"scene {scene_index + 1} produced no translated utterances")
        result.extend(translated_scene)
        history.extend(translated_scene)
        base.log(f"V2.1 scene {scene_index + 1}/{len(scenes)}: {len(rows)} raw cues -> {len(translated_scene)} utterances")

    result.sort(key=lambda item: (float(item.get("start", 0.0)), int(item.get("id", 0))))
    if not result:
        raise RuntimeError("contextual translator produced no subtitle segments")
    story["translationScenes"] = len(scenes)
    story["outputUtterances"] = len(result)
    story["translationPromptVersion"] = TRANSLATION_PROMPT_VERSION
    return result, story


def translate_segments(segments: list[dict], detected_language: str) -> dict:
    profile = os.getenv("TRANSLATE_PROFILE", "auto").strip().lower() or "auto"
    instruction = os.getenv("TRANSLATE_CONTEXT_HINT", "").strip()
    translated, story = translate_contextual([dict(item) for item in segments], detected_language, profile=profile, instruction=instruction)
    segments[:] = translated
    return story


def write_context_artifact(output_dir: Path, stem: str, context: dict) -> str:
    path = output_dir / f"{stem}.translation-context.json"
    payload = {"version": _CONTEXT_VERSION, **context}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)
