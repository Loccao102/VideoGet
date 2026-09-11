# Smart preserve-frame rendering

VideoGet no longer needs to blur a fixed 24% band at the bottom of every video. The default renderer now analyzes the downloaded source before the final render and keeps the original frame size intact.

## Pipeline

```text
source video
  -> sample frames
  -> detect likely burned-in subtitle band
  -> detect persistent watermark/logo strokes near frame edges
  -> detect recurring faces as protected regions
  -> choose a subtitle anchor
  -> clean only confident regions
  -> render compact Vietnamese ASS subtitles
  -> mix Vietnamese voice + quiet original audio
```

The analyzer never crops or zooms the source. If confidence is low, SMART mode leaves that region alone instead of guessing.

## Cleanup modes

Set `VIDEO_CLEANUP_MODE` to one of:

- `safe`: do not remove source captions or logos. Only add the Vietnamese dub/subtitle. Use this when preserving every source pixel matters more than cleanup.
- `smart` (default): conservative automatic detection. Reuses the source-caption footprint when possible and removes only high-confidence watermark regions.
- `aggressive`: lower detection thresholds and allow more watermark/source-caption cleanup. It still preserves the full frame; it is not a crop mode.
- `legacy`: old fixed masks for rollback/debugging. This is the only mode that uses `VIDEO_SUBTITLE_MASK_*` / `VIDEO_LOGO_MASKS` as the primary cleanup geometry.

## Subtitle cleanup

SMART does not keep a permanent rectangular blur on screen. It detects a smaller source-caption region and enables the blur overlay only while translated subtitle intervals are active. The Vietnamese subtitle itself is an ASS event with a semi-transparent text-sized background box, so only the area behind the current Vietnamese text is covered.

Useful controls:

```env
VIDEO_SOURCE_SUBTITLE_BLUR=8
VIDEO_SOURCE_SUBTITLE_REGION=
VIDEO_SUBTITLE_FONT=Noto Sans
VIDEO_SUBTITLE_FONT_SIZE=
VIDEO_SUBTITLE_BOX_ALPHA=0.58
VIDEO_SUBTITLE_BOX_PADDING=7
VIDEO_SUBTITLE_WRAP_CHARS=
VIDEO_SUBTITLE_MAX_LINES=2
```

Leave `VIDEO_SOURCE_SUBTITLE_REGION` blank for auto-detection. To override a difficult source manually, use normalized `x,y,w,h`, for example `0.08,0.70,0.84,0.12`.

## Watermark cleanup

Watermarks are inferred from edge strokes that stay in the same place across sampled frames. Automatic detection is intentionally limited to watermark-prone edge areas; SMART will not run `delogo` over the central body of the video.

Manual additions use:

```env
VIDEO_MANUAL_LOGO_REGIONS=0.02,0.02,0.18,0.06;0.80,0.02,0.18,0.06
```

Each region is normalized `x,y,w,h`. Manual regions are additive in SMART/AGGRESSIVE mode.

## Layout analysis

`VIDEO_ANALYSIS_SAMPLES=12` controls how many frames are sampled. More samples improve temporal confidence but cost additional CPU time. The result is cached beside localized output as:

```text
<stem>.layout.json
```

The cache key includes source size/mtime, cleanup mode, analyzer version and sample count, so retrying the same job normally avoids analysis work.

The analyzer also writes recurring face regions as protected regions and scores candidate subtitle bands against them. If source captions are detected confidently, VideoGet prefers replacing that existing footprint rather than covering a new part of the image. Otherwise it picks a lower-risk band using visual edge density and protected-region overlap.

## Fonts / square glyphs

The image now includes `fonts-noto-core`, `fonts-noto-cjk`, and `fonts-noto-color-emoji`. This gives libass broader Vietnamese/CJK/symbol fallback and reduces missing-glyph squares. Subtitle text is also sanitized before ASS generation.

## Output artifacts

A localized run can now produce:

```text
<stem>.original.srt
<stem>.vi.srt
<stem>.vi.ass
<stem>.layout.json
<stem>.vi-voice.wav
<stem>.vi-dubbed.mp4
<stem>.localization.json
```

The full source resolution/aspect ratio is preserved. SMART/AGGRESSIVE cleanup changes pixels only inside detected/overridden regions; there is no automatic crop stage.
