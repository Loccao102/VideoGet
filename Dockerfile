FROM rust:1.88-bookworm AS douyin-builder
RUN cargo install douyin-cli --locked --root /opt/douyin

FROM golang:1.26-bookworm AS go-builder
WORKDIR /src
RUN go install github.com/tamnd/bilibili-cli/cmd/bili@v0.3.0
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/videoget .

FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        fonts-noto-core \
        nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    yt-dlp==2026.8.19 \
    faster-whisper \
    edge-tts \
    pydub

COPY --from=douyin-builder /opt/douyin/bin/douyin /usr/local/bin/douyin
COPY --from=go-builder /go/bin/bili /usr/local/bin/bili
COPY --from=go-builder /out/videoget /usr/local/bin/videoget
COPY scripts /app/scripts

WORKDIR /app
RUN mkdir -p /app/downloads /root/.cache

ENV ADDR=:8080 \
    DOWNLOAD_DIR=/app/downloads \
    BILIBILI_BIN=bili \
    BILIBILI_SEARCH_DELAY_MS=1200 \
    BILIBILI_REQUEST_RATE=800ms \
    BILIBILI_RETRIES=2 \
    AUTO_LOCALIZE=true \
    LOCALIZE_SCRIPT=/app/scripts/localize_fast.py \
    LOCALIZE_CONCURRENCY=1 \
    WHISPER_MODEL=base \
    WHISPER_DEVICE=cpu \
    WHISPER_COMPUTE_TYPE=int8 \
    WHISPER_LANGUAGE=zh \
    WHISPER_BEAM_SIZE=1 \
    TRANSLATE_PROVIDER=ollama \
    TRANSLATE_BATCH_SIZE=12 \
    TRANSLATE_TIMEOUT_SEC=180 \
    TRANSLATE_RETRIES=2 \
    OLLAMA_BASE_URL=http://host.docker.internal:11434 \
    OLLAMA_MODEL=qwen3:8b \
    OLLAMA_KEEP_ALIVE=15m \
    TTS_VOICE=vi-VN-HoaiMyNeural \
    TTS_RATE=+8% \
    TTS_CONCURRENCY=4 \
    TTS_GROUP_MAX_CHARS=220 \
    TTS_GROUP_MAX_DURATION_SEC=12 \
    BURN_SUBTITLES=true \
    ORIGINAL_AUDIO_VOLUME=0.08

EXPOSE 8080
VOLUME ["/app/downloads", "/root/.cache"]
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1
CMD ["videoget"]
