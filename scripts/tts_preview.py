#!/usr/bin/env python3
"""Generate one Vietnamese TTS preview without rendering the video."""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

import adaptive_tts
from pydub import AudioSegment


async def run(args) -> dict:
    text = adaptive_tts.clean_text(args.text)
    if not text:
        raise RuntimeError("preview text is empty")
    if len(text) > 1200:
        raise RuntimeError("preview text is too long")

    start = 0.0
    duration = max(0.5, float(args.duration))
    end = start + duration
    unit = {
        "text": text,
        "start": start,
        "end": end,
        "speechRate": args.speech_rate or "auto",
        "voice": args.voice or "",
        "voiceGender": args.voice_gender or "",
    }
    voice = adaptive_tts._voice_for_unit(unit)
    rate_percent = adaptive_tts.auto_rate_percent_for_slot(
        text, start, end, unit["speechRate"]
    )

    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="videoget-tts-preview-") as temp:
        first = Path(temp) / "preview.mp3"
        await adaptive_tts.synthesize_file(
            text,
            first,
            voice,
            adaptive_tts.edge_rate(rate_percent),
            "TTS preview",
        )
        clip = AudioSegment.from_file(first)
        measured_ms = len(clip)
        slot_ms = max(500, int(duration * 1000))
        # Match the production policy: if the first render is materially longer,
        # ask Edge TTS to speak faster once more instead of immediately time-warping.
        tolerance = adaptive_tts.env_int("TTS_EDITOR_OVERRUN_MS", 180, 0)
        if measured_ms > slot_ms + tolerance and str(unit["speechRate"]).lower() == "auto":
            ratio = measured_ms / max(500, slot_ms)
            max_rate = adaptive_tts.env_int("TTS_EDITOR_MAX_RATE_PERCENT", 70, 0)
            refined = min(max_rate, max(rate_percent, rate_percent + int(round((ratio - 1.0) * 100.0))))
            if refined > rate_percent:
                second = Path(temp) / "preview_refined.mp3"
                await adaptive_tts.synthesize_file(
                    text,
                    second,
                    voice,
                    adaptive_tts.edge_rate(refined),
                    "TTS preview refined",
                )
                first = second
                clip = AudioSegment.from_file(first)
                measured_ms = len(clip)
                rate_percent = refined
        output.write_bytes(first.read_bytes())

    return {
        "output": str(output),
        "voice": voice,
        "speechRate": adaptive_tts.edge_rate(rate_percent),
        "durationMs": measured_ms,
        "slotMs": int(duration * 1000),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate one Subtitle Studio TTS preview")
    parser.add_argument("--text", required=True)
    parser.add_argument("--duration", type=float, default=3.0)
    parser.add_argument("--speech-rate", default="auto")
    parser.add_argument("--voice", default="")
    parser.add_argument("--voice-gender", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False))


if __name__ == "__main__":
    main()
