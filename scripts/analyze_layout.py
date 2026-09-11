#!/usr/bin/env python3
"""Lightweight visual layout analysis for VideoGet renders.

The goal is conservative cleanup, not OCR. The analyzer samples a handful of frames,
finds likely burned-in subtitle bands, persistent watermark/logo regions, and face
regions, then chooses a subtitle anchor that avoids protected content. All geometry is
returned normalized to the frame so FFmpeg can render without cropping.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

ANALYZER_VERSION = 1


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return max(minimum, default)


@dataclass
class Region:
    x: float
    y: float
    w: float
    h: float
    confidence: float = 0.0
    kind: str = ""

    def padded(self, px: float, py: float) -> "Region":
        nx = clamp(self.x - px, 0.0, 0.99)
        ny = clamp(self.y - py, 0.0, 0.99)
        right = clamp(self.x + self.w + px, nx + 0.01, 1.0)
        bottom = clamp(self.y + self.h + py, ny + 0.01, 1.0)
        return Region(nx, ny, right - nx, bottom - ny, self.confidence, self.kind)

    def as_dict(self) -> dict:
        return {
            "x": round(self.x, 5), "y": round(self.y, 5),
            "w": round(self.w, 5), "h": round(self.h, 5),
            "confidence": round(float(self.confidence), 3), "kind": self.kind,
        }


def overlap(a: Region, b: Region) -> float:
    left, top = max(a.x, b.x), max(a.y, b.y)
    right, bottom = min(a.x + a.w, b.x + b.w), min(a.y + a.h, b.y + b.h)
    if right <= left or bottom <= top:
        return 0.0
    inter = (right - left) * (bottom - top)
    return inter / max(1e-6, min(a.w * a.h, b.w * b.h))


def sample_frames(path: Path, count: int) -> tuple[list[np.ndarray], float, int, int]:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video: {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if width <= 0 or height <= 0:
        cap.release()
        raise RuntimeError("video has invalid dimensions")
    duration = total / fps if total > 0 else 0.0
    if duration <= 0:
        duration = 30.0
    frames: list[np.ndarray] = []
    for index in range(max(3, count)):
        fraction = 0.06 + (0.88 * (index + 0.5) / max(3, count))
        cap.set(cv2.CAP_PROP_POS_MSEC, duration * fraction * 1000.0)
        ok, frame = cap.read()
        if ok and frame is not None:
            frames.append(frame)
    cap.release()
    if not frames:
        raise RuntimeError("could not sample frames")
    return frames, duration, width, height


def edge_maps(frames: list[np.ndarray]) -> list[np.ndarray]:
    out = []
    for frame in frames:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        out.append((cv2.Canny(gray, 65, 145) > 0).astype(np.float32))
    return out


def detect_faces(frames: list[np.ndarray], width: int, height: int) -> list[Region]:
    cascade_path = str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
    cascade = cv2.CascadeClassifier(cascade_path)
    if cascade.empty():
        return []
    detections: list[Region] = []
    for frame in frames:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(
            gray,
            scaleFactor=1.12,
            minNeighbors=5,
            minSize=(max(28, width // 15), max(28, height // 18)),
        )
        for x, y, w, h in faces:
            detections.append(Region(x / width, y / height, w / width, h / height, 0.7, "face"))
    if not detections:
        return []
    clusters: list[list[Region]] = []
    for region in detections:
        placed = False
        for cluster in clusters:
            if any(overlap(region, existing) > 0.25 for existing in cluster):
                cluster.append(region)
                placed = True
                break
        if not placed:
            clusters.append([region])
    result = []
    for cluster in clusters:
        if len(cluster) < max(2, len(frames) // 5):
            continue
        xs = [r.x for r in cluster]
        ys = [r.y for r in cluster]
        rs = [r.x + r.w for r in cluster]
        bs = [r.y + r.h for r in cluster]
        region = Region(
            float(np.median(xs)),
            float(np.median(ys)),
            float(np.median(rs) - np.median(xs)),
            float(np.median(bs) - np.median(ys)),
            min(0.98, 0.55 + len(cluster) / max(1, len(frames)) * 0.35),
            "face",
        )
        result.append(region.padded(0.025, 0.025))
    return result[:3]


def subtitle_band_score(edge: np.ndarray, y0: int, y1: int, x0: int, x1: int) -> float:
    roi = edge[y0:y1, x0:x1]
    if roi.size == 0:
        return 0.0
    row_density = roi.mean(axis=1)
    dense_rows = float(np.mean(row_density > 0.085))
    very_dense_rows = float(np.mean(row_density > 0.15))
    edge_density = float(np.mean(roi))
    return edge_density * 1.25 + dense_rows * 0.55 + very_dense_rows * 0.35


def detect_source_subtitle(edges: list[np.ndarray], width: int, height: int, mode: str) -> Region | None:
    candidates = [0.56, 0.62, 0.68, 0.74, 0.80, 0.86]
    band_h = 0.105 if height >= width else 0.13
    x0, x1 = int(width * 0.06), int(width * 0.94)
    scored = []
    for center in candidates:
        y0 = max(0, int(height * (center - band_h / 2)))
        y1 = min(height, int(height * (center + band_h / 2)))
        values = [subtitle_band_score(edge, y0, y1, x0, x1) for edge in edges]
        score = float(np.median(values) * 0.55 + np.mean(values) * 0.45)
        occupancy = float(np.mean(np.array(values) > (0.16 if mode == "aggressive" else 0.19)))
        scored.append((score + occupancy * 0.12, center, values))
    score, center, _ = max(scored, key=lambda item: item[0])
    threshold = 0.22 if mode == "aggressive" else 0.27
    if score < threshold:
        return None

    y0 = max(0, int(height * (center - band_h / 2)))
    y1 = min(height, int(height * (center + band_h / 2)))
    column_presence = []
    for edge in edges:
        roi = edge[y0:y1]
        column_presence.append((roi.mean(axis=0) > 0.055).astype(np.float32))
    presence = np.mean(column_presence, axis=0)
    xs = np.where(presence > (0.25 if mode == "aggressive" else 0.34))[0]
    if len(xs) > 8:
        left, right = xs.min() / width, (xs.max() + 1) / width
        if right - left > 0.92:
            left, right = 0.08, 0.92
        else:
            left, right = clamp(left - 0.025, 0.03, 0.85), clamp(right + 0.025, 0.15, 0.97)
    else:
        left, right = 0.08, 0.92
    confidence = clamp((score - threshold) / 0.45 + 0.55, 0.55, 0.98)
    return Region(
        left,
        max(0.0, center - band_h * 0.62),
        right - left,
        min(0.18, band_h * 1.24),
        confidence,
        "source_subtitle",
    )


def merge_stroke_components(
    components: list[tuple[int, int, int, int, int]], width: int, height: int
) -> list[Region]:
    if not components:
        return []
    comps = sorted(components, key=lambda c: (c[1], c[0]))
    groups: list[list[tuple[int, int, int, int, int]]] = []
    for comp in comps:
        x, y, w, h, _ = comp
        cy = y + h / 2
        placed = False
        for group in groups:
            gx0 = min(c[0] for c in group)
            gy0 = min(c[1] for c in group)
            gx1 = max(c[0] + c[2] for c in group)
            gy1 = max(c[1] + c[3] for c in group)
            gcy = (gy0 + gy1) / 2
            gap = max(0, x - gx1, gx0 - (x + w))
            if abs(cy - gcy) <= height * 0.022 and gap <= width * 0.045:
                group.append(comp)
                placed = True
                break
        if not placed:
            groups.append([comp])
    out = []
    for group in groups:
        if len(group) < 3:
            continue
        x0 = min(c[0] for c in group)
        y0 = min(c[1] for c in group)
        x1 = max(c[0] + c[2] for c in group)
        y1 = max(c[1] + c[3] for c in group)
        rw, rh = (x1 - x0) / width, (y1 - y0) / height
        if not (0.045 <= rw <= 0.42 and 0.008 <= rh <= 0.10):
            continue
        out.append(Region(x0 / width, y0 / height, rw, rh, 0.0, "watermark"))
    return out


def detect_watermarks(edges: list[np.ndarray], width: int, height: int, mode: str) -> list[Region]:
    persistence = np.mean(np.stack(edges, axis=0), axis=0)
    threshold = 0.42 if mode == "aggressive" else 0.52
    mask = (persistence >= threshold).astype(np.uint8) * 255
    allowed = np.zeros_like(mask)
    top_h = int(height * 0.22)
    bottom_y = int(height * 0.84)
    allowed[:top_h, :] = mask[:top_h, :]
    allowed[bottom_y:, : int(width * 0.38)] = mask[bottom_y:, : int(width * 0.38)]
    allowed[bottom_y:, int(width * 0.62) :] = mask[bottom_y:, int(width * 0.62) :]
    count, _, stats, _ = cv2.connectedComponentsWithStats(allowed, 8)
    components = []
    for idx in range(1, count):
        x, y, w, h, area = [int(v) for v in stats[idx]]
        if area < 4 or w > width * 0.10 or h > height * 0.07:
            continue
        components.append((x, y, w, h, area))
    groups = merge_stroke_components(components, width, height)
    scored = []
    for region in groups:
        x0, y0 = int(region.x * width), int(region.y * height)
        x1, y1 = int((region.x + region.w) * width), int((region.y + region.h) * height)
        density = float(np.mean(persistence[y0:y1, x0:x1])) if x1 > x0 and y1 > y0 else 0.0
        edge_bonus = 0.12 if region.y < 0.08 else 0.0
        confidence = clamp(0.48 + density * 1.5 + edge_bonus, 0.0, 0.98)
        region.confidence = confidence
        if confidence >= (0.68 if mode == "smart" else 0.58):
            scored.append(region.padded(0.012, 0.008))
    scored.sort(key=lambda r: r.confidence, reverse=True)
    return scored[: (3 if mode == "aggressive" else 2)]


def region_edge_cost(edges: list[np.ndarray], region: Region, width: int, height: int) -> float:
    x0, y0 = int(region.x * width), int(region.y * height)
    x1, y1 = int((region.x + region.w) * width), int((region.y + region.h) * height)
    if x1 <= x0 or y1 <= y0:
        return 1.0
    return float(np.mean([edge[y0:y1, x0:x1].mean() for edge in edges]))


def choose_subtitle_placement(
    edges: list[np.ndarray],
    width: int,
    height: int,
    source_subtitle: Region | None,
    faces: list[Region],
    watermarks: list[Region],
) -> dict:
    if source_subtitle is not None and source_subtitle.confidence >= 0.68:
        center_y = clamp(source_subtitle.y + source_subtitle.h * 0.56, 0.12, 0.90)
        return {
            "anchorX": 0.5,
            "anchorY": round(center_y, 5),
            "alignment": 2,
            "reason": "replace_source_subtitle",
        }

    portrait = height >= width
    candidates = [
        Region(0.08, 0.72 if portrait else 0.74, 0.84, 0.15, kind="subtitle_zone"),
        Region(0.08, 0.61 if portrait else 0.63, 0.84, 0.15, kind="subtitle_zone"),
        Region(0.08, 0.49, 0.84, 0.15, kind="subtitle_zone"),
        Region(0.08, 0.12, 0.84, 0.15, kind="subtitle_zone"),
    ]
    protected = faces + watermarks
    best = None
    for index, candidate in enumerate(candidates):
        cost = region_edge_cost(edges, candidate, width, height)
        protected_penalty = sum(
            overlap(candidate, item) * (1.8 if item.kind == "face" else 1.0)
            for item in protected
        )
        preference = index * 0.018
        total = cost + protected_penalty + preference
        if best is None or total < best[0]:
            best = (total, candidate)
    candidate = best[1]
    return {
        "anchorX": 0.5,
        "anchorY": round(candidate.y + candidate.h * 0.58, 5),
        "alignment": 2,
        "reason": "lowest_safe_zone",
    }


def analyze(path: Path, mode: str = "smart", samples: int = 12) -> dict:
    mode = mode if mode in {"safe", "smart", "aggressive"} else "smart"
    frames, duration, width, height = sample_frames(path, samples)
    edges = edge_maps(frames)
    if mode == "safe":
        source_subtitle = None
        watermarks: list[Region] = []
        faces = detect_faces(frames, width, height)
    else:
        source_subtitle = detect_source_subtitle(edges, width, height, mode)
        watermarks = detect_watermarks(edges, width, height, mode)
        faces = detect_faces(frames, width, height)
    placement = choose_subtitle_placement(edges, width, height, source_subtitle, faces, watermarks)
    return {
        "version": ANALYZER_VERSION,
        "mode": mode,
        "width": width,
        "height": height,
        "duration": round(duration, 3),
        "samples": len(frames),
        "sourceSubtitle": source_subtitle.as_dict() if source_subtitle else None,
        "watermarks": [r.as_dict() for r in watermarks],
        "protectedRegions": [r.as_dict() for r in faces],
        "subtitlePlacement": placement,
        "strategy": "preserve_frame_no_crop",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--mode", default=os.getenv("VIDEO_CLEANUP_MODE", "smart"))
    parser.add_argument("--samples", type=int, default=env_int("VIDEO_ANALYSIS_SAMPLES", 12, 3))
    parser.add_argument("--output")
    args = parser.parse_args()
    result = analyze(Path(args.input).resolve(), args.mode.strip().lower(), args.samples)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
