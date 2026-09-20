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
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np
from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR

import localize as base
import localize_fast as fast  # noqa: F401 - installs optimized translation overrides on base
import ocr_quality
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


def make_ocr_engine(model_size: str | None = None) -> RapidOCR:
    raw_size = (model_size or os.getenv("OCR_MODEL_SIZE", "small")).strip().lower()
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
    return ocr_quality.normalize_text(value)


def similarity(left: str, right: str) -> float:
    return ocr_quality.similarity(left, right)


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


def _select_caption_items(
    accepted: list[tuple[float, float, str, float, np.ndarray | None]],
    crop_height: int,
    crop_width: int,
) -> list[tuple[float, float, str, float, np.ndarray | None]]:
    """Prefer one coherent caption line/stack over unrelated scene text in the ROI."""
    if len(accepted) <= 1:
        return accepted

    line_tolerance = max(8.0, crop_height * env_float("OCR_LINE_Y_TOLERANCE", 0.045, 0.01))
    lines: list[list[tuple[float, float, str, float, np.ndarray | None]]] = []
    for item in sorted(accepted, key=lambda value: (value[0], value[1])):
        box = item[4]
        cy = item[0]
        if box is not None and box.size:
            cy = float(np.mean(box[:, 1]))
        target = None
        for line in lines:
            centers = []
            for existing in line:
                existing_box = existing[4]
                centers.append(
                    float(np.mean(existing_box[:, 1]))
                    if existing_box is not None and existing_box.size
                    else existing[0]
                )
            if abs(cy - float(np.mean(centers))) <= line_tolerance:
                target = line
                break
        if target is None:
            lines.append([item])
        else:
            target.append(item)

    def metrics(items):
        boxes = [item[4] for item in items if item[4] is not None and item[4].size]
        if boxes:
            left = min(float(np.min(box[:, 0])) for box in boxes)
            right = max(float(np.max(box[:, 0])) for box in boxes)
            top = min(float(np.min(box[:, 1])) for box in boxes)
            bottom = max(float(np.max(box[:, 1])) for box in boxes)
        else:
            left = min(item[1] for item in items)
            right = left
            top = min(item[0] for item in items)
            bottom = top
        text = "".join(item[2] for item in items)
        confidence = float(np.mean([item[3] for item in items]))
        coverage = max(0.0, right - left) / max(1.0, float(crop_width))
        center_x = ((left + right) / 2.0) / max(1.0, float(crop_width))
        center_score = max(0.0, 1.0 - abs(center_x - 0.5) * 2.0)
        center_y = ((top + bottom) / 2.0) / max(1.0, float(crop_height))
        length_score = min(1.0, len(normalize_text(text)) / 18.0)
        return confidence, coverage, center_score, center_y, length_score

    candidates: list[list[tuple[float, float, str, float, np.ndarray | None]]] = []
    for index, line in enumerate(lines):
        candidates.append(line)
        if index + 1 < len(lines):
            first = metrics(line)
            second = metrics(lines[index + 1])
            # Two-line captions are common. Only combine adjacent rows when both
            # look horizontally caption-like rather than arbitrary scene labels.
            if abs(first[3] - second[3]) <= env_float("OCR_TWO_LINE_MAX_GAP", 0.15, 0.02):
                candidates.append(line + lines[index + 1])

    def rank(items) -> float:
        confidence, coverage, center_score, center_y, length_score = metrics(items)
        return (
            confidence * 1.55
            + min(1.0, coverage / 0.55) * 0.75
            + center_score * 0.35
            + length_score * 0.55
            + center_y * 0.15
        )

    return max(candidates, key=rank)


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

    accepted = _select_caption_items(accepted, crop.shape[0], crop.shape[1])
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

    observations = list(segment.pop("_observations", []) or [])
    consensus = ocr_quality.choose_consensus(
        observations,
        threshold=env_float("OCR_CONSENSUS_SIMILARITY", 0.72, 0.0),
    )
    if consensus is not None:
        segment["text"] = consensus["text"]
        segment["confidence"] = round(float(consensus["confidence"]), 4)
        segment["consensusScore"] = round(float(consensus["consensusScore"]), 4)
        segment["observationCount"] = int(consensus["observationCount"])
        segment["winnerCount"] = int(consensus["winnerCount"])
        representative_boxes = consensus.get("representativeBoxes") or []
        if representative_boxes:
            _apply_geometry(segment, representative_boxes)

        cleanup_regions = ocr_quality.merge_temporal_regions(
            consensus.get("boxes") or [],
            overlap_threshold=env_float("OCR_CLEANUP_TEMPORAL_OVERLAP", 0.28, 0.0),
            pad_x=env_float("OCR_CLEANUP_PAD_X", 0.010, 0.0),
            pad_y=env_float("OCR_CLEANUP_PAD_Y", 0.008, 0.0),
            max_regions=env_int("OCR_CLEANUP_MAX_REGIONS", 10, 1),
        )
        if cleanup_regions:
            segment["cleanupRegions"] = [
                [round(value, 6) for value in region] for region in cleanup_regions
            ]
    else:
        segment["consensusScore"] = 0.0
        segment["observationCount"] = int(segment.get("observations", 1))

    # Very long unchanging text is usually a watermark/product label rather than captions.
    if persistent_limit > 0 and duration >= persistent_limit and int(segment.get("observationCount", 1)) >= 4:
        base.log(f"OCR bỏ text tĩnh {duration:.1f}s: {segment['text'][:60]}")
        return
    segment["id"] = len(output)
    output.append(segment)


def _parse_rate(value: str | None) -> float:
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    if "/" in raw:
        left, right = raw.split("/", 1)
        try:
            denominator = float(right)
            return float(left) / denominator if denominator else 0.0
        except ValueError:
            return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _probe_video(input_path: Path) -> tuple[int, int, float, float]:
    process = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate:format=duration",
            "-of", "json", str(input_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "ffprobe failed for OCR")
    payload = json.loads(process.stdout or "{}")
    streams = payload.get("streams") or []
    if not streams:
        raise RuntimeError("video stream not found for OCR")
    stream = streams[0]
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    source_fps = _parse_rate(stream.get("avg_frame_rate")) or _parse_rate(stream.get("r_frame_rate")) or 25.0
    try:
        duration = float((payload.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if width <= 0 or height <= 0 or duration <= 0:
        raise RuntimeError("video metadata is unavailable for OCR")
    return width, height, source_fps, duration


def _read_exact(stream, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            break
        chunks.extend(chunk)
    return bytes(chunks)


def _expand_box(
    region: tuple[float, float, float, float],
    pad_x: float,
    pad_y: float,
) -> tuple[float, float, float, float]:
    x, y, w, h = region
    nx = clamp(x - pad_x, 0.0, 0.995)
    ny = clamp(y - pad_y, 0.0, 0.995)
    right = clamp(x + w + pad_x, nx + 0.005, 1.0)
    bottom = clamp(y + h + pad_y, ny + 0.005, 1.0)
    return nx, ny, right - nx, bottom - ny


def _segment_geometry(boxes: list[tuple[float, float, float, float]]) -> tuple[list[list[float]], list[float] | None, str]:
    if not boxes:
        return [], None, ""
    detail_pad_x = env_float("OCR_SEGMENT_DETAIL_PAD_X", 0.004, 0.0)
    detail_pad_y = env_float("OCR_SEGMENT_DETAIL_PAD_Y", 0.004, 0.0)
    max_boxes = env_int("OCR_SEGMENT_DETAIL_MAX_BOXES", 12, 1)
    details = [
        [round(value, 6) for value in _expand_box(box, detail_pad_x, detail_pad_y)]
        for box in boxes[:max_boxes]
    ]
    left = min(box[0] for box in boxes)
    top = min(box[1] for box in boxes)
    right = max(box[0] + box[2] for box in boxes)
    bottom = max(box[1] + box[3] for box in boxes)
    union = _expand_box(
        (left, top, max(0.001, right - left), max(0.001, bottom - top)),
        env_float("OCR_SEGMENT_BBOX_PAD_X", 0.012, 0.0),
        env_float("OCR_SEGMENT_BBOX_PAD_Y", 0.008, 0.0),
    )
    placement = "bottom" if union[1] + union[3] >= env_float("OCR_OVERLAY_BOTTOM_THRESHOLD", 0.68, 0.0) else "upper"
    return details, [round(value, 6) for value in union], placement


def _apply_geometry(segment: dict, boxes: list[tuple[float, float, float, float]]) -> None:
    details, bbox, placement = _segment_geometry(boxes)
    if bbox is None:
        return
    segment["bbox"] = bbox
    segment["bboxRegions"] = details
    segment["bboxConfidence"] = round(float(segment.get("confidence", 0.0)), 3)
    segment["placement"] = placement


def extract_segments(input_path: Path, output_dir: Path) -> tuple[list[dict], list[tuple[float, float, float, float]], dict]:
    """Decode sampled frames with ffmpeg directly; never create a full-video OCR proxy."""
    width, height, source_fps, duration = _probe_video(input_path)
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

    frame_bytes = width * height * 3
    cmd = [
        "ffmpeg", "-v", "error", "-hwaccel", "none", "-i", str(input_path),
        "-map", "0:v:0", "-an", "-sn", "-dn",
        "-vf", f"fps={ocr_fps:g}",
        "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
    ]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if process.stdout is None:
        raise RuntimeError("could not open ffmpeg OCR frame pipe")

    segments: list[dict] = []
    observed_boxes: list[tuple[float, float, float, float]] = []
    current: dict | None = None
    base.log(
        f"OCR stream: ffmpeg direct decode, {ocr_fps:g} fps, "
        f"PP-OCRv6/{os.getenv('OCR_MODEL_SIZE', 'small')}, "
        f"region={','.join(f'{value:.3f}' for value in region)}"
    )

    sampled = 0
    try:
        for index in range(sample_count):
            raw = _read_exact(process.stdout, frame_bytes)
            if not raw:
                break
            if len(raw) != frame_bytes:
                raise RuntimeError(f"short ffmpeg OCR frame: {len(raw)}/{frame_bytes} bytes")
            frame = np.frombuffer(raw, dtype=np.uint8).reshape((height, width, 3))
            timestamp = min(duration, index * interval)
            sampled += 1
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
                current.setdefault("_observations", []).append({
                    "text": text,
                    "confidence": float(confidence),
                    "boxes": list(boxes),
                })
                # Keep a recent representative for matching while final text is
                # decided by multi-frame consensus in finalize_segment().
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
                    "_observations": [{
                        "text": text,
                        "confidence": float(confidence),
                        "boxes": list(boxes),
                    }],
                }
                _apply_geometry(current, boxes)
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        try:
            process.stdout.close()
        except Exception:
            pass

    if current is not None:
        finalize_segment(current, segments, persistent_limit)

    if sampled == 0:
        stderr = ""
        if process.stderr is not None:
            try:
                stderr = process.stderr.read().decode("utf-8", errors="replace")
            except Exception:
                stderr = ""
        raise RuntimeError(f"ffmpeg produced no OCR frames: {stderr.strip()[:600]}")

    for segment in segments:
        segment.pop("lastSeen", None)
        segment.pop("observations", None)
    info = {
        "width": width,
        "height": height,
        "duration": duration,
        "sourceFps": source_fps,
        "ocrFps": ocr_fps,
        "sampleCount": sampled,
        "region": region,
        "decodeMode": "ffmpeg_pipe",
        "segmentBBoxCount": sum(1 for segment in segments if segment.get("bbox")),
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
    segments: list[dict] | None = None,
    aspect: str = "original",
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
        render_subtitles.render(input_path, subtitle_path, output_path, aspect=aspect)
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
    parser.add_argument("--aspect", default="original")
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
    source_language = os.getenv("OCR_SOURCE_LANGUAGE", "zh").strip() or "zh"
    if source_language.lower().startswith("vi"):
        for segment in segments:
            segment["vi"] = segment["text"]
    else:
        if not env_bool("OCR_TRANSLATE", True):
            base.log(
                "OCR_TRANSLATE=false ignored because OCR Vietnamese output requires "
                f"translation from {source_language}."
            )
        base.translate_segments(segments, source_language)
    timings["translate"] = round(time.perf_counter() - translate_started, 3)
    base.write_srt(vi_srt, segments, "vi")

    fallback_region = tuple(float(value) for value in video_info["region"])
    text_region = aggregate_text_region(boxes, fallback_region)
    render_started = time.perf_counter()
    render_ocr_subtitles(input_path, vi_srt, subbed_video, text_region, segments, args.aspect)
    timings["render"] = round(time.perf_counter() - render_started, 3)

    music = choose_music(input_path)
    music_started = time.perf_counter()
    mix_music(subbed_video, music, output_video, float(video_info["duration"]))
    timings["music"] = round(time.perf_counter() - music_started, 3)
    # subbed_video is only an intermediate container. Its video stream has already
    # been copied into output_video, so keeping both doubles disk usage for no benefit.
    if subbed_video != output_video and not env_bool("OCR_KEEP_SUBBED_INTERMEDIATE", False):
        try:
            subbed_video.unlink(missing_ok=True)
        except OSError as error:
            base.log(f"Could not remove OCR+Music intermediate: {error}")
    timings["total"] = round(time.perf_counter() - started, 3)

    metadata = {
        "input": str(input_path),
        "mode": "ocr_music",
        "outputAspect": args.aspect.strip().lower() or "original",
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
