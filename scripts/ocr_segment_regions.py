#!/usr/bin/env python3
"""Attach a stable normalized bbox to each OCR subtitle segment.

The first OCR pass already determines the timed text segments. To avoid running OCR
again over the whole video, this helper samples only a few representative frames
inside each segment, verifies that the detected text still resembles the segment's
source text, then stores one robust bbox for that segment.
"""
from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np

import localize as base
import localize_ocr_music as ocr


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return max(minimum, default)


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except ValueError:
        return max(minimum, default)


def _expand(region: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x, y, w, h = region
    pad_x = _env_float("OCR_SEGMENT_BBOX_PAD_X", 0.012)
    pad_y = _env_float("OCR_SEGMENT_BBOX_PAD_Y", 0.008)
    nx = ocr.clamp(x - pad_x, 0.0, 0.99)
    ny = ocr.clamp(y - pad_y, 0.0, 0.99)
    right = ocr.clamp(x + w + pad_x, nx + 0.01, 1.0)
    bottom = ocr.clamp(y + h + pad_y, ny + 0.01, 1.0)
    return nx, ny, right - nx, bottom - ny


def _frame_union(boxes: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float] | None:
    if not boxes:
        return None
    left = min(item[0] for item in boxes)
    top = min(item[1] for item in boxes)
    right = max(item[0] + item[2] for item in boxes)
    bottom = max(item[1] + item[3] for item in boxes)
    return left, top, max(0.001, right - left), max(0.001, bottom - top)


def _robust_bbox(regions: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float] | None:
    if not regions:
        return None
    lefts = np.array([item[0] for item in regions], dtype=np.float32)
    tops = np.array([item[1] for item in regions], dtype=np.float32)
    rights = np.array([item[0] + item[2] for item in regions], dtype=np.float32)
    bottoms = np.array([item[1] + item[3] for item in regions], dtype=np.float32)
    # Median is intentionally conservative: a single OCR hit on a logo/UI element
    # should not drag the subtitle box across the frame.
    left = float(np.median(lefts))
    top = float(np.median(tops))
    right = float(np.median(rights))
    bottom = float(np.median(bottoms))
    return _expand((left, top, max(0.02, right - left), max(0.015, bottom - top)))


def _expand_detail(region: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Small padding for source-text cleanup; deliberately tighter than subtitle placement."""
    x, y, w, h = region
    pad_x = _env_float("OCR_SEGMENT_DETAIL_PAD_X", 0.004)
    pad_y = _env_float("OCR_SEGMENT_DETAIL_PAD_Y", 0.004)
    nx = ocr.clamp(x - pad_x, 0.0, 0.995)
    ny = ocr.clamp(y - pad_y, 0.0, 0.995)
    right = ocr.clamp(x + w + pad_x, nx + 0.005, 1.0)
    bottom = ocr.clamp(y + h + pad_y, ny + 0.005, 1.0)
    return nx, ny, right - nx, bottom - ny


def _merge_overlapping_boxes(
    boxes: list[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    """Merge only boxes that clearly belong to the same OCR text line/word run."""
    if not boxes:
        return []
    pending = sorted(boxes, key=lambda item: (item[1], item[0]))
    merged: list[list[float]] = []
    y_tolerance = _env_float("OCR_SEGMENT_DETAIL_Y_TOLERANCE", 0.012)
    x_gap = _env_float("OCR_SEGMENT_DETAIL_X_GAP", 0.018)

    for x, y, w, h in pending:
        right = x + w
        bottom = y + h
        cy = y + h / 2.0
        matched = False
        for item in merged:
            ix, iy, iw, ih = item
            iright = ix + iw
            ibottom = iy + ih
            icy = iy + ih / 2.0
            vertical_overlap = max(0.0, min(bottom, ibottom) - max(y, iy))
            min_h = max(0.001, min(h, ih))
            same_line = vertical_overlap / min_h >= 0.45 or abs(cy - icy) <= y_tolerance
            close_x = x <= iright + x_gap and right >= ix - x_gap
            if same_line and close_x:
                nx = min(ix, x)
                ny = min(iy, y)
                nr = max(iright, right)
                nb = max(ibottom, bottom)
                item[:] = [nx, ny, nr - nx, nb - ny]
                matched = True
                break
        if not matched:
            merged.append([x, y, w, h])

    return [_expand_detail(tuple(item)) for item in merged]


def _sample_times(start: float, end: float, count: int) -> list[float]:
    if end <= start:
        return [max(0.0, start)]
    duration = end - start
    if count <= 1:
        return [start + duration * 0.5]
    if count == 2:
        return [start + duration * 0.33, start + duration * 0.67]
    # Keep samples away from caption transition boundaries.
    return [start + duration * ratio for ratio in (0.22, 0.50, 0.78)][:count]


def attach_segment_bboxes(input_path: Path, segments: list[dict], video_info: dict) -> int:
    if not segments:
        return 0

    broad_region = tuple(float(value) for value in (video_info.get("region") or (0.04, 0.48, 0.92, 0.46)))
    min_confidence = _env_float("OCR_MIN_CONFIDENCE", 0.65)
    text_similarity = _env_float("OCR_SEGMENT_BBOX_TEXT_SIMILARITY", 0.46)
    sample_count = min(3, _env_int("OCR_SEGMENT_BBOX_SAMPLES", 3))
    bottom_threshold = _env_float("OCR_OVERLAY_BOTTOM_THRESHOLD", 0.68)

    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        base.log("OCR segment bbox: cannot open video; keeping global-region fallback")
        return 0

    engine = ocr.make_ocr_engine()
    attached = 0
    try:
        for segment in segments:
            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", start))
            source_text = str(segment.get("text", ""))
            candidate_regions: list[tuple[float, float, float, float]] = []
            matched_samples = 0
            best_detail_boxes: list[tuple[float, float, float, float]] = []
            best_detail_score = -1.0

            for timestamp in _sample_times(start, end, sample_count):
                cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, timestamp) * 1000.0)
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                detected_text, confidence, boxes = ocr.ocr_frame(engine, frame, broad_region, min_confidence)
                if not detected_text or not boxes:
                    continue
                similarity = ocr.similarity(source_text, detected_text) if source_text else 1.0
                if source_text and similarity < text_similarity:
                    continue
                frame_region = _frame_union(boxes)
                if frame_region is None:
                    continue
                candidate_regions.append(frame_region)
                matched_samples += 1

                # Preserve a representative set of OCR boxes instead of flattening
                # every line/word into one large rectangle. The renderer uses these
                # tight regions only for source-text blur; the union bbox remains for
                # Vietnamese subtitle placement.
                detail_score = similarity * max(0.01, float(confidence))
                if detail_score > best_detail_score:
                    best_detail_score = detail_score
                    best_detail_boxes = _merge_overlapping_boxes(boxes)

            bbox = _robust_bbox(candidate_regions)
            if bbox is None:
                continue

            segment["bbox"] = [round(value, 6) for value in bbox]
            if best_detail_boxes:
                segment["bboxRegions"] = [
                    [round(value, 6) for value in region]
                    for region in best_detail_boxes
                ]
            segment["bboxConfidence"] = round(matched_samples / max(1, sample_count), 3)
            segment["placement"] = "bottom" if bbox[1] + bbox[3] >= bottom_threshold else "upper"
            attached += 1
    finally:
        cap.release()

    base.log(f"OCR segment bbox: attached {attached}/{len(segments)} segment regions")
    return attached
