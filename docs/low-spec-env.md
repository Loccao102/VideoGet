# Low-spec / CPU-only preset

Use `.env.low.example` for machines that use integrated graphics or do not have NVIDIA CUDA.

```bash
cp .env.low.example .env
docker compose up -d --build
```

The preset reduces concurrency, keeps Whisper on CPU/int8, uses a smaller Ollama translation model, lowers browser/search parallelism, disables keyword LLM expansion by default, and uses a faster CPU-oriented render preset.

For very weak CPUs or 8 GB RAM, change `WHISPER_MODEL=base` and consider `OLLAMA_MODEL=qwen3:1.7b` or a cloud translation provider.
