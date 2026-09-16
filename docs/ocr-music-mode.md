# OCR + Music mode

`ocr_music` is a lightweight localization path for short videos whose useful content is burned into the video as text and does not need Vietnamese voice-over.

## Flow

```text
Downloaded video
  -> sample 2-3 frames/sec
  -> RapidOCR / PP-OCRv6 on likely caption region
  -> merge repeated OCR observations into timed segments
  -> translate segments with the configured VideoGet translation provider
  -> derive the source-caption footprint from OCR boxes
  -> blur that footprint only while captions are active
  -> burn Vietnamese subtitles
  -> replace/duck original audio with local background music
```

This path does **not** load Whisper and does **not** run TTS.

## Music

Place licensed/royalty-free tracks under `assets/music/`. Docker Compose mounts that folder read-only into `/app/assets/music`.

You can also force one track:

```env
OCR_MUSIC_FILE=/app/assets/music/track.mp3
```

By default, a missing music track fails the `ocr_music` job with a clear error (`OCR_MUSIC_REQUIRED=true`) instead of silently producing a video without music.

## Main settings

```env
OCR_MODEL_SIZE=small
OCR_FPS=3
OCR_MIN_CONFIDENCE=0.65
OCR_SUBTITLE_REGION=auto
OCR_TEXT_SIMILARITY=0.78
OCR_IGNORE_PERSISTENT_SEC=12
OCR_TRANSLATE=true
OCR_REMOVE_SOURCE_TEXT=true

OCR_MUSIC_DIR=/app/assets/music
OCR_MUSIC_REQUIRED=true
OCR_MUSIC_VOLUME=0.18
OCR_ORIGINAL_AUDIO_MODE=mute
```

`OCR_SUBTITLE_REGION` accepts `auto`, `full`, or normalized `x,y,w,h`. Use a manual region when captions are consistently outside the lower half of the frame.

`OCR_ORIGINAL_AUDIO_MODE` accepts:

- `mute`: background music replaces source audio.
- `duck`: source audio remains quiet under the music.
- `mix`: source audio is mixed more audibly with the music.

## Low-spec preset

`.env.low.example` uses:

```env
OCR_MODEL_SIZE=tiny
OCR_FPS=2
OCR_MAX_SAMPLES=360
OLLAMA_MODEL=qwen3:1.7b
LOCALIZE_WORKER_PREWARM=false
```

This keeps OCR CPU-only and avoids loading Whisper in advance. Increase `OCR_FPS` or switch to `small` if very short/fast-changing captions are missed.

## Troubleshooting

If OCR returns no timed segments:

1. Set a manual `OCR_SUBTITLE_REGION` around the captions.
2. Lower `OCR_MIN_CONFIDENCE` gradually, e.g. `0.60`.
3. Increase `OCR_FPS` to `3` or `4`.
4. Check whether text is highly stylized, animated, vertical, or heavily occluded.

If static product labels are becoming subtitles, lower `OCR_IGNORE_PERSISTENT_SEC` so long unchanging text is discarded sooner.
