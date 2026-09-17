# VideoGet brand assets

`Xứ Sở Nhiều Lông` is the default VideoGet pet-channel overlay brand.

- `xu-so-nhieu-long.svg` stores the approved transparent badge asset as an embedded PNG so it remains previewable in GitHub while staying compatible with the text-only repository connector.
- The OCR overlay renderer extracts the embedded PNG to a temporary file before ffmpeg rendering.
- Bilibili watermark/uploader cleanup is auto-detected per video; the badge is then placed on the detected side instead of assuming left or right.

Runtime defaults can be overridden with the `BRAND_*` and `OCR_OVERLAY_BILIBILI_*` environment variables documented in `.env.example`.
