#!/usr/bin/env python3
"""Context-aware Chinese -> Vietnamese subtitle translation for Localization V2.

The old pipeline translated tiny Whisper segments with only a few neighbouring
segments. That is especially brittle for short drama where ASR often splits one
sentence across many one-second fragments and may mis-hear names/kinship terms.

This module performs a two-pass translation:
1. build a compact story/context bible from semantic source blocks;
2. translate semantic blocks while preserving original timing ids, rolling context,
   consistent names/relationships, and optional user guidance.

It does not trust generated corrections as source truth. Obvious ASR repairs are
stored separately as `sourceCorrected` for review.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Iterable

import localization_v2_quality as quality
import localize as base

TRANSLATION_PROMPT_VERSION = 3
_CONTEXT_VERSION = 1
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return max(minimum, default)


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except ValueError:
        return max(minimum, default)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _clean(value: object) -> str:
    return " ".join(str(value or "").replace("\u200b", " ").replace("\ufeff", " ").split()).strip()


def _ollama_base() -> str:
    url = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434").rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3].rstrip("/")
    return url


def _provider() -> str:
    value = os.getenv("TRANSLATE_PROVIDER", "ollama").strip().lower()
    if value == "openai_compatible":
        value = "openai"
    if value not in {"ollama", "openai"}:
        raise RuntimeError("TRANSLATE_PROVIDER must be ollama, openai, or openai_compatible")
    return value


def _chat_json(system: str, user: str, *, temperature: float | None = None) -> dict:
    provider = _provider()
    timeout = _env_int("TRANSLATE_TIMEOUT_SEC", 180, 30)
    temp = _env_float("TRANSLATE_TEMPERATURE", 0.15, 0.0) if temperature is None else max(0.0, temperature)

    if provider == "ollama":
        payload = {
            "model": os.getenv("OLLAMA_MODEL", "qwen3:8b"),
            "stream": False,
            "think": False,
            "format": "json",
            "keep_alive": os.getenv("OLLAMA_KEEP_ALIVE", "15m"),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {"temperature": temp},
        }
        response = base.http_json(_ollama_base() + "/api/chat", payload, timeout=timeout)
        content = str((response.get("message") or {}).get("content", ""))
    else:
        base_url = os.getenv("OPENAI_COMPAT_BASE_URL", "http://host.docker.internal:11434/v1").rstrip("/")
        model = os.getenv("OPENAI_COMPAT_MODEL", os.getenv("OLLAMA_MODEL", "qwen3:8b"))
        api_key = os.getenv("OPENAI_COMPAT_API_KEY", "")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        payload = {
            "model": model,
            "temperature": temp,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        response = base.http_json(base_url + "/chat/completions", payload, headers=headers, timeout=timeout)
        choices = response.get("choices") or []
        if not choices:
            raise RuntimeError("translation endpoint returned no choices")
        content = str((choices[0].get("message") or {}).get("content", ""))

    parsed = base.extract_json(content)
    if not isinstance(parsed, dict):
        raise RuntimeError("translation endpoint did not return a JSON object")
    return parsed


def translation_signature(transcript_signature: dict, *, profile: str | None = None, instruction: str | None = None) -> dict:
    provider = os.getenv("TRANSLATE_PROVIDER", "ollama").strip().lower()
    if provider == "openai_compatible":
        provider = "openai"
    model = (
        os.getenv("OLLAMA_MODEL", "qwen3:8b")
        if provider == "ollama"
        else os.getenv("OPENAI_COMPAT_MODEL", "")
    )
    chosen_profile = (profile if profile is not None else os.getenv("TRANSLATE_PROFILE", "auto")).strip().lower() or "auto"
    context_hint = instruction if instruction is not None else os.getenv("TRANSLATE_CONTEXT_HINT", "")
    return {
        "version": 3,
        "promptVersion": TRANSLATION_PROMPT_VERSION,
        "transcript": transcript_signature,
        "provider": provider,
        "model": model,
        "style": os.getenv("TRANSLATE_STYLE", "natural_social"),
        "profile": chosen_profile,
        "contextual": True,
        "contextHint": str(context_hint or "").strip(),
        "contextAnalysisChars": _env_int("TRANSLATE_CONTEXT_ANALYSIS_CHARS", 10000, 2000),
        "rollingContextSegments": _env_int("TRANSLATE_ROLLING_CONTEXT_SEGMENTS", 8, 0),
        "temperature": os.getenv("TRANSLATE_TEMPERATURE", "0.15"),
    }


def _source_rows(segments: Iterable[dict]) -> list[dict]:
    rows: list[dict] = []
    for index, item in enumerate(segments):
        text = _clean(item.get("text") or item.get("sourceText"))
        if not text:
            continue
        try:
            start = float(item.get("start", 0.0))
            end = float(item.get("end", start))
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        rows.append({
            "id": int(item.get("id", index)),
            "start": start,
            "end": end,
            "durationSec": round(max(0.12, end - start), 3),
            "text": text,
        })
    return rows


def _chunk_blocks(blocks: list[dict], max_chars: int) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    current: list[dict] = []
    chars = 0
    for block in blocks:
        compact = {
            "id": block.get("id"),
            "start": block.get("start"),
            "end": block.get("end"),
            "text": _clean(block.get("sourceText")),
        }
        size = len(compact["text"]) + 32
        if current and chars + size > max_chars:
            chunks.append(current)
            current = []
            chars = 0
        current.append(compact)
        chars += size
    if current:
        chunks.append(current)
    return chunks


def _fallback_context(profile: str, instruction: str, title: str) -> dict:
    chosen = profile if profile and profile != "auto" else "general"
    return {
        "version": _CONTEXT_VERSION,
        "profile": chosen,
        "contentType": chosen,
        "videoTitle": title,
        "summary": "",
        "characters": [],
        "relationships": [],
        "terms": [],
        "asrCorrections": [],
        "tone": "natural spoken Vietnamese",
        "userInstruction": instruction,
    }


def _analyze_context_chunk(chunk: list[dict], *, title: str, profile: str, instruction: str) -> dict:
    system = (
        "Bạn là biên tập viên thoại phim/video Trung Quốc và chuyên gia kiểm tra ASR. "
        "Bạn chỉ suy luận từ transcript được cung cấp, không bịa chi tiết cốt truyện. Return strict JSON only."
    )
    prompt = f"""
MỤC TIÊU
Tạo một context bible ngắn để dịch toàn video nhất quán. Transcript có thể bị Whisper cắt câu và nghe sai từ đồng âm.

THÔNG TIN
- Tiêu đề video: {title or '(không có)'}
- Profile yêu cầu: {profile or 'auto'}
- Ghi chú người dùng: {instruction or '(không có)'}
- Transcript semantic blocks:
{json.dumps(chunk, ensure_ascii=False)}

HÃY SUY LUẬN CẨN THẬN
- Nhận diện loại nội dung: short_drama / affiliate / tutorial / general.
- Nhân vật/tên riêng: giữ một cách gọi tiếng Việt nhất quán. Với phim Trung, ưu tiên tên Hán-Việt tự nhiên khi đủ chắc chắn; nếu không chắc giữ tên nguồn.
- Quan hệ/xưng hô: cha/mẹ, anh/chị/em, cô/chú/bác/cậu/dì, họ hàng, sếp/nhân viên... chỉ ghi khi transcript hỗ trợ.
- Tìm các lỗi ASR có xác suất cao do từ đồng âm hoặc lặp lại, ví dụ một từ vô nghĩa nhưng ngữ cảnh cho thấy thuật ngữ quen thuộc. Không sửa nếu không chắc.
- Tóm tắt mạch cảnh hiện tại, động cơ/xung đột chính, và tone thoại.

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
    return _chat_json(system, prompt, temperature=0.05)


def _dedupe_dict_list(items: Iterable[dict], key_fields: tuple[str, ...]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, ...]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        key = tuple(_clean(item.get(field)).lower() for field in key_fields)
        if not any(key) or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _merge_context_locally(parts: list[dict], *, title: str, profile: str, instruction: str) -> dict:
    merged = _fallback_context(profile, instruction, title)
    summaries: list[str] = []
    content_types: list[str] = []
    characters: list[dict] = []
    relationships: list[dict] = []
    terms: list[dict] = []
    corrections: list[dict] = []
    tones: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if _clean(part.get("summary")):
            summaries.append(_clean(part.get("summary")))
        if _clean(part.get("contentType")):
            content_types.append(_clean(part.get("contentType")))
        characters.extend(part.get("characters") or [])
        relationships.extend(part.get("relationships") or [])
        terms.extend(part.get("terms") or [])
        corrections.extend(part.get("asrCorrections") or [])
        if _clean(part.get("tone")):
            tones.append(_clean(part.get("tone")))
    merged["summary"] = " | ".join(summaries)[:5000]
    if profile and profile != "auto":
        merged["profile"] = profile
        merged["contentType"] = profile
    elif content_types:
        preferred = max(set(content_types), key=content_types.count)
        merged["profile"] = "drama" if preferred == "short_drama" else preferred
        merged["contentType"] = preferred
    merged["characters"] = _dedupe_dict_list(characters, ("source", "vi"))
    merged["relationships"] = _dedupe_dict_list(relationships, ("from", "to", "relation"))
    merged["terms"] = _dedupe_dict_list(terms, ("source", "vi"))
    merged["asrCorrections"] = _dedupe_dict_list(corrections, ("heard", "likely"))
    merged["tone"] = " | ".join(dict.fromkeys(tones))[:1200] or merged["tone"]
    return merged


def _merge_context_with_model(parts: list[dict], *, title: str, profile: str, instruction: str) -> dict:
    if len(parts) <= 1:
        return _merge_context_locally(parts, title=title, profile=profile, instruction=instruction)
    compact = _merge_context_locally(parts, title=title, profile=profile, instruction=instruction)
    system = "Bạn hợp nhất context bible cho dịch phụ đề Trung -> Việt. Không bịa. Return strict JSON only."
    prompt = f"""
Hợp nhất các phân tích transcript thành MỘT context bible nhất quán. Nếu các chunk mâu thuẫn về tên/quan hệ, ưu tiên phương án được lặp lại hoặc có confidence cao; nếu vẫn mơ hồ thì ghi confidence thấp thay vì bịa.

Tiêu đề: {title or '(không có)'}
Profile yêu cầu: {profile or 'auto'}
Ghi chú người dùng: {instruction or '(không có)'}

Context đã gộp sơ bộ:
{json.dumps(compact, ensure_ascii=False)}

Trả cùng schema: contentType, summary, characters, relationships, terms, asrCorrections, tone.
""".strip()
    try:
        merged = _chat_json(system, prompt, temperature=0.05)
        local = _merge_context_locally([merged], title=title, profile=profile, instruction=instruction)
        if compact.get("characters") and not local.get("characters"):
            local["characters"] = compact["characters"]
        if compact.get("asrCorrections") and not local.get("asrCorrections"):
            local["asrCorrections"] = compact["asrCorrections"]
        return local
    except Exception as error:
        base.log(f"Context merge fallback: {error}")
        return compact


def build_story_context(
    segments: Iterable[dict], *, title: str = "", profile: str = "auto", instruction: str = ""
) -> dict:
    source = _source_rows(segments)
    if not source:
        return _fallback_context(profile, instruction, title)
    blocks = quality.build_semantic_blocks(source)
    max_chars = _env_int("TRANSLATE_CONTEXT_ANALYSIS_CHARS", 10000, 2000)
    chunks = _chunk_blocks(blocks, max_chars)
    analyses: list[dict] = []
    required = _env_bool("TRANSLATE_CONTEXT_ANALYSIS_REQUIRED", False)
    for index, chunk in enumerate(chunks):
        try:
            base.log(f"Context analysis {index + 1}/{len(chunks)}: {len(chunk)} semantic blocks")
            analyses.append(_analyze_context_chunk(chunk, title=title, profile=profile, instruction=instruction))
        except Exception as error:
            base.log(f"Context analysis chunk {index + 1} failed: {error}")
            if required:
                raise
    if not analyses:
        return _fallback_context(profile, instruction, title)
    context = _merge_context_with_model(analyses, title=title, profile=profile, instruction=instruction)
    context["version"] = _CONTEXT_VERSION
    context["videoTitle"] = title
    context["userInstruction"] = instruction
    context["semanticBlocks"] = len(blocks)
    return context


def _compact_story_context(context: dict) -> dict:
    return {
        "profile": context.get("profile"),
        "contentType": context.get("contentType"),
        "summary": context.get("summary"),
        "characters": context.get("characters") or [],
        "relationships": context.get("relationships") or [],
        "terms": context.get("terms") or [],
        "asrCorrections": context.get("asrCorrections") or [],
        "tone": context.get("tone"),
        "userInstruction": context.get("userInstruction"),
    }


def _translation_system(profile: str) -> str:
    if profile in {"drama", "short_drama"}:
        return (
            "Bạn là biên tập viên Việt hóa phim ngắn Trung Quốc. Bạn hiểu cả cảnh trước khi dịch từng cue. "
            "Ưu tiên thoại tự nhiên, xưng hô đúng quan hệ, tên nhân vật nhất quán. Return strict JSON only."
        )
    if profile == "affiliate":
        return (
            "Bạn là biên tập viên Việt hóa video review/affiliate Trung Quốc. Giữ đúng claim, model, số liệu; "
            "không bịa công dụng. Return strict JSON only."
        )
    return "Bạn là biên tập viên phụ đề Trung -> Việt theo ngữ cảnh. Return strict JSON only."


def _translate_block_once(
    rows: list[dict], *, block_id: int, story: dict, previous: list[dict], next_rows: list[dict]
) -> list[dict]:
    profile = _clean(story.get("profile") or story.get("contentType") or "general")
    system = _translation_system(profile)
    prompt = f"""
QUAN TRỌNG: Không được dịch từng fragment độc lập.
Các id trong CURRENT thường là những mảnh Whisper của CÙNG một câu thoại. Hãy hiểu toàn bộ câu/cảnh trước, sau đó phân bổ lời Việt trở lại đúng timing id để đọc LIỀN MẠCH.

STORY / CONTEXT BIBLE:
{json.dumps(_compact_story_context(story), ensure_ascii=False)}

THOẠI TRƯỚC ĐÃ DỊCH (chỉ để giữ mạch/xưng hô):
{json.dumps(previous, ensure_ascii=False)}

CURRENT BLOCK #{block_id}:
{json.dumps(rows, ensure_ascii=False)}

NGUỒN NGAY SAU BLOCK:
{json.dumps(next_rows, ensure_ascii=False)}

QUY TẮC BẮT BUỘC
1. Trả MỖI id trong CURRENT đúng một lần, không thêm id ngoài CURRENT.
2. `vi` phải là tiếng Việt tự nhiên, KHÔNG để sót chữ Trung.
3. Nếu nhiều id tạo thành một câu, bản dịch qua các id phải nối thành một câu tự nhiên. Không tạo các mảnh vô nghĩa chỉ vì source bị cắt.
4. Xưng hô và tên người phải nhất quán với context bible. Không tự đổi Gia Nghi/Giác Nghị/Giác Y giữa các cue.
5. Các từ quan hệ như 大伯/表叔/伯父/妈/哥/妹子 phải dịch theo QUAN HỆ trong cảnh, không máy móc từng từ.
6. Transcript có thể sai ASR. Chỉ khi ngữ cảnh đủ rõ mới điền `correctedSource`; nếu không chắc để trống. Không được biến phỏng đoán thành sự thật.
7. Thành ngữ/khẩu ngữ dịch theo ý. Không dịch literal kiểu "往死路上逼" -> "lên đường chết".
8. Giữ số, model, thương hiệu và thông tin thực tế. Không thêm claim hoặc chi tiết nguồn không nói.
9. Tôn trọng `durationSec`: cue 1 giây phải rất gọn; có thể chuyển một phần ý sang cue liền kề trong CÙNG utterance miễn toàn câu đúng nghĩa.
10. `utteranceId`: các id liền nhau thuộc cùng một câu/người nói dùng cùng một mã ngắn như u1, u2. Đây sẽ được dùng để ghép TTS cho tự nhiên.
11. `speaker`: dùng tên ổn định nếu context đủ chắc; không chắc thì "".
12. `confidence`: 0..1 cho chất lượng hiểu câu nguồn, không phải tự chấm độ hay của bản dịch.

JSON schema:
{{"translations":[{{"id":1,"vi":"...","correctedSource":"","utteranceId":"u1","speaker":"","confidence":0.9}}]}}
""".strip()
    payload = _chat_json(system, prompt)
    values = payload.get("translations") or []
    if not isinstance(values, list):
        raise RuntimeError("translator returned invalid translations list")
    return [item for item in values if isinstance(item, dict)]


def _translate_block_resilient(
    rows: list[dict], *, block_id: int, story: dict, previous: list[dict], next_rows: list[dict]
) -> list[dict]:
    retries = _env_int("TRANSLATE_RETRIES", 2, 0)
    last_error: Exception | None = None
    expected = {int(row["id"]) for row in rows}
    for attempt in range(retries + 1):
        try:
            values = _translate_block_once(rows, block_id=block_id, story=story, previous=previous, next_rows=next_rows)
            by_id: dict[int, dict] = {}
            for item in values:
                try:
                    item_id = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                if item_id in expected and _clean(item.get("vi")):
                    by_id[item_id] = item
            missing = expected - set(by_id)
            if missing:
                raise RuntimeError(f"translator omitted ids: {sorted(missing)}")
            return [by_id[int(row["id"])] for row in rows]
        except Exception as error:
            last_error = error
            if attempt < retries:
                wait = min(8, 2**attempt)
                base.log(f"Contextual translation retry {attempt + 1}/{retries} after {wait}s: {error}")
                time.sleep(wait)

    if len(rows) > 2:
        middle = len(rows) // 2
        left = _translate_block_resilient(
            rows[:middle], block_id=block_id, story=story, previous=previous, next_rows=rows[middle:] + next_rows
        )
        left_context = previous + [
            {"id": item.get("id"), "vi": _clean(item.get("vi")), "speaker": _clean(item.get("speaker"))}
            for item in left
        ]
        right = _translate_block_resilient(
            rows[middle:], block_id=block_id, story=story, previous=left_context[-8:], next_rows=next_rows
        )
        return left + right
    raise RuntimeError(f"contextual translation failed for block {block_id}: {last_error}")


def _repair_untranslated(rows: list[dict], story: dict) -> list[dict]:
    bad = [item for item in rows if not _clean(item.get("vi")) or _CJK_RE.search(_clean(item.get("vi")))]
    if not bad:
        return rows
    system = "Bạn sửa lỗi cuối của phụ đề Việt. Không để chữ Trung trong `vi`. Return strict JSON only."
    prompt = f"""
Context:
{json.dumps(_compact_story_context(story), ensure_ascii=False)}

Các dòng cần sửa vì còn chữ Trung/trống:
{json.dumps(bad, ensure_ascii=False)}

Giữ nguyên id, utteranceId, speaker nếu có. Chỉ sửa `vi` sang tiếng Việt tự nhiên đúng nghĩa. Nếu `correctedSource` đang có thì giữ nguyên.
Schema: {{"translations":[{{"id":1,"vi":"...","correctedSource":"","utteranceId":"u1","speaker":"","confidence":0.8}}]}}
""".strip()
    try:
        fixed = _chat_json(system, prompt, temperature=0.05).get("translations") or []
        fixed_map: dict[int, dict] = {}
        for item in fixed:
            if not isinstance(item, dict):
                continue
            try:
                fixed_map[int(item.get("id"))] = item
            except (TypeError, ValueError):
                pass
        out = []
        for item in rows:
            try:
                item_id = int(item.get("id"))
            except (TypeError, ValueError):
                out.append(item)
                continue
            replacement = fixed_map.get(item_id)
            if replacement and _clean(replacement.get("vi")):
                merged = dict(item)
                merged.update(replacement)
                out.append(merged)
            else:
                out.append(item)
        return out
    except Exception as error:
        base.log(f"Final untranslated-Chinese repair skipped: {error}")
        return rows


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
        translated = [dict(item, vi=_clean(item.get("text"))) for item in source_segments]
        return translated, _fallback_context(profile, instruction, title)

    title = title or (_clean(source_segments[0].get("_videoTitle")) if source_segments else "")
    story = build_story_context(source_segments, title=title, profile=profile, instruction=instruction)
    rows = [dict(item) for item in source_segments]

    existing_by_id: dict[int, dict] = {}
    for item in existing_segments or []:
        try:
            existing_by_id[int(item.get("id"))] = item
        except (TypeError, ValueError):
            continue
    for row in rows:
        try:
            current = existing_by_id.get(int(row.get("id")))
        except (TypeError, ValueError):
            current = None
        if current:
            for key in ("vi", "speechRate", "appliedSpeechRate", "sourceCorrected", "utteranceId", "speaker", "translationConfidence"):
                if key in current:
                    row[key] = current[key]

    blocks = quality.build_semantic_blocks(source_segments)
    source_by_id = {int(item["id"]): item for item in _source_rows(source_segments)}
    index_by_id = {int(item.get("id", index)): index for index, item in enumerate(rows)}
    rolling = _env_int("TRANSLATE_ROLLING_CONTEXT_SEGMENTS", 8, 0)

    for block in blocks:
        block_ids = [int(value) for value in (block.get("segmentIds") or []) if int(value) in source_by_id]
        if not block_ids:
            continue
        if target_ids is not None and not any(value in target_ids for value in block_ids):
            continue
        current_rows = [source_by_id[value] for value in block_ids]
        first_index = min(index_by_id.get(value, 0) for value in block_ids)
        last_index = max(index_by_id.get(value, first_index) for value in block_ids)

        previous: list[dict] = []
        for item in rows[max(0, first_index - rolling):first_index]:
            if _clean(item.get("vi")):
                previous.append({
                    "id": item.get("id"),
                    "source": _clean(item.get("text")),
                    "vi": _clean(item.get("vi")),
                    "speaker": _clean(item.get("speaker")),
                })
        next_rows = [
            source_by_id[int(item.get("id"))]
            for item in rows[last_index + 1:last_index + 1 + rolling]
            if int(item.get("id")) in source_by_id
        ]

        values = _translate_block_resilient(
            current_rows,
            block_id=int(block.get("id", 0)),
            story=story,
            previous=previous,
            next_rows=next_rows,
        )
        values = _repair_untranslated(values, story)
        value_map = {int(item["id"]): item for item in values if "id" in item}
        for item_id in block_ids:
            translated = value_map.get(item_id)
            if not translated:
                continue
            row = rows[index_by_id[item_id]]
            row["vi"] = _clean(translated.get("vi"))
            corrected = _clean(translated.get("correctedSource"))
            if corrected and corrected != _clean(row.get("text")):
                row["sourceCorrected"] = corrected
            else:
                row.pop("sourceCorrected", None)
            utterance = _clean(translated.get("utteranceId"))
            if utterance:
                row["utteranceId"] = f"b{int(block.get('id', 0))}:{utterance}"
            else:
                row["utteranceId"] = f"b{int(block.get('id', 0))}:u{item_id}"
            speaker = _clean(translated.get("speaker"))
            if speaker:
                row["speaker"] = speaker
            try:
                confidence = float(translated.get("confidence"))
                row["translationConfidence"] = round(max(0.0, min(1.0, confidence)), 3)
            except (TypeError, ValueError):
                pass
        base.log(f"Contextual translation: block {int(block.get('id', 0)) + 1}/{len(blocks)}")

    missing = [row.get("id") for row in rows if not _clean(row.get("vi"))]
    if missing:
        raise RuntimeError(f"contextual translator left empty ids: {missing[:20]}")
    return rows, story


def translate_segments(segments: list[dict], detected_language: str) -> dict:
    """Drop-in replacement for localize_fast.translate_segments."""
    profile = os.getenv("TRANSLATE_PROFILE", "auto").strip().lower() or "auto"
    instruction = os.getenv("TRANSLATE_CONTEXT_HINT", "").strip()
    translated, story = translate_contextual(
        segments,
        detected_language,
        profile=profile,
        instruction=instruction,
    )
    by_id = {int(item.get("id")): item for item in translated}
    for index, segment in enumerate(segments):
        item = by_id.get(int(segment.get("id", index)))
        if not item:
            continue
        for key in ("vi", "sourceCorrected", "utteranceId", "speaker", "translationConfidence"):
            if key in item:
                segment[key] = item[key]
    return story


def write_context_artifact(output_dir: Path, stem: str, context: dict) -> str:
    path = output_dir / f"{stem}.translation-context.json"
    payload = {"version": _CONTEXT_VERSION, **context}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)
