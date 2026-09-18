#!/usr/bin/env python3
"""Dependency-free regressions for direct ffmpeg OCR sampling.

The OCR path must not create a full-video H.264 compatibility proxy just so
OpenCV can seek AV1 frames. FFmpeg now decodes sampled frames directly to a
raw-video pipe and OCR captures geometry during that same pass.
"""
from __future__ import annotations

import unittest
from pathlib import Path


OCR_SOURCE = Path(__file__).with_name("localize_ocr_music.py")
SUBTITLE_SOURCE = Path(__file__).with_name("localize_ocr_subtitles.py")


class OCRDirectDecodeSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ocr = OCR_SOURCE.read_text(encoding="utf-8")
        cls.subtitles = SUBTITLE_SOURCE.read_text(encoding="utf-8")

    def test_ocr_uses_ffmpeg_rawvideo_pipe(self) -> None:
        self.assertIn('"ffmpeg", "-v", "error", "-hwaccel", "none"', self.ocr)
        self.assertIn('"-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"', self.ocr)
        self.assertIn("subprocess.Popen(cmd, stdout=subprocess.PIPE", self.ocr)

    def test_direct_sampling_does_not_downscale_ocr_frames(self) -> None:
        # The direct OCR command only applies fps sampling; source resolution is
        # retained so this optimization does not trade OCR accuracy for speed.
        self.assertIn('"-vf", f"fps={ocr_fps:g}"', self.ocr)
        direct_block = self.ocr[self.ocr.index('cmd = [\n        "ffmpeg"'):self.ocr.index("process = subprocess.Popen", self.ocr.index('cmd = [\n        "ffmpeg"'))]
        self.assertNotIn("scale=", direct_block)

    def test_geometry_is_captured_during_same_ocr_pass(self) -> None:
        self.assertIn('segment["bboxRegions"] = details', self.ocr)
        self.assertIn('segment["bbox"] = bbox', self.ocr)
        self.assertIn("_apply_geometry(current, boxes)", self.ocr)

    def test_subtitle_mode_no_longer_builds_proxy_or_second_bbox_pass(self) -> None:
        main_start = self.subtitles.index("def main()")
        main_source = self.subtitles[main_start:]
        self.assertNotIn("prepare_ocr_input(", main_source)
        self.assertNotIn("ocr_segment_regions.attach_segment_bboxes(", main_source)
        self.assertIn("ocr.extract_segments(input_path, output_dir)", main_source)
        self.assertIn('bbox_attached = sum(1 for segment in segments if segment.get("bbox"))', main_source)

    def test_empty_ocr_has_one_automatic_recovery_pass(self) -> None:
        self.assertIn("OCR_RETRY_ON_EMPTY", self.subtitles)
        self.assertIn("OCR_RETRY_REGION", self.subtitles)
        self.assertIn("OCR_RETRY_MIN_CONFIDENCE", self.subtitles)
        self.assertIn("OCR first pass found 0 timed segments; retrying once", self.subtitles)

    def test_manual_ocr_region_is_not_overridden(self) -> None:
        self.assertIn('original_region.lower() in {"", "auto"}', self.subtitles)


if __name__ == "__main__":
    unittest.main()
