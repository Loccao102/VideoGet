#!/usr/bin/env python3
"""Adaptive Vietnamese TTS for editable subtitles.

Each subtitle segment owns its timing slot. Longer Vietnamese text automatically
gets a faster Edge TTS speaking rate before a final loss-minimizing fit pass.
A segment can override the automatic rate with `speechRate` (for example +20%).
"""
import asyncio
import os
import random
import re
from pathlib import Path

import edge_tts
from pydub import AudioSegment
from pydub.effects import speedup


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


def clean_text(value: str) -> str:
    value = str(value or "").replace("\u200b", " ").replace("\ufeff", " ")
    return " ".join(value.split()).strip()


def parse_rate(value: object, fallback: int) -> int:
    if value is None:
        return fallback
    raw = str(value).strip().lower()
    if not raw or raw == "auto":
        return fallback
    match = re.search(r"[-+]?\d+", raw)
    if not match:
        return fallback
    try:
        return int(match.group(0))
    except ValueError:
        return fallback


def edge_rate(percent: int) -> str:
    return f"{percent:+d}%"


def auto_rate_percent(segment: dict) -> int:
    base_rate = parse_rate(os.getenv("TTS_RATE", "+8%"), 8)
    override = str(segment.get("speechRate", "auto") or "auto").strip().lower()
    min_rate = env_int("TTS_EDITOR_MIN_RATE_PERCENT", max(-20, base_rate), -80)
    max_rate = env_int("TTS_EDITOR_MAX_RATE_PERCENT", 70, 0)
    if override and override != "auto":
        return max(min_rate, min(max_rate, parse_rate(override, base_rate)))

    start = float(segment.get("start", 0.0))
    end = float(segment.get("end", start + 0.15))
    duration = max(0.15, end - start)
    text = clean_text(segment.get("vi", ""))
    # Count visible characters rather than spaces. Vietnamese short-form subtitles
    # are comfortable around 13-15 visible chars/s; above that we ask TTS to speak
    # faster instead of simply truncating the resulting audio.
    visible_chars = len(re.sub(r"\s+", "", text))
    target_cps = env_float("TTS_TARGET_CHARS_PER_SEC", 14.0, 6.0)
    cps = visible_chars / duration if duration > 0 else target_cps
    ratio = max(1.0, cps / target_cps)
    required = base_rate + int(round((ratio - 1.0) * 100.0))
    return max(min_rate, min(max_rate, required))


async def synthesize_file(text: str, output: Path, voice: str, rate: str, label: str) -> None:
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
                await asyncio.sleep(min(12.0, (2**attempt) + random.uniform(0.25, 1.25)))
    raise RuntimeError(f"{label} failed after {retries + 1} attempts: {last_error}")


def fit_clip(clip: AudioSegment, target_ms: int) -> AudioSegment:
    if len(clip) <= target_ms + 100:
        return clip
    ratio = len(clip) / max(150, target_ms)
    max_speed = env_float("TTS_EDITOR_POST_MAX_SPEED", 1.35, 1.0)
    playback_speed = min(max_speed, max(1.01, ratio))
    try:
        clip = speedup(clip, playback_speed=playback_speed, chunk_size=60, crossfade=8)
    except Exception:
        pass
    return clip[:target_ms] if len(clip) > target_ms + 100 else clip


async def synthesize_segments(segments: list[dict], workdir: Path, total_ms: int) -> Path:
    voice = os.getenv("TTS_VOICE", "vi-VN-HoaiMyNeural")
    concurrency = env_int("TTS_EDITOR_CONCURRENCY", 3, 1)
    gain_db = env_float("TTS_GAIN_DB", 0.0, -30.0)
    semaphore = asyncio.Semaphore(concurrency)

    active = [segment for segment in segments if clean_text(segment.get("vi", ""))]

    async def render_one(index: int, segment: dict):
        async with semaphore:
            text = clean_text(segment.get("vi", ""))
            start_ms = max(0, int(float(segment.get("start", 0.0)) * 1000))
            end_ms = max(start_ms + 150, int(float(segment.get("end", 0.0)) * 1000))
            rate_percent = auto_rate_percent(segment)
            output = workdir / f"tts_edit_{index:05}.mp3"
            await synthesize_file(text, output, voice, edge_rate(rate_percent), f"TTS segment {index + 1}")
            clip = AudioSegment.from_file(output).set_frame_rate(48000).set_channels(1)
            clip = fit_clip(clip, end_ms - start_ms)
            if gain_db:
                clip += gain_db
            return index, start_ms, clip, rate_percent

    rendered = await asyncio.gather(*(render_one(i, item) for i, item in enumerate(active)))
    track = AudioSegment.silent(duration=total_ms + 500, frame_rate=48000).set_channels(1)
    for _, start_ms, clip, _ in rendered:
        track = track.overlay(clip, position=start_ms)

    # Persist the actually selected automatic rate into the in-memory segment list.
    # This is useful for the subtitle editor after the worker writes localization.json.
    active_index = {id(segment): segment for segment in active}
    for rendered_index, _, _, rate_percent in rendered:
        segment = active[rendered_index]
        segment["appliedSpeechRate"] = edge_rate(rate_percent)
        if id(segment) in active_index and not segment.get("speechRate"):
            segment["speechRate"] = "auto"

    output = workdir / "voice_vi.wav"
    track[:total_ms].export(output, format="wav")
    return output
