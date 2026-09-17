# Hardware environment presets

VideoGet provides two ready-to-copy presets. Both are CPU-first and work without NVIDIA/CUDA.

| Preset | Recommended machine | Local model | Localization concurrency | OCR | Whisper | Render |
|---|---|---|---:|---|---|---|
| `.env.low.example` | 4-8 logical CPU threads, 8-16 GB RAM, integrated graphics | `qwen3:1.7b` | 1 | PP-OCRv6 tiny, 2 FPS | small / int8 / 4 threads | ultrafast, CRF 23 |
| `.env.high.example` | 12+ logical CPU threads, 24-32 GB RAM or more | `qwen3:8b` | 2 | PP-OCRv6 medium, 4 FPS | medium / int8 / 10 threads | fast, CRF 20 |

## Weak / low-spec machine

PowerShell:

```powershell
Copy-Item .env.low.example .env -Force
ollama pull qwen3:1.7b
docker compose up -d --build --force-recreate
```

Main characteristics:

- `LOCALIZE_CONCURRENCY=1`
- `DOWNLOAD_CONCURRENCY=2`
- `KEYWORD_EXPANDER=off`
- `OCR_MODEL_SIZE=tiny`
- `OCR_FPS=2`
- AV1 OCR compatibility proxy limited to 2 ffmpeg threads
- `WHISPER_MODEL=small`, CPU `int8`
- `OLLAMA_MODEL=qwen3:1.7b`
- `VIDEO_PRESET=ultrafast`

This preset is intended to keep the machine responsive while VideoGet runs.

## Strong / high-spec machine

PowerShell:

```powershell
Copy-Item .env.high.example .env -Force
ollama pull qwen3:8b
docker compose up -d --build --force-recreate
```

Main characteristics:

- `LOCALIZE_CONCURRENCY=2`
- `DOWNLOAD_CONCURRENCY=4`
- Ollama keyword expansion enabled
- `OCR_MODEL_SIZE=medium`
- `OCR_FPS=4`
- AV1 OCR proxy can use ffmpeg automatic thread count
- `WHISPER_MODEL=medium`, CPU `int8`, 10 CPU threads
- `OLLAMA_MODEL=qwen3:8b`
- larger translation batches and TTS concurrency
- more visual-analysis samples
- `VIDEO_PRESET=fast`, CRF 20 for higher output quality

## Important when switching presets

Copying a preset over `.env` replaces the current `.env`, including local cookies and API keys. Back up or copy these values back afterwards:

```env
DOUYIN_COOKIE=
BILIBILI_COOKIE=
OPENAI_COMPAT_API_KEY=
HF_TOKEN=
```

For an existing Docker image, environment-only changes normally only need a recreate:

```powershell
docker compose down
docker compose up -d --force-recreate
```

Use `--build` when the repository code or Dockerfile has also changed.

## Notes about GPU

The presets intentionally keep:

```env
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
```

because the current Docker setup does not automatically expose CUDA/Intel/AMD GPU acceleration to the localization pipeline. A high-spec preset therefore means more CPU/RAM throughput, not automatic GPU acceleration.

OCR-only modes remain lighter than full dubbing because they do not load Whisper or TTS:

```text
OCR -> Sub Việt
OCR -> Sub Việt + nhạc
```
