#!/usr/bin/env python3
from __future__ import annotations

import unittest
from pathlib import Path

import bilibili_brand


SOURCE = Path(__file__).with_name("bilibili_brand.py")


class BilibiliBrandDetectionTests(unittest.TestCase):
    def test_repeated_right_brand_wins(self) -> None:
        observations = []
        for sample in range(5):
            observations.append({
                "sample": sample,
                "side": "right",
                "region": [0.76, 0.03, 0.18, 0.05],
                "text": "MaodVlog bilibili",
                "confidence": 0.92,
            })
        observations.append({
            "sample": 1,
            "side": "left",
            "region": [0.04, 0.04, 0.10, 0.04],
            "text": "random",
            "confidence": 0.70,
        })
        result = bilibili_brand.choose_persistent_region(observations, 5)
        self.assertIsNotNone(result)
        self.assertEqual(result["side"], "right")
        self.assertGreaterEqual(result["hits"], 5)
        self.assertGreater(result["region"][0], 0.5)

    def test_repeated_left_brand_wins(self) -> None:
        observations = [
            {
                "sample": sample,
                "side": "left",
                "region": [0.03, 0.02, 0.20, 0.055],
                "text": "猫猫频道 bilibili",
                "confidence": 0.88,
            }
            for sample in range(4)
        ]
        result = bilibili_brand.choose_persistent_region(observations, 4)
        self.assertIsNotNone(result)
        self.assertEqual(result["side"], "left")
        self.assertLess(result["region"][0] + result["region"][2] / 2.0, 0.5)

    def test_ffmpeg_fallback_does_not_create_full_h264_proxy(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("def _detect_ffmpeg", source)
        self.assertIn('"rawvideo", "pipe:1"', source)
        self.assertIn('"decodeMode"] = "ffmpeg_pipe"', source)
        self.assertNotIn("def _proxy_for_detection", source)
        self.assertNotIn("TemporaryDirectory", source)

    def test_one_off_corner_text_is_not_enough(self) -> None:
        observations = [{
            "sample": 0,
            "side": "right",
            "region": [0.78, 0.03, 0.12, 0.04],
            "text": "12:30",
            "confidence": 0.80,
        }]
        self.assertIsNone(bilibili_brand.choose_persistent_region(observations, 6))


if __name__ == "__main__":
    unittest.main()
