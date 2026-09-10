#!/usr/bin/env python3
"""Fast localization pipeline for VideoGet.

Optimizations:
- disables Qwen3 thinking for subtitle translation;
- retries and recursively splits failed translation batches;
- groups subtitle segments before TTS and synthesizes groups concurrently;
- treats no-speech videos as a successful skip;
- renders one final Vietnamese video with source-caption/logo cleanup and subtle color grading.
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
        # Normalized x,y,w,h. Defaults cover common top-right/bottom-right platform watermark zones.
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

    # Cleanup/color/subtitle filters require re-encoding the video stream.
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
