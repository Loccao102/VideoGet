# Low-spec / CPU-only preset

Use `.env.low.example` for machines with about 4-8 logical CPU threads and 8-16 GB RAM, including systems that only use integrated graphics.

```powershell
Copy-Item .env.low.example .env -Force
ollama pull qwen3:1.7b
docker compose up -d --build --force-recreate
```

The preset reduces concurrency, keeps Whisper on CPU/int8, uses `qwen3:1.7b` for local Ollama translation, uses PP-OCRv6 tiny at 2 FPS, limits the AV1 OCR compatibility proxy to 2 ffmpeg threads, lowers browser/search parallelism, disables keyword LLM expansion by default, and uses a fast CPU-oriented render preset.

For very weak CPUs or 8 GB RAM, also consider:

```env
WHISPER_MODEL=base
DOWNLOAD_CONCURRENCY=1
OCR_FPS=1.5
```

For a stronger machine, use `.env.high.example` instead. The complete low/high comparison is in `docs/env-presets.md`.
