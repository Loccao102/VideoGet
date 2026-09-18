# Preset Cao / High / Workstation

Dùng `.env.high.example` cho máy:

- **20-32+ logical CPU threads**
- **32-64+ GB RAM**
- NVMe khuyến nghị
- cooling đủ tốt cho sustained CPU load
- GPU không bắt buộc, nhưng rất hữu ích nếu Ollama trên host có thể offload model lên GPU

> Máy 12 threads / 24 GB RAM không còn được xem là High. Với cấu hình đó nên dùng preset Medium.

## Mục tiêu

Preset High ưu tiên throughput và quality:

```env
LOCALIZE_CONCURRENCY=2
DOWNLOAD_CONCURRENCY=4
PREVIEW_CONCURRENCY=6

OCR_MODEL_SIZE=medium
OCR_FPS=4
OCR_MAX_SAMPLES=1200

OCR_DECODE_PROXY_PRESET=veryfast
OCR_DECODE_PROXY_THREADS=0

OLLAMA_MODEL=qwen3:8b
TRANSLATE_BATCH_SIZE=12
TRANSLATE_CONTEXT_SEGMENTS=4

WHISPER_MODEL=medium
WHISPER_CPU_THREADS=10
WHISPER_BEAM_SIZE=2

TTS_CONCURRENCY=4

VIDEO_PRESET=fast
VIDEO_CRF=20
```

## Cài model và chạy

```powershell
Copy-Item .env.high.example .env -Force
ollama pull qwen3:8b
docker compose up -d --build --force-recreate
```

## Vì sao cần nhiều CPU/RAM?

High có thể để 2 localization chạy cùng lúc. Mỗi job có thể cạnh tranh tài nguyên giữa:

```text
OpenCV / RapidOCR
FFmpeg decode/proxy
Qwen translation
FFmpeg overlay/render
```

Nếu Ollama cũng chạy CPU thì áp lực CPU/RAM còn lớn hơn.

Nếu sau khi chọn High mà từng job chậm hơn Medium, hãy hạ ngay:

```env
LOCALIZE_CONCURRENCY=1
```

Điều đó thường cho latency từng video tốt hơn dù tổng concurrency thấp hơn.

Xem giải thích chi tiết từng thông số tại [env-presets.md](./env-presets.md).
