#!/usr/bin/env python3
"""Fast unit coverage for the subtitle-only no-speech result contract."""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path


# Load only the module logic: full transcription dependencies are not needed
# to test how a confirmed no-speech outcome is serialized for the Go worker.
for name in ("faster_whisper", "localize", "localize_fast", "render_subtitles"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["faster_whisper"].WhisperModel = object
sys.modules["localize"].log = lambda *_args: None

module_path = Path(__file__).with_name("localize_subtitles.py")
spec = importlib.util.spec_from_file_location("tested_localize_subtitles", module_path)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SubtitleNoSpeechTests(unittest.TestCase):
    def test_collect_segments_ignores_blank_transcripts(self):
        items = [
            types.SimpleNamespace(start=0.0, end=1.0, text="  "),
            types.SimpleNamespace(start=1.0, end=2.0, text=" hello "),
        ]
        self.assertEqual(module.collect_segments(items), [
            {"id": 1, "start": 1.0, "end": 2.0, "text": "hello"}
        ])

    def test_no_speech_returns_original_media_and_explicit_skip_reason(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "silent.mp4"
            source.write_bytes(b"not-a-video-fixture")
            metadata = Path(folder) / "silent.subtitle.json"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                module.emit_no_speech_result(
                    source, metadata, {"transcribe": 1.5}, 0.0, "vi"
                )
            result = json.loads(output.getvalue().strip())
            saved = json.loads(metadata.read_text(encoding="utf-8"))
            self.assertEqual(result["outputVideo"], str(source))
            self.assertEqual(saved["outputVideo"], str(source))
            self.assertEqual(result["skippedReason"], "no_speech")
            self.assertEqual(saved["skippedReason"], "no_speech")
            self.assertEqual(result["segments"], 0)
            self.assertEqual(saved["segments"], [])
            self.assertFalse(result["worker"])
            self.assertFalse((Path(folder) / "silent.vi-subbed.mp4").exists())


if __name__ == "__main__":
    unittest.main()
