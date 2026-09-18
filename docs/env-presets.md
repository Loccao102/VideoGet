# VideoGet hardware presets: Yếu / Vừa / Cao

VideoGet có 3 preset phần cứng rõ ràng. Mục tiêu của các preset là tránh tình trạng "máy mạnh hơn thì tăng mọi thông số" vì OCR, FFmpeg, Chromium, Whisper và Ollama có thể tranh CPU/RAM với nhau và làm throughput thực tế chậm hơn.

> **Logical CPU threads** là số luồng hệ điều hành nhìn thấy trong Task Manager / `lscpu`, không phải số core vật lý.

## 1. Chọn preset theo máy

| Tier | File | CPU logical threads | RAM | GPU | Storage | OCR | Ollama | Localization | Render |
|---|---|---:|---:|---|---|---|---|---:|---|
| **Yếu** | `.env.low.example` | 4-8 | 8-16 GB | Không cần | SSD nên có | `tiny`, 2 FPS | `qwen3:1.7b` | 1 | `ultrafast`, CRF 23 |
| **Vừa** | `.env.medium.example` | 8-16 | 16-32 GB | Optional | SSD/NVMe | `small`, 3 FPS | `qwen3:4b` | 1 | `veryfast`, CRF 21 |
| **Cao** | `.env.high.example` | 20-32+ | 32-64+ GB | Không bắt buộc; hữu ích cho Ollama | NVMe khuyến nghị | `medium`, 4 FPS | `qwen3:8b` | 2 | `fast`, CRF 20 |

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
docker compose up -d --build --force-recreate
```

### Vừa

```powershell
Copy-Item .env.medium.example .env -Force
ollama pull qwen3:4b
docker compose up -d --build --force-recreate
```

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

- Low: 2
- Medium: 3
- High: 4

Nếu caption thay đổi chậm, tăng lên 5-6 FPS thường không đáng với chi phí CPU.

## `OCR_MAX_SAMPLES`

Giới hạn cứng tổng số frame OCR cho một video.

- Low: 360
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

---

# 6. AV1 compatibility proxy

Một số video AV1 không decode ổn định bằng OpenCV. Khi đó VideoGet có thể tạo proxy H.264 tạm trước OCR.

## `OCR_DECODE_PROXY_PRESET`

Preset FFmpeg/libx264 cho proxy:

```text
ultrafast → nhanh, file proxy lớn hơn
veryfast  → cân bằng
fast      → chậm hơn
```

Low dùng `ultrafast`; Medium/High dùng `veryfast`.

## `OCR_DECODE_PROXY_THREADS`

Số CPU threads dành cho proxy.

- Low: 2
- Medium: 4
- High: 0 = FFmpeg tự quyết định.

Nếu AV1 proxy làm máy 100% CPU, đây là biến cần giảm.

## `OCR_DECODE_PROXY_CRF`

Chất lượng proxy tạm.

- Số thấp hơn = nét hơn/file lớn hơn.
- Số cao hơn = nhẹ hơn/file nhỏ hơn.

Proxy chỉ phục vụ OCR nên không cần chất lượng như final video.

---

# 7. Ollama / Translation

## `OLLAMA_MODEL`

Preset:

```text
Low    → qwen3:1.7b
Medium → qwen3:4b
High   → qwen3:8b
```

Đây là một trong những bottleneck lớn nhất nếu Ollama chạy hoàn toàn bằng CPU.

Nếu translation chậm nhưng OCR nhanh:

1. giảm model,
2. giảm context,
3. hoặc cấu hình Ollama dùng GPU nếu phần cứng hỗ trợ.

## `TRANSLATE_BATCH_SIZE`

Số subtitle segment gửi trong một batch.

- Low: 6
- Medium: 8
- High: 12

Batch lớn giảm overhead request/model nhưng cần nhiều RAM/context hơn.

## `TRANSLATE_CONTEXT_SEGMENTS`

Số segment lân cận dùng làm ngữ cảnh để dịch tự nhiên hơn.

- Low: 2
- Medium: 3
- High: 4

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

# 10. Final render

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

# 11. Nếu máy vẫn chậm thì giảm theo thứ tự nào?

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

Kiểm tra log có dòng compatibility proxy. Nếu có, bottleneck là AV1 → H.264 proxy và `OCR_DECODE_PROXY_*` sẽ quan trọng.

---

# 12. Lưu ý về Docker Desktop

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

# 13. Khi đổi preset

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
