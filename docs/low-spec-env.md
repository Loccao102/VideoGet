# Preset Yếu / Low

Dùng `.env.low.example` cho máy:

- **4-8 logical CPU threads**
- **8-16 GB RAM**
- GPU tích hợp hoặc không có GPU rời
- SSD được khuyến nghị
- Laptop văn phòng, mini PC, CPU U-series hoặc máy cần giữ độ responsive khi VideoGet chạy

## Mục tiêu

Preset này ưu tiên giảm concurrency và công việc thừa, **không hạ chất lượng encode OCR cuối để đổi lấy tốc độ**. OCR final dùng CRF 18; phần tiết kiệm thời gian đến từ direct decode + single-pass render.

Các giới hạn chính:

```env
LOCALIZE_CONCURRENCY=1
DOWNLOAD_CONCURRENCY=2
PREVIEW_CONCURRENCY=2

OCR_MODEL_SIZE=tiny
OCR_FPS=2
OCR_MAX_SAMPLES=360

OCR_RENDER_CRF=18
OCR_KEEP_SUBBED_INTERMEDIATE=false

OLLAMA_MODEL=qwen3:1.7b
TRANSLATE_BATCH_SIZE=6
TRANSLATE_CONTEXT_SEGMENTS=2

WHISPER_MODEL=small
WHISPER_CPU_THREADS=4

TTS_CONCURRENCY=1

VIDEO_PRESET=ultrafast
VIDEO_CRF=23
OCR_RENDER_CRF=18
```

## Cài model và chạy

```powershell
Copy-Item .env.low.example .env -Force
ollama pull qwen3:1.7b
docker compose up -d --build --force-recreate
```

## Nếu máy chỉ có 8 GB RAM

Có thể giảm thêm:

```env
DOWNLOAD_CONCURRENCY=1
PREVIEW_CONCURRENCY=1
OCR_FPS=1.5
WHISPER_MODEL=base
OLLAMA_KEEP_ALIVE=2m
```

Không nên tăng `LOCALIZE_CONCURRENCY` trên tier này.

Xem giải thích chi tiết từng thông số tại [env-presets.md](./env-presets.md).
