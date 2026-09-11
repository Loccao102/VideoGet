#!/usr/bin/env python3
"""Fast localization pipeline for VideoGet.

Optimizations:
- translates with nearby subtitle context instead of isolated machine-translation batches;
- asks the LLM to localize into natural Vietnamese spoken language rather than word-for-word output;
- disables Qwen3 thinking for subtitle translation;
- retries and recursively splits failed translation batches;
- groups subtitle segments before TTS and synthesizes groups concurrently;
- uses adaptive Edge TTS retry/backoff and splits failed groups into smaller requests;
- treats no-speech videos as a successful skip;
- renders one final Vietnamese video with source-caption/logo cleanup and subtle color grading.
"""
import asyncio
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import edge_tts
from pydub import AudioSegment
from pydub.effects import speedup

import localize as base


TRANSLATION_PROMPT_VERSION = 2


def env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return max(minimum, default)


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except ValueError:
        return max(minimum, default)


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def ollama_base() -> str:
    url = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434").rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3].rstrip("/")
    return url


def translation_prompt(batch: list[dict], detected_language: str) -> str:
    context = batch[0].get("_context", {}) if batch else {}
    targets = [{"id": item["id"], "text": item["text"]} for item in batch]
    style = os.getenv("TRANSLATE_STYLE", "natural_social").strip().lower()
    title = str(context.get("videoTitle", "")).strip()
    before = context.get("before") or []
    after = context.get("after") or []

    style_hint = (
        "Giọng tự nhiên như lời thoại video ngắn/TikTok Việt Nam: gọn, dễ nghe, đời thường, không văn dịch."
        if style in {"natural_social", "social", "natural"}
        else "Tiếng Việt tự nhiên, rõ nghĩa và phù hợp phụ đề nói."
    )

    context_payload = {
        "videoTitle": title,
        "before": before,
        "after": after,
    }

    return (
        "Bạn là biên tập viên phụ đề Trung -> Việt, không phải máy dịch từng chữ.\n"
        "Mục tiêu là người Việt nghe một lần phải hiểu ngay người trong video đang nói gì.\n\n"
        "QUY TẮC BẮT BUỘC:\n"
        f"- {style_hint}\n"
        "- Dịch theo Ý NGHĨA của cả câu và ngữ cảnh trước/sau; tiếng Trung thường lược chủ ngữ thì hãy khôi phục tự nhiên khi cần.\n"
        "- Với tiếng lóng, khẩu ngữ, beauty, mỹ phẩm, đồ gia dụng, review sản phẩm, thương mại điện tử: dùng cách gọi phổ biến ở Việt Nam, tránh Hán-Việt cứng và câu nghe như Google Translate.\n"
        "- Giữ đúng tên thương hiệu, tên người, model sản phẩm và thuật ngữ đã quen dùng.\n"
        "- Không tự thêm công dụng, claim quảng cáo, giá, số liệu hoặc thông tin nguồn không nói.\n"
        "- Nếu ASR có vẻ nghe sai hoặc câu nguồn bị cụt, dùng ngữ cảnh để chọn cách hiểu hợp lý nhất; nếu vẫn không chắc thì dịch bảo thủ, KHÔNG bịa.\n"
        "- Mỗi id phải là một câu/ý tiếng Việt ngắn, phù hợp thời lượng phụ đề. Có thể đổi trật tự từ và cách diễn đạt để nghe tự nhiên.\n"
        "- Các câu trong phần context chỉ để hiểu mạch nói. KHÔNG trả translation cho context, chỉ trả các id trong targets.\n"
        "- Không giải thích, không markdown, không ghi chú.\n\n"
        f"Ngôn ngữ nguồn: {detected_language or 'unknown'}\n"
        "NGỮ CẢNH VIDEO:\n"
        + json.dumps(context_payload, ensure_ascii=False)
        + "\n\nCÁC ĐOẠN CẦN DỊCH:\n"
        + json.dumps(targets, ensure_ascii=False)
        + "\n\nChỉ trả JSON hợp lệ đúng schema: "
        + '{"translations":[{"id":0,"text":"..."}]}.'
    )


def translate_batch_ollama(batch: list[dict], detected_language: str) -> list[dict]:
    payload = {
        "model": os.getenv("OLLAMA_MODEL", "qwen3:8b"),
        "stream": False,
        "think": False,
        "format": "json",
        "keep_alive": os.getenv("OLLAMA_KEEP_ALIVE", "15m"),
        "messages": [
            {
                "role": "system",
                "content": "Bạn là biên tập viên phụ đề tiếng Việt. Return strict JSON only. Do not reason aloud.",
            },
            {"role": "user", "content": translation_prompt(batch, detected_language)},
        ],
        "options": {
            "temperature": env_float("TRANSLATE_TEMPERATURE", 0.15, 0.0),
        },
    }
    response = base.http_json(
        ollama_base() + "/api/chat",
        payload,
        timeout=env_int("TRANSLATE_TIMEOUT_SEC", 180, 30),
    )
    content = response.get("message", {}).get("content", "")
    parsed = base.extract_json(content)
    return parsed.get("translations", [])


def translate_batch_openai(batch: list[dict], detected_language: str) -> list[dict]:
    base_url = os.getenv("OPENAI_COMPAT_BASE_URL", "http://host.docker.internal:11434/v1").rstrip("/")
    model = os.getenv("OPENAI_COMPAT_MODEL", os.getenv("OLLAMA_MODEL", "qwen3:8b"))
    api_key = os.getenv("OPENAI_COMPAT_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = {
        "model": model,
        "temperature": env_float("TRANSLATE_TEMPERATURE", 0.15, 0.0),
        "messages": [
            {
                "role": "system",
                "content": "Bạn là biên tập viên phụ đề tiếng Việt. Return strict JSON only. Do not reason aloud.",
            },
            {"role": "user", "content": translation_prompt(batch, detected_language)},
        ],
    }
    response = base.http_json(
        base_url + "/chat/completions",
        payload,
        headers=headers,
        timeout=env_int("TRANSLATE_TIMEOUT_SEC", 180, 30),
    )
    choices = response.get("choices") or []
    if not choices:
        raise RuntimeError("translation endpoint returned no choices")
    content = choices[0].get("message", {}).get("content", "")
    parsed = base.extract_json(content)
    return parsed.get("translations", [])


def translate_once(batch: list[dict], language: str, provider: str) -> list[dict]:
    if provider == "ollama":
        return translate_batch_ollama(batch, language)
    return translate_batch_openai(batch, language)


def translate_resilient(batch: list[dict], language: str, provider: str) -> list[dict]:
    retries = env_int("TRANSLATE_RETRIES", 2, 0)
    last_error = None
    for attempt in range(retries + 1):
        try:
            return translate_once(batch, language, provider)
        except Exception as error:
            last_error = error
            if attempt < retries:
                wait = min(8, 2**attempt)
                base.log(f"Dịch batch lỗi, retry {attempt + 1}/{retries} sau {wait}s: {error}")
                time.sleep(wait)
    if len(batch) > 1:
        middle = max(1, len(batch) // 2)
        base.log(f"Tách batch lỗi {len(batch)} -> {middle} + {len(batch)-middle}")
        return translate_resilient(batch[:middle], language, provider) + translate_resilient(
            batch[middle:], language, provider
        )
    raise RuntimeError(f"cannot translate segment {batch[0]['id']}: {last_error}")


def context_item(item: dict, include_vi: bool = False) -> dict:
    result = {"id": item.get("id"), "text": str(item.get("text", "")).strip()}
    if include_vi and str(item.get("vi", "")).strip():
        result["vi"] = str(item.get("vi", "")).strip()
    return result


def translate_segments(segments: list[dict], detected_language: str) -> None:
    if detected_language.lower().startswith("vi"):
        for segment in segments:
            segment["vi"] = segment["text"]
        return

    provider = os.getenv("TRANSLATE_PROVIDER", "ollama").strip().lower()
    if provider not in {"ollama", "openai", "openai_compatible"}:
        raise RuntimeError("TRANSLATE_PROVIDER must be ollama, openai, or openai_compatible")

    batch_size = env_int("TRANSLATE_BATCH_SIZE", 10, 1)
    context_radius = env_int("TRANSLATE_CONTEXT_SEGMENTS", 3, 0)
    video_title = ""
    if segments:
        video_title = str(segments[0].get("_videoTitle", "")).strip()

    for start in range(0, len(segments), batch_size):
        chunk = segments[start:start + batch_size]
        before_start = max(0, start - context_radius)
        after_end = min(len(segments), start + len(chunk) + context_radius)
        before = [context_item(item, include_vi=True) for item in segments[before_start:start]]
        after = [context_item(item) for item in segments[start + len(chunk):after_end]]
        context = {
            "videoTitle": video_title,
            "before": before,
            "after": after,
        }
        batch = [
            {"id": item["id"], "text": item["text"], "_context": context}
            for item in chunk
        ]
        translated = translate_resilient(batch, detected_language, provider)
        lookup = {
            int(item["id"]): str(item.get("text", "")).strip()
            for item in translated
            if "id" in item and str(item.get("text", "")).strip()
        }
        for item in chunk:
            if item["id"] not in lookup:
                one = translate_resilient(
                    [{"id": item["id"], "text": item["text"], "_context": context}],
                    detected_language,
                    provider,
                )
                if not one or not str(one[0].get("text", "")).strip():
                    raise RuntimeError(f"translator omitted segment id {item['id']}")
                lookup[item["id"]] = str(one[0].get("text", "")).strip()
            item["vi"] = lookup[item["id"]]
        base.log(
            f"Đã dịch theo ngữ cảnh {min(start + len(chunk), len(segments))}/{len(segments)} segment"
        )


def group_segments(segments: list[dict]) -> list[dict]:
    max_chars = env_int("TTS_GROUP_MAX_CHARS", 180, 40)
    max_duration = env_float("TTS_GROUP_MAX_DURATION_SEC", 10.0, 2.0)
    max_gap = env_float("TTS_GROUP_MAX_GAP_SEC", 1.2, 0.0)
    groups = []
    current = None

    for segment in segments:
        text = str(segment.get("vi", "")).strip()
        if not text:
            continue
        if current is None:
            current = {"start": segment["start"], "end": segment["end"], "texts": [text]}
            continue
        chars = len(" ".join(current["texts"])) + 1 + len(text)
        duration = float(segment["end"]) - float(current["start"])
        gap = float(segment["start"]) - float(current["end"])
        if chars <= max_chars and duration <= max_duration and gap <= max_gap:
            current["end"] = segment["end"]
            current["texts"].append(text)
        else:
            groups.append(current)
            current = {"start": segment["start"], "end": segment["end"], "texts": [text]}
    if current:
        groups.append(current)
    for group in groups:
        group["text"] = " ".join(group.pop("texts"))
    return groups


def fit_clip(clip: AudioSegment, target_ms: int) -> AudioSegment:
    if len(clip) <= target_ms + 120:
        return clip
    ratio = len(clip) / max(150, target_ms)
    playback_speed = min(env_float("TTS_MAX_SPEED", 2.0, 1.0), max(1.01, ratio))
    try:
        clip = speedup(clip, playback_speed=playback_speed, chunk_size=60, crossfade=8)
    except Exception:
        pass
    return clip[:target_ms] if len(clip) > target_ms + 120 else clip


def clean_tts_text(text: str) -> str:
    text = str(text or "").replace("\u200b", " ").replace("\ufeff", " ")
    text = "".join(ch if ch >= " " or ch in "\n\t" else " " for ch in text)
    return " ".join(text.split()).strip()


def split_tts_text(text: str, max_chars: int) -> list[str]:
    text = clean_tts_text(text)
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    sentences = [part.strip() for part in re.split(r"(?<=[.!?。！？;；:：,，])\s*", text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences or [text]:
        if len(sentence) > max_chars:
            words = sentence.split()
            if len(words) <= 1:
                pieces = [sentence[i:i + max_chars] for i in range(0, len(sentence), max_chars)]
            else:
                pieces = []
                piece = ""
                for word in words:
                    candidate = f"{piece} {word}".strip()
                    if piece and len(candidate) > max_chars:
                        pieces.append(piece)
                        piece = word
                    else:
                        piece = candidate
                if piece:
                    pieces.append(piece)
        else:
            pieces = [sentence]

        for piece in pieces:
            candidate = f"{current} {piece}".strip()
            if current and len(candidate) > max_chars:
                chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk]


async def synthesize_tts_file(text: str, output: Path, voice: str, rate: str, label: str) -> None:
    text = clean_tts_text(text)
    if not text:
        raise RuntimeError(f"{label}: empty TTS text")

    retries = env_int("TTS_RETRIES", 4, 0)
    timeout = env_int("TTS_REQUEST_TIMEOUT_SEC", 75, 15)
    last_error = None
    for attempt in range(retries + 1):
        try:
            output.unlink(missing_ok=True)
            await asyncio.wait_for(
                edge_tts.Communicate(text, voice=voice, rate=rate).save(str(output)),
                timeout=timeout,
            )
            if not output.exists() or output.stat().st_size < 256:
                raise RuntimeError("Edge TTS returned an empty audio file")
            return
        except Exception as error:
            last_error = error
            output.unlink(missing_ok=True)
            if attempt < retries:
                wait = min(12.0, (2**attempt) + random.uniform(0.25, 1.25))
                base.log(
                    f"{label} lỗi, retry {attempt + 1}/{retries} sau {wait:.1f}s: {error}"
                )
                await asyncio.sleep(wait)
    raise RuntimeError(f"{label} failed after {retries + 1} attempts: {last_error}")


async def synthesize_segments(segments: list[dict], workdir: Path, total_ms: int) -> Path:
    groups = group_segments(segments)
    concurrency = env_int("TTS_CONCURRENCY", 2, 1)
    voice = os.getenv("TTS_VOICE", "vi-VN-HoaiMyNeural")
    rate = os.getenv("TTS_RATE", "+8%")
    fallback_chars = env_int("TTS_FALLBACK_MAX_CHARS", 90, 30)
    semaphore = asyncio.Semaphore(concurrency)
    base.log(f"TTS: {len(segments)} segment -> {len(groups)} nhóm; concurrency={concurrency}")

    async def render_one(index: int, group: dict):
        async with semaphore:
            text = clean_tts_text(group["text"])
            output = workdir / f"tts_group_{index:04}.mp3"
            try:
                await synthesize_tts_file(text, output, voice, rate, f"TTS group {index + 1}")
                clip = AudioSegment.from_file(output).set_frame_rate(48000).set_channels(1)
            except Exception as primary_error:
                parts = split_tts_text(text, fallback_chars)
                if len(parts) <= 1:
                    raise RuntimeError(f"TTS group {index + 1} failed: {primary_error}") from primary_error

                base.log(
                    f"TTS group {index + 1} vẫn lỗi; chia {len(text)} ký tự thành "
                    f"{len(parts)} request nhỏ để fallback"
                )
                clip = AudioSegment.silent(duration=0, frame_rate=48000).set_channels(1)
                for part_index, part in enumerate(parts, start=1):
                    part_output = workdir / f"tts_group_{index:04}_part_{part_index:02}.mp3"
                    try:
                        await synthesize_tts_file(
                            part,
                            part_output,
                            voice,
                            rate,
                            f"TTS group {index + 1}.{part_index}",
                        )
                    except Exception as fallback_error:
                        raise RuntimeError(
                            f"TTS group {index + 1} failed after split fallback: {fallback_error}"
                        ) from fallback_error
                    part_clip = AudioSegment.from_file(part_output).set_frame_rate(48000).set_channels(1)
                    if len(clip) > 0:
                        clip += AudioSegment.silent(duration=60, frame_rate=48000).set_channels(1)
                    clip += part_clip

            start_ms = max(0, int(float(group["start"]) * 1000))
            end_ms = max(start_ms + 150, int(float(group["end"]) * 1000))
            return start_ms, fit_clip(clip, end_ms - start_ms)

    clips = await asyncio.gather(*(render_one(i, group) for i, group in enumerate(groups)))
    track = AudioSegment.silent(duration=total_ms + 500, frame_rate=48000).set_channels(1)
    for i, (start_ms, clip) in enumerate(clips, start=1):
        track = track.overlay(clip, position=start_ms)
        base.log(f"Đã ghép voice {i}/{len(clips)}")
    output = workdir / "voice_vi.wav"
    track[:total_ms].export(output, format="wav")
    return output


def parse_regions(value: str) -> list[tuple[float, float, float, float]]:
    regions = []
    for raw in value.split(";"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            x, y, width, height = [float(item.strip()) for item in raw.split(",")]
        except (ValueError, TypeError):
            base.log(f"Bỏ qua VIDEO_LOGO_MASKS không hợp lệ: {raw}")
            continue
        x = max(0.0, min(0.98, x))
        y = max(0.0, min(0.98, y))
        width = max(0.01, min(1.0 - x, width))
        height = max(0.01, min(1.0 - y, height))
        regions.append((x, y, width, height))
    return regions


def add_blur_region(filters: list[str], input_label: str, index: int, region: tuple[float, float, float, float], radius: int) -> str:
    x, y, width, height = region
    base_label = f"maskbase{index}"
    crop_label = f"maskcrop{index}"
    blur_label = f"maskblur{index}"
    output_label = f"maskout{index}"
    filters.append(f"[{input_label}]split=2[{base_label}][{crop_label}]")
    filters.append(
        f"[{crop_label}]crop=w=iw*{width:.5f}:h=ih*{height:.5f}:x=iw*{x:.5f}:y=ih*{y:.5f},"
        f"boxblur=luma_radius={radius}:luma_power=1:chroma_radius={max(1, radius//2)}:chroma_power=1[{blur_label}]"
    )
    filters.append(
        f"[{base_label}][{blur_label}]overlay=x=main_w*{x:.5f}:y=main_h*{y:.5f}[{output_label}]"
    )
    return output_label


def render_video(input_path: Path, voice_track: Path, vi_srt: Path, output_path: Path) -> None:
    burn_subtitles = env_bool("BURN_SUBTITLES", True)
    cleanup = env_bool("VIDEO_CLEANUP", True)
    cleanup_source_subtitles = env_bool("VIDEO_CLEANUP_SOURCE_SUBTITLES", True)
    cleanup_logos = env_bool("VIDEO_CLEANUP_LOGOS", True)
    color_grade = env_bool("VIDEO_COLOR_GRADE", True)
    original_volume = max(0.0, env_float("ORIGINAL_AUDIO_VOLUME", 0.08, 0.0))
    source_has_audio = base.has_audio_stream(input_path)

    cmd = ["ffmpeg", "-y", "-i", str(input_path), "-i", str(voice_track)]
    filters: list[str] = []

    if source_has_audio and original_volume > 0:
        filters.append(f"[0:a]volume={original_volume}[original]")
        filters.append("[original][1:a]amix=inputs=2:duration=longest:normalize=0[aout]")
        audio_map = "[aout]"
    else:
        audio_map = "1:a:0"

    video_label = "0:v"
    mask_index = 0
    if cleanup and cleanup_source_subtitles:
        subtitle_region = (
            env_float("VIDEO_SUBTITLE_MASK_X", 0.02),
            env_float("VIDEO_SUBTITLE_MASK_Y", 0.72),
            env_float("VIDEO_SUBTITLE_MASK_W", 0.96),
            env_float("VIDEO_SUBTITLE_MASK_H", 0.24),
        )
        video_label = add_blur_region(
            filters,
            video_label,
            mask_index,
            subtitle_region,
            env_int("VIDEO_SUBTITLE_BLUR", 14, 2),
        )
        mask_index += 1

    if cleanup and cleanup_logos:
        raw_masks = os.getenv(
            "VIDEO_LOGO_MASKS",
            "0.76,0.02,0.22,0.10;0.76,0.86,0.22,0.12",
        )
        for region in parse_regions(raw_masks):
            video_label = add_blur_region(
                filters,
                video_label,
                mask_index,
                region,
                env_int("VIDEO_LOGO_BLUR", 12, 2),
            )
            mask_index += 1

    if color_grade:
        graded = "vgraded"
        contrast = env_float("VIDEO_CONTRAST", 1.04, 0.1)
        saturation = env_float("VIDEO_SATURATION", 1.08, 0.0)
        brightness = float(os.getenv("VIDEO_BRIGHTNESS", "0.01"))
        filters.append(
            f"[{video_label}]eq=contrast={contrast}:saturation={saturation}:brightness={brightness},"
            "unsharp=5:5:0.35:5:5:0.0[vgraded]"
        )
        video_label = graded

    if burn_subtitles:
        escaped = base.escape_subtitle_path(vi_srt)
        filters.append(
            f"[{video_label}]subtitles='{escaped}':"
            "force_style='FontName=Noto Sans,FontSize=20,Bold=1,PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=0,MarginV=48,Alignment=2'[vout]"
        )
        video_map = "[vout]"
    else:
        video_map = f"[{video_label}]" if video_label != "0:v" else "0:v:0"

    if filters:
        cmd += ["-filter_complex", ";".join(filters)]
    cmd += ["-map", video_map, "-map", audio_map]

    if filters:
        cmd += [
            "-c:v", "libx264",
            "-preset", os.getenv("VIDEO_PRESET", "veryfast"),
            "-crf", os.getenv("VIDEO_CRF", "21"),
            "-pix_fmt", "yuv420p",
        ]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(output_path)]

    base.log("Đang render final video: cleanup chữ/logo -> color grade -> burn sub Việt...")
    base.run(cmd)


def input_arg() -> str:
    try:
        index = sys.argv.index("--input")
        return str(Path(sys.argv[index + 1]).resolve())
    except (ValueError, IndexError):
        return ""


base.translate_batch_ollama = translate_batch_ollama
base.translate_segments = translate_segments
base.synthesize_segments = synthesize_segments
base.render_video = render_video

if __name__ == "__main__":
    try:
        base.main()
    except RuntimeError as error:
        if "Whisper did not detect any speech" in str(error):
            print(json.dumps({"outputVideo": input_arg(), "segments": 0, "skippedReason": "no_speech"}, ensure_ascii=False))
        else:
            base.log(f"ERROR: {error}")
            sys.exit(1)
    except Exception as error:
        base.log(f"ERROR: {error}")
        sys.exit(1)
