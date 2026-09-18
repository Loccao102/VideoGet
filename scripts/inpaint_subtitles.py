#!/usr/bin/env python3
"""CPU subtitle inpainting for OCR-detected hard subtitles.

This helper does not run OCR. It consumes the timed per-segment bboxes already
stored by VideoGet and removes the source caption pixels before the Vietnamese
subtitle renderer runs.

Decode/encode is delegated to ffmpeg so AV1 sources do not depend on OpenCV's
video backend. OpenCV is used only for per-frame mask construction + TELEA
inpainting inside the small subtitle ROIs.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

import cv2
import numpy as np


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return max(minimum, default)


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except ValueError:
        return max(minimum, default)


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def ffprobe_video(path: Path) -> dict:
    process = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate,duration",
            "-of", "json", str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "ffprobe failed")
    payload = json.loads(process.stdout or "{}")
    streams = payload.get("streams") or []
    if not streams:
        raise RuntimeError("video stream not found")
    stream = streams[0]
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise RuntimeError("invalid source dimensions")

    def parse_rate(value: str | None) -> float:
        raw = str(value or "").strip()
        if not raw:
            return 0.0
        if "/" in raw:
            left, right = raw.split("/", 1)
            try:
                denom = float(right)
                return float(left) / denom if denom else 0.0
            except ValueError:
                return 0.0
        try:
            return float(raw)
        except ValueError:
            return 0.0

    fps = parse_rate(stream.get("avg_frame_rate")) or parse_rate(stream.get("r_frame_rate")) or 25.0
    return {"width": width, "height": height, "fps": fps}


def normalize_region(raw) -> tuple[float, float, float, float] | None:
    try:
        x, y, w, h = [float(value) for value in raw]
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    x = clamp(x, 0.0, 0.995)
    y = clamp(y, 0.0, 0.995)
    w = clamp(w, 0.005, 1.0 - x)
    h = clamp(h, 0.005, 1.0 - y)
    return x, y, w, h


def metadata_regions(metadata: dict) -> list[dict]:
    pad_x = env_float("OCR_INPAINT_PAD_X", 0.004, 0.0)
    pad_y = env_float("OCR_INPAINT_PAD_Y", 0.004, 0.0)
    out: list[dict] = []
    for raw in metadata.get("segments") or []:
        if not isinstance(raw, dict):
            continue
        region = normalize_region(raw.get("bbox"))
        if region is None:
            continue
        try:
            start = float(raw.get("start", 0.0))
            end = float(raw.get("end", start))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        x, y, w, h = region
        nx = clamp(x - pad_x, 0.0, 0.995)
        ny = clamp(y - pad_y, 0.0, 0.995)
        right = clamp(x + w + pad_x, nx + 0.005, 1.0)
        bottom = clamp(y + h + pad_y, ny + 0.005, 1.0)
        out.append({
            "start": max(0.0, start),
            "end": max(start, end),
            "region": (nx, ny, right - nx, bottom - ny),
        })
    out.sort(key=lambda item: item["start"])
    return out


def active_regions(segments: list[dict], timestamp: float) -> list[tuple[float, float, float, float]]:
    # Caption segment counts are normally small (< 250), so a straightforward
    # scan is cheaper and less error-prone than maintaining a frame-time index.
    return [
        item["region"] for item in segments
        if float(item["start"]) <= timestamp <= float(item["end"])
    ]


def _odd(value: int) -> int:
    value = max(1, int(value))
    return value if value % 2 else value + 1


def build_text_mask(roi: np.ndarray) -> np.ndarray:
    """Create a conservative stroke mask inside an already-tight OCR bbox."""
    height, width = roi.shape[:2]
    if height <= 1 or width <= 1:
        return np.zeros((height, width), dtype=np.uint8)

    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    blur_kernel = _odd(env_int("OCR_INPAINT_LOCAL_BLUR", 7, 3))
    local = cv2.GaussianBlur(gray, (blur_kernel, blur_kernel), 0)
    contrast = cv2.absdiff(gray, local)

    contrast_threshold = env_int("OCR_INPAINT_CONTRAST", 22, 1)
    bright_threshold = env_int("OCR_INPAINT_BRIGHT", 155, 0)
    dark_threshold = env_int("OCR_INPAINT_DARK", 90, 0)

    strong = contrast >= contrast_threshold
    tone = (gray >= bright_threshold) | (gray <= dark_threshold)
    mask = np.where(strong & tone, 255, 0).astype(np.uint8)

    # Subtitle glyphs often contain thin white strokes plus a dark outline.
    # Canny helps connect both without blanketing the whole bbox.
    canny_low = env_int("OCR_INPAINT_CANNY_LOW", 45, 1)
    canny_high = env_int("OCR_INPAINT_CANNY_HIGH", 135, canny_low + 1)
    edges = cv2.Canny(gray, canny_low, canny_high)
    mask = cv2.bitwise_or(mask, edges)

    dilate = env_int("OCR_INPAINT_DILATE", 3, 0)
    if dilate > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (_odd(dilate), _odd(dilate)))
        mask = cv2.dilate(mask, kernel, iterations=1)

    close_size = env_int("OCR_INPAINT_CLOSE", 3, 0)
    if close_size > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (_odd(close_size), _odd(close_size)))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    ratio = float(np.count_nonzero(mask)) / float(max(1, mask.size))
    min_ratio = env_float("OCR_INPAINT_MIN_MASK_RATIO", 0.01, 0.0)
    max_ratio = env_float("OCR_INPAINT_MAX_MASK_RATIO", 0.70, 0.01)

    if ratio < min_ratio and env_bool("OCR_INPAINT_FALLBACK_BOX", True):
        # Tight bbox fallback: use only the inner area so neighboring background
        # pixels remain available to TELEA as context.
        margin_x = max(1, int(width * 0.03))
        margin_y = max(1, int(height * 0.08))
        mask[:] = 0
        if width > margin_x * 2 and height > margin_y * 2:
            mask[margin_y:height - margin_y, margin_x:width - margin_x] = 255
    elif ratio > max_ratio:
        # Highly textured backgrounds can make edge detection too aggressive.
        # Erode once to avoid repainting the entire local scene.
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        mask = cv2.erode(mask, kernel, iterations=1)

    return mask


def inpaint_region(
    frame: np.ndarray,
    region: tuple[float, float, float, float],
) -> int:
    height, width = frame.shape[:2]
    x, y, w, h = region
    x0 = max(0, min(width - 1, int(round(x * width))))
    y0 = max(0, min(height - 1, int(round(y * height))))
    x1 = max(x0 + 1, min(width, int(round((x + w) * width))))
    y1 = max(y0 + 1, min(height, int(round((y + h) * height))))
    roi = frame[y0:y1, x0:x1]
    if roi.size == 0:
        return 0

    mode = os.getenv("OCR_INPAINT_MASK_MODE", "strokes").strip().lower()
    if mode == "box":
        mask = np.full(roi.shape[:2], 255, dtype=np.uint8)
        margin_x = max(1, int(mask.shape[1] * 0.02))
        margin_y = max(1, int(mask.shape[0] * 0.05))
        if mask.shape[1] > margin_x * 2 and mask.shape[0] > margin_y * 2:
            mask[:margin_y, :] = 0
            mask[-margin_y:, :] = 0
            mask[:, :margin_x] = 0
            mask[:, -margin_x:] = 0
    else:
        mask = build_text_mask(roi)

    if not np.any(mask):
        return 0

    radius = env_float("OCR_INPAINT_RADIUS", 3.0, 0.5)
    method_raw = os.getenv("OCR_INPAINT_METHOD", "telea").strip().lower()
    method = cv2.INPAINT_NS if method_raw in {"ns", "navier", "navier-stokes"} else cv2.INPAINT_TELEA
    frame[y0:y1, x0:x1] = cv2.inpaint(roi, mask, radius, method)
    return int(np.count_nonzero(mask))


def clean_video(input_path: Path, metadata_path: Path, output_path: Path) -> dict:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    segments = metadata_regions(metadata)
    if not segments:
        raise RuntimeError("OCR metadata has no timed segment bbox for inpainting")
    if not hasattr(cv2, "inpaint"):
        raise RuntimeError("OpenCV build has no cv2.inpaint")

    info = ffprobe_video(input_path)
    width, height, fps = info["width"], info["height"], info["fps"]
    frame_bytes = width * height * 3
    output_path.parent.mkdir(parents=True, exist_ok=True)

    decoder = subprocess.Popen(
        [
            "ffmpeg", "-v", "error", "-i", str(input_path),
            "-map", "0:v:0", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    preset = os.getenv("OCR_INPAINT_TEMP_PRESET", "veryfast").strip() or "veryfast"
    crf = os.getenv("OCR_INPAINT_TEMP_CRF", "14").strip() or "14"
    encoder = subprocess.Popen(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}", "-r", f"{fps:.8f}", "-i", "pipe:0",
            "-i", str(input_path),
            "-map", "0:v:0", "-map", "1:a:0?",
            "-c:v", "libx264", "-preset", preset, "-crf", crf,
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", "-shortest",
            str(output_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    if decoder.stdout is None or encoder.stdin is None:
        raise RuntimeError("could not open ffmpeg raw-video pipes")

    frames = 0
    touched_frames = 0
    masked_pixels = 0
    try:
        while True:
            raw = decoder.stdout.read(frame_bytes)
            if not raw:
                break
            if len(raw) != frame_bytes:
                raise RuntimeError(f"short decoded frame: {len(raw)}/{frame_bytes} bytes")
            frame = np.frombuffer(raw, dtype=np.uint8).reshape((height, width, 3)).copy()
            timestamp = frames / fps
            regions = active_regions(segments, timestamp)
            changed = 0
            for region in regions:
                changed += inpaint_region(frame, region)
            if changed:
                touched_frames += 1
                masked_pixels += changed
            encoder.stdin.write(frame.tobytes())
            frames += 1
    except BrokenPipeError as error:
        raise RuntimeError("ffmpeg inpaint encoder pipe closed early") from error
    finally:
        try:
            decoder.stdout.close()
        except Exception:
            pass
        try:
            encoder.stdin.close()
        except Exception:
            pass

    decoder_stderr = decoder.stderr.read().decode("utf-8", errors="replace") if decoder.stderr else ""
    encoder_stderr = encoder.stderr.read().decode("utf-8", errors="replace") if encoder.stderr else ""
    decoder_code = decoder.wait()
    encoder_code = encoder.wait()

    if decoder_code != 0:
        raise RuntimeError(f"ffmpeg decode failed: {decoder_stderr.strip()[:800]}")
    if encoder_code != 0:
        raise RuntimeError(f"ffmpeg inpaint encode failed: {encoder_stderr.strip()[:800]}")
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise RuntimeError("inpaint produced no usable video")

    return {
        "frames": frames,
        "touchedFrames": touched_frames,
        "maskedPixels": masked_pixels,
        "segments": len(segments),
        "fps": round(fps, 4),
        "width": width,
        "height": height,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = clean_video(
        Path(args.input).resolve(),
        Path(args.metadata).resolve(),
        Path(args.output).resolve(),
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", flush=True)
        raise SystemExit(1)
