# VideoGet

VideoGet là hệ thống tìm video theo từ khóa trên **Bilibili + Douyin**, xem metadata, tải video đã chọn rồi tự động **nhận giọng → tạo subtitle → dịch sang tiếng Việt → tạo voice tiếng Việt → render video Việt hóa**.

## Pipeline hiện tại

```text
Keyword
  -> Bilibili / Douyin search
  -> metadata results
  -> select video
  -> download
  -> faster-whisper STT
  -> subtitle gốc (.srt)
  -> dịch từng segment sang tiếng Việt
  -> subtitle tiếng Việt (.vi.srt)
  -> Vietnamese TTS
  -> timeline alignment
  -> FFmpeg render
  -> video.vi-dubbed.mp4
```

Mỗi job được tách riêng trong `downloads/<job-id>/` để nhiều video có thể chạy song song mà không giẫm file lên nhau.

## Thành phần

- **Go**: HTTP API, source adapters, job orchestration.
- **yt-dlp**: search/download Bilibili.
- **douyin-cli**: search/download Douyin.
- **faster-whisper**: speech-to-text local.
- **Ollama hoặc OpenAI-compatible endpoint**: dịch subtitle sang tiếng Việt.
- **edge-tts**: tạo giọng tiếng Việt.
- **FFmpeg**: tách audio, căn voice theo timeline, mix audio và hard-sub.

## Chạy bằng Docker

```bash
cp .env.example .env
```

### 1. Douyin

Nếu chỉ dùng Bilibili thì có thể để `DOUYIN_COOKIE` trống. Để search Douyin, đăng nhập Douyin trên trình duyệt của bạn rồi đặt Cookie hiện tại vào `.env`:

```dotenv
DOUYIN_COOKIE=your_cookie_here
```

Không commit Cookie thật lên GitHub.

### 2. Ollama để dịch sang tiếng Việt

Mặc định VideoGet gọi Ollama trên máy host:

```dotenv
TRANSLATE_PROVIDER=ollama
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_MODEL=qwen2.5:3b
```

Cài/pull model trước khi chạy job Việt hóa:

```bash
ollama pull qwen2.5:3b
```

Bạn có thể đổi sang model khác bằng `OLLAMA_MODEL`.

Nếu muốn dùng một API tương thích OpenAI thay cho Ollama:

```dotenv
TRANSLATE_PROVIDER=openai_compatible
OPENAI_COMPAT_BASE_URL=http://your-endpoint/v1
OPENAI_COMPAT_MODEL=your-model
OPENAI_COMPAT_API_KEY=your-key
```

### 3. Start

```bash
docker compose up --build
```

Mở:

```text
http://localhost:8080
```

Ở lần Việt hóa đầu tiên, `faster-whisper` sẽ tải model đã chọn và cache vào Docker volume `whisper-cache`.

`edge-tts` cần kết nối Internet để tổng hợp giọng nói. Mặc định voice là:

```dotenv
TTS_VOICE=vi-VN-HoaiMyNeural
```

## Output của một job

Ví dụ:

```text
downloads/
  <job-id>/
    original-video.mp4
    localized/
      original-video.original.srt
      original-video.vi.srt
      original-video.vi-voice.wav
      original-video.vi-dubbed.mp4
      original-video.localization.json
```

`localization.json` chứa timestamp, transcript gốc và bản dịch từng segment để sau này editor/AI agent có thể xử lý tiếp.

## Job status

```text
queued
  -> downloading
  -> localizing
  -> done
```

Nếu download thành công nhưng phần sub/voice lỗi:

```text
localization_failed
```

Video gốc vẫn được giữ lại trong `sourceOutput`.

## API

### Search

```http
POST /api/search
Content-Type: application/json

{
  "keyword": "AI Agent",
  "sources": ["bilibili", "douyin"],
  "limit": 20
}
```

Search chỉ lấy metadata, chưa tải media.

### Download + tự Việt hóa

```http
POST /api/download
Content-Type: application/json

{
  "video": {
    "id": "BV...",
    "platform": "bilibili",
    "title": "Example",
    "url": "https://www.bilibili.com/video/BV..."
  }
}
```

Khi `AUTO_LOCALIZE=true`, job tự chạy toàn bộ pipeline sau download.

### Job status

```http
GET /api/jobs
GET /api/jobs/{id}
```

Response hoàn tất có thêm:

```json
{
  "status": "done",
  "sourceOutput": "downloads/.../source.mp4",
  "output": "downloads/.../localized/source.vi-dubbed.mp4",
  "localization": {
    "originalSubtitle": "...original.srt",
    "vietnameseSubtitle": "...vi.srt",
    "voiceTrack": "...vi-voice.wav",
    "outputVideo": "...vi-dubbed.mp4",
    "detectedLanguage": "zh",
    "segments": 24
  }
}
```

## Environment

| Variable | Default | Ý nghĩa |
|---|---|---|
| `ADDR` | `:8080` | HTTP listen address |
| `DOWNLOAD_DIR` | `downloads` | thư mục chứa video/job |
| `DOUYIN_COOKIE` | trống | Cookie Douyin của phiên đăng nhập |
| `AUTO_LOCALIZE` | `true` | tự Việt hóa sau download |
| `WHISPER_MODEL` | `small` | model faster-whisper |
| `WHISPER_DEVICE` | `cpu` | `cpu` hoặc `cuda` nếu image/runtime phù hợp |
| `WHISPER_COMPUTE_TYPE` | `int8` | compute type của faster-whisper |
| `TRANSLATE_PROVIDER` | `ollama` | `ollama`, `openai`, `openai_compatible` |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Ollama endpoint |
| `OLLAMA_MODEL` | `qwen2.5:3b` | model dịch |
| `TTS_VOICE` | `vi-VN-HoaiMyNeural` | voice tiếng Việt |
| `TTS_RATE` | `+0%` | tốc độ TTS cơ bản |
| `TTS_MAX_SPEED` | `2.0` | mức tăng tốc tối đa khi voice dài hơn slot subtitle |
| `BURN_SUBTITLES` | `true` | hard-sub tiếng Việt vào video |
| `ORIGINAL_AUDIO_VOLUME` | `0.08` | âm lượng audio gốc dưới voice Việt; đặt `0` để bỏ hoàn toàn |

## Lưu ý chất lượng

Bản hiện tại dịch theo **segment Whisper**, vì vậy chạy được tự động và giữ timestamp tốt nhưng chưa phải dubbing cấp studio. Các bước nâng cấp hợp lý tiếp theo là:

- gom nhiều segment thành câu/đoạn theo ngữ nghĩa trước khi dịch;
- voice cloning / chọn speaker theo giới tính;
- source separation để giữ nhạc/ambient nhưng loại giọng gốc;
- scene-aware subtitle formatting;
- AI reviewer kiểm tra transcript, bản dịch và độ dài voice trước khi render;
- retry riêng từng stage thay vì chạy lại cả job.

## Third-party

VideoGet gọi/tích hợp các tool sau:

- `yt-dlp`
- `douyin-cli`
- `faster-whisper`
- `edge-tts`
- `FFmpeg`

`douyin-cli` upstream dùng AGPL-3.0. Hãy xem kỹ license và điều khoản nền tảng trước khi redistribute hoặc dùng thương mại.

Chỉ tải và biến đổi nội dung bạn có quyền truy cập/sử dụng; tôn trọng điều khoản nền tảng, bản quyền và quyền riêng tư.
