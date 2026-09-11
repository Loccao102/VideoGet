# VideoGet

VideoGet là hệ thống **Affiliate Trend Hunter + Video Localization** cho Douyin/Bilibili.

```text
chủ đề tiếng Việt
  -> mở rộng keyword Trung
  -> search Douyin / Bilibili
  -> deduplicate + filter + ranking
  -> queue download
  -> persistent faster-whisper worker
  -> dịch bằng Ollama
  -> Vietnamese TTS
  -> cleanup video
  -> burn subtitle Việt
  -> video.vi-dubbed.mp4
```

## Tính năng hiện tại

### Discovery
- Search **Douyin** qua `douyin-cli`.
- Search **Bilibili** qua `bilibili-cli`; request được throttle để giảm lỗi risk-control `-412`.
- Keyword expander deterministic + Ollama cho chủ đề tiếng Việt.
- Search nhiều keyword, deduplicate, filter và xếp hạng candidate.
- Score `engagement`, `recency`, `relevance`, `trend`, `overall`.
- Auto queue Top 3/5/10 hoặc chọn thủ công.

### Download
- Mỗi job có thư mục riêng.
- Bilibili download bằng `yt-dlp`, có nhiều format fallback, retry/backoff và kiểm tra audio bằng `ffprobe`.
- `DOWNLOAD_CONCURRENCY` giới hạn số download chạy đồng thời.

### Localization tối ưu hiệu năng
- `faster-whisper` tạo transcript/subtitle gốc.
- Whisper chạy trong **persistent Python worker**: model được load một lần và giữ trong RAM thay vì load lại cho từng video.
- Worker có thể prewarm ngay khi VideoGet khởi động để giảm latency của job đầu tiên.
- `WHISPER_BEAM_SIZE=1`, `int8` và cấu hình CPU threads ưu tiên tốc độ trên máy local.
- Ollama dịch Trung -> Việt; mặc định `qwen3:8b`, tắt thinking cho tác vụ dịch JSON.
- Translation retry + tự chia nhỏ batch lỗi.
- Edge TTS tạo voice Việt; nhiều segment được gộp thành nhóm và synthesize song song.
- `LOCALIZE_CONCURRENCY=1` mặc định để các Whisper job không tranh CPU/RAM.
- Video không nhận diện được speech được skip có kiểm soát thay vì làm hỏng toàn bộ queue.

### Stage cache
Artifact trung gian được cache theo source/config của từng stage:

```text
localized/
  *.transcript.json    # transcript + timestamp
  *.translated.json    # bản dịch Việt
  *.tts.json           # cấu hình voice đã dùng
  *.original.srt
  *.vi.srt
  *.vi-voice.wav
  *.vi-dubbed.mp4
  *.localization.json
```

Khi retry:
- source và Whisper config không đổi -> **skip Whisper**;
- translation provider/model không đổi -> **skip dịch**;
- voice/rate/grouping không đổi -> **skip TTS**;
- renderer vẫn có thể chạy lại để áp dụng cleanup/subtitle/color config mới.

Ví dụ một job lỗi ở render có thể retry theo đường:

```text
transcript cache hit
 -> translation cache hit
 -> TTS cache hit
 -> render lại
```

### Final render
- Giữ file `.srt` để chỉnh sửa/re-render.
- Tạo `*.vi-dubbed.mp4` đã burn subtitle Việt trực tiếp vào video.
- Có thể blur vùng subtitle nguồn đã burn sẵn và các vùng watermark/logo cấu hình.
- Color-grade nhẹ trước khi export.
- Mix voice Việt với audio gốc ở mức âm lượng cấu hình.

### Persistent/resumable jobs
Job không còn chỉ nằm trong memory. VideoGet lưu trạng thái vào **SQLite** tại:

```text
downloads/videoget.db
```

Do `downloads` được bind mount ra máy host nên database vẫn còn sau khi restart/recreate container.

Trạng thái job:

```text
queued -> downloading -> localizing -> done
                 |             |
                 v             v
              failed   localization_failed
```

Khi VideoGet khởi động lại:
- job đang `queued`, `downloading` hoặc `localizing` được đưa trở lại queue;
- nếu source video đã tải đầy đủ và còn audio, VideoGet tái sử dụng file đó thay vì tải lại;
- job `failed` / `localization_failed` không tự retry vô hạn; UI có nút **Thử lại**;
- retry localization dùng stage cache để tránh chạy lại công việc đã hoàn thành.

## Chạy bằng Docker

Lần đầu:

```powershell
Copy-Item .env.example .env
```

Sau khi đã có `.env`, **không copy lại** vì sẽ ghi đè cookie/cấu hình riêng của bạn.

### Ollama local

VideoGet chạy trong Docker còn Ollama chạy trên Windows:

```env
KEYWORD_EXPANDER=ollama
TRANSLATE_PROVIDER=ollama
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_MODEL=qwen3:8b
OLLAMA_KEEP_ALIVE=15m
```

Chuẩn bị model:

```powershell
ollama pull qwen3:8b
curl http://localhost:11434/api/tags
```

### Persistent Whisper worker

Mặc định Docker bật:

```env
LOCALIZE_PERSISTENT_WORKER=true
LOCALIZE_WORKER_SCRIPT=/app/scripts/localize_worker.py
LOCALIZE_WORKER_FALLBACK=true
LOCALIZE_WORKER_PREWARM=true
LOCALIZE_WORKER_START_TIMEOUT_SEC=600

WHISPER_MODEL=base
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
WHISPER_LANGUAGE=zh
WHISPER_BEAM_SIZE=1
WHISPER_CPU_THREADS=8
WHISPER_NUM_WORKERS=1
```

Nếu worker gặp lỗi hạ tầng, `LOCALIZE_WORKER_FALLBACK=true` cho phép VideoGet quay về `localize_fast.py` one-shot. Lỗi nội dung của chính job không bị chạy lại vô ích bằng fallback.

### Cookies

```env
DOUYIN_COOKIE=
BILIBILI_COOKIE=
```

Cookie phải lấy từ chính browser session mà bạn có quyền sử dụng. Không commit cookie thật lên GitHub.

### Start

```powershell
docker compose up --build
```

Mở:

```text
http://localhost:8080
```

Health:

```text
GET /api/health
```

`localization` trong health trả thêm trạng thái `persistentWorker`, `workerRunning`, số request, số lần start và thông tin model/device đang được giữ trong RAM.

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

Ví dụ score:

```json
{
  "scores": {
    "engagement": 87.3,
    "recency": 95.1,
    "relevance": 75,
    "trend": 89.2,
    "overall": 84.5
  }
}
```

Score hiện tại là heuristic nội bộ để so sánh các candidate đã crawl, không phải metric chính thức của Douyin/Bilibili.

## Download / Job API

Tạo job:

```http
POST /api/download
Content-Type: application/json

{
  "video": {
    "id": "...",
    "platform": "bilibili",
    "title": "Example",
    "url": "https://www.bilibili.com/video/..."
  }
}
```

Danh sách job:

```http
GET /api/jobs
```

Một job:

```http
GET /api/jobs/{id}
```

Retry job lỗi:

```http
POST /api/jobs/{id}/retry
```

Job hoàn tất có thể trả thêm số liệu:

```json
{
  "localization": {
    "worker": true,
    "cacheHits": ["transcript", "translation", "tts"],
    "timings": {
      "transcribe": 0.01,
      "translate": 0.01,
      "tts": 0.01,
      "render": 7.42,
      "total": 7.48
    }
  }
}
```

UI hiển thị các timing này để xác định bottleneck thực tế trên máy đang chạy.

## Output

```text
downloads/
  videoget.db
  <job-id>/
    original-video.mp4
    localized/
      original-video.transcript.json
      original-video.translated.json
      original-video.tts.json
      original-video.original.srt
      original-video.vi.srt
      original-video.vi-voice.wav
      original-video.vi-dubbed.mp4
      original-video.localization.json
```

`*.vi-dubbed.mp4` là file final đã render.

## Environment chính

| Variable | Default | Ý nghĩa |
|---|---|---|
| `JOB_DB_PATH` | `/app/downloads/videoget.db` | SQLite job store |
| `DOWNLOAD_CONCURRENCY` | `3` | download đồng thời |
| `JOB_TIMEOUT_MINUTES` | `180` | timeout toàn job |
| `DOUYIN_COOKIE` | trống | session Douyin |
| `BILIBILI_COOKIE` | trống | session Bilibili |
| `KEYWORD_EXPANDER` | `ollama` | keyword expansion |
| `AUTO_LOCALIZE` | `true` | tự Việt hóa sau download |
| `LOCALIZE_CONCURRENCY` | `1` | số localization chạy đồng thời |
| `LOCALIZE_PERSISTENT_WORKER` | `true` | giữ Whisper model trong RAM |
| `LOCALIZE_WORKER_PREWARM` | `true` | warm model lúc app start |
| `WHISPER_MODEL` | `base` | faster-whisper model |
| `WHISPER_LANGUAGE` | `zh` | ngôn ngữ nguồn ưu tiên |
| `WHISPER_BEAM_SIZE` | `1` | decode nhanh trên CPU |
| `WHISPER_CPU_THREADS` | `8` | CPU threads cho CTranslate2 |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Ollama local từ Docker |
| `OLLAMA_MODEL` | `qwen3:8b` | local LLM |
| `TTS_VOICE` | `vi-VN-HoaiMyNeural` | voice Việt |
| `TTS_RATE` | `+8%` | tốc độ TTS |
| `VIDEO_CLEANUP` | `true` | cleanup trước final render |
| `BURN_SUBTITLES` | `true` | hard-sub tiếng Việt |
| `ORIGINAL_AUDIO_VOLUME` | `0.08` | audio gốc dưới voice Việt |

## Kiến trúc hiện tại

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
   |     +-- Bilibili search -> bili CLI
   |     +-- Bilibili download -> yt-dlp
   |
   +-- Ranking
   |     +-- dedupe / filters / scores
   |
   +-- Persistent Job Manager
         +-- SQLite (WAL)
         +-- download semaphore
         +-- resume / retry
         |
         +-- Persistent Python Worker
               +-- faster-whisper loaded once
               +-- transcript cache
               +-- translation cache
               +-- TTS cache
               +-- Ollama translation
               +-- edge-tts
               +-- FFmpeg final render
```

## Ưu tiên tiếp theo

1. **Re-render API/UI**: đổi subtitle style, voice, cleanup và color grade trực tiếp từ UI mà không chạy lại stage không cần thiết.
2. **Media Library**: preview original/final, transcript, subtitle, timing và re-render.
3. **Affiliate Analyzer**: cluster nhiều video thành sản phẩm/ngách, phân tích pain point, hook, selling point và độ phù hợp affiliate Việt Nam.
4. **Render acceleration**: tùy phần cứng có thể thêm NVENC/Quick Sync profile thay cho `libx264` CPU.
5. Advanced OCR/inpainting chỉ bật khi cần xử lý text nguồn nằm rải rác trong frame.

## Third-party & sử dụng nội dung

VideoGet tích hợp/call `douyin-cli`, `bilibili-cli`, `yt-dlp`, `faster-whisper`, `edge-tts`, FFmpeg, Ollama và SQLite.

`douyin-cli` upstream dùng AGPL-3.0. Hãy xem kỹ license và điều khoản nền tảng trước khi redistribute/dùng thương mại. Chỉ tải và biến đổi nội dung bạn có quyền sử dụng; translation, dubbing hoặc xóa watermark không tự tạo quyền tái sử dụng nội dung.
