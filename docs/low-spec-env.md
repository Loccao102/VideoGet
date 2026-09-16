# Low-spec / CPU-only preset

Use `.env.low.example` for machines that use integrated graphics or do not have NVIDIA CUDA.

```bash
cp .env.low.example .env
docker compose up -d --build
```

The preset reduces concurrency, keeps Whisper on CPU/int8, uses `qwen3:1.7b` for local Ollama translation, lowers browser/search parallelism, disables keyword LLM expansion by default, and uses a faster CPU-oriented render preset.

For very weak CPUs or 8 GB RAM, also consider `WHISPER_MODEL=base`. If `qwen3:1.7b` translation quality is not sufficient, switch to `qwen3:4b` or use a cloud translation provider.
