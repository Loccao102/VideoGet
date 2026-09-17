# High-spec CPU/RAM preset

Use `.env.high.example` for a stronger desktop/workstation with roughly 12+ logical CPU threads and 24-32 GB RAM or more.

```powershell
Copy-Item .env.high.example .env -Force
ollama pull qwen3:8b
docker compose up -d --build --force-recreate
```

This preset raises download/preview/localization concurrency, enables Ollama keyword expansion, uses PP-OCRv6 medium at 4 FPS, allows the AV1 OCR compatibility proxy to use automatic ffmpeg thread count, upgrades Whisper to `medium`, uses `qwen3:8b`, increases translation/TTS batch sizes, and renders with a higher-quality `fast` preset at CRF 20.

The current Docker pipeline still defaults to CPU processing. This preset does not automatically enable CUDA, Intel Quick Sync, AMD AMF, or VAAPI.

See `docs/env-presets.md` for the full comparison with `.env.low.example`.
