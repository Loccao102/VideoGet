#!/usr/bin/env python3
"""Lightweight unit tests for OCR AV1 compatibility helpers."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import localize_ocr_subtitles as mod


class OCRDecodeProxyTests(unittest.TestCase):
    def test_non_av1_decodable_source_skips_proxy(self) -> None:
        source = Path("source.mp4")
        with mock.patch.object(mod, "video_codec", return_value="h264"), mock.patch.object(
            mod, "opencv_can_decode", return_value=True
        ):
            selected, used, codec = mod.prepare_ocr_input(source, Path("."))
        self.assertEqual(selected, source)
        self.assertFalse(used)
        self.assertEqual(codec, "h264")

    def test_av1_source_uses_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            expected_proxy = root / "source.ocr-decode-proxy.mp4"

            def fake_run(_cmd: list[str]) -> None:
                expected_proxy.write_bytes(b"proxy")

            with mock.patch.object(mod, "video_codec", return_value="av1"), mock.patch.object(
                mod.base, "run", side_effect=fake_run
            ), mock.patch.object(mod, "opencv_can_decode", return_value=True):
                selected, used, codec = mod.prepare_ocr_input(source, root)

            self.assertEqual(selected, expected_proxy)
            self.assertTrue(used)
            self.assertEqual(codec, "av1")


if __name__ == "__main__":
    unittest.main()
