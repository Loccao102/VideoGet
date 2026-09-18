# OCR AV1 compatibility

Một số video Bilibili/public-source dùng AV1. OpenCV không phải lúc nào cũng decode AV1 ổn định, còn system FFmpeg trong container có `libdav1d` và decode software tốt hơn.

## Pipeline hiện tại

VideoGet không còn transcode toàn bộ video AV1 sang một file H.264 proxy chỉ để OCR.

```text
source AV1
  -> FFmpeg decode trực tiếp
  -> fps sampler
  -> raw BGR frame pipe
  -> RapidOCR
```

Frame OCR giữ nguyên độ phân giải nguồn. Không có bước scale xuống 960/1280 để đổi tốc độ lấy độ chính xác.

Cùng lúc OCR text, VideoGet giữ luôn geometry:

```text
timestamp
text
confidence
bbox
bboxRegions
```

Vì vậy không cần mở video và OCR lại một lượt thứ hai để tìm bbox cho blur nguồn.

## Lợi ích

Với source AV1 nén tốt, ví dụ 15–25 MB, pipeline cũ có thể sinh H.264 proxy hàng chục hoặc hơn 100 MB vì `libx264 ultrafast` nén kém hơn AV1. Direct frame pipe bỏ hoàn toàn file tạm này và cũng bỏ encode proxy.

Final video luôn render từ source gốc.

## Settings cũ

Các biến sau có thể còn trong `.env` local từ phiên bản trước:

```env
OCR_FORCE_DECODE_PROXY=
OCR_DECODE_PROXY_PRESET=
OCR_DECODE_PROXY_CRF=
OCR_DECODE_PROXY_THREADS=
OCR_DECODE_PROXY_MAX_WIDTH=
OCR_DECODE_PROXY_FPS=
OCR_KEEP_DECODE_PROXY=
```

Chúng không còn nằm trên đường chạy OCR chính và có thể xóa khỏi `.env`.

Lưu ý: Bilibili brand detector vẫn có fallback proxy riêng rất nhỏ nếu OpenCV không đọc được vài frame vùng trên; đây không phải full-video OCR proxy và được quản lý bằng `OCR_OVERLAY_BILIBILI_DECODE_PROXY`.

Nếu system FFmpeg cũng không decode được source AV1, OCR sẽ fail trực tiếp với lỗi FFmpeg thay vì âm thầm tạo một file trung gian lớn.
