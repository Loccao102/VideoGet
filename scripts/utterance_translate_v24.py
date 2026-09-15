#!/usr/bin/env python3
"""Localization V2.4: source-first repair + explainable scene review.

V2.3 already separates context/translation/review roles and supports user-approved
context locks. V2.4 adds one missing stage before Vietnamese translation: a Chinese
source interpreter that conservatively repairs obvious ASR mistakes inside a scene.

The important rule is that source repair is advisory unless it is already user
approved. High-confidence automatic repairs are passed to the translator as a
normalized source view and are surfaced in subtitle metadata for review; the raw
Whisper text/timing stays intact.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import contextual_translate as v3
import localize as base
import utterance_translate as v21
import utterance_translate_v22 as v22
import utterance_translate_v23 as v23

TRANSLATION_PROMPT_VERSION = 8
_CONTEXT_VERSION = 6


def _source_model() -> str:
    translate_model = v22._role_model("TRANSLATE_MODEL")
    context_model = v22._role_model("TRANSLATE_CONTEXT_MODEL", translate_model)
    return v22._role_model("TRANSLATE_SOURCE_MODEL", context_model)


def translation_signature(
    transcript_signature: dict, *, profile: str | None = None, instruction: str | None = None
) -> dict:
    signature = v23.translation_signature(
        transcript_signature, profile=profile, instruction=instruction
    )
    signature.update(
        {
            "version": 8,
            "promptVersion": TRANSLATION_PROMPT_VERSION,
            "sourceInterpretation": True,
            "sourceModel": _source_model(),
            "sourceRepairEnabled": v3._env_bool("TRANSLATE_SOURCE_REPAIR_ENABLED", True),
            "sourceRepairMinConfidence": v3._env_float(
                "TRANSLATE_SOURCE_REPAIR_MIN_CONFIDENCE", 0.78, 0.0
            ),
            "explainableReview": True,
        }
    )
    return signature


def _normalize_source_response(values: object, rows: list[dict]) -> list[dict]:
    if not isinstance(values, list):
        raise RuntimeError("source interpreter returned invalid items")
    by_id: dict[int, dict] = {}
    for raw in values:
        if not isinstance(raw, dict):
            continue
        try:
            item_id = int(raw.get("id"))
        except (TypeError, ValueError):
            continue
        normalized = v3._clean(raw.get("normalizedText"))
        if not normalized:
            continue
        try:
            confidence = round(max(0.0, min(1.0, float(raw.get("confidence", 0.5)))), 3)
        except (TypeError, ValueError):
            confidence = 0.5
        by_id[item_id] = {
            "id": item_id,
            "normalizedText": normalized,
            "changed": bool(raw.get("changed")),
            "confidence": confidence,
            "reason": v3._clean(raw.get("reason")),
            "meaningHint": v3._clean(raw.get("meaningHint")),
        }

    expected = [int(row["id"]) for row in rows]
    if set(by_id) != set(expected):
        missing = [item_id for item_id in expected if item_id not in by_id]
        raise RuntimeError(f"source interpreter omitted ids: {missing[:20]}")
    return [by_id[item_id] for item_id in expected]


def _interpret_scene_source(
    rows: list[dict],
    *,
    scene_id: int,
    story: dict,
    previous: list[dict],
    next_rows: list[dict],
) -> tuple[list[dict], dict[int, dict]]:
    if not v3._env_bool("TRANSLATE_SOURCE_REPAIR_ENABLED", True):
        return [dict(row) for row in rows], {}

    model = _source_model()
    prompt = f"""
Bạn đang làm SOURCE INTERPRETATION cho SCENE #{scene_id + 1} trước khi dịch Trung -> Việt.
KHÔNG dịch sang tiếng Việt ở bước này.

CONTEXT BIBLE:
{json.dumps(v3._compact_story_context(story), ensure_ascii=False)}

THOẠI TRƯỚC ĐÃ CHỐT:
{json.dumps(v21._compact_history(previous), ensure_ascii=False)}

CURRENT RAW WHISPER CUES:
{json.dumps(rows, ensure_ascii=False)}

NGUỒN NGAY SAU:
{json.dumps(next_rows, ensure_ascii=False)}

MỤC TIÊU
- Đọc cả scene để hiểu câu nào bị Whisper cắt vụn hoặc nghe sai từ đồng âm.
- `normalizedText` vẫn là tiếng Trung và phải giữ NGUYÊN Ý nguồn.
- Chỉ sửa chữ khi có căn cứ mạnh từ ngữ pháp/ngữ cảnh/tên/quan hệ đã khóa.
- Không tự thêm chủ thể, quan hệ, tên người hay tình tiết nguồn không nói.
- Nếu raw text đã hợp lý, giữ nguyên normalizedText và changed=false.
- Nếu chỉ nghi ngờ nhưng chưa đủ chắc, giữ raw text; ghi nghi ngờ trong reason nhưng changed=false.
- APPROVED_LOCKED_FACTS nếu có là dữ kiện người dùng đã xác nhận, ưu tiên cao nhất.
- `meaningHint` là diễn giải NGẮN bằng tiếng Trung về ý câu nếu câu dễ mơ hồ; không phải bản dịch Việt.

Trả đúng một item cho mỗi id, cùng thứ tự.
JSON:
{{"items":[{{"id":1,"normalizedText":"...","changed":false,"confidence":0.95,"reason":"","meaningHint":"..."}}]}}
""".strip()
    response = v22._chat_json(
        "Bạn là biên tập viên thoại tiếng Trung và chuyên gia sửa lỗi ASR. Sửa bảo thủ, không đoán bừa. Return strict JSON only.",
        prompt,
        role_model=model,
        temperature=0.0,
    )
    interpreted = _normalize_source_response(response.get("items"), rows)
    min_conf = v3._env_float("TRANSLATE_SOURCE_REPAIR_MIN_CONFIDENCE", 0.78, 0.0)
    by_id = {int(item["id"]): item for item in interpreted}
    model_rows: list[dict] = []
    for row in rows:
        item = by_id[int(row["id"])]
        raw_text = v3._clean(row.get("text"))
        normalized = v3._clean(item.get("normalizedText")) or raw_text
        changed = bool(item.get("changed")) and normalized != raw_text
        accepted = changed and float(item.get("confidence", 0.0)) >= min_conf
        enriched = dict(row)
        enriched["rawText"] = raw_text
        enriched["normalizedSuggestion"] = normalized if changed else ""
        enriched["sourceInterpretationReason"] = item.get("reason", "")
        enriched["sourceInterpretationConfidence"] = item.get("confidence", 0.0)
        enriched["meaningHint"] = item.get("meaningHint", "")
        # Only a conservative high-confidence automatic correction is used as the
        # model-facing source. The raw Whisper value remains available in rawText
        # and remains the persisted source unless the user later approves it.
        if accepted:
            enriched["text"] = normalized
            enriched["sourceInterpretationApplied"] = True
        model_rows.append(enriched)
    base.log(
        f"V2.4 source interpretation scene {scene_id + 1} · model={model} · "
        f"{sum(1 for row in model_rows if row.get('sourceInterpretationApplied'))}/{len(model_rows)} high-confidence repairs"
    )
    return model_rows, by_id


def _merge_review_extras(normalized: list[dict], raw_values: object, draft: list[dict]) -> list[dict]:
    values = raw_values if isinstance(raw_values, list) else []
    extras: dict[tuple[int, ...], dict] = {}
    for raw in values:
        if not isinstance(raw, dict):
            continue
        ids: list[int] = []
        for value in raw.get("sourceIds") or []:
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                pass
        if ids:
            extras[tuple(ids)] = raw
    draft_by_ids = {
        tuple(int(value) for value in item.get("sourceIds") or []): item for item in draft
    }
    out: list[dict] = []
    for item in normalized:
        current = dict(item)
        key = tuple(int(value) for value in current.get("sourceIds") or [])
        raw = extras.get(key) or {}
        before = draft_by_ids.get(key) or {}
        changed = v3._clean(before.get("vi")) != v3._clean(current.get("vi"))
        reason = v3._clean(raw.get("reviewReason"))
        if changed:
            current["reviewChanged"] = True
            current["reviewReason"] = reason or "Reviewer đã sửa bản dịch để khớp nghĩa/ngữ cảnh hơn."
        elif reason and bool(raw.get("changed")):
            current["reviewReason"] = reason
        out.append(current)
    return out


def _review_scene_v24(
    rows: list[dict],
    draft: list[dict],
    *,
    scene_id: int,
    story: dict,
    previous: list[dict],
) -> list[dict]:
    if not v3._env_bool("TRANSLATE_REVIEW_ENABLED", True):
        return draft
    translate_model = v22._role_model("TRANSLATE_MODEL")
    model = v22._role_model("TRANSLATE_REVIEW_MODEL", translate_model)
    prompt = f"""
Bạn REVIEW bản Việt hóa SCENE #{scene_id + 1}. Không rewrite chỉ để câu khác đi.

CONTEXT BIBLE:
{json.dumps(v3._compact_story_context(story), ensure_ascii=False)}

SOURCE ĐÃ QUA BƯỚC INTERPRETATION:
{json.dumps(rows, ensure_ascii=False)}

DRAFT:
{json.dumps(draft, ensure_ascii=False)}

THOẠI TRƯỚC:
{json.dumps(v21._compact_history(previous), ensure_ascii=False)}

KIỂM TRA THEO THỨ TỰ
1. Source thật sự nói gì? Ưu tiên `text` đã chuẩn hóa khi sourceInterpretationApplied=true; `rawText` chỉ để đối chiếu.
2. Chủ thể/đối tượng/phủ định/nguyên nhân-kết quả có bị đảo không?
3. Tên riêng, quan hệ họ hàng và xưng hô có khớp APPROVED_LOCKED_FACTS/context không?
4. Thành ngữ/khẩu ngữ có bị dịch literal không?
5. Có bỏ ý hoặc tự thêm tình tiết không?
6. Câu Việt có ngắn, nói được và tự nhiên khi lồng tiếng không?
7. Có chữ Trung hoặc câu cụt không?

Nếu sửa một utterance, `changed=true` và `reviewReason` nêu NGẮN lý do thực chất, ví dụ:
- "sai quan hệ: 大伯 ở cảnh này là bác"
- "dịch literal thành ngữ; sửa thành dồn vào đường cùng"
- "khôi phục chủ thể bị mất"
Nếu không sửa: changed=false, reviewReason="".
Có thể gộp/tách utterance nếu cần nhưng sourceIds phải phủ đủ, liên tiếp, đúng thứ tự, không overlap.

JSON:
{{"utterances":[{{"sourceIds":[1,2],"vi":"...","sourceCorrected":"","speaker":"","confidence":0.9,"changed":false,"reviewReason":""}}]}}
""".strip()
    try:
        response = v22._chat_json(
            "Bạn là reviewer Trung-Việt khó tính. Ưu tiên đúng nghĩa/quan hệ trước độ văn vẻ và phải giải thích ngắn mọi sửa đổi. Return strict JSON only.",
            prompt,
            role_model=model,
            temperature=0.0,
        )
        normalized = v21._normalize_utterances(
            response.get("utterances"), [int(row["id"]) for row in rows]
        )
        return _merge_review_extras(normalized, response.get("utterances"), draft)
    except Exception as error:
        base.log(f"V2.4 reviewer fallback scene {scene_id + 1}: {error}")
        return draft


def _annotate_source_interpretation(
    utterances: list[dict], interpretations: dict[int, dict], model_rows: list[dict]
) -> list[dict]:
    model_by_id = {int(row["id"]): row for row in model_rows}
    out: list[dict] = []
    for raw in utterances:
        item = dict(raw)
        ids = [int(value) for value in item.get("sourceIds") or []]
        applied = [model_by_id[value] for value in ids if model_by_id.get(value, {}).get("sourceInterpretationApplied")]
        if applied:
            corrected = " ".join(v3._clean(model_by_id[value].get("text")) for value in ids if value in model_by_id).strip()
            if corrected:
                item["sourceCorrected"] = corrected
            reasons = [v3._clean(row.get("sourceInterpretationReason")) for row in applied if v3._clean(row.get("sourceInterpretationReason"))]
            item["sourceCorrectionReason"] = " | ".join(dict.fromkeys(reasons))[:900]
            item["sourceCorrectionConfidence"] = round(
                min(float(row.get("sourceInterpretationConfidence", 0.0)) for row in applied), 3
            )
            item["sourceCorrectionAutomatic"] = True
        meaning = [
            v3._clean(interpretations[value].get("meaningHint"))
            for value in ids
            if value in interpretations and v3._clean(interpretations[value].get("meaningHint"))
        ]
        if meaning:
            item["sourceMeaningHint"] = " ".join(dict.fromkeys(meaning))[:900]
        out.append(item)
    return out


def _translate_rows_resilient_v24(
    rows: list[dict],
    *,
    scene_id: int,
    story: dict,
    previous: list[dict],
    next_rows: list[dict],
) -> list[dict]:
    retries = v3._env_int("TRANSLATE_RETRIES", 2, 0)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            model_rows, interpretations = _interpret_scene_source(
                rows,
                scene_id=scene_id,
                story=story,
                previous=previous,
                next_rows=next_rows,
            )
            draft = v22._translate_scene_once(
                model_rows,
                scene_id=scene_id,
                story=story,
                previous=previous,
                next_rows=next_rows,
            )
            reviewed = _review_scene_v24(
                model_rows,
                draft,
                scene_id=scene_id,
                story=story,
                previous=previous,
            )
            return _annotate_source_interpretation(reviewed, interpretations, model_rows)
        except Exception as error:
            last_error = error
            if attempt < retries:
                wait = min(8, 2**attempt)
                base.log(f"V2.4 scene retry {attempt + 1}/{retries}: {error}")
                time.sleep(wait)

    if len(rows) > 3:
        middle = len(rows) // 2
        left_rows, right_rows = rows[:middle], rows[middle:]
        left = _translate_rows_resilient_v24(
            left_rows,
            scene_id=scene_id,
            story=story,
            previous=previous,
            next_rows=right_rows + next_rows,
        )
        left_history = previous + [
            {"text": "", "vi": item.get("vi", ""), "speaker": item.get("speaker", "")}
            for item in left
        ]
        right = _translate_rows_resilient_v24(
            right_rows,
            scene_id=scene_id,
            story=story,
            previous=left_history,
            next_rows=next_rows,
        )
        return left + right
    raise RuntimeError(f"V2.4 scene translation failed for scene {scene_id + 1}: {last_error}")


_original_utterances_to_segments = v21._utterances_to_segments


def _utterances_to_segments_v24(
    utterances: list[dict], *, scene_id: int, source_by_id: dict[int, dict]
) -> list[dict]:
    segments = _original_utterances_to_segments(
        utterances, scene_id=scene_id, source_by_id=source_by_id
    )
    by_ids = {
        tuple(int(value) for value in item.get("sourceIds") or []): item for item in utterances
    }
    for segment in segments:
        ids = tuple(int(value) for value in segment.get("sourceSegmentIds") or [segment.get("id")])
        source = by_ids.get(ids) or {}
        for key in (
            "sourceCorrectionReason",
            "sourceCorrectionConfidence",
            "sourceCorrectionAutomatic",
            "sourceMeaningHint",
            "reviewReason",
            "reviewChanged",
        ):
            if key in source:
                segment[key] = source[key]
    return segments


# V2.3 delegates scene execution to V2.2. Patch only the two V2.4 extension
# points, preserving all approved-context and global-consistency behaviour.
v22._translate_rows_resilient = _translate_rows_resilient_v24
v22.v21._utterances_to_segments = _utterances_to_segments_v24


def translate_contextual(*args, **kwargs):
    translated, story = v23.translate_contextual(*args, **kwargs)
    story["translationPromptVersion"] = TRANSLATION_PROMPT_VERSION
    story["translationContextVersion"] = _CONTEXT_VERSION
    story.setdefault("modelRoles", {})["source"] = _source_model()
    story["sourceInterpretationEnabled"] = v3._env_bool("TRANSLATE_SOURCE_REPAIR_ENABLED", True)
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
