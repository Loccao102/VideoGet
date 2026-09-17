# Xứ Sở Nhiều Lông brand overlay

VideoGet replaces the Bilibili uploader/platform mark during OCR Overlay rendering instead of assuming that every source puts it on the same side.

## Runtime flow

1. Confirm the source is Bilibili.
2. Sample several frames from the top band of the source video.
3. OCR the top band and cluster text that remains in a stable position across multiple samples.
4. Prefer persistent clusters containing `bilibili` or uploader-like ASCII text.
5. Decide whether the persistent cluster is on the left or right.
6. Blur only the detected source region.
7. Overlay the `Xứ Sở Nhiều Lông` badge on the same side.
8. Cache the result in OCR metadata as `bilibiliBrand` so later re-renders do not repeat detection unless requested.

If OpenCV cannot read an AV1 Bilibili file, VideoGet can create a temporary low-quality H.264 proxy for brand detection only. The final render always uses the original source video.

## Brand asset

The repository reference asset is:

`assets/brand/xu-so-nhieu-long.svg`

At runtime `scripts/brand_badge.py` creates a transparent PNG with the same visual language: dark rounded badge, orange border, cat + dog motif, white/orange `Xứ Sở Nhiều Lông` text and a paw mark.

To use a custom raster asset instead:

```env
BRAND_WATERMARK_ASSET=/app/assets/brand/custom.png
```

## Main settings

```env
OCR_OVERLAY_HIDE_BILIBILI=true
OCR_OVERLAY_BILIBILI_AUTO_DETECT=true
OCR_OVERLAY_BILIBILI_DECODE_PROXY=true
OCR_OVERLAY_BILIBILI_SAMPLES=6
OCR_OVERLAY_BILIBILI_TOP_BAND=0.20
OCR_OVERLAY_BILIBILI_OCR_CONFIDENCE=0.48
OCR_OVERLAY_BILIBILI_MIN_HITS=2
OCR_OVERLAY_BILIBILI_BLUR=17
OCR_OVERLAY_BILIBILI_REDETECT=false

BRAND_WATERMARK_ENABLED=true
BRAND_WATERMARK_TEXT=Xứ Sở Nhiều Lông
BRAND_WATERMARK_WIDTH=0.20
BRAND_WATERMARK_MARGIN_X=0.012
BRAND_WATERMARK_MARGIN_Y=0.012
```

`BRAND_WATERMARK_WIDTH` is normalized relative to source video width. The same badge is placed on the side selected by the detector.

## Manual override

Normally these stay empty:

```env
OCR_OVERLAY_BILIBILI_MANUAL_REGIONS=
OCR_OVERLAY_BILIBILI_REGIONS=
```

`OCR_OVERLAY_BILIBILI_MANUAL_REGIONS` forces one or more normalized `x,y,w,h` regions. `OCR_OVERLAY_BILIBILI_REGIONS` is only a fallback if automatic detection cannot decide. Neither is required for ordinary Bilibili jobs.

To force a fresh decision on an existing job metadata file:

```env
OCR_OVERLAY_BILIBILI_REDETECT=true
```

Turn it back off after testing so re-renders reuse the cached detection.

## OCR subtitle placement

Brand replacement is independent of subtitle placement. OCR Overlay still follows the per-segment bbox rules:

- lower source caption -> black box centered over the original caption + white Vietnamese text;
- upper/middle source caption -> blur only the source-text bbox + place Vietnamese text at the same position;
- edited SRT keeps the original bbox when its timestamp can still be matched;
- old metadata falls back to the video-level OCR region.
