# Preset Vừa / Medium / Balanced

Dùng `.env.medium.example` cho máy:

- **8-16 logical CPU threads**
- **16-32 GB RAM**
- GPU không bắt buộc
- SSD/NVMe khuyến nghị
- Desktop dev phổ thông, laptop gaming, i5/i7 H-series, Ryzen 5/7 phổ thông

Đây cũng là cấu hình của `.env.example` và là tier được khuyến nghị cho đa số người dùng.

## Mục tiêu

Preset này cân bằng:

```text
chất lượng OCR tốt
+ dịch tự nhiên
+ máy vẫn responsive
+ không chạy quá nhiều localization đồng thời
```

Thông số chính:

```env
LOCALIZE_CONCURRENCY=1
DOWNLOAD_CONCURRENCY=3
PREVIEW_CONCURRENCY=4

OCR_MODEL_SIZE=small
OCR_FPS=3
OCR_MAX_SAMPLES=650

OCR_DECODE_PROXY_PRESET=veryfast
OCR_DECODE_PROXY_THREADS=4

OLLAMA_MODEL=qwen3:4b
TRANSLATE_BATCH_SIZE=8
TRANSLATE_CONTEXT_SEGMENTS=3

WHISPER_MODEL=small
WHISPER_CPU_THREADS=6

TTS_CONCURRENCY=2

VIDEO_PRESET=veryfast
VIDEO_CRF=21
```

## Cài model và chạy

```powershell
Copy-Item .env.medium.example .env -Force
ollama pull qwen3:4b
docker compose up -d --build --force-recreate
```

## Khi nào tăng lên High?

Chỉ nên chuyển sang High khi:

- CPU có khoảng **20+ logical threads**;
- RAM **32 GB trở lên**, tốt hơn 48-64 GB khi chạy nhiều job;
- máy có khả năng chạy tải nặng liên tục;
- hoặc Ollama được GPU tăng tốc đủ tốt và CPU còn dư cho OCR/FFmpeg.

Nếu máy 12-16 threads + 32 GB RAM nhưng CPU thường xuyên 90-100%, vẫn nên giữ Medium.

Xem giải thích chi tiết từng thông số tại [env-presets.md](./env-presets.md).
