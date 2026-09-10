#!/usr/bin/env python3
import argparse
import asyncio
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from faster_whisper import WhisperModel
from pydub import AudioSegment
import edge_tts


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def run(cmd: list[str]) -> None:
    process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or f"command failed: {' '.join(cmd)}")


def ffprobe_duration(path: Path) -> float:
    process = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or f"ffprobe failed for {path}")
    return float(process.stdout.strip())


def has_audio_stream(path: Path) -> bool:
    process = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return process.returncode == 0 and bool(process.stdout.strip())


def srt_time(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{milliseconds:03}"


def write_srt(path: Path, segments: list[dict], field: str) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for index, segment in enumerate(segments, start=1):
            text = str(segment.get(field, "")).strip().replace("\n", " ")
            if not text:
                continue
            handle.write(f"{index}\n")
            handle.write(f"{srt_time(segment['start'])} --> {srt_time(segment['end'])}\n")
            handle.write(text + "\n\n")


def http_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 180) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=data, headers=request_headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"translation endpoint returned HTTP {error.code}: {body[:1000]}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"cannot reach translation endpoint {url}: {error}") from error


def extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def translation_prompt(batch: list[dict], detected_language: str) -> str:
    source = [
        {"id": item["id"], "text": item["text"]}
        for item in batch
    ]
    return (
        "Bạn là bộ máy dịch phụ đề video. Dịch toàn bộ nội dung sang tiếng Việt tự nhiên, ngắn gọn, "
        "đúng ngữ cảnh nói, giữ nguyên tên riêng/thuật ngữ khi cần. Không giải thích. Không thêm thông tin. "
        "Giữ đúng số lượng phần tử và id. Chỉ trả JSON hợp lệ theo dạng "
        '{"translations":[{"id":0,"text":"..."}]}. '
        f"Ngôn ngữ nguồn được nhận diện: {detected_language or 'unknown'}. "
        "Dữ liệu cần dịch:\n"
        + json.dumps(source, ensure_ascii=False)
    )


def translate_batch_ollama(batch: list[dict], detected_language: str) -> list[dict]:
    base_url = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": "Return strict JSON only."},
            {"role": "user", "content": translation_prompt(batch, detected_language)},
        ],
        "options": {"temperature": 0},
    }
    response = http_json(base_url + "/api/chat", payload)
    content = response.get("message", {}).get("content", "")
    parsed = extract_json(content)
    return parsed.get("translations", [])


def translate_batch_openai(batch: list[dict], detected_language: str) -> list[dict]:
    base_url = os.getenv("OPENAI_COMPAT_BASE_URL", "http://host.docker.internal:11434/v1").rstrip("/")
    model = os.getenv("OPENAI_COMPAT_MODEL", os.getenv("OLLAMA_MODEL", "qwen2.5:3b"))
    api_key = os.getenv("OPENAI_COMPAT_API_KEY", "")
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "Return strict JSON only."},
            {"role": "user", "content": translation_prompt(batch, detected_language)},
        ],
    }
    response = http_json(base_url + "/chat/completions", payload, headers=headers)
    choices = response.get("choices") or []
    if not choices:
        raise RuntimeError("translation endpoint returned no choices")
    content = choices[0].get("message", {}).get("content", "")
    parsed = extract_json(content)
    return parsed.get("translations", [])


def translate_segments(segments: list[dict], detected_language: str) -> None:
    if detected_language.lower().startswith("vi"):
        for segment in segments:
            segment["vi"] = segment["text"]
        return

    provider = os.getenv("TRANSLATE_PROVIDER", "ollama").strip().lower()
    batch_size = max(1, int(os.getenv("TRANSLATE_BATCH_SIZE", "12")))

    if provider not in {"ollama", "openai", "openai_compatible"}:
        raise RuntimeError(
            "TRANSLATE_PROVIDER must be ollama, openai, or openai_compatible"
        )

    for start in range(0, len(segments), batch_size):
        chunk = segments[start : start + batch_size]
        batch = [
            {"id": item["id"], "text": item["text"]}
            for item in chunk
        ]
        if provider == "ollama":
            translated = translate_batch_ollama(batch, detected_language)
        else:
            translated = translate_batch_openai(batch, detected_language)

        lookup = {
            int(item["id"]): str(item.get("text", "")).strip()
            for item in translated
            if "id" in item
        }
        missing = [item["id"] for item in chunk if item["id"] not in lookup]
        if missing:
            raise RuntimeError(f"translator omitted segment ids: {missing}")
        for item in chunk:
            item["vi"] = lookup[item["id"]]
        log(f"Đã dịch {min(start + len(chunk), len(segments))}/{len(segments)} segment")


def atempo_filter(speed: float) -> str:
    if speed <= 1.0:
        return "atempo=1.0"
    factors: list[float] = []
    remaining = speed
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    factors.append(max(0.5, remaining))
    return ",".join(f"atempo={factor:.6f}" for factor in factors)


def fit_voice(source: Path, target: Path, target_ms: int) -> None:
    duration = ffprobe_duration(source)
    target_seconds = max(0.15, target_ms / 1000)
    speed = duration / target_seconds if duration > target_seconds else 1.0
    max_speed = max(1.0, float(os.getenv("TTS_MAX_SPEED", "2.0")))
    speed = min(speed, max_speed)
    cmd = ["ffmpeg", "-y", "-i", str(source), "-vn"]
    if speed > 1.01:
        cmd += ["-af", atempo_filter(speed)]
    cmd += ["-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(target)]
    run(cmd)


async def synthesize_segments(segments: list[dict], workdir: Path, total_ms: int) -> Path:
    voice = os.getenv("TTS_VOICE", "vi-VN-HoaiMyNeural")
    rate = os.getenv("TTS_RATE", "+0%")
    gain_db = float(os.getenv("TTS_GAIN_DB", "0"))
    track = AudioSegment.silent(duration=total_ms + 500, frame_rate=48000).set_channels(1)

    for index, segment in enumerate(segments):
        text = str(segment.get("vi", "")).strip()
        if not text:
            continue
        raw = workdir / f"tts_{index:05}.mp3"
        fitted = workdir / f"tts_{index:05}.wav"
        communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate)
        await communicate.save(str(raw))

        start_ms = max(0, int(segment["start"] * 1000))
        end_ms = max(start_ms + 150, int(segment["end"] * 1000))
        fit_voice(raw, fitted, end_ms - start_ms)
        clip = AudioSegment.from_file(fitted)
        slot_ms = end_ms - start_ms
        if len(clip) > slot_ms + 120:
            clip = clip[:slot_ms]
        if gain_db:
            clip += gain_db
        track = track.overlay(clip, position=start_ms)
        log(f"Đã tạo voice {index + 1}/{len(segments)}")

    voice_track = workdir / "voice_vi.wav"
    track[:total_ms].export(voice_track, format="wav")
    return voice_track


def escape_subtitle_path(path: Path) -> str:
    value = str(path.resolve())
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def render_video(input_path: Path, voice_track: Path, vi_srt: Path, output_path: Path) -> None:
    burn_subtitles = os.getenv("BURN_SUBTITLES", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    original_volume = max(0.0, float(os.getenv("ORIGINAL_AUDIO_VOLUME", "0.08")))
    source_has_audio = has_audio_stream(input_path)

    cmd = ["ffmpeg", "-y", "-i", str(input_path), "-i", str(voice_track)]
    filter_parts: list[str] = []

    if source_has_audio and original_volume > 0:
        filter_parts.append(f"[0:a]volume={original_volume}[original]")
        filter_parts.append("[original][1:a]amix=inputs=2:duration=first:normalize=0[aout]")
        audio_map = "[aout]"
    else:
        audio_map = "1:a:0"

    if burn_subtitles:
        escaped = escape_subtitle_path(vi_srt)
        filter_parts.append(
            "[0:v]subtitles='"
            + escaped
            + "':force_style='FontName=Noto Sans,FontSize=18,Outline=2,Shadow=0,MarginV=36'[vout]"
        )
        video_map = "[vout]"
    else:
        video_map = "0:v:0"

    if filter_parts:
        cmd += ["-filter_complex", ";".join(filter_parts)]
    cmd += ["-map", video_map, "-map", audio_map]

    if burn_subtitles:
        cmd += ["-c:v", "libx264", "-preset", os.getenv("VIDEO_PRESET", "medium"), "-crf", os.getenv("VIDEO_CRF", "20")]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(output_path)]
    run(cmd)


def main() -> None:
    parser = argparse.ArgumentParser(description="Transcribe, translate, dub and subtitle a video in Vietnamese")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    stem = input_path.stem
    original_srt = output_dir / f"{stem}.original.srt"
    vi_srt = output_dir / f"{stem}.vi.srt"
    output_video = output_dir / f"{stem}.vi-dubbed.mp4"
    metadata_path = output_dir / f"{stem}.localization.json"

    with tempfile.TemporaryDirectory(prefix="videoget-localize-") as temp:
        workdir = Path(temp)
        audio_path = workdir / "source.wav"
        log("Đang tách audio...")
        run([
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(audio_path),
        ])

        model_name = os.getenv("WHISPER_MODEL", "small")
        device = os.getenv("WHISPER_DEVICE", "cpu")
        compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "int8" if device == "cpu" else "float16")
        log(f"Đang transcribe bằng faster-whisper model={model_name}, device={device}...")
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        raw_segments, info = model.transcribe(
            str(audio_path),
            beam_size=int(os.getenv("WHISPER_BEAM_SIZE", "5")),
            vad_filter=True,
            condition_on_previous_text=False,
        )

        segments: list[dict] = []
        for index, segment in enumerate(raw_segments):
            text = segment.text.strip()
            if not text:
                continue
            segments.append(
                {
                    "id": index,
                    "start": float(segment.start),
                    "end": float(segment.end),
                    "text": text,
                }
            )

        if not segments:
            raise RuntimeError("Whisper did not detect any speech")

        detected_language = getattr(info, "language", "") or ""
        write_srt(original_srt, segments, "text")
        log(f"Đã nhận diện {len(segments)} segment, language={detected_language or 'unknown'}")

        translate_segments(segments, detected_language)
        write_srt(vi_srt, segments, "vi")

        total_ms = max(1000, int(math.ceil(ffprobe_duration(input_path) * 1000)))
        voice_track_temp = asyncio.run(synthesize_segments(segments, workdir, total_ms))
        voice_track = output_dir / f"{stem}.vi-voice.wav"
        shutil.copy2(voice_track_temp, voice_track)

        log("Đang ghép voice và subtitle vào video...")
        render_video(input_path, voice_track, vi_srt, output_video)

    metadata = {
        "input": str(input_path),
        "detectedLanguage": detected_language,
        "segments": segments,
        "originalSubtitle": str(original_srt),
        "vietnameseSubtitle": str(vi_srt),
        "voiceTrack": str(voice_track),
        "outputVideo": str(output_video),
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {
        "originalSubtitle": str(original_srt),
        "vietnameseSubtitle": str(vi_srt),
        "voiceTrack": str(voice_track),
        "outputVideo": str(output_video),
        "detectedLanguage": detected_language,
        "segments": len(segments),
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log(f"ERROR: {error}")
        sys.exit(1)
