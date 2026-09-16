FROM golang:1.26-bookworm AS go-builder
WORKDIR /src
RUN go install github.com/tamnd/bilibili-cli/cmd/bili@v0.3.0
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/videoget .

FROM python:3.12-slim-bookworm
# Docker Desktop/WSL can occasionally route Debian CDN IPv6 very slowly. Keep apt on IPv4,
# fail/retry stalled transfers, and persist apt indexes/packages in BuildKit caches between rebuilds.
RUN printf 'Acquire::ForceIPv4 "true";\nAcquire::Retries "5";\nAcquire::http::Timeout "20";\nAcquire::https::Timeout "20";\n' \
        > /etc/apt/apt.conf.d/99videoget-network
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        curl \
        ffmpeg \
        fonts-noto-core \
        fonts-noto-cjk \
        fonts-noto-color-emoji

# yt-dlp remains for the existing Bilibili/public-source branches. Douyin no longer calls it.
# Chromium is used only for Douyin native-search/browser fallback paths.
RUN pip install --no-cache-dir \
    yt-dlp==2026.8.19 \
    faster-whisper \
    edge-tts==7.2.8 \
    pydub \
    opencv-python-headless

COPY --from=go-builder /go/bin/bili /usr/local/bin/bili
COPY --from=go-builder /out/videoget /usr/local/bin/videoget
COPY scripts /app/scripts

WORKDIR /app
RUN mkdir -p /app/downloads /root/.cache

ENV PYTHONUNBUFFERED=1 \
    ADDR=:8080 \
    DOWNLOAD_DIR=/app/downloads \
    JOB_DB_PATH=/app/downloads/videoget.db \
    DOWNLOAD_CONCURRENCY=3 \
    JOB_TIMEOUT_MINUTES=180 \
    DOUYIN_SEARCH_TIMEOUT_SEC=20 \
    DOUYIN_NATIVE_SEARCH=true \
    DOUYIN_NATIVE_SEARCH_TIMEOUT_SEC=45 \
    DOUYIN_NATIVE_SEARCH_RENDER_MS=12000 \
    DOUYIN_NATIVE_SEARCH_DOM_MAX_MB=32 \
    DOUYIN_RESOLVE_TIMEOUT_SEC=18 \
    DOUYIN_PAGE_TIMEOUT_SEC=25 \
    DOUYIN_MEDIA_TIMEOUT_SEC=120 \
    DOUYIN_MEDIA_CANDIDATES=12 \
    DOUYIN_PREFER_ORIGINAL=false \
    DOUYIN_BROWSER_FALLBACK=true \
    DOUYIN_BROWSER_BIN=chromium \
    DOUYIN_BROWSER_TIMEOUT_SEC=45 \
    DOUYIN_BROWSER_RENDER_MS=10000 \
    DOUYIN_BROWSER_DOM_MAX_MB=24 \
    DOUYIN_BROWSER_NO_SANDBOX=true \
    BILIBILI_BIN=bili \
    BILIBILI_SEARCH_DELAY_MS=1200 \
    BILIBILI_REQUEST_RATE=800ms \
    BILIBILI_RETRIES=2 \
    BILIBILI_DOWNLOAD_ATTEMPTS=3 \
    AUTO_LOCALIZE=true \
    LOCALIZE_SCRIPT=/app/scripts/localize_smart.py \
    LOCALIZE_CONCURRENCY=1 \
    LOCALIZE_PERSISTENT_WORKER=true \
    LOCALIZE_WORKER_SCRIPT=/app/scripts/localize_worker_smart.py \
    LOCALIZE_WORKER_FALLBACK=true \
    LOCALIZE_WORKER_PREWARM=true \
    LOCALIZE_WORKER_START_TIMEOUT_SEC=600 \
    WHISPER_MODEL=small \
    WHISPER_DEVICE=cpu \
    WHISPER_COMPUTE_TYPE=int8 \
    WHISPER_LANGUAGE=zh \
    WHISPER_BEAM_SIZE=1 \
    WHISPER_CPU_THREADS=8 \
    WHISPER_NUM_WORKERS=1 \
    WHISPER_DIRECT_DECODE=true \
    WHISPER_CONDITION_PREVIOUS_TEXT=true \
    WHISPER_USE_TITLE_PROMPT=true \
    TRANSLATE_PROVIDER=ollama \
    TRANSLATE_STYLE=natural_social \
    TRANSLATE_BATCH_SIZE=10 \
    TRANSLATE_CONTEXT_SEGMENTS=3 \
    TRANSLATE_TEMPERATURE=0.15 \
    TRANSLATE_TIMEOUT_SEC=180 \
    TRANSLATE_RETRIES=2 \
    OLLAMA_BASE_URL=http://host.docker.internal:11434 \
    OLLAMA_MODEL=qwen3:8b \
    OLLAMA_KEEP_ALIVE=15m \
    TTS_VOICE=vi-VN-HoaiMyNeural \
    TTS_RATE=+8% \
    TTS_CONCURRENCY=2 \
    TTS_RETRIES=4 \
    TTS_REQUEST_TIMEOUT_SEC=75 \
    TTS_GROUP_MAX_CHARS=180 \
    TTS_GROUP_MAX_DURATION_SEC=10 \
    TTS_FALLBACK_MAX_CHARS=90 \
    BURN_SUBTITLES=true \
    VIDEO_CLEANUP=true \
    VIDEO_CLEANUP_MODE=smart \
    VIDEO_ANALYSIS_SAMPLES=12 \
    VIDEO_CLEANUP_SOURCE_SUBTITLES=true \
    VIDEO_CLEANUP_LOGOS=true \
    VIDEO_SOURCE_SUBTITLE_BLUR=8 \
    VIDEO_SUBTITLE_FONT=Noto\ Sans \
    VIDEO_SUBTITLE_BOX_ALPHA=0.58 \
    VIDEO_SUBTITLE_BOX_PADDING=7 \
    VIDEO_SUBTITLE_MAX_LINES=2 \
    VIDEO_COLOR_GRADE=true \
    VIDEO_PRESET=veryfast \
    VIDEO_CRF=21 \
    ORIGINAL_AUDIO_VOLUME=0.08

EXPOSE 8080
VOLUME ["/app/downloads", "/root/.cache"]
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1
CMD ["videoget"]
