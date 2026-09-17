#!/usr/bin/env python3
"""Dependency-free tests for timestamp -> per-segment OCR bbox matching."""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


# render_ocr_overlay only needs a tiny subset of these modules for the matching tests.
localize_stub = types.ModuleType("localize")
smart_stub = types.ModuleType("smart_render")
smart_stub.env_float = lambda name, default, minimum=0.0: default
smart_stub.env_int = lambda name, default, minimum=0: default
sys.modules.setdefault("localize", localize_stub)
sys.modules.setdefault("smart_render", smart_stub)

MODULE_PATH = Path(__file__).with_name("render_ocr_overlay.py")
spec = importlib.util.spec_from_file_location("render_ocr_overlay_tested", MODULE_PATH)
overlay = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(overlay)


class OCRSegmentRegionTests(unittest.TestCase):
    def test_each_srt_entry_uses_its_own_bbox(self) -> None:
        entries = [
            {"start": 0.0, "end": 2.0, "text": "A"},
            {"start": 3.0, "end": 5.0, "text": "B"},
        ]
        metadata = {
            "segments": [
                {"start": 0.0, "end": 2.0, "bbox": [0.42, 0.82, 0.16, 0.04], "placement": "bottom", "bboxConfidence": 1.0},
                {"start": 3.0, "end": 5.0, "bbox": [0.18, 0.20, 0.50, 0.10], "placement": "upper", "bboxConfidence": 0.67},
            ]
        }
        matched = overlay.attach_regions(entries, metadata, (0.05, 0.70, 0.90, 0.20))
        self.assertEqual(matched, 2)
        self.assertTrue(entries[0]["bottom"])
        self.assertFalse(entries[1]["bottom"])
        self.assertNotEqual(entries[0]["region"], entries[1]["region"])
        self.assertTrue(entries[0]["bboxMatched"])
        # The lower display box expands for longer Vietnamese text but remains
        # centered on the original OCR source region.
        self.assertGreaterEqual(entries[0]["region"][2], 0.30)
        self.assertEqual(entries[0]["sourceRegion"][0:2], overlay.normalize_region([0.42, 0.82, 0.16, 0.04])[0:2])
        # Upper text must blur and render on the exact OCR region, not a large bottom box.
        self.assertEqual(entries[1]["region"], entries[1]["sourceRegion"])

    def test_nearby_retimed_entry_can_reuse_bbox(self) -> None:
        entries = [{"start": 2.1, "end": 2.8, "text": "edited"}]
        metadata = {"segments": [{"start": 1.0, "end": 2.0, "bbox": [0.2, 0.7, 0.6, 0.1]}]}
        matched = overlay.attach_regions(entries, metadata, (0.05, 0.70, 0.90, 0.20))
        self.assertEqual(matched, 1)
        self.assertTrue(entries[0]["bboxMatched"])

    def test_old_metadata_falls_back_to_global_region(self) -> None:
        fallback = (0.05, 0.70, 0.90, 0.20)
        entries = [{"start": 0.0, "end": 1.0, "text": "legacy"}]
        matched = overlay.attach_regions(entries, {"segments": []}, fallback)
        self.assertEqual(matched, 0)
        self.assertEqual(entries[0]["sourceRegion"], fallback)
        self.assertFalse(entries[0]["bboxMatched"])

    def test_bilibili_default_cleanup_targets_top_right(self) -> None:
        x, y, w, h = [float(value) for value in overlay.DEFAULT_BILIBILI_REGIONS.split(",")]
        self.assertGreaterEqual(x, 0.65)
        self.assertLess(y, 0.05)
        self.assertGreater(w, 0.15)
        self.assertLessEqual(x + w, 1.0)
        self.assertLess(h, 0.12)


if __name__ == "__main__":
    unittest.main()
