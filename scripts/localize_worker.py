#!/usr/bin/env python3
"""Persistent localization worker for VideoGet.

Protocol: newline-delimited JSON on stdin/stdout. Human-readable logs always go to
stderr so the Go side can safely parse stdout.

The worker keeps faster-whisper loaded in memory and persists stage caches next to
the localized outputs. A retry can therefore skip transcription, translation and
TTS when the source/configuration for those stages has not changed.
"""

import asyncio
import json
import math
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from faster_whisper import WhisperModel

import localize as base
import localize_fast as fast


def env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return max(minimum, default)


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def source_signature(path: Path) -> dict:
    stat = path.stat()
    return {
        "size": stat.st_size,
        "mtimeNs": stat.st_mtime_ns,
        "whisperModel": os.getenv("WHISPER_MODEL", "base"),
        "whisperLanguage": os.getenv("WHISPER_LANGUAGE", "zh").strip(),
        "whisperBeamSize": env_int("WHISPER_BEAM_SIZE", 1, 1),
    }


def translation_signature(transcript_signature: dict) -> dict:
    provider = os.getenv("TRANSLATE_PROVIDER", "ollama").strip().lower()
    model = (
        os.getenv("OLLAMA_MODEL", "qwen3:8b")
        if provider == "ollama"
        else os.getenv("OPENAI_COMPAT_MODEL", "")
    )
    return {
        "version": 1,
        "transcript": transcript_signature,
        "provider": provider,
        "model": model,
    }


def tts_signature(translation_sig: dict) -> dict:
    return {
        "version": 1,
        "translation": translation_sig,
        "voice": os.getenv("TTS_VOICE", "vi-VN-HoaiMyNeural"),
        "rate": os.getenv("TTS_RATE", "+8%"),
        "maxSpeed": os.getenv("TTS_MAX_SPEED", "2.0"),
        "groupChars": os.getenv("TTS_GROUP_MAX_CHARS", "220"),
        "groupDuration": os.getenv("TTS_GROUP_MAX_DURATION_SEC", "12"),
        "groupGap": os.getenv("TTS_GROUP_MAX_GAP_SEC", "1.2"),
    }


def read_cache(path: Path, signature: dict) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("signature") == signature:
            return data
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return None


def write_cache(path: Path, signature: dict, **payload) -> None:
    data = {"signature": signature, **payload}
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def build_model() -> tuple[WhisperModel, dict]:
    model_name = os.getenv("WHISPER_MODEL", "base")
    device = os.getenv("WHISPER_DEVICE", "cpu")
    compute_type = os.getenv(
        "WHISPER_COMPUTE_TYPE", "int8" if device == "cpu" else "float16"
    )
    cpu_threads = env_int("WHISPER_CPU_THREADS", 8, 1)
    num_workers = env_int("WHISPER_NUM_WORKERS", 1, 1)

    base.log(
        f"Persistent Whisper: loading model={model_name}, device={device}, "
        f"compute={compute_type}, cpu_threads={cpu_threads}..."
    )
    started = time.perf_counter()
    model = WhisperModel(
        model_name,
        device=device,
        compute_type=compute_type,
        cpu_threads=cpu_threads,
        num_workers=num_workers,
    )
    elapsed = time.perf_counter() - started
    base.log(f"Persistent Whisper ready after {elapsed:.2f}s")
    return model, {
        "model": model_name,
        "device": device,
        "computeType": compute_type,
        "cpuThreads": cpu_threads,
        "directDecode": env_bool("WHISPER_DIRECT_DECODE", True),
        "loadSeconds": round(elapsed, 3),
    }


def decode_segments(model: WhisperModel, media_path: Path, language: str | None, beam_size: int):
    raw_segments, info = model.transcribe(
        str(media_path),
        language=language,
        beam_size=beam_size,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    segments = []
    for segment in raw_segments:
        text = segment.text.strip()
        if not text:
            continue
        segments.append({
            "id": len(segments),
            "start": float(segment.start),
            "end": float(segment.end),
            "text": text,
        })
    return segments, info


def transcribe(model: WhisperModel, input_path: Path, output_dir: Path, stem: str):
    signature = source_signature(input_path)
    cache_path = output_dir / f"{stem}.transcript.json"
    original_srt = output_dir / f"{stem}.original.srt"
    cached = read_cache(cache_path, signature)
    if cached and cached.get("segments"):
        segments = cached["segments"]
        language = str(cached.get("detectedLanguage", ""))
        if not original_srt.exists():
            base.write_srt(original_srt, segments, "text")
        base.log(f"Whisper cache hit: {len(segments)} segment")
        return segments, language, original_srt, True

    language = os.getenv("WHISPER_LANGUAGE", "zh").strip() or None
    beam_size = env_int("WHISPER_BEAM_SIZE", 1, 1)
    base.log(
        f"Đang transcribe bằng persistent faster-whisper "
        f"model={os.getenv('WHISPER_MODEL', 'base')}, language={language or 'auto'}..."
    )

    segments = []
    info = None
    direct_error = None
    if env_bool("WHISPER_DIRECT_DECODE", True):
        try:
            base.log("Whisper fast path: decode trực tiếp media, không tạo WAV tạm...")
            segments, info = decode_segments(model, input_path, language, beam_size)
        except Exception as error:
            direct_error = error
            base.log(f"Direct decode lỗi, fallback sang WAV 16 kHz: {error}")

    if info is None:
        with tempfile.TemporaryDirectory(prefix="videoget-whisper-") as temp:
            audio_path = Path(temp) / "source.wav"
            base.log("Đang tách audio WAV fallback...")
            base.run([
                "ffmpeg", "-y", "-i", str(input_path), "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", str(audio_path),
            ])
            try:
                segments, info = decode_segments(model, audio_path, language, beam_size)
            except Exception as error:
                if direct_error is not None:
                    raise RuntimeError(
                        f"Whisper direct decode failed ({direct_error}); WAV fallback failed ({error})"
                    ) from error
                raise

    if not segments:
        return [], language or "", original_srt, False

    detected_language = getattr(info, "language", "") or language or ""
    base.write_srt(original_srt, segments, "text")
    write_cache(
        cache_path,
        signature,
        detectedLanguage=detected_language,
        segments=segments,
    )
    base.log(f"Đã nhận diện {len(segments)} segment, language={detected_language or 'unknown'}")
    return segments, detected_language, original_srt, False


def translate(segments: list[dict], detected_language: str, output_dir: Path, stem: str, transcript_sig: dict):
    signature = translation_signature(transcript_sig)
    cache_path = output_dir / f"{stem}.translated.json"
    vi_srt = output_dir / f"{stem}.vi.srt"
    cached = read_cache(cache_path, signature)
    if cached and cached.get("segments"):
        translated = cached["segments"]
        if not vi_srt.exists():
            base.write_srt(vi_srt, translated, "vi")
        base.log(f"Translation cache hit: {len(translated)} segment")
        return translated, vi_srt, signature, True

    translated = [dict(item) for item in segments]
    fast.translate_segments(translated, detected_language)
    base.write_srt(vi_srt, translated, "vi")
    write_cache(cache_path, signature, segments=translated)
    return translated, vi_srt, signature, False


def synthesize(segments: list[dict], output_dir: Path, stem: str, input_path: Path, translation_sig: dict):
    signature = tts_signature(translation_sig)
    cache_path = output_dir / f"{stem}.tts.json"
    voice_track = output_dir / f"{stem}.vi-voice.wav"
    cached = read_cache(cache_path, signature)
    if cached and voice_track.exists() and voice_track.stat().st_size > 1024:
        base.log("TTS cache hit")
        return voice_track, signature, True

    total_ms = max(1000, int(math.ceil(base.ffprobe_duration(input_path) * 1000)))
    with tempfile.TemporaryDirectory(prefix="videoget-tts-") as temp:
        workdir = Path(temp)
        voice_temp = asyncio.run(fast.synthesize_segments(segments, workdir, total_ms))
        shutil.copy2(voice_temp, voice_track)
    write_cache(cache_path, signature, voiceTrack=str(voice_track))
    return voice_track, signature, False


def process_job(model: WhisperModel, input_value: str, output_value: str) -> dict:
    started_total = time.perf_counter()
    timings: dict[str, float] = {}
    cache_hits: list[str] = []

    input_path = Path(input_value).resolve()
    output_dir = Path(output_value).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not input_path.exists():
        raise FileNotFoundError(input_path)
    if not base.has_audio_stream(input_path):
        raise RuntimeError("input video has no audio stream")

    stem = input_path.stem
    transcript_sig = source_signature(input_path)

    stage = time.perf_counter()
    segments, detected_language, original_srt, hit = transcribe(model, input_path, output_dir, stem)
    timings["transcribe"] = round(time.perf_counter() - stage, 3)
    if hit:
        cache_hits.append("transcript")
    if not segments:
        return {
            "outputVideo": str(input_path),
            "detectedLanguage": detected_language,
            "segments": 0,
            "skippedReason": "no_speech",
            "timings": {**timings, "total": round(time.perf_counter() - started_total, 3)},
            "cacheHits": cache_hits,
            "worker": True,
        }

    stage = time.perf_counter()
    translated, vi_srt, translation_sig, hit = translate(
        segments, detected_language, output_dir, stem, transcript_sig
    )
    timings["translate"] = round(time.perf_counter() - stage, 3)
    if hit:
        cache_hits.append("translation")

    stage = time.perf_counter()
    voice_track, _, hit = synthesize(
        translated, output_dir, stem, input_path, translation_sig
    )
    timings["tts"] = round(time.perf_counter() - stage, 3)
    if hit:
        cache_hits.append("tts")

    output_video = output_dir / f"{stem}.vi-dubbed.mp4"
    stage = time.perf_counter()
    fast.render_video(input_path, voice_track, vi_srt, output_video)
    timings["render"] = round(time.perf_counter() - stage, 3)
    timings["total"] = round(time.perf_counter() - started_total, 3)

    metadata_path = output_dir / f"{stem}.localization.json"
    metadata = {
        "input": str(input_path),
        "detectedLanguage": detected_language,
        "segments": translated,
        "originalSubtitle": str(original_srt),
        "vietnameseSubtitle": str(vi_srt),
        "voiceTrack": str(voice_track),
        "outputVideo": str(output_video),
        "timings": timings,
        "cacheHits": cache_hits,
        "worker": True,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "originalSubtitle": str(original_srt),
        "vietnameseSubtitle": str(vi_srt),
        "voiceTrack": str(voice_track),
        "outputVideo": str(output_video),
        "detectedLanguage": detected_language,
        "segments": len(translated),
        "timings": timings,
        "cacheHits": cache_hits,
        "worker": True,
    }


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> None:
    model, model_info = build_model()
    emit({"type": "ready", **model_info})

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        request_id = ""
        try:
            request = json.loads(raw)
            request_id = str(request.get("id", ""))
            input_value = str(request.get("input", "")).strip()
            output_value = str(request.get("outputDir", "")).strip()
            if not input_value or not output_value:
                raise ValueError("input and outputDir are required")
            result = process_job(model, input_value, output_value)
            emit({"id": request_id, "ok": True, "result": result})
        except Exception as error:
            base.log(f"WORKER ERROR: {error}")
            emit({"id": request_id, "ok": False, "error": str(error)})


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        base.log(f"WORKER FATAL: {error}")
        sys.exit(1)
