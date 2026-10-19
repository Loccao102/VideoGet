#!/usr/bin/env python3
"""Transcribe + translate + burn Vietnamese subtitles without generating TTS."""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import tempfile
import time
from pathlib import Path

from faster_whisper import WhisperModel

import localize as base
import localize_fast as fast  # installs faster translation overrides on base
import render_subtitles



def input_has_stream(input_path: Path, stream_kind: str) -> bool:
    """Probe media metadata; a video must actually contain video to be reusable."""
    if stream_kind not in ("a", "v"):
        raise ValueError("ffprobe stream kind must be audio or video")
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", stream_kind,
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            str(input_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(result.stdout.strip())


def input_has_audio_stream(input_path: Path) -> bool:
    return input_has_stream(input_path, "a")


def input_has_video_stream(input_path: Path) -> bool:
    return input_has_stream(input_path, "v")


def input_has_decodable_video_frame(input_path: Path) -> bool:
    """Require one actually decodable frame, not merely a video stream header.

    This runs only when audio extraction failed. A single frame avoids
    decoding the whole source and prevents a broken MP4 with valid metadata
    from masquerading as a successful no-speech video.
    """
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-i",
                str(input_path), "-map", "0:v:0", "-frames:v", "1",
                "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            ],
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False
    return bool(result.stdout)


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def collect_segments(raw_segments) -> list[dict]:
    segments: list[dict] = []
    for segment in raw_segments:
        text = segment.text.strip()
        if not text:
            continue
        start, end = float(segment.start), float(segment.end)
        # Speech with bad timestamps must not be mistaken for silent audio.
        # That would silently return an unsubtitled original video.
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end):
            raise ValueError(f"Whisper returned invalid subtitle timestamps: {start}, {end}")
        segments.append({
            "id": len(segments),
            "start": start,
            "end": end,
            "text": text,
        })
    return segments


def emit_no_speech_result(
    input_path: Path,
    metadata_path: Path,
    timings: dict[str, float],
    started: float,
    detected_language: str = "",
    derived_artifacts: tuple[Path, ...] = (),
) -> None:
    # Previous attempts may have left subtitles or rendered media using the
    # same input stem. Never expose obsolete outputs for a no-speech result.
    for artifact in derived_artifacts:
        if artifact.resolve() == input_path.resolve():
            raise ValueError("Refusing to remove the original input video")
        artifact.unlink(missing_ok=True)
    timings.setdefault("translate", 0.0)
    timings.setdefault("render", 0.0)
    timings["tts"] = 0.0
    timings["total"] = round(time.perf_counter() - started, 3)
    metadata = {
        "input": str(input_path),
        "mode": "subtitles",
        "detectedLanguage": detected_language,
        "segments": [],
        "outputVideo": str(input_path),
        "skippedReason": "no_speech",
        "timings": timings,
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    base.log("Không phát hiện lời thoại sau retry; giữ nguyên video gốc và bỏ qua tạo sub.")
    print(json.dumps({
        "outputVideo": str(input_path),
        "detectedLanguage": detected_language,
        "segments": 0,
        "skippedReason": "no_speech",
        "timings": timings,
        "worker": False,
    }, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="Create Vietnamese subtitles without TTS")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    stem = input_path.stem
    original_srt = output_dir / f"{stem}.original.srt"
    vi_srt = output_dir / f"{stem}.vi.srt"
    output_video = output_dir / f"{stem}.vi-subbed.mp4"
    metadata_path = output_dir / f"{stem}.subtitle.json"
    # A previous successful run may have left a valid-looking result. If the
    # current attempt fails during FFmpeg/Whisper/render, that old metadata
    # must not be mistaken for the outcome of this attempt.
    metadata_path.unlink(missing_ok=True)
    timings: dict[str, float] = {}
    started = time.perf_counter()

    with tempfile.TemporaryDirectory(prefix="videoget-subtitles-") as temp:
        workdir = Path(temp)
        audio_path = workdir / "source.wav"
        base.log("Đang tách audio cho subtitle-only...")
        try:
            base.run([
                "ffmpeg",
                "-y",
                "-i",
                str(input_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(audio_path),
            ])
        except Exception:
            # Valid videos can have no audio track at all. Only treat a
            # confirmed absent audio stream as no_speech; preserve real
            # FFmpeg errors for corrupt files or failed decoding.
            if (
                not input_has_audio_stream(input_path)
                and input_has_video_stream(input_path)
                and input_has_decodable_video_frame(input_path)
            ):
                emit_no_speech_result(
                    input_path, metadata_path, timings, started,
                    derived_artifacts=(original_srt, vi_srt, output_video),
                )
                return
            raise

        transcribe_started = time.perf_counter()
        model_name = os.getenv("WHISPER_MODEL", "small")
        device = os.getenv("WHISPER_DEVICE", "cpu")
        compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "int8" if device == "cpu" else "float16")
        model = WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
            cpu_threads=int(os.getenv("WHISPER_CPU_THREADS", "8")),
            num_workers=int(os.getenv("WHISPER_NUM_WORKERS", "1")),
        )
        language = os.getenv("WHISPER_LANGUAGE", "").strip() or None
        condition_previous = env_bool("WHISPER_CONDITION_PREVIOUS_TEXT", True)

        raw_segments, info = model.transcribe(
            str(audio_path),
            beam_size=int(os.getenv("WHISPER_BEAM_SIZE", "1")),
            vad_filter=True,
            condition_on_previous_text=condition_previous,
            language=language,
        )
        segments = collect_segments(raw_segments)
        detected_language = getattr(info, "language", "") or ""

        # Some short/quiet clips are filtered too aggressively by the default
        # Silero VAD settings. Retry once with a lower VAD threshold before we
        # decide that the video genuinely contains no speech.
        if not segments and env_bool("WHISPER_NO_SPEECH_RETRY", True):
            base.log("Whisper chưa thấy lời thoại; retry với VAD nhạy hơn...")
            retry_segments, retry_info = model.transcribe(
                str(audio_path),
                beam_size=max(1, int(os.getenv("WHISPER_RETRY_BEAM_SIZE", "2"))),
                vad_filter=True,
                vad_parameters={
                    "threshold": float(os.getenv("WHISPER_RETRY_VAD_THRESHOLD", "0.25")),
                    "min_speech_duration_ms": int(os.getenv("WHISPER_RETRY_MIN_SPEECH_MS", "120")),
                    "min_silence_duration_ms": int(os.getenv("WHISPER_RETRY_MIN_SILENCE_MS", "350")),
                },
                condition_on_previous_text=False,
                language=language,
            )
            segments = collect_segments(retry_segments)
            retry_language = getattr(retry_info, "language", "") or ""
            if retry_language:
                detected_language = retry_language

        timings["transcribe"] = round(time.perf_counter() - transcribe_started, 3)
        if not segments:
            emit_no_speech_result(
                input_path,
                metadata_path,
                timings,
                started,
                detected_language,
                derived_artifacts=(original_srt, vi_srt, output_video),
            )
            return

        base.write_srt(original_srt, segments, "text")

        translate_started = time.perf_counter()
        base.translate_segments(segments, detected_language)
        timings["translate"] = round(time.perf_counter() - translate_started, 3)
        base.write_srt(vi_srt, segments, "vi")

        render_started = time.perf_counter()
        render_subtitles.render(input_path, vi_srt, output_video)
        timings["render"] = round(time.perf_counter() - render_started, 3)

    timings["total"] = round(time.perf_counter() - started, 3)
    timings["tts"] = 0.0
    metadata = {
        "input": str(input_path),
        "mode": "subtitles",
        "detectedLanguage": detected_language,
        "segments": segments,
        "originalSubtitle": str(original_srt),
        "vietnameseSubtitle": str(vi_srt),
        "outputVideo": str(output_video),
        "timings": timings,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "originalSubtitle": str(original_srt),
        "vietnameseSubtitle": str(vi_srt),
        "outputVideo": str(output_video),
        "detectedLanguage": detected_language,
        "segments": len(segments),
        "timings": timings,
        "worker": False,
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        base.log(f"ERROR: {error}")
        raise SystemExit(1)
