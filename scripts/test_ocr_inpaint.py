#!/usr/bin/env python3
"""Dependency-free source regression for CPU OCR inpainting."""
from __future__ import annotations

import unittest
from pathlib import Path


SOURCE = Path(__file__).with_name("inpaint_subtitles.py")
OVERLAY = Path(__file__).with_name("render_ocr_overlay.py")


class OCRInpaintSourceTests(unittest.TestCase):
    def test_ffmpeg_handles_decode_and_audio_mapping(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn('"ffmpeg", "-v", "error", "-i", str(input_path)', source)
        self.assertIn('"-map", "1:a:0?"', source)
        self.assertIn('"libx264"', source)

    def test_opencv_only_inpaints_timed_bbox_regions(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn('metadata_regions(metadata)', source)
        self.assertIn('active_regions(segments, timestamp)', source)
        self.assertIn('cv2.inpaint(roi, mask, radius, method)', source)

    def test_stroke_mask_and_box_fallback_exist(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn('OCR_INPAINT_MASK_MODE', source)
        self.assertIn('OCR_INPAINT_FALLBACK_BOX', source)
        self.assertIn('OCR_INPAINT_CONTRAST', source)
        self.assertIn('cv2.Canny', source)

    def test_overlay_falls_back_to_blur(self) -> None:
        source = OVERLAY.read_text(encoding="utf-8")
        self.assertIn('OCR_INPAINT_FALLBACK_BLUR', source)
        self.assertIn('OCR inpaint failed; falling back to blur cleanup', source)
        self.assertIn('render_style != "inpaint" or not inpaint_ok', source)


if __name__ == "__main__":
    unittest.main()
