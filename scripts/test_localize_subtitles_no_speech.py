#!/usr/bin/env python3
"""Fast unit coverage for the subtitle-only no-speech result contract."""
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
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
            {"id": 0, "start": 1.0, "end": 2.0, "text": "hello"}
        ])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools not installed")
    def test_real_video_only_mp4_probe_and_main_skip_without_loading_whisper(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "actual-video-only.mp4"
            output = Path(folder) / "out"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=32x32:d=0.3",
                "-an", "-c:v", "mpeg4", str(source)
            ], check=True, capture_output=True, text=True)
            self.assertFalse(module.input_has_audio_stream(source))
            stdout = io.StringIO()
            def real_ffmpeg(args):
                subprocess.run(args, check=True, capture_output=True, text=True)
            with (
                mock.patch.object(module.base, "run", real_ffmpeg, create=True),
                mock.patch.object(module.base, "log"),
                mock.patch.object(module, "WhisperModel") as whisper,
                mock.patch.object(sys, "argv", [
                    "localize_subtitles.py", "--input", str(source),
                    "--output-dir", str(output)
                ]),
                contextlib.redirect_stdout(stdout),
            ):
                module.main()
            result = json.loads(stdout.getvalue().strip())
            self.assertEqual(result["skippedReason"], "no_speech")
            self.assertEqual(result["outputVideo"], str(source))
            self.assertTrue((output / "actual-video-only.subtitle.json").exists())
            whisper.assert_not_called()

    def test_video_with_no_audio_stream_skips_whisper_and_returns_original(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "video-only.mp4"
            source.write_bytes(b"fixture")
            output = Path(folder) / "out"
            captured = io.StringIO()
            with (
                mock.patch.object(module.base, "run", side_effect=RuntimeError("no audio output"), create=True),
                mock.patch.object(module, "input_has_audio_stream", return_value=False) as probe,
                mock.patch.object(module, "input_has_video_stream", return_value=True) as video_probe,
                mock.patch.object(module, "WhisperModel") as model,
                mock.patch.object(module.base, "log"),
                mock.patch.object(sys, "argv", [
                    "localize_subtitles.py", "--input", str(source),
                    "--output-dir", str(output)
                ]),
                contextlib.redirect_stdout(captured),
            ):
                module.main()
            result = json.loads(captured.getvalue().strip())
            self.assertEqual(result["outputVideo"], str(source))
            self.assertEqual(result["skippedReason"], "no_speech")
            model.assert_not_called()
            probe.assert_called_once_with(source)
            video_probe.assert_called_once_with(source)
            self.assertEqual(json.loads((output / "video-only.subtitle.json").read_text())["segments"], [])

    def test_audio_extraction_failure_without_audio_or_video_does_not_claim_silence(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "not-a-video.mp4"
            source.write_bytes(b"corrupt fixture")
            output = Path(folder) / "out"
            with (
                mock.patch.object(module.base, "run", side_effect=RuntimeError("invalid input"), create=True),
                mock.patch.object(module, "input_has_audio_stream", return_value=False),
                mock.patch.object(module, "input_has_video_stream", return_value=False),
                mock.patch.object(module.base, "log"),
                mock.patch.object(sys, "argv", [
                    "localize_subtitles.py", "--input", str(source),
                    "--output-dir", str(output)
                ]),
            ):
                with self.assertRaisesRegex(RuntimeError, "invalid input"):
                    module.main()
            self.assertFalse((output / "not-a-video.subtitle.json").exists())

    def test_stream_probe_rejects_unexpected_stream_kinds(self):
        with self.assertRaisesRegex(ValueError, "audio or video"):
            module.input_has_stream(Path("fixture.mp4"), "s")

    def test_ffmpeg_error_with_audio_stream_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "bad.mp4"
            source.write_bytes(b"fixture")
            output = Path(folder) / "out"
            with (
                mock.patch.object(module.base, "run", side_effect=RuntimeError("decode failed"), create=True),
                mock.patch.object(module, "input_has_audio_stream", return_value=True),
                mock.patch.object(module.base, "log"),
                mock.patch.object(sys, "argv", [
                    "localize_subtitles.py", "--input", str(source),
                    "--output-dir", str(output)
                ]),
            ):
                with self.assertRaisesRegex(RuntimeError, "decode failed"):
                    module.main()
            self.assertFalse((output / "bad.subtitle.json").exists())

    def test_ffprobe_looks_only_for_audio_streams(self):
        with mock.patch.object(module.subprocess, "run",
                               return_value=types.SimpleNamespace(stdout="")) as probe:
            self.assertFalse(module.input_has_audio_stream(Path("/tmp/silent.mp4")))
            self.assertTrue(probe.call_args.kwargs["check"])
            self.assertIn("-select_streams", probe.call_args.args[0])
        with mock.patch.object(module.subprocess, "run",
                               return_value=types.SimpleNamespace(stdout="1\\n")):
            self.assertTrue(module.input_has_audio_stream(Path("/tmp/with-audio.mp4")))

    def test_no_speech_removes_artifacts_from_previous_runs_without_deleting_input(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "clip.mp4"
            source.write_bytes(b"original footage")
            output = root / "renders"
            output.mkdir()
            metadata = output / "clip.subtitle.json"
            old_original = output / "clip.original.srt"
            old_translated = output / "clip.vi.srt"
            old_rendered = output / "clip.vi-subbed.mp4"
            for artifact in (old_original, old_translated, old_rendered):
                artifact.write_text("outdated subtitles", encoding="utf-8")

            output_capture = io.StringIO()
            with mock.patch.object(module.base, "log"), contextlib.redirect_stdout(output_capture):
                module.emit_no_speech_result(
                    source, metadata, {}, 0.0,
                    derived_artifacts=(old_original, old_translated, old_rendered)
                )
            result = json.loads(output_capture.getvalue().strip())
            self.assertEqual(result["outputVideo"], str(source))
            self.assertEqual(result["skippedReason"], "no_speech")
            self.assertEqual(source.read_bytes(), b"original footage")
            self.assertTrue(metadata.is_file())
            for artifact in (old_original, old_translated, old_rendered):
                self.assertFalse(artifact.exists(), f"stale output should be deleted: {artifact}")

    def test_no_speech_cleanup_never_unlinks_original_input(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "clip.mp4"
            source.write_bytes(b"original footage")
            with self.assertRaisesRegex(ValueError, "original input video"):
                module.emit_no_speech_result(
                    source, Path(folder) / "clip.subtitle.json",
                    {}, 0.0, derived_artifacts=(source,)
                )
            self.assertEqual(source.read_bytes(), b"original footage")

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

    def test_collect_segments_reindexes_nonempty_subtitles_without_gaps(self):
        raw = [
            types.SimpleNamespace(start=0, end=1, text="hello"),
            types.SimpleNamespace(start=1, end=2, text="   "),
            types.SimpleNamespace(start=2, end=3, text="world"),
        ]
        self.assertEqual([s["id"] for s in module.collect_segments(raw)], [0, 1])

    def test_collect_segments_rejects_invalid_timestamps_instead_of_claiming_silence(self):
        for start, end in ((2, 1), (0, 0), (float("nan"), 1), (0, float("inf")), (-1, 2)):
            with self.subTest(start=start, end=end):
                raw = [types.SimpleNamespace(start=start, end=end, text="hello")]
                with self.assertRaisesRegex(ValueError, "invalid subtitle timestamps"):
                    module.collect_segments(raw)

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
