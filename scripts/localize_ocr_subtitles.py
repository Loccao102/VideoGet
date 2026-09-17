#!/usr/bin/env python3
"""Local OCR -> Vietnamese subtitles -> burn into the original video/audio.

This mode reuses the OCR/translation primitives from localize_ocr_music but stops
before music replacement. It never loads Whisper or TTS and preserves the source
audio track while covering the OCR-detected source-caption footprint and burning
Vietnamese subtitles.

Some short-video sources (notably Bilibili) may deliver AV1. OpenCV's bundled video
backend can fail to decode those streams even though the system ffmpeg can decode
AV1 in software. For OCR only, this script therefore creates a temporary H.264 proxy
when AV1 is detected or OpenCV cannot read the first frame. Final rendering still
uses the original downloaded source video and preserves its original audio.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path

import cv2

import localize as base
import localize_fast as fast  # noqa: F401 - installs optimized translation overrides on base
import localize_ocr_music as ocr
import ocr_segment_regions
import render_ocr_overlay


def video_codec(path: Path) -> str:
    process = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if process.returncode != 0:
        base.log(f"OCR codec probe failed; OpenCV decode check will decide fallback: {process.stderr.strip()[:400]}")
        return ""
    return process.stdout.strip().lower()


def opencv_can_decode(path: Path) -> bool:
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return False
        ok, frame = cap.read()
        return bool(ok and frame is not None and frame.size > 0)
    finally:
        cap.release()


def prepare_ocr_input(input_path: Path, output_dir: Path) -> tuple[Path, bool, str]:
    """Return an OpenCV-readable OCR source while keeping final render on input_path."""
    codec = video_codec(input_path)
    force_proxy = ocr.env_bool("OCR_FORCE_DECODE_PROXY", False)
    needs_proxy = force_proxy or codec in {"av1", "av01"}

    if not needs_proxy:
        needs_proxy = not opencv_can_decode(input_path)

    if not needs_proxy:
        return input_path, False, codec

    proxy = output_dir / f"{input_path.stem}.ocr-decode-proxy.mp4"
    preset = os.getenv("OCR_DECODE_PROXY_PRESET", "ultrafast").strip() or "ultrafast"
    crf = str(ocr.env_int("OCR_DECODE_PROXY_CRF", 28, 0))
    # Keep this numeric until the ffmpeg command is built. Converting it to str
    # before the comparison causes Python 3 to raise: str > int.
    threads = ocr.env_int("OCR_DECODE_PROXY_THREADS", 0, 0)

    reason = f"codec={codec or 'unknown'}" if codec else "OpenCV cannot decode source"
    if force_proxy:
        reason += ", forced by OCR_FORCE_DECODE_PROXY"
    base.log(f"OCR decode compatibility proxy required ({reason}); transcoding video-only H.264 proxy on CPU")

    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-hwaccel",
        "none",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        crf,
        "-pix_fmt",
        "yuv420p",
    ]
    if threads > 0:
        cmd += ["-threads", str(threads)]
    cmd += ["-movflags", "+faststart", str(proxy)]
    base.run(cmd)

    if not proxy.exists() or proxy.stat().st_size <= 0:
        raise RuntimeError("ffmpeg created no usable OCR decode proxy")
    if not opencv_can_decode(proxy):
        raise RuntimeError(
            "source video cannot be decoded by OpenCV even after H.264 compatibility transcoding; "
            "check ffmpeg AV1 software decoder support"
        )
    return proxy, True, codec


def infer_platform(input_path: Path) -> str:
    explicit = os.getenv("VIDEOGET_SOURCE_PLATFORM", "").strip().lower()
    if explicit:
        return explicit
    # VideoGet's Bilibili downloader keeps the BV id in the filename, e.g.
    # "title [BV1PkDhBXEVM].mp4". This gives the one-shot OCR process enough
    # information to apply Bilibili watermark cleanup without changing old callers.
    if re.search(r"\[bv[0-9a-z]+\]", input_path.name, flags=re.IGNORECASE):
        return "bilibili"
    if "bilibili" in input_path.name.lower():
        return "bilibili"
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(description="OCR source captions, translate to Vietnamese, keep original audio")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    stem = input_path.stem
    original_srt = output_dir / f"{stem}.ocr.original.srt"
    vi_srt = output_dir / f"{stem}.ocr.vi.srt"
    output_video = output_dir / f"{stem}.ocr-vi-subbed.mp4"
    metadata_path = output_dir / f"{stem}.ocr-subtitles.json"
    timings: dict[str, float] = {
        "transcribe": 0.0,
        "tts": 0.0,
        "music": 0.0,
        "decodeProxy": 0.0,
        "bbox": 0.0,
        "render": 0.0,
    }
    started = time.perf_counter()

    proxy_started = time.perf_counter()
    ocr_input, proxy_used, source_codec = prepare_ocr_input(input_path, output_dir)
    timings["decodeProxy"] = round(time.perf_counter() - proxy_started, 3) if proxy_used else 0.0

    bbox_attached = 0
    try:
        ocr_started = time.perf_counter()
        segments, boxes, video_info = ocr.extract_segments(ocr_input, output_dir)
        timings["ocr"] = round(time.perf_counter() - ocr_started, 3)

        if segments:
            bbox_started = time.perf_counter()
            try:
                bbox_attached = ocr_segment_regions.attach_segment_bboxes(
                    ocr_input, segments, video_info
                )
            except Exception as error:
                base.log(f"OCR segment bbox unavailable; global region fallback will be used: {error}")
            timings["bbox"] = round(time.perf_counter() - bbox_started, 3)
    finally:
        if proxy_used and ocr_input != input_path and not ocr.env_bool("OCR_KEEP_DECODE_PROXY", False):
            try:
                ocr_input.unlink(missing_ok=True)
            except OSError as error:
                base.log(f"Could not remove OCR decode proxy: {error}")

    if not segments:
        raise RuntimeError(
            "OCR did not detect timed subtitle text. Try lowering OCR_MIN_CONFIDENCE, "
            "increasing OCR_FPS, or set OCR_SUBTITLE_REGION=x,y,w,h."
        )
    base.write_srt(original_srt, segments, "text")

    translate_started = time.perf_counter()
    if ocr.env_bool("OCR_TRANSLATE", True):
        base.translate_segments(segments, os.getenv("OCR_SOURCE_LANGUAGE", "zh"))
    else:
        for segment in segments:
            segment["vi"] = segment["text"]
    timings["translate"] = round(time.perf_counter() - translate_started, 3)
    base.write_srt(vi_srt, segments, "vi")

    fallback_region = tuple(float(value) for value in video_info["region"])
    text_region = ocr.aggregate_text_region(boxes, fallback_region)
    platform = infer_platform(input_path)
    initial_render_style = os.getenv("OCR_SUBTITLE_INITIAL_RENDER_STYLE", "ocr_overlay").strip().lower() or "ocr_overlay"
    if initial_render_style not in {"ocr_overlay", "standard"}:
        raise RuntimeError("OCR_SUBTITLE_INITIAL_RENDER_STYLE must be ocr_overlay or standard")

    metadata = {
        "input": str(input_path),
        "mode": "ocr_subtitles",
        "detectedLanguage": os.getenv("OCR_SOURCE_LANGUAGE", "zh"),
        "segments": segments,
        "originalSubtitle": str(original_srt),
        "vietnameseSubtitle": str(vi_srt),
        "outputVideo": str(output_video),
        "preserveOriginalAudio": True,
        "sourceVideoCodec": source_codec,
        "sourcePlatform": platform,
        "ocrDecodeProxyUsed": proxy_used,
        "initialRenderStyle": initial_render_style,
        "ocr": {
            **video_info,
            "modelSize": os.getenv("OCR_MODEL_SIZE", "small"),
            "minConfidence": ocr.env_float("OCR_MIN_CONFIDENCE", 0.65),
            "textRegion": text_region,
            "segmentBBoxCount": bbox_attached,
        },
        "timings": timings,
    }
    # Overlay rendering consumes this metadata, so persist it before the render pass.
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    render_started = time.perf_counter()
    # Final render deliberately uses input_path, not the compatibility proxy, so the
    # source audio is preserved and the proxy never becomes the user's final media.
    if initial_render_style == "ocr_overlay":
        render_ocr_overlay.render(input_path, vi_srt, metadata_path, output_video, platform)
    else:
        ocr.render_ocr_subtitles(input_path, vi_srt, output_video, text_region)
    timings["render"] = round(time.perf_counter() - render_started, 3)
    timings["total"] = round(time.perf_counter() - started, 3)
    metadata["timings"] = timings
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "originalSubtitle": str(original_srt),
        "vietnameseSubtitle": str(vi_srt),
        "outputVideo": str(output_video),
        "detectedLanguage": os.getenv("OCR_SOURCE_LANGUAGE", "zh"),
        "segments": len(segments),
        "timings": timings,
        "worker": False,
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        base.log(f"ERROR: {error}")
        raise SystemExit(1)
