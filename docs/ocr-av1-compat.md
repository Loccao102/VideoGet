# OCR AV1 compatibility

Some Bilibili/public-source videos are delivered as AV1. The `opencv-python-headless` wheel used by VideoGet may fail to decode those frames even when the system `ffmpeg` package can decode AV1 in software.

For `ocr_subtitles`, VideoGet now checks the source codec and verifies that OpenCV can read the first frame. If the source is AV1 or OpenCV cannot decode it, VideoGet creates a temporary video-only H.264 proxy with system ffmpeg, performs OCR on that proxy, then deletes it.

The final subtitle render still uses the original downloaded source video, so original audio is preserved.

Optional settings:

```env
# Always create an OCR decode proxy, useful for debugging decoder issues.
OCR_FORCE_DECODE_PROXY=false

# Fast/low-cost proxy because it is only used for OCR frame extraction.
OCR_DECODE_PROXY_PRESET=ultrafast
OCR_DECODE_PROXY_CRF=28

# 0 lets ffmpeg choose the thread count automatically.
OCR_DECODE_PROXY_THREADS=0

# Keep the proxy for debugging. Default false.
OCR_KEEP_DECODE_PROXY=false
```

If ffmpeg itself cannot decode the source AV1 stream, the job fails with a clear compatibility error instead of the repeated OpenCV `Failed to get pixel format` message.
