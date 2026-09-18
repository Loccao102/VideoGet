#!/usr/bin/env python3
"""Dependency-free source regression for optional OpenCV face detection."""
from __future__ import annotations

import unittest
from pathlib import Path


SOURCE = Path(__file__).with_name("analyze_layout.py")


class LayoutOptionalFaceDetectionTests(unittest.TestCase):
    def test_missing_cascade_api_does_not_abort_layout_analysis(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn('getattr(cv2, "CascadeClassifier", None)', source)
        self.assertIn('getattr(cv2, "data", None)', source)
        self.assertIn("if cascade_factory is None or not haarcascades:", source)
        self.assertIn("return []", source)


if __name__ == "__main__":
    unittest.main()
