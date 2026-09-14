#!/usr/bin/env python3
"""Localization V2.2: role-separated context -> scene translation -> review.

V2.1 changed the translation unit from raw Whisper fragments to natural utterances.
V2.2 keeps that model and separates language responsibilities so one model no longer
has to infer the whole story, translate every scene, and review itself with the same
prompt/context. It also adds a final whole-video consistency pass for names, kinship
and Vietnamese forms of address.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterable

import contextual_translate as v3
import localization_v2_quality as quality
import localize as base
import utterance_translate as v21

TRANSLATION_PROMPT_VERSION = 5
_CONTEXT_VERSION = 3


def _default_model() -> str:
    if v3._provider() == "ollama":
        return os.getenv("OLLAMA_MODEL", "qwen3:8b").strip()
    return os.getenv("OPENAI_COMPAT_MODEL", "").strip()


def _role_model(name: str, fallback: str | None = None) -> str:
    return os.getenv(name, "").strip() or (fallback or "").strip() or _default_model()


def _chat_json(system: str, user: str, *, role_model: str, temperature: float) -> dict:
    return v21._chat_json(system, user, temperature=temperature, model_override=role_model)


def translation_signature(
    transcript_signature: dict, *, profile: str | None = None, instruction: str | None = None
) -> dict:
    signature = v21.translation_signature(
        transcript_signature, profile=profile, instruction=instruction
    )
    translate_model = _role_model("TRANSLATE_MODEL")
    review_model = _role_model("TRANSLATE_REVIEW_MODEL", translate_model)
    context_model = _role_model("TRANSLATE_CONTEXT_MODEL", translate_model)
    global_review_model = _role_model("TRANSLATE_GLOBAL_REVIEW_MODEL", review_model)
    signature.update(
        {
            "version": 5,
            "promptVersion": TRANSLATION_PROMPT_VERSION,
            "roleSeparated": True,
            "contextModel": context_model,
            "translateModel": translate_model,
            "reviewModel": review_model,
            "globalReviewModel": global_review_model,
            "globalReviewEnabled": v3._env_bool("TRANSLATE_GLOBAL_REVIEW_ENABLED", True),
            "globalReviewChunkSize": v3._env_int("TRANSLATE_GLOBAL_REVIEW_CHUNK_SIZE", 60, 10),
        }
    )
    return signature


def _context_system() -> str:
    return (
        "Bạn là story editor và chuyên gia kiểm tra ASR tiếng Trung. Nhiệm vụ là dựng facts/context phục vụ dịch, "
        "không phải dịch phụ đề. Chỉ suy luận khi transcript hỗ trợ, phân biệt điều chắc chắn và điều còn mơ hồ. "
        "Return strict JSON only."
    )


def _analyze_context_chunk(
    chunk: list[dict], *, title: str, profile: str, instruction: str, model: str
) -> dict:
    prompt = f"""
Phân tích đoạn transcript dưới đây để xây CONTEXT BIBLE cho toàn video.

Tiêu đề: {title or '(không có)'}
Profile: {profile or 'auto'}
Ghi chú người dùng: {instruction or '(không có)'}
Transcript semantic blocks:
{json.dumps(chunk, ensure_ascii=False)}

YÊU CẦU
- Phân loại: short_drama / affiliate / tutorial / general.
- Nhân vật: tên nguồn, cách gọi tiếng Việt ổn định, alias, vai trò, giới tính nếu đủ căn cứ.
- Quan hệ: ai là gì của ai. Đặc biệt họ hàng/xưng hô chỉ ghi nếu có bằng chứng.
- Terms: từ riêng, thuật ngữ, tên sản phẩm cần giữ nhất quán.
- ASR corrections: chỉ đề xuất khi từ bị nghe sai gần như chắc chắn; ghi confidence.
- Summary: mạch sự kiện và xung đột hiện tại. Không bịa phần không nghe thấy.
- Tone: cách thoại nên dùng khi Việt hóa.
- Nếu một tên/quan hệ chưa chắc, vẫn có thể ghi nhưng confidence thấp.

JSON schema:
{{
  "contentType":"short_drama|affiliate|tutorial|general",
  "summary":"...",
  "characters":[{{"source":"...","vi":"...","aliases":["..."],"role":"...","gender":"unknown|female|male","confidence":0.0}}],
  "relationships":[{{"from":"...","to":"...","relation":"...","confidence":0.0}}],
  "terms":[{{"source":"...","vi":"...","note":"..."}}],
  "asrCorrections":[{{"heard":"...","likely":"...","reason":"...","confidence":0.0}}],
  "tone":"..."
}}
""".strip()
    return _chat_json(_context_system(), prompt, role_model=model, temperature=0.02)


def _merge_context_parts(
    parts: list[dict], *, title: str, profile: str, instruction: str, model: str
) -> dict:
    compact = v3._merge_context_locally(
        parts, title=title, profile=profile, instruction=instruction
    )
    if len(parts) <= 1:
        return compact
    prompt = f"""
Hợp nhất context bible sau thành MỘT bộ facts nhất quán.

Tiêu đề: {title or '(không có)'}
Profile yêu cầu: {profile or 'auto'}
Ghi chú người dùng: {instruction or '(không có)'}
Context sơ bộ:
{json.dumps(compact, ensure_ascii=False)}

QUY TẮC
- Không tạo thêm facts mới.
- Khi tên/quan hệ mâu thuẫn, ưu tiên thứ được lặp lại + confidence cao.
- Nếu chưa đủ chắc, giữ phương án confidence thấp thay vì chốt bừa.
- Tên nhân vật phải có một cách gọi tiếng Việt ưu tiên để translator dùng xuyên suốt.

Trả lại cùng schema: contentType, summary, characters, relationships, terms, asrCorrections, tone.
""".strip()
    try:
        merged = _chat_json(_context_system(), prompt, role_model=model, temperature=0.01)
        normalized = v3._merge_context_locally(
            [merged], title=title, profile=profile, instruction=instruction
        )
        for key in ("characters", "relationships", "terms", "asrCorrections"):
            if compact.get(key) and not normalized.get(key):
                normalized[key] = compact[key]
        return normalized
    except Exception as error:
        base.log(f"V2.2 context merge fallback: {error}")
        return compact


def build_story_context(
    segments: Iterable[dict], *, title: str = "", profile: str = "auto", instruction: str = ""
) -> dict:
    source = v3._source_rows(segments)
    if not source:
        return v3._fallback_context(profile, instruction, title)
    blocks = quality.build_semantic_blocks(source)
    max_chars = v3._env_int("TRANSLATE_CONTEXT_ANALYSIS_CHARS", 10000, 2000)
    chunks = v3._chunk_blocks(blocks, max_chars)
    model = _role_model("TRANSLATE_CONTEXT_MODEL", _role_model("TRANSLATE_MODEL"))
    analyses: list[dict] = []
    required = v3._env_bool("TRANSLATE_CONTEXT_ANALYSIS_REQUIRED", False)
    for index, chunk in enumerate(chunks):
        try:
            base.log(
                f"V2.2 context {index + 1}/{len(chunks)} · model={model} · {len(chunk)} semantic blocks"
            )
            analyses.append(
                _analyze_context_chunk(
                    chunk,
                    title=title,
                    profile=profile,
                    instruction=instruction,
                    model=model,
                )
            )
        except Exception as error:
            base.log(f"V2.2 context chunk {index + 1} failed: {error}")
            if required:
                raise
    if not analyses:
        return v3._fallback_context(profile, instruction, title)
    context = _merge_context_parts(
        analyses, title=title, profile=profile, instruction=instruction, model=model
    )
    context["version"] = _CONTEXT_VERSION
    context["videoTitle"] = title
    context["userInstruction"] = instruction
    context["semanticBlocks"] = len(blocks)
    context["contextModel"] = model
    return context


def _translate_scene_once(
    rows: list[dict], *, scene_id: int, story: dict, previous: list[dict], next_rows: list[dict]
) -> list[dict]:
    profile = v3._clean(story.get("profile") or story.get("contentType") or "general")
    model = _role_model("TRANSLATE_MODEL")
    prompt = f"""
Dịch SCENE #{scene_id + 1} theo CÂU THOẠI tự nhiên. Không dịch từng fragment Whisper.

CONTEXT BIBLE:
{json.dumps(v3._compact_story_context(story), ensure_ascii=False)}

THOẠI TRƯỚC ĐÃ CHỐT:
{json.dumps(v21._compact_history(previous), ensure_ascii=False)}

CURRENT RAW ASR CUES:
{json.dumps(rows, ensure_ascii=False)}

NGUỒN NGAY SAU:
{json.dumps(next_rows, ensure_ascii=False)}

QUY TẮC BẮT BUỘC
1. Gom source id LIỀN NHAU thành đúng utterance/câu thoại. Một utterance có thể chứa nhiều sourceIds.
2. Mỗi source id CURRENT xuất hiện đúng một lần, đúng thứ tự, không overlap.
3. Trước khi chọn xưng hô, xác định người nói -> người nghe -> quan hệ trong context/cảnh.
4. Dịch theo Ý. Thành ngữ/khẩu ngữ phải thành câu Việt tương đương, không dịch từng chữ.
5. Tên nhân vật/địa danh/thuật ngữ theo context bible và phải nhất quán.
6. Không bịa chi tiết. Không đổi số, model, thương hiệu, quan hệ nếu nguồn không nói.
7. Nếu ASR sai gần như chắc chắn, ghi toàn câu nguồn sửa vào sourceCorrected; chưa chắc để trống.
8. `vi` phải giống thoại Việt có thể lồng tiếng: gọn, nói được, không văn viết máy móc, không còn chữ Trung.
9. Độ dài phải hợp timing của toàn utterance. Ưu tiên rút câu Việt thay vì ép TTS nói quá nhanh.
10. `speaker` chỉ ghi khi đủ chắc; `confidence` là độ chắc khi hiểu source, 0..1.

JSON:
{{"utterances":[{{"sourceIds":[1,2],"vi":"...","sourceCorrected":"","speaker":"","confidence":0.9}}]}}
""".strip()
    payload = _chat_json(
        v21._system_for_profile(profile), prompt, role_model=model, temperature=0.05
    )
    return v21._normalize_utterances(
        payload.get("utterances"), [int(row["id"]) for row in rows]
    )


def _review_scene(
    rows: list[dict], draft: list[dict], *, scene_id: int, story: dict, previous: list[dict]
) -> list[dict]:
    if not v3._env_bool("TRANSLATE_REVIEW_ENABLED", True):
        return draft
    translate_model = _role_model("TRANSLATE_MODEL")
    model = _role_model("TRANSLATE_REVIEW_MODEL", translate_model)
    prompt = f"""
Bạn đang REVIEW bản Việt hóa SCENE #{scene_id + 1}, không dịch lại tùy hứng.

CONTEXT BIBLE:
{json.dumps(v3._compact_story_context(story), ensure_ascii=False)}
SOURCE:
{json.dumps(rows, ensure_ascii=False)}
DRAFT:
{json.dumps(draft, ensure_ascii=False)}
THOẠI TRƯỚC:
{json.dumps(v21._compact_history(previous), ensure_ascii=False)}

Chỉ sửa khi draft có vấn đề. Kiểm tra lần lượt:
- ai nói với ai / chủ thể / đối tượng;
- quan hệ họ hàng và xưng hô;
- tên riêng có đúng context không;
- thành ngữ, câu mỉa, phủ định, nguyên nhân-kết quả;
- ý bị sót hoặc tự thêm;
- câu Việt có nói tự nhiên khi lồng tiếng không;
- còn chữ Trung, câu cụt, câu literal máy móc hay không.

Có thể gộp/tách utterance nếu thật sự cần, nhưng sourceIds vẫn phủ đủ, liên tiếp, đúng thứ tự, không overlap.
Nếu draft đúng thì GIỮ, đừng rewrite chỉ để khác câu chữ.

JSON:
{{"utterances":[{{"sourceIds":[1,2],"vi":"...","sourceCorrected":"","speaker":"","confidence":0.9}}]}}
""".strip()
    try:
        payload = _chat_json(
            "Bạn là reviewer Trung-Việt chuyên phim/video. Ưu tiên đúng nghĩa và đúng quan hệ trước độ văn vẻ. Return strict JSON only.",
            prompt,
            role_model=model,
            temperature=0.01,
        )
        return v21._normalize_utterances(
            payload.get("utterances"), [int(row["id"]) for row in rows]
        )
    except Exception as error:
        base.log(f"V2.2 scene reviewer fallback scene {scene_id + 1}: {error}")
        return draft


def _translate_rows_resilient(
    rows: list[dict], *, scene_id: int, story: dict, previous: list[dict], next_rows: list[dict]
) -> list[dict]:
    retries = v3._env_int("TRANSLATE_RETRIES", 2, 0)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            draft = _translate_scene_once(
                rows,
                scene_id=scene_id,
                story=story,
                previous=previous,
                next_rows=next_rows,
            )
            return _review_scene(
                rows, draft, scene_id=scene_id, story=story, previous=previous
            )
        except Exception as error:
            last_error = error
            if attempt < retries:
                wait = min(8, 2**attempt)
                base.log(
                    f"V2.2 scene translation retry {attempt + 1}/{retries}: {error}"
                )
                time.sleep(wait)
    if len(rows) > 3:
        middle = len(rows) // 2
        left_rows, right_rows = rows[:middle], rows[middle:]
        left = _translate_rows_resilient(
            left_rows,
            scene_id=scene_id,
            story=story,
            previous=previous,
            next_rows=right_rows + next_rows,
        )
        left_history = previous + [
            {"text": "", "vi": item["vi"], "speaker": item.get("speaker", "")}
            for item in left
        ]
        right = _translate_rows_resilient(
            right_rows,
            scene_id=scene_id,
            story=story,
            previous=left_history,
            next_rows=next_rows,
        )
        return left + right
    raise RuntimeError(
        f"V2.2 contextual translation failed for scene {scene_id + 1}: {last_error}"
    )


def _global_review_chunk(items: list[dict], story: dict, *, model: str) -> list[dict]:
    payload_rows = [
        {
            "id": int(item.get("id", 0)),
            "sceneId": item.get("sceneId"),
            "source": v3._clean(item.get("sourceCorrected") or item.get("text")),
            "vi": v3._clean(item.get("vi")),
            "speaker": v3._clean(item.get("speaker")),
            "confidence": item.get("translationConfidence"),
        }
        for item in items
    ]
    prompt = f"""
GLOBAL CONSISTENCY REVIEW cho các utterance đã dịch.

CONTEXT BIBLE:
{json.dumps(v3._compact_story_context(story), ensure_ascii=False)}

UTTERANCES:
{json.dumps(payload_rows, ensure_ascii=False)}

Bạn KHÔNG được đổi timing, id hay cấu trúc câu. Chỉ rà tính nhất quán xuyên cảnh:
- cùng một nhân vật không được đổi nhiều cách gọi/tên;
- quan hệ/xưng hô phải nhất quán khi người nói/người nghe không đổi;
- không đảo nghĩa phủ định, chủ thể, nguyên nhân-kết quả;
- không còn câu literal/máy móc hoặc chữ Trung;
- không tự thêm thông tin.
Nếu một câu đã đúng thì giữ nguyên.

Trả ĐÚNG một item cho mỗi id, cùng thứ tự.
JSON: {{"items":[{{"id":1,"vi":"...","speaker":"","confidence":0.9}}]}}
""".strip()
    response = _chat_json(
        "Bạn là final consistency reviewer Trung-Việt. Sửa ít nhất có thể, ưu tiên nhất quán và đúng nghĩa. Return strict JSON only.",
        prompt,
        role_model=model,
        temperature=0.0,
    )
    values = response.get("items") or []
    if not isinstance(values, list):
        raise RuntimeError("global reviewer returned invalid items")
    by_id: dict[int, dict] = {}
    for raw in values:
        if not isinstance(raw, dict):
            continue
        try:
            item_id = int(raw.get("id"))
        except (TypeError, ValueError):
            continue
        vi = v3._clean(raw.get("vi"))
        if vi:
            by_id[item_id] = raw
    out: list[dict] = []
    for original in items:
        item = dict(original)
        replacement = by_id.get(int(item.get("id", 0)))
        if replacement:
            item["vi"] = v3._clean(replacement.get("vi")) or item.get("vi", "")
            speaker = v3._clean(replacement.get("speaker"))
            if speaker:
                item["speaker"] = speaker
            try:
                confidence = float(replacement.get("confidence"))
                item["translationConfidence"] = round(
                    max(0.0, min(1.0, confidence)), 3
                )
            except (TypeError, ValueError):
                pass
        out.append(item)
    return out


def global_consistency_review(items: list[dict], story: dict) -> list[dict]:
    if not v3._env_bool("TRANSLATE_GLOBAL_REVIEW_ENABLED", True) or len(items) < 2:
        return items
    review_model = _role_model("TRANSLATE_REVIEW_MODEL", _role_model("TRANSLATE_MODEL"))
    model = _role_model("TRANSLATE_GLOBAL_REVIEW_MODEL", review_model)
    chunk_size = v3._env_int("TRANSLATE_GLOBAL_REVIEW_CHUNK_SIZE", 60, 10)
    reviewed: list[dict] = []
    for offset in range(0, len(items), chunk_size):
        chunk = items[offset : offset + chunk_size]
        try:
            base.log(
                f"V2.2 global review {offset // chunk_size + 1}/{(len(items) + chunk_size - 1) // chunk_size} · model={model}"
            )
            reviewed.extend(_global_review_chunk(chunk, story, model=model))
        except Exception as error:
            base.log(f"V2.2 global reviewer fallback: {error}")
            reviewed.extend(chunk)
    return reviewed


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

    title = title or (
        v3._clean(source_segments[0].get("_videoTitle")) if source_segments else ""
    )
    story = build_story_context(
        source_segments, title=title, profile=profile, instruction=instruction
    )
    source = v3._source_rows(source_segments)
    source_by_id = {int(item["id"]): item for item in source}
    scenes = v21.build_translation_scenes(source_segments)
    existing = [dict(item) for item in (existing_segments or [])]
    target_source_ids = v21._expand_targets(target_ids, existing)
    rolling = v3._env_int("TRANSLATE_ROLLING_CONTEXT_SEGMENTS", 8, 0)
    result: list[dict] = []
    history: list[dict] = []

    for scene_index, scene in enumerate(scenes):
        scene_ids = {int(value) for value in scene.get("segmentIds") or []}
        selected = target_source_ids is None or any(
            value in target_source_ids for value in scene_ids
        )
        if not selected:
            preserved = v21._existing_for_scene(existing, scene_ids)
            if preserved:
                result.extend(preserved)
                history.extend(preserved)
                continue

        rows = v21._scene_rows(scene, source_by_id)
        next_rows = (
            v21._scene_rows(scenes[scene_index + 1], source_by_id)[:rolling]
            if scene_index + 1 < len(scenes)
            else []
        )
        utterances = _translate_rows_resilient(
            rows,
            scene_id=int(scene.get("id", scene_index)),
            story=story,
            previous=history[-rolling:] if rolling else [],
            next_rows=next_rows,
        )
        translated_scene = v21._utterances_to_segments(
            utterances,
            scene_id=int(scene.get("id", scene_index)),
            source_by_id=source_by_id,
        )
        if not translated_scene:
            raise RuntimeError(
                f"scene {scene_index + 1} produced no translated utterances"
            )
        result.extend(translated_scene)
        history.extend(translated_scene)
        base.log(
            f"V2.2 scene {scene_index + 1}/{len(scenes)}: {len(rows)} raw cues -> {len(translated_scene)} utterances"
        )

    result.sort(key=lambda item: (float(item.get("start", 0.0)), int(item.get("id", 0))))
    if not result:
        raise RuntimeError("V2.2 contextual translator produced no subtitle segments")

    # A selected-scene repair should not silently rewrite unrelated reviewed scenes.
    if target_source_ids is None:
        result = global_consistency_review(result, story)

    translate_model = _role_model("TRANSLATE_MODEL")
    review_model = _role_model("TRANSLATE_REVIEW_MODEL", translate_model)
    story["translationScenes"] = len(scenes)
    story["outputUtterances"] = len(result)
    story["translationPromptVersion"] = TRANSLATION_PROMPT_VERSION
    story["modelRoles"] = {
        "context": _role_model("TRANSLATE_CONTEXT_MODEL", translate_model),
        "translation": translate_model,
        "review": review_model,
        "globalReview": _role_model("TRANSLATE_GLOBAL_REVIEW_MODEL", review_model),
    }
    return result, story


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
