#!/usr/bin/env python3
"""Local OCR -> Vietnamese subtitle -> background music pipeline.

This mode is intentionally independent from Whisper/TTS. It samples a small number
of video frames, OCRs the likely source-caption area with RapidOCR/PP-OCRv6, merges
repeated observations into timed segments, translates them using VideoGet's existing
translation provider, covers the detected source-caption footprint, burns Vietnamese
subtitles, then replaces/ducks the original audio with a local music track.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

import cv2
import numpy as np
from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR

import localize as base
import localize_fast as fast  # noqa: F401 - installs optimized translation overrides on base
import render_subtitles
import smart_render as smart


AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except ValueError:
        return max(minimum, default)


def env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return max(minimum, default)


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def parse_region(raw: str) -> tuple[float, float, float, float] | None:
    raw = raw.strip()
    if not raw or raw.lower() == "auto":
        return None
    if raw.lower() == "full":
        return 0.0, 0.0, 1.0, 1.0
    try:
        x, y, w, h = [float(item.strip()) for item in raw.split(",")]
    except (ValueError, TypeError):
        raise RuntimeError("OCR_SUBTITLE_REGION must be auto, full, or normalized x,y,w,h")
    x = clamp(x, 0.0, 0.99)
    y = clamp(y, 0.0, 0.99)
    w = clamp(w, 0.01, 1.0 - x)
    h = clamp(h, 0.01, 1.0 - y)
    return x, y, w, h


def expand_region(region: tuple[float, float, float, float], px: float, py: float) -> tuple[float, float, float, float]:
    x, y, w, h = region
    nx = clamp(x - px, 0.0, 0.99)
    ny = clamp(y - py, 0.0, 0.99)
    right = clamp(x + w + px, nx + 0.01, 1.0)
    bottom = clamp(y + h + py, ny + 0.01, 1.0)
    return nx, ny, right - nx, bottom - ny


def detect_ocr_region(input_path: Path, output_dir: Path, width: int, height: int) -> tuple[float, float, float, float]:
    explicit = parse_region(os.getenv("OCR_SUBTITLE_REGION", "auto"))
    if explicit is not None:
        return explicit

    # Reuse VideoGet's existing visual analyzer when it is confident enough.
    try:
        probe_output = output_dir / f"{input_path.stem}.ocr-layout-probe.mp4"
        layout = smart.analyze_or_load(input_path, probe_output, width, height, "smart")
        source = layout.get("sourceSubtitle") or {}
        confidence = float(source.get("confidence", 0.0))
        if confidence >= env_float("OCR_LAYOUT_MIN_CONFIDENCE", 0.58):
            region = (
                float(source.get("x", 0.08)),
                float(source.get("y", 0.60)),
                float(source.get("w", 0.84)),
                float(source.get("h", 0.16)),
            )
            return expand_region(region, 0.035, 0.03)
    except Exception as error:
        base.log(f"OCR layout analyzer unavailable; using lower-frame fallback: {error}")

    # Conservative fallback: most short-video burned captions live in the lower half.
    return 0.04, 0.48, 0.92, 0.46


def make_ocr_engine() -> RapidOCR:
    raw_size = os.getenv("OCR_MODEL_SIZE", "small").strip().lower()
    model_type = {
        "tiny": ModelType.TINY,
        "small": ModelType.SMALL,
        "medium": ModelType.MEDIUM,
    }.get(raw_size, ModelType.SMALL)
    params = {
        "Det.engine_type": EngineType.ONNXRUNTIME,
        "Det.model_type": model_type,
        "Det.ocr_version": OCRVersion.PPOCRV6,
        "Rec.engine_type": EngineType.ONNXRUNTIME,
        "Rec.model_type": model_type,
        "Rec.ocr_version": OCRVersion.PPOCRV6,
    }
    try:
        return RapidOCR(params=params)
    except Exception as error:
        # Keep the feature usable across RapidOCR minor versions. The package's
        # default model is still a Chinese-capable compact OCR pipeline.
        base.log(f"RapidOCR PP-OCRv6 explicit config failed; using package defaults: {error}")
        return RapidOCR()


def normalize_text(value: str) -> str:
    value = re.sub(r"\s+", "", value or "")
    value = re.sub(r"[\u200b-\u200f\ufeff]", "", value)
    return value.strip()


def similarity(left: str, right: str) -> float:
    a = re.sub(r"[^\w\u3400-\u9fff]", "", normalize_text(left), flags=re.UNICODE)
    b = re.sub(r"[^\w\u3400-\u9fff]", "", normalize_text(right), flags=re.UNICODE)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def result_items(result) -> list[tuple[str, float, np.ndarray | None]]:
    if result is None:
        return []
    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    boxes = getattr(result, "boxes", None)

    # Compatibility with older RapidOCR tuple/list return values.
    if texts is None and isinstance(result, (tuple, list)) and result:
        legacy = result[0]
        if isinstance(legacy, list):
            items = []
            for row in legacy:
                if not isinstance(row, (tuple, list)) or len(row) < 2:
                    continue
                box = np.asarray(row[0], dtype=np.float32) if row[0] is not None else None
                payload = row[1]
                if isinstance(payload, (tuple, list)) and payload:
                    text = str(payload[0])
                    score = float(payload[1]) if len(payload) > 1 else 1.0
                else:
                    text, score = str(payload), 1.0
                items.append((text, score, box))
            return items
        return []

    if texts is None:
        return []
    texts = list(texts)
    scores = list(scores) if scores is not None else [1.0] * len(texts)
    box_list = list(boxes) if boxes is not None else [None] * len(texts)
    out = []
    for index, text in enumerate(texts):
        score = float(scores[index]) if index < len(scores) else 1.0
        box = np.asarray(box_list[index], dtype=np.float32) if index < len(box_list) and box_list[index] is not None else None
        out.append((str(text), score, box))
    return out


def ocr_frame(
    engine: RapidOCR,
    frame: np.ndarray,
    region: tuple[float, float, float, float],
    min_confidence: float,
) -> tuple[str, float, list[tuple[float, float, float, float]]]:
    height, width = frame.shape[:2]
    rx, ry, rw, rh = region
    x0, y0 = max(0, int(rx * width)), max(0, int(ry * height))
    x1, y1 = min(width, int((rx + rw) * width)), min(height, int((ry + rh) * height))
    crop = frame[y0:y1, x0:x1]
    if crop.size == 0:
        return "", 0.0, []

    result = engine(crop)
    accepted = []
    for text, score, box in result_items(result):
        text = normalize_text(text)
        if not text or score < min_confidence:
            continue
        if len(text) < env_int("OCR_MIN_TEXT_CHARS", 2, 1):
            continue
        top = float(np.min(box[:, 1])) if box is not None and box.size else 0.0
        left = float(np.min(box[:, 0])) if box is not None and box.size else 0.0
        accepted.append((top, left, text, score, box))
    if not accepted:
        return "", 0.0, []

    accepted.sort(key=lambda item: (round(item[0] / max(1.0, crop.shape[0] * 0.035)), item[1]))
    text = " ".join(item[2] for item in accepted).strip()
    confidence = float(np.mean([item[3] for item in accepted]))
    boxes_norm: list[tuple[float, float, float, float]] = []
    for _, _, _, _, box in accepted:
        if box is None or not box.size:
            continue
        bx0 = x0 + float(np.min(box[:, 0]))
        by0 = y0 + float(np.min(box[:, 1]))
        bx1 = x0 + float(np.max(box[:, 0]))
        by1 = y0 + float(np.max(box[:, 1]))
        boxes_norm.append((bx0 / width, by0 / height, max(0.001, (bx1 - bx0) / width), max(0.001, (by1 - by0) / height)))
    return text, confidence, boxes_norm


def finalize_segment(segment: dict, output: list[dict], persistent_limit: float) -> None:
    duration = float(segment["end"]) - float(segment["start"])
    if duration < env_float("OCR_MIN_DURATION_SEC", 0.35, 0.05):
        return
    # Very long unchanging text is usually a watermark/product label rather than captions.
    if persistent_limit > 0 and duration >= persistent_limit and int(segment.get("observations", 1)) >= 4:
        base.log(f"OCR bỏ text tĩnh {duration:.1f}s: {segment['text'][:60]}")
        return
    segment["id"] = len(output)
    output.append(segment)


def extract_segments(input_path: Path, output_dir: Path) -> tuple[list[dict], list[tuple[float, float, float, float]], dict]:
    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video for OCR: {input_path}")
    source_fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    duration = total_frames / source_fps if total_frames > 0 and source_fps > 0 else 0.0
    if width <= 0 or height <= 0 or duration <= 0:
        cap.release()
        raise RuntimeError("video metadata is unavailable for OCR")

    region = detect_ocr_region(input_path, output_dir, width, height)
    ocr_fps = env_float("OCR_FPS", 3.0, 0.25)
    interval = 1.0 / ocr_fps
    max_samples = env_int("OCR_MAX_SAMPLES", 900, 10)
    sample_count = min(max_samples, max(1, int(duration / interval) + 1))
    min_confidence = env_float("OCR_MIN_CONFIDENCE", 0.65, 0.0)
    similarity_threshold = env_float("OCR_TEXT_SIMILARITY", 0.78, 0.0)
    max_gap = env_float("OCR_MAX_GAP_SEC", max(0.75, interval * 2.6), 0.05)
    persistent_limit = env_float("OCR_IGNORE_PERSISTENT_SEC", 12.0, 0.0)
    engine = make_ocr_engine()

    segments: list[dict] = []
    observed_boxes: list[tuple[float, float, float, float]] = []
    current: dict | None = None
    base.log(
        f"OCR local: {ocr_fps:g} fps, PP-OCRv6/{os.getenv('OCR_MODEL_SIZE', 'small')}, "
        f"region={','.join(f'{value:.3f}' for value in region)}"
    )

    for index in range(sample_count):
        timestamp = min(duration, index * interval)
        cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        text, confidence, boxes = ocr_frame(engine, frame, region, min_confidence)
        observed_boxes.extend(boxes)
        if not text:
            if current is not None and timestamp - float(current["lastSeen"]) > max_gap:
                finalize_segment(current, segments, persistent_limit)
                current = None
            continue

        if current is not None and timestamp - float(current["lastSeen"]) <= max_gap and similarity(current["text"], text) >= similarity_threshold:
            current["end"] = min(duration, timestamp + interval)
            current["lastSeen"] = timestamp
            current["observations"] = int(current.get("observations", 1)) + 1
            if confidence > float(current.get("confidence", 0.0)):
                current["text"] = text
                current["confidence"] = round(confidence, 4)
        else:
            if current is not None:
                finalize_segment(current, segments, persistent_limit)
            current = {
                "id": 0,
                "start": timestamp,
                "end": min(duration, timestamp + interval),
                "lastSeen": timestamp,
                "text": text,
                "confidence": round(confidence, 4),
                "observations": 1,
            }
    cap.release()
    if current is not None:
        finalize_segment(current, segments, persistent_limit)

    for segment in segments:
        segment.pop("lastSeen", None)
        segment.pop("observations", None)
    info = {
        "width": width,
        "height": height,
        "duration": duration,
        "sourceFps": source_fps,
        "ocrFps": ocr_fps,
        "sampleCount": sample_count,
        "region": region,
    }
    return segments, observed_boxes, info


def aggregate_text_region(
    boxes: list[tuple[float, float, float, float]],
    fallback: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    if not boxes:
        return fallback
    lefts = np.array([item[0] for item in boxes], dtype=np.float32)
    tops = np.array([item[1] for item in boxes], dtype=np.float32)
    rights = np.array([item[0] + item[2] for item in boxes], dtype=np.float32)
    bottoms = np.array([item[1] + item[3] for item in boxes], dtype=np.float32)
    # Quantiles reject occasional OCR hits on UI elements inside the broad search ROI.
    left = float(np.quantile(lefts, 0.05))
    top = float(np.quantile(tops, 0.05))
    right = float(np.quantile(rights, 0.95))
    bottom = float(np.quantile(bottoms, 0.95))
    return expand_region((left, top, max(0.03, right - left), max(0.025, bottom - top)), 0.025, 0.018)


def choose_music(input_path: Path) -> Path | None:
    explicit = os.getenv("OCR_MUSIC_FILE", "").strip()
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError(f"OCR_MUSIC_FILE does not exist: {path}")
        return path

    directory = Path(os.getenv("OCR_MUSIC_DIR", "/app/assets/music")).expanduser()
    if not directory.exists():
        return None
    candidates = sorted(path for path in directory.rglob("*") if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS)
    if not candidates:
        return None
    digest = hashlib.sha1(input_path.name.encode("utf-8")).digest()
    index = int.from_bytes(digest[:4], "big") % len(candidates)
    return candidates[index]


def music_filter(duration: float, label: str = "1:a") -> str:
    volume = env_float("OCR_MUSIC_VOLUME", 0.18, 0.0)
    fade_in = min(duration / 3.0, env_float("OCR_MUSIC_FADE_IN_SEC", 0.8, 0.0))
    fade_out = min(duration / 3.0, env_float("OCR_MUSIC_FADE_OUT_SEC", 1.5, 0.0))
    filters = [f"[{label}]atrim=0:{duration:.3f}", "asetpts=N/SR/TB", f"volume={volume:.4f}"]
    if fade_in > 0:
        filters.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        filters.append(f"afade=t=out:st={max(0.0, duration - fade_out):.3f}:d={fade_out:.3f}")
    return ",".join(filters)


def mix_music(input_video: Path, music: Path | None, output_video: Path, duration: float) -> None:
    if music is None:
        if env_bool("OCR_MUSIC_REQUIRED", True):
            raise RuntimeError(
                "OCR+Music completed OCR/subtitles but no music file was found. "
                "Put royalty-free audio in OCR_MUSIC_DIR or set OCR_MUSIC_FILE."
            )
        shutil.copy2(input_video, output_video)
        return

    audio_mode = os.getenv("OCR_ORIGINAL_AUDIO_MODE", "mute").strip().lower()
    if audio_mode not in {"mute", "duck", "mix"}:
        audio_mode = "mute"
    has_original = base.has_audio_stream(input_video)
    cmd = ["ffmpeg", "-y", "-i", str(input_video), "-stream_loop", "-1", "-i", str(music)]
    bg = music_filter(duration)
    if audio_mode == "mute" or not has_original:
        cmd += ["-filter_complex", f"{bg}[music]", "-map", "0:v:0", "-map", "[music]"]
    else:
        original_volume = env_float("OCR_ORIGINAL_AUDIO_VOLUME", 0.08 if audio_mode == "duck" else 0.35, 0.0)
        cmd += [
            "-filter_complex",
            f"[0:a:0]volume={original_volume:.4f}[original];{bg}[music];"
            "[original][music]amix=inputs=2:duration=first:dropout_transition=2[aout]",
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
        ]
    cmd += [
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        os.getenv("OCR_MUSIC_AUDIO_BITRATE", "192k"),
        "-movflags",
        "+faststart",
        "-shortest",
        str(output_video),
    ]
    base.run(cmd)


def render_ocr_subtitles(
    input_path: Path,
    subtitle_path: Path,
    output_path: Path,
    text_region: tuple[float, float, float, float],
) -> None:
    previous = {
        "VIDEO_CLEANUP_MODE": os.environ.get("VIDEO_CLEANUP_MODE"),
        "VIDEO_SOURCE_SUBTITLE_REGION": os.environ.get("VIDEO_SOURCE_SUBTITLE_REGION"),
        "VIDEO_CLEANUP_LOGOS": os.environ.get("VIDEO_CLEANUP_LOGOS"),
    }
    try:
        if env_bool("OCR_REMOVE_SOURCE_TEXT", True):
            # Legacy mode skips expensive visual analysis; the OCR-derived footprint is
            # supplied explicitly and is blurred only during actual subtitle intervals.
            os.environ["VIDEO_CLEANUP_MODE"] = "legacy"
            os.environ["VIDEO_SOURCE_SUBTITLE_REGION"] = ",".join(f"{value:.5f}" for value in text_region)
            os.environ["VIDEO_CLEANUP_LOGOS"] = "false"
        render_subtitles.render(input_path, subtitle_path, output_path)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> None:
    parser = argparse.ArgumentParser(description="OCR Chinese captions, translate to Vietnamese and add background music")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    stem = input_path.stem
    original_srt = output_dir / f"{stem}.ocr.original.srt"
    vi_srt = output_dir / f"{stem}.ocr.vi.srt"
    subbed_video = output_dir / f"{stem}.ocr-subbed.mp4"
    output_video = output_dir / f"{stem}.ocr-music.mp4"
    metadata_path = output_dir / f"{stem}.ocr-music.json"
    timings: dict[str, float] = {"transcribe": 0.0, "tts": 0.0}
    started = time.perf_counter()

    ocr_started = time.perf_counter()
    segments, boxes, video_info = extract_segments(input_path, output_dir)
    timings["ocr"] = round(time.perf_counter() - ocr_started, 3)
    if not segments:
        raise RuntimeError(
            "OCR did not detect timed subtitle text. Try lowering OCR_MIN_CONFIDENCE, "
            "increasing OCR_FPS, or set OCR_SUBTITLE_REGION=x,y,w,h."
        )
    base.write_srt(original_srt, segments, "text")

    translate_started = time.perf_counter()
    if env_bool("OCR_TRANSLATE", True):
        base.translate_segments(segments, os.getenv("OCR_SOURCE_LANGUAGE", "zh"))
    else:
        for segment in segments:
            segment["vi"] = segment["text"]
    timings["translate"] = round(time.perf_counter() - translate_started, 3)
    base.write_srt(vi_srt, segments, "vi")

    fallback_region = tuple(float(value) for value in video_info["region"])
    text_region = aggregate_text_region(boxes, fallback_region)
    render_started = time.perf_counter()
    render_ocr_subtitles(input_path, vi_srt, subbed_video, text_region)
    timings["render"] = round(time.perf_counter() - render_started, 3)

    music = choose_music(input_path)
    music_started = time.perf_counter()
    mix_music(subbed_video, music, output_video, float(video_info["duration"]))
    timings["music"] = round(time.perf_counter() - music_started, 3)
    timings["total"] = round(time.perf_counter() - started, 3)

    metadata = {
        "input": str(input_path),
        "mode": "ocr_music",
        "detectedLanguage": os.getenv("OCR_SOURCE_LANGUAGE", "zh"),
        "segments": segments,
        "originalSubtitle": str(original_srt),
        "vietnameseSubtitle": str(vi_srt),
        "musicTrack": str(music) if music else "",
        "outputVideo": str(output_video),
        "ocr": {
            **video_info,
            "modelSize": os.getenv("OCR_MODEL_SIZE", "small"),
            "minConfidence": env_float("OCR_MIN_CONFIDENCE", 0.65),
            "textRegion": text_region,
        },
        "timings": timings,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "originalSubtitle": str(original_srt),
        "vietnameseSubtitle": str(vi_srt),
        "outputVideo": str(output_video),
        "detectedLanguage": os.getenv("OCR_SOURCE_LANGUAGE", "zh"),
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
