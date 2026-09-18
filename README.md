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
- UI có hai chế độ: **Theo từ khóa** và **Theo kênh / creator**.
- **Theo từ khóa** giữ flow hiện tại: có thể mở rộng keyword Trung, search nhiều query, deduplicate, áp filter rồi ranking.
- **Theo kênh / creator** tắt keyword expansion, lấy candidate rộng hơn rồi chỉ giữ video có `Author` khớp chính xác tên kênh sau normalize (`@`, khoảng trắng, `_`/`-`). Video của author khác hoặc thiếu author bị loại.
- Search **Douyin** không phụ thuộc CLI riêng: dùng public web index để lấy canonical `douyin.com/video/...` URL; native search có thể trả author để dùng channel filter.
- Search **Bilibili** qua `bilibili-cli`; kết quả có `owner_name` nên hỗ trợ channel filter trực tiếp; request được throttle để giảm lỗi risk-control `-412`.
- Keyword expander deterministic + Ollama cho chủ đề tiếng Việt.
- Search nhiều keyword, deduplicate, filter và xếp hạng candidate.
- Score `engagement`, `recency`, `relevance`, `trend`, `overall`.
- Auto queue Top 3/5/10 hoặc chọn thủ công.

### Download
- Kết quả **Search** được đối chiếu với lịch sử job trong SQLite theo `platform + video id`, fallback URL. Mỗi card hiển thị rõ `Chưa tải`, `Đang tải / xử lý`, `Đã tải` hoặc `Lần trước bị lỗi`.
- Nếu source đã tải nhưng localization lỗi, card vẫn được đánh dấu **Đã tải · xử lý lỗi** để không nhầm với video chưa từng tải.
- Nút **Top N** tự bỏ qua video đã tải/đang xử lý; video đã tải vẫn có thể bấm thủ công để tạo một job mới khi muốn xử lý lại bằng mode/tỉ lệ khác.
- Mỗi job có thư mục riêng.
- **Douyin không dùng `douyin-cli` và cũng không dùng `yt-dlp` trong nhánh tải.**
- Douyin resolve `aweme_id`, request `https://www.iesdouyin.com/share/video/{id}/` bằng iPhone User-Agent, parse `window._ROUTER_DATA -> videoInfoRes.item_list[0]`, rồi tải trực tiếp `video.bit_rate` / `video.play_addr`.
- Nếu chỉ có internal video URI, VideoGet tự dựng `aweme.snssdk.com/aweme/v1/play/` theo 1080p -> 720p -> 540p -> default. Có thể ưu tiên `default` bằng `DOUYIN_PREFER_ORIGINAL=true`.
- Raw `<video src>` / media URL trong HTML chỉ là fallback cuối cùng khi cấu trúc `_ROUTER_DATA` thay đổi.
- Bilibili download bằng `yt-dlp`, có nhiều format fallback, retry/backoff và kiểm tra audio bằng `ffprobe`.
- `DOWNLOAD_CONCURRENCY` giới hạn số download chạy đồng thời.

> Lý do tách riêng Douyin: web detail API `www.douyin.com/aweme/v1/web/aweme/detail/` hiện có thể bị anti-bot trả response rỗng dù HTTP 200. Share page `iesdouyin.com` SSR là đường server-side chính; không phụ thuộc X-Bogus hay một Douyin CLI khác.

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
- OCR cleanup lưu bbox nhỏ theo từng detection/dòng chữ và blur riêng từng bbox; không còn bắt buộc gom cả caption thành một khung blur lớn.
- AV1 OCR proxy không còn cần giữ nguyên 1080p/30fps: proxy có thể downscale + giảm fps chỉ để OCR. Preset Low dùng 960px / 8fps / CRF 30 và xóa proxy ngay sau OCR; final render vẫn dùng source gốc.
- Bilibili brand detection tái sử dụng OCR-compatible input để tránh tạo thêm một AV1→H.264 proxy thứ hai trong cùng pipeline.
- Blur nguồn dùng multi-pass mạnh hơn để phá nét chữ sâu hơn nhưng giữ vùng ảnh bị tác động nhỏ.
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

## Preset phần cứng

VideoGet có 3 cấu hình phần cứng:

| Tier | File | CPU logical threads | RAM | OCR | Ollama |
|---|---|---:|---:|---|---|
| Yếu | `.env.low.example` | 4-8 | 8-16 GB | tiny / 2 FPS | qwen3:1.7b |
| Vừa / mặc định | `.env.medium.example` hoặc `.env.example` | 8-16 | 16-32 GB | small / 3 FPS | qwen3:4b |
| Cao / workstation | `.env.high.example` | 20-32+ | 32-64+ GB | medium / 4 FPS | qwen3:8b |

Chi tiết cách chọn máy, ý nghĩa `LOCALIZE_CONCURRENCY`, `OCR_FPS`, `OCR_MODEL_SIZE`, Ollama, Whisper, AV1 proxy và render nằm tại [docs/env-presets.md](docs/env-presets.md).

## Tỉ lệ output social

UI có option **Tỉ lệ xuất** cho từng job. Các lựa chọn hiện tại:

```text
Giữ nguyên
16:9
3:4
9:16
1:1
```

UI mặc định chọn **3:4** cho workflow TikTok / Reels / YouTube Shorts. Video nguồn luôn được giữ nguyên; VideoGet chỉ tạo thêm derivative ở cuối pipeline.

Với **job đã hoàn tất**, UI còn có **Xuất thêm tỉ lệ**. Có thể lấy cùng một bản video đã hoàn thiện và tạo thêm 3:4 / 9:16 / 16:9 / 1:1 mà **không chạy lại OCR, dịch, TTS hay render subtitle**. Các derivative đã tạo được lưu trong `aspectOutputs` của job; bản chính trong `output` không bị ghi đè.

VideoGet giữ riêng `renderedOutput` là bản hoàn chỉnh **trước bước đổi aspect**. Vì vậy khi tạo thêm tỉ lệ, hệ thống luôn xuất từ bản này thay vì lấy 3:4 rồi convert tiếp sang 9:16, tránh encode/crop chồng làm giảm chất lượng.

Chiến lược mặc định là `auto`:

- center-crop khi vẫn giữ được đủ phần khung nguồn;
- nếu crop quá mạnh thì chuyển sang **blur-fill** để giữ toàn bộ nội dung;
- `16:9 -> 3:4` hiện đủ ngưỡng để center-crop;
- `16:9 -> 9:16` thường fallback blur-fill để tránh cắt quá nhiều hai bên.


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
LOCALIZE_WORKER_SCRIPT=/app/scripts/localize_worker_smart.py
LOCALIZE_WORKER_FALLBACK=true
LOCALIZE_WORKER_PREWARM=true
LOCALIZE_WORKER_START_TIMEOUT_SEC=600

WHISPER_MODEL=small
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
WHISPER_LANGUAGE=zh
WHISPER_BEAM_SIZE=1
WHISPER_CPU_THREADS=8
WHISPER_NUM_WORKERS=1
```

Nếu worker gặp lỗi hạ tầng, `LOCALIZE_WORKER_FALLBACK=true` cho phép VideoGet quay về one-shot processor. Lỗi nội dung của chính job không bị chạy lại vô ích bằng fallback.

### Cookies

```env
DOUYIN_COOKIE=
BILIBILI_COOKIE=
```

Cookie phải lấy từ chính browser session mà bạn có quyền sử dụng. Không commit cookie thật lên GitHub.

Douyin không còn yêu cầu `douyin-cli` hoặc `yt-dlp`. Cookie là optional cho share-page resolver nhưng có thể tăng độ ổn định khi Douyin áp dụng risk-control.

Các biến Douyin chính:

```env
DOUYIN_MOBILE_USER_AGENT=Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) ...
DOUYIN_RESOLVE_TIMEOUT_SEC=18
DOUYIN_PAGE_TIMEOUT_SEC=25
DOUYIN_MEDIA_TIMEOUT_SEC=120
DOUYIN_MEDIA_CANDIDATES=12
DOUYIN_PREFER_ORIGINAL=false
```

`DOUYIN_PREFER_ORIGINAL=false` ưu tiên bitrate URL/1080p hợp lý hơn cho pipeline affiliate. Bật `true` nếu muốn thử stream `ratio=default` trước, có thể lớn hơn đáng kể.

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
  },
  "mode": "ocr_subtitles",
  "aspect": "3:4"
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

Tạo thêm một bản tỉ lệ khác từ **job đã hoàn tất**:

```http
POST /api/jobs/{id}/aspect/render
Content-Type: application/json

{
  "aspect": "9:16"
}
```

Endpoint này không chạy lại OCR/dịch/TTS/subtitle. Nó dùng `renderedOutput` trước bước đổi aspect và thêm kết quả vào `aspectOutputs`.

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
| `DOUYIN_COOKIE` | trống | session Douyin, optional |
| `DOUYIN_SEARCH_TIMEOUT_SEC` | `20` | timeout discovery Douyin public index |
| `DOUYIN_RESOLVE_TIMEOUT_SEC` | `18` | timeout resolve short link / aweme id |
| `DOUYIN_PAGE_TIMEOUT_SEC` | `25` | timeout lấy iesdouyin share page |
| `DOUYIN_MEDIA_TIMEOUT_SEC` | `120` | timeout tải media trực tiếp |
| `DOUYIN_MEDIA_CANDIDATES` | `12` | tối đa media candidate thử cho một video |
| `DOUYIN_PREFER_ORIGINAL` | `false` | ưu tiên ratio=default trước 1080p |
| `BILIBILI_COOKIE` | trống | session Bilibili |
| `KEYWORD_EXPANDER` | `ollama` | keyword expansion |
| `AUTO_LOCALIZE` | `true` | tự Việt hóa sau download |
| `LOCALIZE_CONCURRENCY` | `1` | số localization chạy đồng thời |
| `LOCALIZE_PERSISTENT_WORKER` | `true` | giữ Whisper model trong RAM |
| `LOCALIZE_WORKER_PREWARM` | `true` | warm model lúc app start |
| `WHISPER_MODEL` | `small` | faster-whisper model |
| `WHISPER_LANGUAGE` | `zh` | ngôn ngữ nguồn ưu tiên |
| `WHISPER_BEAM_SIZE` | `1` | decode nhanh trên CPU |
| `WHISPER_CPU_THREADS` | `8` | CPU threads cho CTranslate2 |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Ollama local từ Docker |
| `OLLAMA_MODEL` | `qwen3:8b` | local LLM |
| `TTS_VOICE` | `vi-VN-HoaiMyNeural` | voice Việt |
| `TTS_RATE` | `+8%` | tốc độ TTS |
| `VIDEO_CLEANUP` | `true` | cleanup trước final render |
| `BURN_SUBTITLES` | `true` | hard-sub tiếng Việt |
| `ASPECT_CONVERT_MODE` | `auto` | auto crop hoặc blur-fill cho tỉ lệ output |
| `ASPECT_CROP_MIN_RETAIN` | `0.40` | ngưỡng phần khung cần giữ để cho phép crop |
| `ASPECT_OUTPUT_CRF` | `18` | chất lượng encode derivative social |
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
   |     +-- Douyin search -> public web index
   |     +-- Douyin download -> iesdouyin _ROUTER_DATA -> direct CDN/play URL
   |     +-- Bilibili search -> bili CLI
   |     +-- Bilibili download -> yt-dlp
   |     +-- Other public sources -> existing public downloader
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
5. Nếu `iesdouyin _ROUTER_DATA` bị loại bỏ trong tương lai, thêm browser-network-interception fallback riêng cho Douyin thay vì làm browser trở thành dependency mặc định.

## Third-party & sử dụng nội dung

VideoGet tích hợp/call `bilibili-cli`, `yt-dlp` (cho các nhánh không phải Douyin), `faster-whisper`, `edge-tts`, FFmpeg, Ollama và SQLite. Douyin không còn dùng CLI riêng hoặc yt-dlp.

Hãy xem kỹ license và điều khoản của từng nền tảng/công cụ trước khi redistribute hoặc dùng thương mại. Chỉ tải và biến đổi nội dung bạn có quyền sử dụng; translation, dubbing hoặc xóa watermark không tự tạo quyền tái sử dụng nội dung.
