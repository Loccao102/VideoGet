FROM rust:1.88-bookworm AS douyin-builder
RUN cargo install douyin-cli --locked --root /opt/douyin

FROM golang:1.23-bookworm AS go-builder
WORKDIR /src
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/videoget .

FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg nodejs && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir yt-dlp==2026.8.19
COPY --from=douyin-builder /opt/douyin/bin/douyin /usr/local/bin/douyin
COPY --from=go-builder /out/videoget /usr/local/bin/videoget
WORKDIR /app
RUN mkdir -p /app/downloads
ENV ADDR=:8080 DOWNLOAD_DIR=/app/downloads
EXPOSE 8080
VOLUME ["/app/downloads"]
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1
CMD ["videoget"]
