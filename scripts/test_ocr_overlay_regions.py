#!/usr/bin/env python3
"""Dependency-free tests for OCR overlay placement, styles and default wiring."""
from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


# render_ocr_overlay only needs a tiny subset of these modules for the matching tests.
localize_stub = types.ModuleType("localize")
smart_stub = types.ModuleType("smart_render")
smart_stub.env_float = lambda name, default, minimum=0.0: default
smart_stub.env_int = lambda name, default, minimum=0: default
sys.modules.setdefault("localize", localize_stub)
sys.modules.setdefault("smart_render", smart_stub)

MODULE_PATH = Path(__file__).with_name("render_ocr_overlay.py")
LOCALIZER_PATH = Path(__file__).with_name("localize_ocr_subtitles.py")
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
        self.assertGreaterEqual(entries[0]["region"][2], 0.24)
        self.assertEqual(entries[0]["sourceRegion"], (0.42, 0.82, 0.16, 0.04))
        self.assertEqual(entries[1]["region"], entries[1]["sourceRegion"])

    def test_detail_regions_are_kept_separate_for_source_blur(self) -> None:
        entries = [{"start": 0.0, "end": 2.0, "text": "A"}]
        metadata = {
            "segments": [{
                "start": 0.0,
                "end": 2.0,
                "bbox": [0.20, 0.78, 0.60, 0.08],
                "bboxRegions": [
                    [0.22, 0.79, 0.20, 0.04],
                    [0.55, 0.79, 0.18, 0.04],
                ],
                "placement": "bottom",
            }]
        }
        matched = overlay.attach_regions(entries, metadata, (0.05, 0.70, 0.90, 0.20))
        self.assertEqual(matched, 1)
        self.assertEqual(len(entries[0]["sourceRegions"]), 2)
        self.assertEqual(entries[0]["sourceRegions"][0], (0.22, 0.79, 0.20, 0.04))
        self.assertLess(entries[0]["sourceRegions"][0][2], entries[0]["sourceRegion"][2])

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

    def test_bilibili_cleanup_is_not_hard_coded_to_one_side(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("bilibili_brand.detect(input_path)", source)
        self.assertIn('brand_side in {"left", "right"}', source)
        self.assertNotIn("DEFAULT_BILIBILI_REGIONS", source)


class OCROverlayStyleTests(unittest.TestCase):
    def test_clean_is_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OCR_OVERLAY_STYLE", None)
            self.assertEqual(overlay.normalize_style(None), "clean")

    def test_legacy_overlay_alias_maps_to_clean(self) -> None:
        self.assertEqual(overlay.normalize_style("ocr_overlay"), "clean")
        self.assertEqual(overlay.normalize_style("ocr_clean"), "clean")

    def test_capsule_and_box_aliases(self) -> None:
        self.assertEqual(overlay.normalize_style("ocr_capsule"), "capsule")
        self.assertEqual(overlay.normalize_style("ocr_box"), "box")

    def test_unknown_style_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            overlay.normalize_style("huge-black-panel")

    def test_clean_renderer_blurs_original_text_instead_of_default_black_box(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn('render_style == "box" and entry.get("bottom")', source)
        self.assertIn('if render_style == "capsule"', source)
        self.assertIn('OCR_OVERLAY_CLEAN_OUTLINE', source)
        self.assertIn('entry.get("sourceRegions")', source)
        self.assertIn('OCR_OVERLAY_SOURCE_BLUR_POWER', source)
        self.assertIn('for tight_region in source_regions', source)


    def test_aspect_is_appended_inside_overlay_filter_graph(self) -> None:
        filters = []
        label, width, height, mode = overlay.append_aspect_filter(
            filters, "subbed", 1920, 1080, "3:4"
        )
        self.assertEqual(label, "aspectout")
        self.assertEqual((width, height), (1080, 1440))
        self.assertEqual(mode, "crop")
        self.assertTrue(any("[subbed]crop=810:1080" in value for value in filters))

    def test_audio_is_stream_copied_in_ocr_subtitle_render(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn('cmd += ["-map", "0:a:0", "-c:a", "copy"]', source)
        self.assertIn("OCR_RENDER_CRF", source)


class OCRDefaultRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = LOCALIZER_PATH.read_text(encoding="utf-8")

    def test_initial_render_defaults_to_overlay(self) -> None:
        self.assertIn('OCR_SUBTITLE_INITIAL_RENDER_STYLE", "ocr_overlay"', self.source)
        self.assertIn("render_ocr_overlay.render(input_path, vi_srt, metadata_path, output_video, platform, aspect=aspect)", self.source)

    def test_bilibili_can_be_inferred_from_bv_filename(self) -> None:
        self.assertIn('return "bilibili"', self.source)
        self.assertIn("VIDEOGET_SOURCE_PLATFORM", self.source)


if __name__ == "__main__":
    unittest.main()
