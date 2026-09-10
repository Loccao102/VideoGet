#!/usr/bin/env python3
"""Performance wrapper around localize.py.

Keeps the stable pipeline but replaces the slow/network-sensitive stages:
- disables Qwen3 thinking for translation;
- retries and splits failed translation batches;
- groups subtitle segments before TTS and synthesizes groups concurrently;
- treats no-speech videos as a successful skip instead of a failed job.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import edge_tts
from pydub import AudioSegment
from pydub.effects import speedup

import localize as base


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


def ollama_base() -> str:
    url = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434").rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3].rstrip("/")
    return url


def translate_batch_ollama(batch: list[dict], detected_language: str) -> list[dict]:
    payload = {
        "model": os.getenv("OLLAMA_MODEL", "qwen3:8b"),
        "stream": False,
        "think": False,
        "keep_alive": os.getenv("OLLAMA_KEEP_ALIVE", "15m"),
        "messages": [
            {"role": "system", "content": "Return strict JSON only. Do not reason aloud."},
            {"role": "user", "content": base.translation_prompt(batch, detected_language)},
        ],
        "options": {"temperature": 0},
    }
    response = base.http_json(
        ollama_base() + "/api/chat",
        payload,
        timeout=env_int("TRANSLATE_TIMEOUT_SEC", 180, 30),
    )
    content = response.get("message", {}).get("content", "")
    parsed = base.extract_json(content)
    return parsed.get("translations", [])


def translate_once(batch: list[dict], language: str, provider: str) -> list[dict]:
    if provider == "ollama":
        return translate_batch_ollama(batch, language)
    return base.translate_batch_openai(batch, language)


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


def translate_segments(segments: list[dict], detected_language: str) -> None:
    if detected_language.lower().startswith("vi"):
        for segment in segments:
            segment["vi"] = segment["text"]
        return

    provider = os.getenv("TRANSLATE_PROVIDER", "ollama").strip().lower()
    if provider not in {"ollama", "openai", "openai_compatible"}:
        raise RuntimeError("TRANSLATE_PROVIDER must be ollama, openai, or openai_compatible")

    batch_size = env_int("TRANSLATE_BATCH_SIZE", 12, 1)
    for start in range(0, len(segments), batch_size):
        chunk = segments[start:start + batch_size]
        batch = [{"id": item["id"], "text": item["text"]} for item in chunk]
        translated = translate_resilient(batch, detected_language, provider)
        lookup = {
            int(item["id"]): str(item.get("text", "")).strip()
            for item in translated
            if "id" in item
        }
        for item in chunk:
            if item["id"] not in lookup:
                one = translate_resilient(
                    [{"id": item["id"], "text": item["text"]}], detected_language, provider
                )
                if not one:
                    raise RuntimeError(f"translator omitted segment id {item['id']}")
                lookup[item["id"]] = str(one[0].get("text", "")).strip()
            item["vi"] = lookup[item["id"]]
        base.log(f"Đã dịch {min(start + len(chunk), len(segments))}/{len(segments)} segment")


def group_segments(segments: list[dict]) -> list[dict]:
    max_chars = env_int("TTS_GROUP_MAX_CHARS", 220, 40)
    max_duration = env_float("TTS_GROUP_MAX_DURATION_SEC", 12.0, 2.0)
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


async def synthesize_segments(segments: list[dict], workdir: Path, total_ms: int) -> Path:
    groups = group_segments(segments)
    concurrency = env_int("TTS_CONCURRENCY", 4, 1)
    voice = os.getenv("TTS_VOICE", "vi-VN-HoaiMyNeural")
    rate = os.getenv("TTS_RATE", "+8%")
    semaphore = asyncio.Semaphore(concurrency)
    base.log(f"TTS: {len(segments)} segment -> {len(groups)} nhóm; concurrency={concurrency}")

    async def render_one(index: int, group: dict):
        async with semaphore:
            output = workdir / f"tts_group_{index:04}.mp3"
            retries = env_int("TTS_RETRIES", 2, 0)
            last_error = None
            for attempt in range(retries + 1):
                try:
                    await edge_tts.Communicate(group["text"], voice=voice, rate=rate).save(str(output))
                    last_error = None
                    break
                except Exception as error:
                    last_error = error
                    if attempt < retries:
                        await asyncio.sleep(min(6, 2**attempt))
            if last_error:
                raise RuntimeError(f"TTS group {index + 1} failed: {last_error}")
            clip = AudioSegment.from_file(output).set_frame_rate(48000).set_channels(1)
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


def input_arg() -> str:
    try:
        index = sys.argv.index("--input")
        return str(Path(sys.argv[index + 1]).resolve())
    except (ValueError, IndexError):
        return ""


base.translate_batch_ollama = translate_batch_ollama
base.translate_segments = translate_segments
base.synthesize_segments = synthesize_segments

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
