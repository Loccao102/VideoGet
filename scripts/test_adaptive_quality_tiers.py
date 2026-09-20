#!/usr/bin/env python3
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class AdaptiveQualityTierTests(unittest.TestCase):
    def test_low_escalates_instead_of_staying_tiny_everywhere(self) -> None:
        env = read(".env.low.example")
        self.assertIn("VIDEGET_QUALITY_TIER=low", env)
        self.assertIn("OCR_MODEL_SIZE=tiny", env)
        self.assertIn("OCR_REFINEMENT_MODEL_SIZE=small", env)
        self.assertIn("TRANSLATE_REPAIR_MODEL=qwen3:4b", env)

    def test_medium_uses_selective_medium_and_8b_repair(self) -> None:
        env = read(".env.medium.example")
        self.assertIn("VIDEGET_QUALITY_TIER=medium", env)
        self.assertIn("OCR_MODEL_SIZE=small", env)
        self.assertIn("OCR_REFINEMENT_MODEL_SIZE=medium", env)
        self.assertIn("TRANSLATE_REPAIR_MODEL=qwen3:8b", env)

    def test_high_spends_more_samples_without_changing_quality_contract(self) -> None:
        env = read(".env.high.example")
        self.assertIn("VIDEGET_QUALITY_TIER=high", env)
        self.assertIn("OCR_MODEL_SIZE=medium", env)
        self.assertIn("OCR_REFINEMENT_SAMPLES=5", env)
        self.assertIn("OCR_REFINEMENT_MAX_SEGMENTS=40", env)
        self.assertIn("OCR_SOURCE_CLEANUP_MODE=cover", env)

    def test_runtime_has_consensus_refinement_and_av1_fallback(self) -> None:
        source = read("scripts/localize_ocr_music.py")
        self.assertIn("ocr_quality.choose_consensus", source)
        self.assertIn("refine_uncertain_segments", source)
        self.assertIn("_ffmpeg_frame_at", source)
        self.assertIn("del engine", source)

    def test_renderer_prefers_temporal_cleanup_regions_and_cover(self) -> None:
        source = read("scripts/render_ocr_overlay.py")
        self.assertIn('raw.get("cleanupRegions") or raw.get("bboxRegions")', source)
        self.assertIn('OCR_SOURCE_CLEANUP_MODE', source)
        self.assertIn('cleanup_mode in {"cover", "hybrid"}', source)


if __name__ == "__main__":
    unittest.main()
