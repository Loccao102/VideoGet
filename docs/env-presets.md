# VideoGet hardware presets: Yếu / Vừa / Cao

VideoGet có 3 preset phần cứng rõ ràng. Mục tiêu của các preset là tránh tình trạng "máy mạnh hơn thì tăng mọi thông số" vì OCR, FFmpeg, Chromium, Whisper và Ollama có thể tranh CPU/RAM với nhau và làm throughput thực tế chậm hơn.

> **Logical CPU threads** là số luồng hệ điều hành nhìn thấy trong Task Manager / `lscpu`, không phải số core vật lý.

## 1. Chọn preset theo máy

| Tier | File | CPU logical threads | RAM | OCR strategy | Dịch chính | Repair khi khó | Localization |
|---|---|---:|---:|---|---|---|---:|
| **Yếu** | `.env.low.example` | 4-8 | 8-16 GB | `tiny` 2 FPS → chỉ segment đáng ngờ dùng `small` | `qwen3:1.7b` | `qwen3:4b` | 1 |
| **Vừa** | `.env.medium.example` | 8-16 | 16-32 GB | `small` 3 FPS → segment đáng ngờ dùng `medium` | `qwen3:4b` | `qwen3:8b` | 1 |
| **Cao** | `.env.high.example` | 20-32+ | 32-64+ GB | `medium` 4 FPS + 5 mẫu refinement | `qwen3:8b` | `qwen3:8b` | 2 |

`.env.example` hiện dùng cấu hình **Vừa / Balanced** vì đây là mức phù hợp nhất cho đa số máy dev hiện đại.

### Quy tắc chọn nhanh

- 4-8 logical threads hoặc RAM <= 16 GB: chọn **Yếu**.
- 8-16 logical threads và RAM 16-32 GB: chọn **Vừa**.
- Chỉ chọn **Cao** khi có khoảng 20+ logical threads và 32 GB RAM trở lên, tốt hơn là 48-64 GB nếu chạy nhiều job.
- Có RTX/AMD GPU không tự động biến preset thành High. Phần lớn OCR + OpenCV + libx264 hiện vẫn CPU-oriented. GPU chủ yếu có lợi nếu Ollama trên host thực sự dùng được GPU.

## 2. Cách dùng

### Yếu

```powershell
Copy-Item .env.low.example .env -Force
ollama pull qwen3:1.7b
ollama pull qwen3:4b
docker compose up -d --build --force-recreate
```

Model 4B chỉ dùng cho segment mà 1.7B dịch lỗi/echo tiếng Trung, không phải mọi batch.

### Vừa

```powershell
Copy-Item .env.medium.example .env -Force
ollama pull qwen3:4b
ollama pull qwen3:8b
docker compose up -d --build --force-recreate
```

8B chỉ là repair model nên chi phí trung bình vẫn gần profile 4B.

### Cao

```powershell
Copy-Item .env.high.example .env -Force
ollama pull qwen3:8b
docker compose up -d --build --force-recreate
```

> Copy preset sẽ ghi đè `.env`. Hãy giữ lại cookie/API key local trước khi copy.

Các giá trị cần lưu lại nếu đang dùng:

```env
DOUYIN_COOKIE=
BILIBILI_COOKIE=
OPENAI_COMPAT_API_KEY=
HF_TOKEN=
```

---

# 3. Những thông số ảnh hưởng hiệu năng nhiều nhất

## `LOCALIZE_CONCURRENCY`

Số video được phép vào pipeline localization cùng lúc.

Một job OCR/localization có thể đồng thời dùng:

```text
OpenCV / decode
→ RapidOCR
→ bbox detection
→ Ollama translation
→ FFmpeg blur/overlay/render
```

Do đó:

- `1`: an toàn cho Low và Medium.
- `2`: chỉ nên dùng ở High.
- Không nên tăng lên 3-4 chỉ vì máy có nhiều RAM; CPU và FFmpeg có thể tranh nhau.

Nếu CPU 100% liên tục và từng job chậm hơn khi chạy nhiều job, giảm biến này trước tiên.

## `DOWNLOAD_CONCURRENCY`

Số video tải đồng thời.

- Ăn network + disk nhiều hơn CPU.
- Low = 2, Medium = 3, High = 4.
- Nếu mạng nhanh nhưng ổ đĩa yếu, tăng quá cao vẫn có thể làm hệ thống ì.

## `PREVIEW_CONCURRENCY`

Số preview card được resolve đồng thời.

Preview có thể gọi HTTP, metadata parser hoặc Chromium fallback. Không trực tiếp ảnh hưởng chất lượng video.

- Low = 2
- Medium = 4
- High = 6

Nếu UI search làm máy nóng trước cả khi download, giảm biến này.

## `PUBLIC_INDEX_CONCURRENCY`

Số request discovery từ các nguồn public chạy song song.

- Tăng giúp search nhanh hơn.
- Tăng quá cao có thể làm network/risk-control kém ổn định.

---

# 4. OCR

## `OCR_MODEL_SIZE`

Model OCR chính.

```text
tiny   → nhẹ nhất
small  → cân bằng
medium → nặng nhất trong 3 preset
```

Preset:

- Low: `tiny`
- Medium: `small`
- High: `medium`

Model lớn hơn có thể đọc text nhỏ/mờ tốt hơn nhưng tốn CPU/RAM hơn. Với video Bilibili/Douyin chữ rõ, `small` thường là điểm cân bằng tốt.

## Adaptive quality thay vì hạ chất lượng toàn cục

Cả ba tier dùng cùng một quality contract:

```text
first-pass OCR
  -> multi-frame consensus
  -> segment nào confidence/consensus thấp mới refinement
  -> temporal cleanup envelope
  -> cover chữ nguồn
  -> contextual translation
  -> repair model nếu output còn tiếng Trung
```

Điểm khác biệt chỉ là **bao nhiêu tài nguyên được dùng trước khi escalation**.

Các biến chính:

```env
VIDEGET_QUALITY_TIER=low|medium|high
OCR_CONSENSUS_SIMILARITY=...
OCR_REFINEMENT_ENABLED=true
OCR_REFINEMENT_MODEL_SIZE=...
OCR_REFINEMENT_TRIGGER_CONFIDENCE=...
OCR_REFINEMENT_TRIGGER_CONSENSUS=...
OCR_REFINEMENT_MAX_SEGMENTS=...
OCR_REFINEMENT_SAMPLES=...
```

Low không còn chịu cảnh "tiny đọc sai thì dịch sai luôn": tiny chỉ là first-pass; segment đáng ngờ được đọc lại bằng small. Medium dùng small -> medium. High dùng medium và tăng số frame refinement.

### Multi-frame consensus

Một frame OCR confidence cao không còn tự động thắng. VideoGet gom nhiều observation gần nhau, cluster theo độ giống text, rồi ưu tiên cụm xuất hiện trên nhiều frame.

Ví dụ:

```text
frame 1: 渴死我了   0.76
frame 2: 渴死我了   0.79
frame 3: 谢谢我来这里 0.98
```

Kết quả consensus là `渴死我了`, không phải frame 3 chỉ vì confidence 0.98.

### Selective refinement

Chỉ segment có một trong các dấu hiệu sau mới tốn model lớn:

- confidence thấp;
- consensus thấp;
- quá ít observation.

Refinement dùng 1-5 frame nằm bên trong interval của segment. Nếu OpenCV không decode được AV1, VideoGet tự fallback sang FFmpeg frame decode; không tạo full-video proxy.

## `OCR_FPS`

Số frame OCR lấy trên mỗi giây video.

Ví dụ video 180 giây:

```text
2 FPS → ~360 frame
3 FPS → ~540 frame
4 FPS → ~720 frame
```

Tăng FPS gần như tăng trực tiếp số lần detect/recognize OCR.

Preset:

- Low: 1.5
- Medium: 3
- High: 4

Nếu caption thay đổi chậm, tăng lên 5-6 FPS thường không đáng với chi phí CPU.

## `OCR_MAX_SAMPLES`

Giới hạn cứng tổng số frame OCR cho một video.

- Low: 300
- Medium: 650
- High: 1200

Biến này đặc biệt quan trọng với video dài. Nếu không có giới hạn, một video dài có thể chạy OCR hàng nghìn frame.

## `OCR_MIN_CONFIDENCE`

Ngưỡng confidence để chấp nhận text OCR.

- Thấp hơn: bắt được nhiều text hơn nhưng tăng false-positive.
- Cao hơn: sạch hơn nhưng có thể bỏ sót chữ mờ.

Preset:

- Low: 0.60
- Medium: 0.62
- High: 0.65

## `OCR_MIN_DURATION_SEC`

Bỏ các detection tồn tại quá ngắn.

- Low: 0.40
- Medium: 0.35
- High: 0.30

Máy mạnh hơn lấy mẫu dày hơn nên có thể giữ segment ngắn hơn.

## `OCR_TEXT_SIMILARITY`

Ngưỡng để xem hai detection gần nhau có phải cùng caption hay không.

- Quá thấp: dễ merge nhầm.
- Quá cao: dễ tách một câu thành nhiều segment.

Preset đi từ 0.76 → 0.78 → 0.80.

## `OCR_MAX_GAP_SEC`

Khoảng trống tối đa giữa hai observation để còn coi chúng là cùng một đoạn.

Low dùng gap lớn hơn vì FPS thấp hơn. High dùng gap nhỏ hơn vì sampling dày hơn.

## OCR recovery khi lần đầu không tìm thấy subtitle

Nếu `OCR_SUBTITLE_REGION=auto` và pass đầu tiên trả về **0 timed segment**, VideoGet chạy đúng **một** recovery pass:

```env
OCR_RETRY_ON_EMPTY=true
OCR_RETRY_REGION=0.02,0.20,0.96,0.78
OCR_RETRY_MIN_CONFIDENCE=0.52   # Low
# 0.54 ở Medium / default
# 0.56 ở High
```

Recovery mở rộng ROI lên gần toàn bộ phần nội dung video và hạ confidence nhẹ để cứu các video có caption nằm ngoài vùng analyzer dự đoán. Nó chỉ chạy khi pass đầu tiên ra 0 segment, nên video bình thường không bị chậm gấp đôi.

Nếu người dùng đã đặt `OCR_SUBTITLE_REGION=x,y,w,h` thủ công, recovery **không ghi đè** vùng đó.

Ngoài ra face detection trong layout analyzer là best-effort. Nếu bản OpenCV/headless không có `CascadeClassifier`, VideoGet bỏ qua face protection nhưng vẫn tiếp tục phân tích subtitle/watermark thay vì làm hỏng toàn bộ layout analysis.

---

# 5. Bilibili watermark / branding

## `OCR_OVERLAY_BILIBILI_SAMPLES`

Số frame dùng để xác định watermark/uploader nằm bên trái hay bên phải.

- Low: 3
- Medium: 4
- High: 6

Đây không phải OCR caption chính; chỉ là sampling phụ để tìm text/logo tĩnh ở vùng trên.

## `OCR_OVERLAY_BILIBILI_BLUR`

Độ blur vùng watermark được phát hiện. Mặc định 15-17 là đủ để làm mất chữ nguồn trước khi đặt branding **Xứ Sở Nhiều Lông**.

## `OCR_OVERLAY_STYLE`

Không phải setting phần cứng chính nhưng ảnh hưởng render:

```env
OCR_OVERLAY_STYLE=clean
```

- `clean`: mặc định, blur chữ gốc + chữ Việt trắng đậm + outline/shadow.
- `capsule`: thêm nền mỏng bán trong suốt.
- `box`: box đen kiểu cũ.

### Temporal cleanup envelope + cover

OCR vẫn giữ `bbox` cho placement, nhưng cleanup không còn lấy duy nhất bbox của một frame đại diện. Các box thuộc **consensus thắng** được gom qua nhiều frame thành `cleanupRegions[]`.

```text
bbox
  -> placement subtitle Việt

cleanupRegions[]
  -> envelope ổn định qua nhiều frame
  -> padding ăn hết outline/stroke chữ
  -> cover nguồn trước khi vẽ sub Việt
```

Mặc định:

```env
OCR_SOURCE_CLEANUP_MODE=cover
OCR_SOURCE_CLEANUP_ALPHA=1.0
```

`cover` được chọn vì yêu cầu sản phẩm là **không còn đọc được chữ Trung**. `blur` và `hybrid` vẫn có thể bật thủ công nếu muốn giữ nền tự nhiên hơn, nhưng không có cùng guarantee.

Medium/default:

```env
OCR_OVERLAY_SOURCE_BLUR=14
OCR_OVERLAY_SOURCE_BLUR_POWER=3
OCR_SEGMENT_DETAIL_PAD_X=0.004
OCR_SEGMENT_DETAIL_PAD_Y=0.004
OCR_SEGMENT_DETAIL_MAX_BOXES=12
```

`OCR_OVERLAY_SOURCE_BLUR_POWER` tăng số pass boxblur, nên chữ nguồn bị phá nét mạnh hơn mà không cần tăng radius quá lớn. `OCR_SEGMENT_DETAIL_PAD_X/Y` chỉ nới nhẹ bbox OCR để ăn hết viền/outline của chữ; giữ nhỏ để tránh tạo cảm giác một mảng blur hình chữ nhật lớn.

Preset:
- Low: radius 12, power 2.
- Medium: radius 14, power 3.
- High: radius 16, power 3.

Job cũ chưa có `bboxRegions` vẫn fallback về union bbox cũ. Muốn job cũ dùng cleanup nhỏ mới, chạy **OCR lại** để sinh metadata chi tiết rồi render lại.

---

# 6. AV1 direct decode cho OCR

Bilibili thường trả video AV1: file nguồn có thể rất nhỏ nhưng nếu transcode toàn bộ sang H.264 chỉ để OpenCV đọc thì file tạm có thể phình nhiều lần.

Pipeline mới **không tạo full-video OCR proxy** nữa. FFmpeg decode source trực tiếp và pipe các frame đã sample sang OCR:

```text
AV1/H.264 source
  -> ffmpeg decode
  -> fps sampler
  -> raw BGR frame pipe
  -> RapidOCR
```

Frame OCR giữ nguyên độ phân giải source. Vì vậy tối ưu này giảm encode trung gian và I/O đĩa, không đổi resolution/fps của final và cũng không downscale frame OCR.

Trong cùng pass OCR, VideoGet giữ luôn:

```text
text
timestamp
bbox
bboxRegions
```

nên không cần một lượt OCR thứ hai chỉ để tìm bbox.

Các biến `OCR_DECODE_PROXY_*` cũ có thể còn trong file `.env` local sau khi upgrade nhưng không còn nằm trên đường chạy OCR chính; có thể bỏ chúng.


# 7. Ollama / Translation

## `OLLAMA_MODEL`

Primary model:

```text
Low    → qwen3:1.7b
Medium → qwen3:4b
High   → qwen3:8b
```

Repair model khi output còn tiếng Trung / guard fail:

```text
Low    → qwen3:4b
Medium → qwen3:8b
High   → qwen3:8b
```

Nhờ vậy máy yếu không phải chạy 4B cho mọi câu, nhưng cũng không chấp nhận chất lượng 1.7B ở các đoạn khó.

Đây là một trong những bottleneck lớn nhất nếu Ollama chạy hoàn toàn bằng CPU.

Nếu translation chậm nhưng OCR nhanh:

1. giảm model,
2. giảm context,
3. hoặc cấu hình Ollama dùng GPU nếu phần cứng hỗ trợ.

## `TRANSLATE_BATCH_SIZE`

Số subtitle segment gửi trong một batch.

- Low: 4
- Medium: 8
- High: 12

Batch lớn giảm overhead request/model nhưng cần nhiều RAM/context hơn.

## `TRANSLATE_CONTEXT_SEGMENTS`

Số segment lân cận dùng làm ngữ cảnh để dịch tự nhiên hơn.

- Low: 2
- Medium: 4
- High: 5

Tăng context không làm OCR tốt hơn; nó chỉ giúp dịch có ngữ cảnh hơn và làm prompt lớn hơn.

## `OLLAMA_KEEP_ALIVE`

Giữ model trong RAM/VRAM giữa các request.

- Low: 5m
- Medium: 20m
- High: 30m

Nếu RAM ít, giảm thời gian này. Nếu có nhiều job liên tục, tăng giúp tránh load model lặp lại.

---

# 8. Whisper

Whisper chỉ dùng cho mode dựa trên giọng nói:

```text
Giọng nói → Sub Việt
Sub + voice Việt
```

Hai mode OCR:

```text
OCR → Sub Việt
OCR → Sub Việt + nhạc
```

không cần Whisper/TTS.

## `WHISPER_MODEL`

- Low: `small`
- Medium: `small`
- High: `medium`

## `WHISPER_CPU_THREADS`

- Low: 4
- Medium: 6
- High: 10

Không nên đặt bằng toàn bộ logical threads nếu các stage khác cũng đang chạy.

## `WHISPER_BEAM_SIZE`

- 1: nhanh.
- 2+: tìm kiếm decode rộng hơn nhưng tốn CPU.

High dùng 2; Low/Medium dùng 1.

---

# 9. TTS

## `TTS_CONCURRENCY`

Số request/group TTS xử lý đồng thời.

- Low: 1
- Medium: 2
- High: 4

Không ảnh hưởng mode OCR-only.

---

# 10. Tỉ lệ output social

Mỗi job có thể chọn tỉ lệ output độc lập:

```text
original
16:9
3:4
9:16
1:1
```

UI mặc định chọn `3:4`, còn API cũ nếu không truyền `aspect` vẫn giữ nguyên source để tương thích ngược.

Video gốc luôn được giữ ở `SourceOutput`. Bước đổi tỉ lệ chỉ chạy **sau khi download/localization/render hoàn tất**, vì vậy retry OCR/subtitle vẫn sử dụng source gốc và không bị encode chồng nhiều lần.

Job đã hoàn tất còn giữ `RenderedOutput`: đây là bản nội dung cuối cùng trước bước đổi aspect. UI **Xuất thêm tỉ lệ** luôn dùng bản này để tạo derivative mới và lưu đường dẫn theo ratio trong `AspectOutputs`; không OCR/dịch/TTS/sub lại và không convert từ một derivative trước đó sang derivative tiếp theo.

## `ASPECT_CONVERT_MODE`

```env
ASPECT_CONVERT_MODE=auto
```

- `auto`: crop giữa nếu vẫn giữ đủ phần khung nguồn, nếu không thì blur-fill.
- `crop`: luôn crop vào target.
- `blur_fill`: luôn giữ toàn bộ frame và lấp phần thiếu bằng background blur.

## `ASPECT_CROP_MIN_RETAIN`

```env
ASPECT_CROP_MIN_RETAIN=0.40
```

Đây là tỉ lệ khung tối thiểu phải giữ được để `auto` cho phép crop.

Ví dụ:

```text
16:9 -> 3:4  giữ khoảng 42.2% chiều ngang -> crop
16:9 -> 9:16 giữ khoảng 31.6% chiều ngang -> blur-fill
```

Tăng lên `0.50-0.60` nếu muốn bảo thủ hơn và ưu tiên giữ toàn bộ cảnh.

## `ASPECT_OUTPUT_CRF`

Mode không phải OCR vẫn dùng `ASPECT_OUTPUT_CRF=18` cho derivative. OCR job không cần lượt encode aspect riêng: cleanup + sub + branding + aspect được gộp vào một pass và dùng `OCR_RENDER_CRF=18` ở cả Low/Medium/High.

## Target resolution

```text
16:9 -> 1920x1080
3:4  -> 1080x1440
9:16 -> 1080x1920
1:1  -> 1080x1080
```

Nếu source đã đúng tỉ lệ target, VideoGet copy file thay vì re-encode chỉ để đổi resolution.

---

# 11. Final render

## `OCR_RENDER_CRF`

OCR mode dùng một encode cuối duy nhất cho cleanup + sub + branding + aspect:

```env
OCR_RENDER_CRF=18
```

Giá trị này cố ý giống nhau giữa các preset. Tier Low giảm concurrency/model workload và bỏ encode thừa, **không tăng CRF của video OCR final**.

`OCR_RENDER_PRESET` để trống sẽ kế thừa `VIDEO_PRESET`. Preset encoder ảnh hưởng tốc độ/nén file; CRF 18 giữ target chất lượng hình.


## `VIDEO_PRESET`

libx264 preset kiểm soát trade-off thời gian encode và compression:

```text
ultrafast → nhanh nhất, file lớn hơn
veryfast  → cân bằng
fast      → chậm hơn, nén tốt hơn
```

Preset:

- Low: `ultrafast`
- Medium: `veryfast`
- High: `fast`

Nếu final render là stage chậm nhất, đổi `fast -> veryfast -> ultrafast`.

## `VIDEO_CRF`

Điều khiển chất lượng:

```text
CRF thấp hơn → chất lượng cao hơn + file lớn hơn
CRF cao hơn  → file nhỏ hơn + chất lượng thấp hơn
```

Preset:

- Low: 23
- Medium: 21
- High: 20

CRF chủ yếu ảnh hưởng chất lượng/file size; preset encoder mới ảnh hưởng thời gian render mạnh hơn.

## `VIDEO_ANALYSIS_SAMPLES`

Số frame dùng để phân tích layout/logo/cleanup.

- Low: 8
- Medium: 12
- High: 18

Không nhầm với `OCR_FPS`; biến này chỉ dùng visual analysis.

---

# 12. Nếu máy vẫn chậm thì giảm theo thứ tự nào?

## CPU 95-100% liên tục

Giảm theo thứ tự:

```env
LOCALIZE_CONCURRENCY=1
OCR_FPS=2
OCR_MODEL_SIZE=tiny
OCR_DECODE_PROXY_THREADS=2
VIDEO_PRESET=ultrafast
```

## RAM gần đầy / swap mạnh

Giảm:

```env
OLLAMA_MODEL=qwen3:1.7b
TRANSLATE_BATCH_SIZE=6
TRANSLATE_CONTEXT_SEGMENTS=2
OLLAMA_KEEP_ALIVE=5m
LOCALIZE_CONCURRENCY=1
```

## Search/UI đã lag trước khi xử lý video

Giảm:

```env
PREVIEW_CONCURRENCY=2
PUBLIC_INDEX_CONCURRENCY=3
DOWNLOAD_CONCURRENCY=2
```

## OCR nhanh nhưng dịch chậm

Bottleneck là Ollama. Giảm model trước:

```text
8b → 4b → 1.7b
```

## Dịch xong nhanh nhưng render chậm

Đổi:

```text
VIDEO_PRESET=fast
↓
VIDEO_PRESET=veryfast
↓
VIDEO_PRESET=ultrafast
```

## Video AV1 chậm bất thường

OCR giờ decode AV1 trực tiếp qua FFmpeg pipe nên không còn full-video H.264 compatibility proxy. Nếu stage OCR vẫn chậm, bottleneck là software AV1 decode + RapidOCR; xem timing OCR và CPU usage thay vì dung lượng proxy.

---

# 13. Lưu ý về Docker Desktop

Trên Windows, tổng RAM/CPU thực tế còn phải chia cho:

```text
Windows
+ Docker Desktop / WSL2
+ Chromium
+ Ollama trên host
+ VideoGet
```

Vì vậy máy 16 GB RAM không nên cấu hình như một server Linux 16 GB hoàn toàn dành cho VideoGet.

Nếu Docker Desktop bị giới hạn CPU/RAM thấp hơn máy thật, preset High cũng không giúp nhanh hơn; cần kiểm tra resource allocation của Docker/WSL2.

---

# 14. Khi đổi preset

Chỉ thay env:

```powershell
docker compose down
docker compose up -d --force-recreate
```

Nếu vừa pull code/Dockerfile mới:

```powershell
git pull origin main
docker compose down
docker compose up -d --build --force-recreate
```

Các preset không chứa cookie/API key thật. Chỉ điền secret trong `.env` local và không commit.
