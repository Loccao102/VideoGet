#!/usr/bin/env python3
"""Pure-Python OCR quality helpers shared by all hardware tiers."""
from __future__ import annotations

import difflib
import re
from typing import Callable


def normalize_text(value: str) -> str:
    value = re.sub(r"\s+", "", str(value or ""))
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


def choose_consensus(
    observations: list[dict],
    threshold: float = 0.72,
    similarity_fn: Callable[[str, str], float] = similarity,
) -> dict | None:
    """Choose text by multi-frame voting, not by one highest-confidence frame."""
    usable = [
        item for item in observations
        if normalize_text(item.get("text", ""))
    ]
    if not usable:
        return None

    clusters: list[list[dict]] = []
    for item in usable:
        text = str(item.get("text", ""))
        best_cluster = None
        best_score = -1.0
        for cluster in clusters:
            score = max(
                similarity_fn(text, str(existing.get("text", "")))
                for existing in cluster
            )
            if score >= threshold and score > best_score:
                best_score = score
                best_cluster = cluster
        if best_cluster is None:
            clusters.append([item])
        else:
            best_cluster.append(item)

    def cluster_rank(cluster: list[dict]) -> tuple[int, float]:
        return (
            len(cluster),
            sum(max(0.0, float(item.get("confidence", 0.0))) for item in cluster),
        )

    winner = max(clusters, key=cluster_rank)

    def representative_rank(item: dict) -> float:
        text = str(item.get("text", ""))
        peers = [
            similarity_fn(text, str(other.get("text", "")))
            for other in winner
            if other is not item
        ]
        agreement = sum(peers) / len(peers) if peers else 1.0
        confidence = max(0.0, float(item.get("confidence", 0.0)))
        return agreement * 0.82 + confidence * 0.18

    representative = max(winner, key=representative_rank)
    confidence = sum(
        max(0.0, float(item.get("confidence", 0.0))) for item in winner
    ) / max(1, len(winner))

    boxes = []
    for item in winner:
        boxes.extend(item.get("boxes") or [])

    return {
        "text": str(representative.get("text", "")).strip(),
        "confidence": confidence,
        "consensusScore": len(winner) / max(1, len(usable)),
        "observationCount": len(usable),
        "winnerCount": len(winner),
        "boxes": boxes,
        "representativeBoxes": list(representative.get("boxes") or []),
    }


def _overlap_over_min_area(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    left, top = max(ax, bx), max(ay, by)
    right, bottom = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if right <= left or bottom <= top:
        return 0.0
    inter = (right - left) * (bottom - top)
    return inter / max(1e-9, min(aw * ah, bw * bh))


def merge_temporal_regions(
    regions: list[tuple[float, float, float, float]],
    *,
    overlap_threshold: float = 0.28,
    pad_x: float = 0.008,
    pad_y: float = 0.006,
    max_regions: int = 10,
) -> list[tuple[float, float, float, float]]:
    """Merge the same glyph/word bbox seen across frames into a stable cleanup envelope."""
    groups: list[list[tuple[float, float, float, float]]] = []
    for region in regions:
        x, y, w, h = [float(value) for value in region]
        if w <= 0.001 or h <= 0.001:
            continue
        candidate = (x, y, w, h)
        target = None
        for group in groups:
            if max(_overlap_over_min_area(candidate, existing) for existing in group) >= overlap_threshold:
                target = group
                break
        if target is None:
            groups.append([candidate])
        else:
            target.append(candidate)

    merged = []
    for group in groups:
        left = min(item[0] for item in group)
        top = min(item[1] for item in group)
        right = max(item[0] + item[2] for item in group)
        bottom = max(item[1] + item[3] for item in group)
        left = max(0.0, left - pad_x)
        top = max(0.0, top - pad_y)
        right = min(1.0, right + pad_x)
        bottom = min(1.0, bottom + pad_y)
        merged.append((left, top, max(0.001, right - left), max(0.001, bottom - top)))

    merged.sort(key=lambda item: (item[1], item[0]))
    return merged[:max(1, max_regions)]


def needs_refinement(
    segment: dict,
    *,
    min_confidence: float,
    min_consensus: float,
    min_observations: int,
) -> bool:
    return (
        float(segment.get("confidence", 0.0)) < min_confidence
        or float(segment.get("consensusScore", 0.0)) < min_consensus
        or int(segment.get("observationCount", 0)) < min_observations
    )
