#!/usr/bin/env python3
"""Isolated safety tests for the persistent localization worker."""
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

# Persistent Whisper and translation dependencies are intentionally not loaded.
for name in ("faster_whisper", "localize", "localize_fast"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["faster_whisper"].WhisperModel = object
sys.modules["localize"].log = lambda *_args: None

spec = importlib.util.spec_from_file_location(
    "tested_localize_worker", Path(__file__).with_name("localize_worker.py")
)
assert spec and spec.loader
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class WorkerOutputSafetyTests(unittest.TestCase):
    def test_whisper_timestamps_must_be_finite_positive_and_ordered(self):
        for start, end in (
            (-1.0, 2.0), (2.0, 1.0), (0.0, 0.0),
            (float("nan"), 2.0), (0.1, float("inf")),
        ):
            with self.subTest(start=start, end=end):
                segment = types.SimpleNamespace(start=start, end=end, text="hello")
                model = mock.Mock()
                model.transcribe.return_value = (
                    iter([segment]), types.SimpleNamespace(language="en")
                )
                with self.assertRaisesRegex(ValueError, "invalid segment timestamps"):
                    worker.decode_segments(model, Path("fixture.mp4"), "en", 1)

        model = mock.Mock()
        model.transcribe.return_value = (
            iter([
                types.SimpleNamespace(start=0, end=0, text="   "),
                types.SimpleNamespace(start=0.4, end=1.5, text="hello"),
            ]), types.SimpleNamespace(language="en")
        )
        segments, _ = worker.decode_segments(model, Path("fixture.mp4"), "en", 1)
        self.assertEqual(segments[0]["id"], 0)
        self.assertEqual(segments[0]["start"], 0.4)
        self.assertEqual(segments[0]["end"], 1.5)

    def test_silent_retry_removes_previous_outputs_but_never_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "clip.mp4"
            source.write_bytes(b"original source")
            output = root / "out"
            output.mkdir()
            stale = [
                output / "clip.original.srt",
                output / "clip.vi.srt",
                output / "clip.vi-voice.wav",
                output / "clip.vi-dubbed.mp4",
                output / "clip.localization.json",
            ]
            for path in stale:
                path.write_bytes(b"stale data")
            with (
                mock.patch.object(worker.base, "has_audio_stream", return_value=True, create=True),
                mock.patch.object(worker, "transcribe",
                                  return_value=([], "en", stale[0], False)),
            ):
                result = worker.process_job(object(), str(source), str(output))
            self.assertEqual(result["skippedReason"], "no_speech")
            self.assertEqual(result["outputVideo"], str(source))
            self.assertEqual(source.read_bytes(), b"original source")
            for path in stale:
                self.assertFalse(path.exists(), str(path))

    def test_source_symlink_is_preflight_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            artifact = root / "artifact.mp4"
            artifact.symlink_to(source)
            other = root / "other.srt"
            other.write_text("keep me", encoding="utf8")
            with self.assertRaisesRegex(ValueError, "original input video"):
                worker.remove_partial_outputs(source, (other, artifact))
            self.assertTrue(other.exists())
            self.assertEqual(source.read_bytes(), b"source")

    def run_render_case(self, failure: str | None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "clip.mp4"
            source.write_bytes(b"source bytes")
            output = root / "out"
            output.mkdir()
            metadata = output / "clip.localization.json"
            metadata.write_text("stale-success", encoding="utf8")
            rendered = output / "clip.vi-dubbed.mp4"
            rendered.write_bytes(b"old output")
            def render(_src, _voice, _srt, destination):
                Path(destination).write_bytes(b"new render bytes")
                if failure == "exception":
                    raise RuntimeError("FFmpeg failed")
            with (
                mock.patch.object(worker.base, "has_audio_stream", return_value=True, create=True),
                mock.patch.object(worker, "transcribe", return_value=(
                    [{"id": 0, "start": 0.1, "end": 0.5, "text": "hello"}],
                    "en", output / "clip.original.srt", False)),
                mock.patch.object(worker, "translate", return_value=(
                    [{"id": 0, "start": 0.1, "end": 0.5, "text": "xin chao"}],
                    output / "clip.vi.srt", {"sig": 1}, False)),
                mock.patch.object(worker, "synthesize", return_value=(
                    output / "clip.vi-voice.wav", {"sig": 2}, False)),
                mock.patch.object(worker.fast, "render_video", side_effect=render, create=True),
                mock.patch.object(worker, "rendered_video_is_usable",
                                  return_value=(failure is None)) as probe,
            ):
                if failure:
                    with self.assertRaisesRegex(RuntimeError, "FFmpeg failed|decodable video"):
                        worker.process_job(object(), str(source), str(output))
                    self.assertFalse(rendered.exists())
                    self.assertFalse(metadata.exists())
                else:
                    result = worker.process_job(object(), str(source), str(output))
                    self.assertEqual(result["outputVideo"], str(rendered))
                    self.assertTrue(metadata.is_file())
                    self.assertEqual(json.loads(metadata.read_text())["outputVideo"], str(rendered))
                    probe.assert_called_once_with(rendered)
            self.assertEqual(source.read_bytes(), b"source bytes")

    def test_renderer_exception_discards_partial_mp4_and_stale_metadata(self):
        self.run_render_case("exception")

    def test_undecodable_render_discards_partial_mp4_and_stale_metadata(self):
        self.run_render_case("undecodable")

    def test_success_requires_verified_output(self):
        self.run_render_case(None)


if __name__ == "__main__":
    unittest.main()
