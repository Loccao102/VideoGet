# VideoGet

VideoGet là hệ thống **Affiliate Trend Hunter + Video Localization** cho Douyin/Bilibili:

```text
chủ đề tiếng Việt
  -> mở rộng keyword Trung
  -> search nhiều nguồn
  -> deduplicate
  -> filter
  -> trend/relevance scoring
  -> chọn Top N
  -> download
  -> faster-whisper
  -> subtitle gốc
  -> dịch tiếng Việt
  -> Vietnamese TTS
  -> FFmpeg render
  -> video.vi-dubbed.mp4
```

## Tính năng hiện tại

- Search theo keyword trên **Bilibili + Douyin**.
- Keyword expander deterministic cho một số ngách affiliate phổ biến.
- Tùy chọn dùng **Ollama** để sinh thêm keyword tiếng Trung cho chủ đề bất kỳ.
- Search đồng thời nhiều keyword/nhiều source.
- Deduplicate candidate theo platform + video ID.
- Filter theo min likes, thời gian đăng và thời lượng video.
- Chấm `engagement`, `recency`, `relevance`, `trend`, `overall` score.
- Sort theo Affiliate score / mới nhất / likes / views.
- Auto queue Top 3/5/10 hoặc chọn thủ công.
- Download video theo job riêng.
- Tự động nhận giọng → sub → dịch → voice Việt → render.

## Affiliate score

Bản V1 dùng heuristic có thể giải thích được:

```text
engagement = log(likes + 2*comments + 3*shares + 0.01*views)
recency    = exponential decay theo tuổi video
velocity   = engagement / age_hours
trend      = 45% engagement + 35% recency + 20% velocity
overall    = 50% trend + 30% relevance + 20% engagement
```

Đây là **ranking nội bộ giữa các candidate đã crawl**, không phải số liệu chính thức của Douyin/Bilibili.

## Chạy bằng Docker

```bash
cp .env.example .env
```

### Douyin Cookie

Để search/download Douyin, đăng nhập Douyin trên browser và đặt cookie hiện tại vào `.env`:

```dotenv
DOUYIN_COOKIE=your_cookie_here
```

Không commit cookie thật lên GitHub.

### Ollama

Mặc định Ollama được dùng cho **keyword expansion + dịch subtitle**:

```dotenv
KEYWORD_EXPANDER=ollama
TRANSLATE_PROVIDER=ollama
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_MODEL=qwen2.5:3b
```

Chuẩn bị model:

```bash
ollama pull qwen2.5:3b
```

Nếu Ollama keyword expansion lỗi, VideoGet vẫn search bằng keyword gốc + bộ mapping deterministic.

### Start

```bash
docker compose up --build
```

Mở:

```text
http://localhost:8080
```

## Search API

```http
POST /api/search
Content-Type: application/json

{
  "keyword": "đồ bếp",
  "sources": ["douyin", "bilibili"],
  "limit": 30,
  "expand": true,
  "sort": "rank",
  "filters": {
    "minLikes": 100,
    "maxDurationSec": 180,
    "publishedWithinDays": 7
  }
}
```

Response trả thêm các keyword thực sự đã crawl:

```json
{
  "keyword": "đồ bếp",
  "keywords": ["đồ bếp", "厨房好物", "厨房神器"],
  "results": [
    {
      "id": "...",
      "platform": "douyin",
      "title": "...",
      "likes": 12000,
      "scores": {
        "engagement": 87.3,
        "recency": 95.1,
        "relevance": 75,
        "trend": 89.2,
        "overall": 84.5
      }
    }
  ]
}
```

## Download + Việt hóa

```http
POST /api/download
Content-Type: application/json

{
  "video": {
    "id": "...",
    "platform": "douyin",
    "title": "Example",
    "url": "https://www.douyin.com/video/..."
  }
}
```

Khi `AUTO_LOCALIZE=true`:

```text
queued
 -> downloading
 -> localizing
 -> done
```

Nếu phần Việt hóa lỗi nhưng video đã tải xong:

```text
localization_failed
```

Video gốc vẫn được giữ lại.

## Output

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

## Environment chính

| Variable | Default | Ý nghĩa |
|---|---|---|
| `DOUYIN_COOKIE` | trống | cookie Douyin |
| `KEYWORD_EXPANDER` | `ollama` | `ollama` hoặc `off` |
| `AUTO_LOCALIZE` | `true` | tự Việt hóa sau download |
| `WHISPER_MODEL` | `small` | faster-whisper model |
| `TRANSLATE_PROVIDER` | `ollama` | provider dịch |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Ollama endpoint |
| `OLLAMA_MODEL` | `qwen2.5:3b` | model dùng cho AI task |
| `TTS_VOICE` | `vi-VN-HoaiMyNeural` | voice tiếng Việt |
| `BURN_SUBTITLES` | `true` | hard-sub vào video |
| `ORIGINAL_AUDIO_VOLUME` | `0.08` | âm lượng audio gốc dưới voice Việt |

## Kiến trúc

```text
Browser UI
   |
   v
Go API
   |
   +-- Discovery
   |     +-- static keyword expander
   |     +-- Ollama keyword expander
   |
   +-- Source adapters
   |     +-- Douyin -> douyin-cli
   |     +-- Bilibili -> yt-dlp
   |
   +-- Ranking
   |     +-- dedupe
   |     +-- filters
   |     +-- trend/relevance score
   |
   +-- Download Manager
         +-- isolated job directory
         +-- faster-whisper
         +-- translator
         +-- edge-tts
         +-- FFmpeg
```

## Hướng tiếp theo

- AI analyzer đọc transcript/metadata và trả `product`, `pain point`, `selling point`, `hook`, `affiliate suitability`.
- Lưu candidate + search history vào PostgreSQL.
- Theo dõi cùng keyword hằng ngày để tính **trend velocity theo lịch sử**, thay vì chỉ scoring trong một lần crawl.
- Demucs/source separation để bỏ voice gốc nhưng giữ nhạc/SFX.
- Retry từng stage và queue Redis.
- Matching sản phẩm tương đương từ sàn affiliate Việt Nam.

## Third-party

VideoGet tích hợp/call các tool: `yt-dlp`, `douyin-cli`, `faster-whisper`, `edge-tts`, `FFmpeg`, Ollama hoặc OpenAI-compatible endpoint.

`douyin-cli` upstream dùng AGPL-3.0. Hãy xem kỹ license và điều khoản nền tảng trước khi redistribute/dùng thương mại. Chỉ tải và biến đổi nội dung bạn có quyền sử dụng; tôn trọng bản quyền, quyền riêng tư và điều khoản nền tảng.
