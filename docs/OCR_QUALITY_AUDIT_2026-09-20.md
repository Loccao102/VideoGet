# OCR quality audit — 2026-09-20

## Scope

Audit dựa trên:
- video gốc `咪咪带你看世界 第一站 [BV1u8JM6WEK6].mp4`
- output hiện tại `咪咪带你看世界 第一站 [BV1u8JM6WEK6].ocr-vi-subbed.aspect-3x4.mp4`
- pipeline OCR hiện tại trên `main`: RapidOCR PP-OCRv6 -> merge segment -> dịch -> OCR overlay -> aspect 3:4.

Mục tiêu sản phẩm:
1. sub nguồn Trung phải được che sạch;
2. sub Việt phải đúng nghĩa, tự nhiên, ngắn và dễ đọc;
3. không OCR nhầm chữ trong cảnh/logo thành lời thoại;
4. không tạo blur block lớn phá hình;
5. crop/aspect không làm mất hoặc cắt subtitle.

## Các lỗi quan sát trực tiếp

### 1. OCR bỏ sót / không thay sub nguồn
Khoảng 00:20, source có `芝士`. Output vẫn hiển thị nguyên `芝士`, không có bản Việt thay thế.

Hệ quả:
- segment có thể không được OCR hoặc bị loại;
- renderer không có timed entry để cleanup, nên chữ Trung tồn tại nguyên.

### 2. OCR sai làm dịch sai hoàn toàn
Khoảng 01:00, source là `渴死我了` (nghĩa gần "Khát chết mất"). Output lại thành `Cảm ơn tôi đã đến đây`.

Đây không chỉ là lỗi dịch. Khi source OCR đã sai, LLM không còn đủ dữ kiện để dịch đúng.

### 3. Che chữ Trung không kín
Cũng tại khoảng 01:00, chữ Trung vẫn còn nhìn thấy bên dưới subtitle Việt.

Nguyên nhân thiết kế hiện tại:
- cleanup ưu tiên `bboxRegions` rất chặt;
- padding mặc định nhỏ;
- style `clean` dùng blur, không phải cover đảm bảo;
- geometry chỉ giữ box của một observation đại diện thay vì envelope ổn định qua nhiều frame.

Kết luận: blur theo OCR box hiện tại không thể đảm bảo yêu cầu "che toàn bộ chữ Trung".

### 4. Blur block sai / quá lớn
Khoảng 01:20 và 02:20 xuất hiện các vùng blur lớn, che cả nội dung hình.

Ở 02:20, cảnh có nhiều text môi trường như `PEACE HOTEL`, chữ địa điểm và sub Trung ở đáy. Pipeline có thể kéo text môi trường vào cùng vùng OCR hoặc nhận nhầm watermark/text scene, dẫn đến cleanup sai vị trí.

### 5. OCR đang gom mọi text trong ROI thành một câu
`ocr_frame()` hiện:
- OCR toàn vùng;
- nhận mọi box vượt confidence;
- sort theo hàng/x;
- nối tất cả text thành một string.

Điều này nguy hiểm ở cảnh có:
- biển hiệu;
- logo;
- location label;
- watermark;
- sub thoại.

Không có bước phân loại "caption line" với "scene text".

### 6. Segment merge chọn observation confidence cao nhất, chưa có consensus
Trong `extract_segments()`, khi frame mới giống text hiện tại:
- segment được kéo dài;
- chỉ khi confidence cao hơn mới thay text và geometry.

Một frame confidence cao nhưng đọc sai có thể thắng nhiều frame đọc đúng hơn. Chưa có voting/consensus theo character/token.

### 7. Geometry cleanup lấy một observation đại diện
`_apply_geometry()` lưu bbox/regions từ một frame. Các frame khác có thể:
- chữ dịch chuyển nhẹ;
- outline rộng hơn;
- text dài/ngắn khác;
- detector cắt thiếu đầu/cuối.

Do đó blur thường sót nét chữ Trung.

### 8. Tiny preset làm chất lượng OCR giảm mạnh
Log thực tế đang dùng:
- `PP-OCRv6_det_tiny.onnx`
- `PP-OCRv6_rec_tiny.onnx`

Preset LOW đặt `OCR_MODEL_SIZE=tiny`. Đây là trade-off tốc độ, không phù hợp khi ưu tiên chất lượng sub.

### 9. Translation recovery trước fix 2026-09-20 bị lặp vô ích
Khi model trả lại nguyên tiếng Trung:
- guard phát hiện đúng;
- retry lại cùng prompt gần deterministic;
- retry 1/2, 2/2;
- split batch;
- vẫn lặp cùng lỗi;
- cuối cùng fail segment.

Ví dụ log:
`translator returned untranslated Chinese for segment ids: 0,1,2,...`

Đã sửa trong nhánh `fix/ocr-translation-recovery`:
- normal prompt fail vì còn Han -> chuyển ngay sang forced Chinese->Vietnamese recovery prompt;
- recovery dùng temperature 0, instruction song ngữ rất ngắn;
- có thể dùng model riêng qua `TRANSLATE_REPAIR_MODEL`;
- nếu recovery vẫn còn tiếng Trung thì split ngay, không retry cùng request vô ích;
- bump translation prompt version để tránh reuse cache cũ.

### 10. Aspect được áp sau subtitle/cleanup
Renderer burn cleanup + sub theo hệ tọa độ source rồi mới crop/scale sang 3:4 / 9:16.

Hệ quả:
- sub có thể quá sát mép/cắt sau crop;
- vùng cleanup nhìn quá lớn sau crop;
- placement không dựa trên final canvas.

## Kiến trúc nâng cấp đề xuất

### P0 — Làm ngay: "không được sai thô"

#### A. OCR multi-frame consensus
Mỗi caption interval giữ 3-7 observation:
- normalize từng candidate;
- cluster theo similarity;
- vote theo số frame;
- confidence chỉ là trọng số phụ;
- text thắng phải xuất hiện trên >= N frame hoặc đạt consensus ratio.

Không dùng "frame confidence cao nhất thắng".

#### B. Tách detection khỏi recognition
Dùng OCR detector để tìm text boxes trước, sau đó:
- cluster theo horizontal line;
- chọn candidate gần vùng caption ổn định;
- loại text scene/logo dựa persistence + position + motion;
- chỉ recognition candidate caption.

#### C. Source-caption envelope độc lập với text recognition
Cleanup cần biết "chỗ có chữ", không phụ thuộc OCR có đọc đúng hay không.

Mỗi segment:
- union/quantile boxes qua nhiều frame;
- morphological expansion thêm outline;
- lưu `cleanupRegions` riêng với `bboxRegions`.

`bboxRegions` dùng cho placement; `cleanupRegions` dùng để xóa chữ nguồn.

#### D. Chế độ cover bảo đảm
Thêm `OCR_SOURCE_CLEANUP_MODE=cover|blur|inpaint`.

Mặc định chất lượng:
- `cover`: vùng caption được che kín bằng dark translucent/opaque patch theo envelope rồi đặt sub Việt;
- không dùng blur-only nếu yêu cầu là "không được thấy chữ Trung".

Sau khi ổn mới nâng lên inpaint.

#### E. Translation recovery
Đã triển khai trong fix hiện tại:
- normal contextual translation;
- forced Vietnamese recovery nếu còn Han;
- split ngay nếu recovery fail;
- optional stronger repair model.

### P1 — Sub thông minh hơn

#### A. Context theo scene/chunk, không chỉ batch
Gom các segment liên tiếp 5-12 giây thành một semantic chunk:
1. đưa toàn bộ source text + timing cho model;
2. model hiểu mạch hội thoại;
3. trả lại từng id;
4. giữ timing gốc.

#### B. OCR correction trước translation
Trước khi dịch, model nhận:
- 2-3 OCR candidates mỗi segment;
- previous/next source;
- confidence;
- yêu cầu chọn/correct câu Trung hợp lý nhất.

Ví dụ:
`渴死我了` phải được phục hồi trước khi dịch, thay vì dịch một OCR string đã sai.

#### C. Subtitle rewrite theo reading speed
Sau dịch:
- giới hạn ký tự/giây;
- tối đa 2 dòng;
- tránh dòng 1 rất ngắn / dòng 2 quá dài;
- rút gọn khẩu ngữ nhưng giữ nghĩa;
- không kéo câu quá dài vào caption 0.5-1.0s.

#### D. Quality score
Mỗi segment có:
- OCR consensus score;
- translation confidence/guard result;
- cleanup coverage score.

Segment score thấp -> second-pass OCR mạnh hơn thay vì render luôn.

### P2 — Quality mode mạnh hơn

#### A. OCR second pass
Nếu confidence thấp:
- crop caption envelope;
- upscale 1.5-2x;
- grayscale/CLAHE/sharpen;
- thử 2 preprocessing variants;
- dùng `small` hoặc `medium` model cho second pass.

#### B. Text mask / inpaint
Thay rectangle blur bằng:
- OCR detector mask;
- dilation theo stroke;
- inpaint hoặc temporal patch.

Mục tiêu là xóa chữ nguồn mà không tạo block lớn.

#### C. Final-canvas rendering
Aspect/crop được quyết định trước.
Sau đó transform:
- cleanup regions;
- subtitle anchor;
- safe margins;
sang final canvas và mới render.

Điều này tránh subtitle bị cắt sau khi đổi 3:4/9:16.

## Cấu hình khuyến nghị hiện tại

Nếu ưu tiên chất lượng hơn tốc độ:

```env
OCR_MODEL_SIZE=small
OCR_FPS=3
OCR_SEGMENT_BBOX_SAMPLES=3
TRANSLATE_REPAIR_ON_HAN=true
# Nếu máy đã có qwen3:8b:
TRANSLATE_REPAIR_MODEL=qwen3:8b
```

Không khuyến nghị `OCR_MODEL_SIZE=tiny` cho output production nếu video có:
- font nhỏ;
- outline dày;
- cảnh chuyển động;
- nhiều text môi trường.

## Acceptance criteria cho bản OCR mới

Một sample video chỉ được coi là pass khi:
- >= 98% caption source có segment;
- 0 caption Trung còn đọc được sau cleanup ở vùng thoại;
- không có blur block > 35% chiều rộng frame nếu không phải manual mask;
- không OCR biển hiệu/logo thành subtitle thoại;
- 100% output subtitle non-Vietnamese bị guard/retry, không burn raw Chinese;
- subtitle nằm trong safe-zone final aspect;
- max 2 dòng, không bị cắt;
- spot-check dịch giữ đúng nghĩa ở các câu ngắn/khẩu ngữ.

## Thứ tự triển khai đề nghị

1. Merge translation recovery hiện tại.
2. Multi-frame text consensus.
3. Caption-vs-scene-text filtering.
4. Multi-frame cleanup envelope + `cover` mode.
5. Final-aspect-aware subtitle placement.
6. OCR correction + contextual translation chunk.
7. Optional inpaint second stage.
