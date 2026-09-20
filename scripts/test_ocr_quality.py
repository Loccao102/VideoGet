#!/usr/bin/env python3
from __future__ import annotations

import unittest

import ocr_quality


class OCRQualityTests(unittest.TestCase):
    def test_consensus_beats_one_high_confidence_wrong_frame(self) -> None:
        result = ocr_quality.choose_consensus([
            {"text": "渴死我了", "confidence": 0.76, "boxes": []},
            {"text": "渴死我了", "confidence": 0.79, "boxes": []},
            {"text": "谢谢我来这里", "confidence": 0.98, "boxes": []},
        ], threshold=0.72)
        self.assertEqual(result["text"], "渴死我了")
        self.assertAlmostEqual(result["consensusScore"], 2 / 3)

    def test_temporal_regions_merge_same_word_without_joining_far_text(self) -> None:
        merged = ocr_quality.merge_temporal_regions([
            (0.40, 0.70, 0.12, 0.04),
            (0.405, 0.702, 0.12, 0.04),
            (0.75, 0.25, 0.08, 0.03),
        ])
        self.assertEqual(len(merged), 2)

    def test_refinement_uses_quality_not_machine_tier(self) -> None:
        self.assertTrue(ocr_quality.needs_refinement(
            {"confidence": 0.88, "consensusScore": 0.50, "observationCount": 4},
            min_confidence=0.70,
            min_consensus=0.67,
            min_observations=2,
        ))
        self.assertFalse(ocr_quality.needs_refinement(
            {"confidence": 0.82, "consensusScore": 0.80, "observationCount": 4},
            min_confidence=0.70,
            min_consensus=0.67,
            min_observations=2,
        ))


if __name__ == "__main__":
    unittest.main()
