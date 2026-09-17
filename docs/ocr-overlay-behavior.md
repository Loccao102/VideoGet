# OCR subtitle overlay behavior

`ocr_subtitles` now defaults to `OCR_SUBTITLE_INITIAL_RENDER_STYLE=ocr_overlay`.

Behavior:
- lower source captions: cover the source caption with a black box centered on the OCR bbox, then place Vietnamese white text on that box;
- upper/middle source captions: blur only the OCR source-text bbox and place Vietnamese text at the same position;
- Bilibili: blur the top-right uploader / `bilibili` watermark area by default;
- preserve original source audio;
- per-segment OCR bbox remains the primary placement source; old jobs fall back to the global OCR region.

Useful overrides:

```env
OCR_SUBTITLE_INITIAL_RENDER_STYLE=ocr_overlay
OCR_OVERLAY_BOTTOM_THRESHOLD=0.68
OCR_OVERLAY_BOTTOM_MIN_WIDTH=0.30
OCR_OVERLAY_BOTTOM_MIN_HEIGHT=0.055
OCR_OVERLAY_BOTTOM_BOX_ALPHA=0.92
OCR_OVERLAY_SOURCE_BLUR=13
OCR_OVERLAY_HIDE_BILIBILI=true
OCR_OVERLAY_BILIBILI_REGIONS=0.72,0.010,0.27,0.085
OCR_OVERLAY_BILIBILI_BLUR=17
```

Set `OCR_SUBTITLE_INITIAL_RENDER_STYLE=standard` only when the legacy renderer is explicitly desired.
