# VideoGet

VideoGet là MVP tìm video theo từ khóa trên **Bilibili + Douyin**, xem metadata trước rồi mới tải video đã chọn.

## Tính năng hiện tại

- Search theo keyword trên nhiều source.
- Bilibili dùng `yt-dlp` với `bilisearch:`.
- Douyin dùng `douyin-cli` ở chế độ `search --no-download`.
- Chuẩn hóa kết quả về một model chung.
- UI web để search, tick video và tải.
- Download chạy theo job bất đồng bộ.
- Bilibili tải bằng `yt-dlp + FFmpeg`.
- Douyin tải bằng `douyin-cli`.
- Có Dockerfile và docker-compose.

## Kiến trúc

```text
Browser
  |
  v
Go HTTP API
  |
  +-- BilibiliProvider --> yt-dlp bilisearch:
  |
  +-- DouyinProvider ----> douyin-cli search --no-download
  |
  +-- Download Manager
        +-- Bilibili --> yt-dlp + FFmpeg
        +-- Douyin ----> douyin-cli
```

## Chạy bằng Docker

```bash
cp .env.example .env
```

Nếu chỉ dùng Bilibili thì có thể để `DOUYIN_COOKIE` trống. Để search Douyin, đăng nhập Douyin trên trình duyệt của bạn rồi đặt Cookie hiện tại vào `.env`:

```dotenv
DOUYIN_COOKIE=your_cookie_here
```

Không commit Cookie thật lên GitHub.

Sau đó:

```bash
docker compose up --build
```

Mở:

```text
http://localhost:8080
```

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

### Queue download

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

### Job status

```http
GET /api/jobs
GET /api/jobs/{id}
```

## Environment

| Variable | Default | Ý nghĩa |
|---|---|---|
| `ADDR` | `:8080` | HTTP listen address |
| `DOWNLOAD_DIR` | `downloads` | thư mục chứa video |
| `DOUYIN_BIN` | `douyin` | đường dẫn binary douyin-cli |
| `DOUYIN_COOKIE` | trống | Cookie Douyin của phiên đăng nhập của bạn |

## Hạn chế hiện tại

- Douyin có thể yêu cầu xác minh hoặc thay đổi API web bất kỳ lúc nào.
- Job queue đang lưu in-memory; restart app sẽ mất lịch sử job nhưng file tải vẫn còn.
- Chưa có database, Redis, ranking hay AI scoring.
- Chưa tự mở rộng keyword tiếng Trung.

## Hướng tiếp theo

```text
keyword
  -> keyword expansion
  -> multi-source search
  -> metadata filter
  -> trend/relevance scoring
  -> AI ranking
  -> selected download
  -> Whisper/Vision/FFmpeg pipeline
```

## Third-party

VideoGet gọi các tool sau như executable độc lập:

- https://github.com/yt-dlp/yt-dlp
- https://github.com/LIghtJUNction/douyin

`douyin-cli` upstream dùng AGPL-3.0. Hãy xem kỹ license và điều khoản nền tảng trước khi redistribute hoặc dùng thương mại.

Chỉ tải nội dung bạn có quyền truy cập/sử dụng và tôn trọng điều khoản nền tảng, bản quyền, quyền riêng tư và rate limit.
