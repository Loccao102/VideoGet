#!/usr/bin/env python3
"""Adaptive Vietnamese TTS for editable/contextual subtitles.

Localization V2.1 normally produces one natural subtitle cue per utterance. Older
jobs may still contain several adjacent cues sharing an `utteranceId`; those cues
are grouped before synthesis.

For auto rate, Edge TTS is rendered once, measured, and if necessary rendered a
second time with a refined speech rate. Waveform speedup is only a conservative
last-mile fit instead of the primary timing mechanism.
"""
import asyncio
import math
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


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


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


def _rate_policy(segment: dict) -> str:
    return str(segment.get("speechRate", "auto") or "auto").strip().lower() or "auto"


def _voice_for_unit(unit: dict) -> str:
    explicit = str(unit.get("voice", "") or "").strip()
    if explicit:
        return explicit
    gender = str(unit.get("voiceGender", "") or "").strip().lower()
    if gender == "male":
        return os.getenv("TTS_VOICE_MALE", "vi-VN-NamMinhNeural")
    if gender == "female":
        return os.getenv("TTS_VOICE_FEMALE", os.getenv("TTS_VOICE", "vi-VN-HoaiMyNeural"))
    return os.getenv("TTS_VOICE", "vi-VN-HoaiMyNeural")


def auto_rate_percent_for_slot(text: str, start: float, end: float, override: str = "auto") -> int:
    base_rate = parse_rate(os.getenv("TTS_RATE", "+8%"), 8)
    min_rate = env_int("TTS_EDITOR_MIN_RATE_PERCENT", max(-20, base_rate), -80)
    max_rate = env_int("TTS_EDITOR_MAX_RATE_PERCENT", 70, 0)
    override = str(override or "auto").strip().lower()
    if override and override != "auto":
        return max(min_rate, min(max_rate, parse_rate(override, base_rate)))

    duration = max(0.15, float(end) - float(start))
    visible_chars = len(re.sub(r"\s+", "", clean_text(text)))
    target_cps = env_float("TTS_TARGET_CHARS_PER_SEC", 14.0, 6.0)
    cps = visible_chars / duration if duration > 0 else target_cps
    ratio = max(1.0, cps / target_cps)
    required = base_rate + int(round((ratio - 1.0) * 100.0))
    return max(min_rate, min(max_rate, required))


def auto_rate_percent(segment: dict) -> int:
    start = float(segment.get("start", 0.0))
    end = float(segment.get("end", start + 0.15))
    return auto_rate_percent_for_slot(clean_text(segment.get("vi", "")), start, end, _rate_policy(segment))


def build_tts_units(segments: list[dict]) -> list[dict]:
    active = [segment for segment in segments if clean_text(segment.get("vi", ""))]
    if not active:
        return []
    if not env_bool("TTS_GROUP_CONTEXTUAL_UTTERANCES", True):
        return [
            {
                "segments": [segment],
                "start": float(segment.get("start", 0.0)),
                "end": float(segment.get("end", 0.0)),
                "text": clean_text(segment.get("vi", "")),
                "speechRate": _rate_policy(segment),
                "utteranceId": str(segment.get("utteranceId", "") or ""),
                "speaker": str(segment.get("speaker", "") or ""),
                "voice": str(segment.get("voice", "") or ""),
                "voiceGender": str(segment.get("voiceGender", "") or ""),
            }
            for segment in active
        ]

    max_duration = env_float("TTS_UTTERANCE_MAX_DURATION_SEC", 10.0, 1.0)
    max_chars = env_int("TTS_UTTERANCE_MAX_CHARS", 220, 30)
    max_gap = env_float("TTS_UTTERANCE_MAX_GAP_SEC", 0.45, 0.0)
    units: list[dict] = []
    current: dict | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        current["text"] = " ".join(clean_text(item.get("vi", "")) for item in current["segments"]).strip()
        units.append(current)
        current = None

    for segment in active:
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start + 0.15))
        utterance = str(segment.get("utteranceId", "") or "").strip()
        speaker = str(segment.get("speaker", "") or "").strip()
        policy = _rate_policy(segment)
        text = clean_text(segment.get("vi", ""))
        voice = str(segment.get("voice", "") or "").strip()
        voice_gender = str(segment.get("voiceGender", "") or "").strip().lower()

        if current is None:
            current = {
                "segments": [segment],
                "start": start,
                "end": end,
                "utteranceId": utterance,
                "speaker": speaker,
                "speechRate": policy,
                "voice": voice,
                "voiceGender": voice_gender,
            }
            continue

        gap = max(0.0, start - float(current["end"]))
        proposed_duration = end - float(current["start"])
        proposed_chars = len(" ".join(clean_text(item.get("vi", "")) for item in current["segments"])) + 1 + len(text)
        same_utterance = bool(utterance) and utterance == current.get("utteranceId")
        speaker_compatible = not speaker or not current.get("speaker") or speaker == current.get("speaker")
        rate_compatible = policy == current.get("speechRate")
        voice_compatible = (not voice or not current.get("voice") or voice == current.get("voice")) and (
            not voice_gender or not current.get("voiceGender") or voice_gender == current.get("voiceGender")
        )

        if same_utterance and speaker_compatible and rate_compatible and voice_compatible and gap <= max_gap and proposed_duration <= max_duration and proposed_chars <= max_chars:
            current["segments"].append(segment)
            current["end"] = end
            if speaker and not current.get("speaker"):
                current["speaker"] = speaker
            if voice and not current.get("voice"):
                current["voice"] = voice
            if voice_gender and not current.get("voiceGender"):
                current["voiceGender"] = voice_gender
        else:
            flush()
            current = {
                "segments": [segment],
                "start": start,
                "end": end,
                "utteranceId": utterance,
                "speaker": speaker,
                "speechRate": policy,
                "voice": voice,
                "voiceGender": voice_gender,
            }
    flush()
    return units


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
    tolerance = env_int("TTS_FIT_TOLERANCE_MS", 100, 0)
    allow_overrun = env_int("TTS_ALLOW_OVERRUN_MS", 120, 0)
    if len(clip) <= target_ms + tolerance:
        return clip
    ratio = len(clip) / max(150, target_ms)
    max_speed = env_float("TTS_EDITOR_POST_MAX_SPEED", 1.25, 1.0)
    playback_speed = min(max_speed, max(1.01, ratio))
    try:
        clip = speedup(clip, playback_speed=playback_speed, chunk_size=60, crossfade=8)
    except Exception:
        pass
    max_len = target_ms + allow_overrun
    if len(clip) > max_len:
        clip = clip[:max_len]
        if len(clip) > 50:
            clip = clip.fade_out(min(60, len(clip) // 4))
    return clip


async def synthesize_segments(segments: list[dict], workdir: Path, total_ms: int) -> Path:
    concurrency = env_int("TTS_EDITOR_CONCURRENCY", 3, 1)
    gain_db = env_float("TTS_GAIN_DB", 0.0, -30.0)
    semaphore = asyncio.Semaphore(concurrency)
    units = build_tts_units(segments)

    async def render_one(index: int, unit: dict):
        async with semaphore:
            text = clean_text(unit.get("text", ""))
            start_ms = max(0, int(float(unit.get("start", 0.0)) * 1000))
            end_ms = max(start_ms + 150, int(float(unit.get("end", 0.0)) * 1000))
            slot_ms = end_ms - start_ms
            policy = str(unit.get("speechRate", "auto") or "auto")
            rate_percent = auto_rate_percent_for_slot(text, float(unit.get("start", 0.0)), float(unit.get("end", 0.0)), policy)
            voice = _voice_for_unit(unit)
            output = workdir / f"tts_edit_{index:05}.mp3"
            label = f"TTS utterance {index + 1}"
            await synthesize_file(text, output, voice, edge_rate(rate_percent), label)
            clip = AudioSegment.from_file(output).set_frame_rate(48000).set_channels(1)

            # The text-density estimate is only a guess. In auto mode, measure the
            # real Edge TTS duration and make one refined synthesis request before
            # touching the waveform.
            if policy.strip().lower() in {"", "auto"} and len(clip) > slot_ms + 100:
                max_rate = env_int("TTS_EDITOR_MAX_RATE_PERCENT", 70, 0)
                ratio = len(clip) / max(150, slot_ms)
                refined = min(max_rate, rate_percent + max(5, int(math.ceil((ratio - 1.0) * 100.0)) + 4))
                if refined >= rate_percent + 4:
                    await synthesize_file(text, output, voice, edge_rate(refined), label + " refined")
                    clip = AudioSegment.from_file(output).set_frame_rate(48000).set_channels(1)
                    rate_percent = refined

            clip = fit_clip(clip, slot_ms)
            if gain_db:
                clip += gain_db
            return index, start_ms, clip, rate_percent, voice

    rendered = await asyncio.gather(*(render_one(i, unit) for i, unit in enumerate(units)))
    track = AudioSegment.silent(duration=total_ms + 1000, frame_rate=48000).set_channels(1)
    for _, start_ms, clip, _, _ in rendered:
        track = track.overlay(clip, position=start_ms)

    for rendered_index, _, _, rate_percent, voice in rendered:
        unit = units[rendered_index]
        for segment in unit.get("segments") or []:
            segment["appliedSpeechRate"] = edge_rate(rate_percent)
            segment["appliedVoice"] = voice
            if not segment.get("speechRate"):
                segment["speechRate"] = "auto"
            if len(unit.get("segments") or []) > 1:
                segment["ttsGrouped"] = True

    output = workdir / "voice_vi.wav"
    track[:total_ms].export(output, format="wav")
    return output
