# VideoGet brand assets

`Xứ Sở Nhiều Lông` is the default VideoGet pet-channel overlay brand.

- `xu-so-nhieu-long.svg` is the repository brand reference: dark rounded badge, cat + dog motif, white/orange channel name, and paw mark.
- `scripts/brand_badge.py` generates the transparent PNG used by ffmpeg at runtime, matching the same visual language and keeping the text editable/configurable.
- Bilibili uploader/watermark cleanup is auto-detected per video. VideoGet samples the top area, detects persistent OCR text, decides left vs right, blurs only that area, then places the `Xứ Sở Nhiều Lông` badge on the same side.
- If OpenCV cannot decode a Bilibili AV1 source, the detector can create a temporary H.264 proxy only for watermark-side detection.

Runtime defaults can be overridden with the `BRAND_*` and `OCR_OVERLAY_BILIBILI_*` environment variables documented in `.env.example`.
