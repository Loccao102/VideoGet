#!/usr/bin/env python3
"""Fast unit coverage for the subtitle-only no-speech result contract."""
import contextlib
import importlib.util
import io
import os
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


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

    def test_main_retries_silence_before_returning_original_video(self):
        class FakeModel:
            calls = []
            def __init__(self, *_args, **_kwargs):
                pass

            def transcribe(self, _audio, **kwargs):
                self.calls.append(kwargs)
                return iter([]), types.SimpleNamespace(language="en")

        with tempfile.TemporaryDirectory() as folder:
            input_path = Path(folder) / "silent.mp4"
            input_path.write_bytes(b"test fixture")
            output = Path(folder) / "out"
            fake = FakeModel()
            fake.calls = []
            buffer = io.StringIO()
            with (
                mock.patch.object(module, "WhisperModel", return_value=fake),
                mock.patch.object(module.base, "run", create=True),
                mock.patch.object(module.base, "log"),
                mock.patch.object(module.render_subtitles, "render", create=True) as render,
                mock.patch.object(sys, "argv", [
                    "localize_subtitles.py", "--input", str(input_path),
                    "--output-dir", str(output)
                ]),
                mock.patch.dict(os.environ, {"WHISPER_NO_SPEECH_RETRY": "true"}),
                contextlib.redirect_stdout(buffer),
            ):
                module.main()

            result = json.loads(buffer.getvalue().strip())
            self.assertEqual(len(fake.calls), 2)
            self.assertTrue(fake.calls[0]["vad_filter"])
            self.assertEqual(fake.calls[1]["vad_parameters"]["threshold"], 0.25)
            self.assertFalse(fake.calls[1]["condition_on_previous_text"])
            self.assertEqual(result["skippedReason"], "no_speech")
            self.assertEqual(result["outputVideo"], str(input_path))
            render.assert_not_called()
            self.assertFalse((output / "silent.original.srt").exists())

    def test_main_renders_if_quiet_speech_is_found_on_retry(self):
        class FakeModel:
            calls = 0
            def __init__(self, *_args, **_kwargs):
                pass

            def transcribe(self, _audio, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return iter([]), types.SimpleNamespace(language="en")
                return iter([
                    types.SimpleNamespace(start=0.4, end=1.2, text=" hello ")
                ]), types.SimpleNamespace(language="en")

        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "quiet.mp4"
            video.write_bytes(b"test fixture")
            output = Path(folder) / "out"
            model = FakeModel()
            srt_files = []
            rendered = []
            def write_srt(path, segments, key):
                srt_files.append((Path(path).name, len(segments), key))
                Path(path).write_text("mock", encoding="utf-8")
            def render(_source, _srt, destination):
                rendered.append(Path(destination).name)
                Path(destination).write_bytes(b"rendered fixture")
            stdout = io.StringIO()
            with (
                mock.patch.object(module, "WhisperModel", return_value=model),
                mock.patch.object(module.base, "run", create=True),
                mock.patch.object(module.base, "write_srt", write_srt, create=True),
                mock.patch.object(module.base, "translate_segments", lambda *_: None, create=True),
                mock.patch.object(module.base, "log"),
                mock.patch.object(module.render_subtitles, "render", render, create=True),
                mock.patch.object(sys, "argv", [
                    "localize_subtitles.py", "--input", str(video),
                    "--output-dir", str(output)
                ]),
                mock.patch.dict(os.environ, {"WHISPER_NO_SPEECH_RETRY": "true"}),
                contextlib.redirect_stdout(stdout),
            ):
                module.main()
            result = json.loads(stdout.getvalue().strip())
            self.assertEqual(model.calls, 2)
            self.assertEqual(result["segments"], 1)
            self.assertNotIn("skippedReason", result)
            self.assertEqual(rendered, ["quiet.vi-subbed.mp4"])
            self.assertEqual(srt_files, [
                ("quiet.original.srt", 1, "text"),
                ("quiet.vi.srt", 1, "vi")
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
