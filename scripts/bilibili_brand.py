#!/usr/bin/env python3
"""Detect persistent Bilibili uploader/watermark text and choose its side automatically."""
from __future__ import annotations

import os
import re
from pathlib import Path


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


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


def _union(regions: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    left = min(item[0] for item in regions)
    top = min(item[1] for item in regions)
    right = max(item[0] + item[2] for item in regions)
    bottom = max(item[1] + item[3] for item in regions)
    return left, top, right - left, bottom - top


def _expand(region: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x, y, w, h = region
    pad_x = env_float("OCR_OVERLAY_BILIBILI_PAD_X", 0.018, 0.0)
    pad_y = env_float("OCR_OVERLAY_BILIBILI_PAD_Y", 0.012, 0.0)
    min_w = env_float("OCR_OVERLAY_BILIBILI_MIN_WIDTH", 0.14, 0.02)
    min_h = env_float("OCR_OVERLAY_BILIBILI_MIN_HEIGHT", 0.045, 0.02)
    max_w = env_float("OCR_OVERLAY_BILIBILI_MAX_WIDTH", 0.40, min_w)
    max_h = env_float("OCR_OVERLAY_BILIBILI_MAX_HEIGHT", 0.16, min_h)
    cx = x + w / 2.0
    cy = y + h / 2.0
    target_w = clamp(max(w + pad_x * 2.0, min_w), min_w, max_w)
    target_h = clamp(max(h + pad_y * 2.0, min_h), min_h, max_h)
    nx = clamp(cx - target_w / 2.0, 0.0, 1.0 - target_w)
    ny = clamp(cy - target_h / 2.0, 0.0, 1.0 - target_h)
    return nx, ny, target_w, target_h


def choose_persistent_region(observations: list[dict], sample_count: int) -> dict | None:
    """Pure scoring helper used by runtime detection and dependency-free CI tests."""
    if not observations or sample_count <= 0:
        return None

    bucket_x = env_float("OCR_OVERLAY_BILIBILI_BUCKET_X", 0.07, 0.02)
    bucket_y = env_float("OCR_OVERLAY_BILIBILI_BUCKET_Y", 0.045, 0.015)
    min_hits = env_int("OCR_OVERLAY_BILIBILI_MIN_HITS", 2, 1)
    clusters: dict[tuple[str, int, int], dict] = {}

    for item in observations:
        side = str(item.get("side", "")).strip().lower()
        if side not in {"left", "right"}:
            continue
        region = item.get("region")
        if not isinstance(region, (list, tuple)) or len(region) != 4:
            continue
        x, y, w, h = [float(value) for value in region]
        cx = x + w / 2.0
        cy = y + h / 2.0
        key = (side, int(cx / bucket_x), int(cy / bucket_y))
        cluster = clusters.setdefault(
            key,
            {"side": side, "samples": set(), "regions": [], "score": 0.0, "bilibili": False, "texts": []},
        )
        cluster["samples"].add(int(item.get("sample", 0)))
        cluster["regions"].append((x, y, w, h))
        text = str(item.get("text", ""))
        confidence = clamp(float(item.get("confidence", 0.0) or 0.0), 0.0, 1.0)
        is_bilibili = "bilibili" in text.lower()
        ascii_bonus = 0.25 if re.search(r"[A-Za-z0-9]", text) else 0.0
        cluster["score"] += confidence + ascii_bonus + (2.75 if is_bilibili else 0.0)
        cluster["bilibili"] = cluster["bilibili"] or is_bilibili
        cluster["texts"].append(text)

    persistent = []
    for cluster in clusters.values():
        hits = len(cluster["samples"])
        if hits >= min_hits or cluster["bilibili"]:
            cluster["hits"] = hits
            persistent.append(cluster)
    if not persistent:
        return None

    side_scores = {"left": 0.0, "right": 0.0}
    for cluster in persistent:
        persistence = len(cluster["samples"]) / max(1, sample_count)
        side_scores[cluster["side"]] += float(cluster["score"]) * (1.0 + persistence)

    side = max(side_scores, key=side_scores.get)
    chosen_score = side_scores[side]
    other_score = side_scores["right" if side == "left" else "left"]
    if chosen_score <= 0:
        return None

    chosen = [cluster for cluster in persistent if cluster["side"] == side]
    regions = [region for cluster in chosen for region in cluster["regions"]]
    region = _expand(_union(regions))
    hits = len(set().union(*(cluster["samples"] for cluster in chosen)))
    ratio = chosen_score / max(0.001, chosen_score + other_score)
    confidence = clamp(0.55 * (hits / max(1, sample_count)) + 0.45 * ratio, 0.0, 1.0)
    texts = [text for cluster in chosen for text in cluster["texts"] if text]
    return {
        "region": [round(value, 6) for value in region],
        "side": side,
        "confidence": round(confidence, 4),
        "source": "ocr",
        "hits": hits,
        "texts": texts[:8],
    }


def _visual_fallback(frames, width: int, height: int) -> dict | None:
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    if len(frames) < 3 or width <= 0 or height <= 0:
        return None

    top_h = max(16, int(height * env_float("OCR_OVERLAY_BILIBILI_TOP_BAND", 0.20, 0.08)))
    side_w = max(16, int(width * 0.34))

    def score(side: str) -> float:
        crops = []
        for frame in frames:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            crop = gray[:top_h, :side_w] if side == "left" else gray[:top_h, width - side_w :]
            crops.append(crop.astype(np.float32))
        stack = np.stack(crops, axis=0)
        median = np.median(stack, axis=0).astype(np.uint8)
        motion = float(np.mean(np.std(stack, axis=0)))
        edges = cv2.Canny(median, 80, 180)
        edge_density = float(np.mean(edges > 0))
        return edge_density / (1.0 + motion / 18.0)

    left = score("left")
    right = score("right")
    best_side = "left" if left > right else "right"
    best = max(left, right)
    other = min(left, right)
    ratio = best / max(other, 1e-6)
    min_ratio = env_float("OCR_OVERLAY_BILIBILI_VISUAL_MIN_RATIO", 1.08, 1.0)
    if best <= 0.005 or ratio < min_ratio:
        return None
    region = (0.01, 0.012, 0.31, 0.11) if best_side == "left" else (0.68, 0.012, 0.31, 0.11)
    return {
        "region": list(region),
        "side": best_side,
        "confidence": round(clamp(0.28 + min(0.32, (ratio - 1.0) * 0.8), 0.0, 0.6), 4),
        "source": "visual",
        "hits": len(frames),
        "texts": [],
    }


def detect(input_path: Path) -> dict | None:
    """Detect whether the persistent Bilibili uploader mark is top-left or top-right."""
    if not env_bool("OCR_OVERLAY_BILIBILI_AUTO_DETECT", True):
        return None
    try:
        import cv2
        import numpy as np
        import localize_ocr_music as ocr
    except Exception:
        return None

    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        return None
    try:
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        duration = frame_count / fps if frame_count > 0 and fps > 0 else 0.0
        if width <= 0 or height <= 0 or duration <= 0:
            return None

        samples = env_int("OCR_OVERLAY_BILIBILI_SAMPLES", 6, 3)
        top_ratio = clamp(env_float("OCR_OVERLAY_BILIBILI_TOP_BAND", 0.20, 0.08), 0.08, 0.35)
        min_conf = clamp(env_float("OCR_OVERLAY_BILIBILI_OCR_CONFIDENCE", 0.48, 0.0), 0.0, 1.0)
        sample_times = np.linspace(duration * 0.08, duration * 0.92, samples)
        engine = ocr.make_ocr_engine()
        observations: list[dict] = []
        frames = []

        for sample_index, timestamp in enumerate(sample_times):
            cap.set(cv2.CAP_PROP_POS_MSEC, float(timestamp) * 1000.0)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            frames.append(frame)
            crop_h = max(8, int(frame.shape[0] * top_ratio))
            crop = frame[:crop_h, :]
            result = engine(crop)
            for text, score, box in ocr.result_items(result):
                text = ocr.normalize_text(text)
                if not text or score < min_conf or box is None or not getattr(box, "size", 0):
                    continue
                bx0 = float(np.min(box[:, 0])) / width
                bx1 = float(np.max(box[:, 0])) / width
                by0 = float(np.min(box[:, 1])) / height
                by1 = float(np.max(box[:, 1])) / height
                cx = (bx0 + bx1) / 2.0
                if cx < 0.48:
                    side = "left"
                elif cx > 0.52:
                    side = "right"
                else:
                    continue
                observations.append({
                    "sample": sample_index,
                    "side": side,
                    "region": [bx0, by0, max(0.002, bx1 - bx0), max(0.002, by1 - by0)],
                    "text": text,
                    "confidence": float(score),
                })

        detected = choose_persistent_region(observations, max(1, len(frames)))
        if detected is not None:
            return detected
        return _visual_fallback(frames, width, height)
    finally:
        cap.release()


def side_from_regions(regions: list[dict]) -> str:
    if not regions:
        return ""
    weighted = 0.0
    total = 0.0
    for region in regions:
        x = float(region.get("x", 0.0))
        w = float(region.get("w", 0.0))
        weight = max(0.01, w)
        weighted += (x + w / 2.0) * weight
        total += weight
    return "left" if weighted / max(total, 0.001) < 0.5 else "right"
