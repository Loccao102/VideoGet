#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SOURCE = Path(__file__).with_name("convert_aspect.py")
SPEC = importlib.util.spec_from_file_location("convert_aspect", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class ConvertAspectTests(unittest.TestCase):
    def test_16_9_to_3_4_retention(self) -> None:
        ratio = MODULE.retention_ratio(1920, 1080, 1080, 1440)
        self.assertAlmostEqual(ratio, 0.421875, places=5)

    def test_center_crop_geometry_is_three_four(self) -> None:
        width, height, x, y = MODULE.crop_geometry(1920, 1080, 1080, 1440)
        self.assertEqual((width, height), (810, 1080))
        self.assertEqual(y, 0)
        self.assertGreater(x, 0)

    def test_targets_include_social_ratios(self) -> None:
        self.assertEqual(MODULE.TARGETS["3:4"], (1080, 1440))
        self.assertEqual(MODULE.TARGETS["9:16"], (1080, 1920))
        self.assertEqual(MODULE.TARGETS["1:1"], (1080, 1080))
        self.assertEqual(MODULE.TARGETS["16:9"], (1920, 1080))


if __name__ == "__main__":
    unittest.main()
