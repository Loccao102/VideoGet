#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


localize_stub = types.ModuleType("localize")
localize_stub.log = lambda *args, **kwargs: None
sys.modules.setdefault("localize", localize_stub)

SOURCE = Path(__file__).with_name("smart_render.py")
SPEC = importlib.util.spec_from_file_location("smart_render_tested", SOURCE)
smart = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(smart)


class SmartBlurRadiusTests(unittest.TestCase):
    def test_tight_region_clamps_radius_to_ffmpeg_limit(self) -> None:
        region = {"x": 0.2, "y": 0.8, "w": 0.2, "h": 0.02}
        # 0.02 * 1080 = 21.6 -> rounded to 22px; boxblur max radius is 11.
        self.assertEqual(smart.bounded_blur_radius(region, 14, 1920, 1080), 11)

    def test_large_region_keeps_requested_radius(self) -> None:
        region = {"x": 0.1, "y": 0.7, "w": 0.8, "h": 0.10}
        self.assertEqual(smart.bounded_blur_radius(region, 14, 1920, 1080), 14)

    def test_unknown_frame_size_keeps_requested_radius(self) -> None:
        region = {"x": 0.1, "y": 0.7, "w": 0.8, "h": 0.02}
        self.assertEqual(smart.bounded_blur_radius(region, 14), 14)


if __name__ == "__main__":
    unittest.main()
