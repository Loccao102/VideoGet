# OCR processing modes

VideoGet has two OCR-first localization modes for short videos whose useful content is burned into the frames as text. Both run locally with RapidOCR / PP-OCRv6 and do **not** load Whisper or TTS.

## `ocr_subtitles`

Use this when you want Vietnamese subtitles from on-screen text but want to keep the source video/audio.

```text
Downloaded source video
  -> FFmpeg decodes only sampled frames directly from source
  -> RapidOCR / PP-OCRv6 gets text + bbox in the same pass
  -> merge repeated observations into timed segments
  -> translate with the configured VideoGet translation provider
  -> one final video encode:
       blur tight source-text bbox regions
       + Vietnamese subtitle
       + optional Bilibili branding
       + selected social aspect
  -> stream-copy original audio
```

There is no full-video H.264 OCR compatibility proxy in the normal OCR path, and there is no second OCR pass just to recover bbox geometry.

The web UI exposes this as a dedicated **OCR → Sub Việt** button on every video card and as the bulk mode `OCR chữ → Sub Việt + giữ video/audio gốc`.

Editing the generated SRT and pressing **Lưu + render lại** renders from the original downloaded source again. OCR and translation are not rerun.

## `ocr_music`

Use this when the same OCR subtitle flow should finish with background music instead of keeping the source audio unchanged.

```text
Downloaded source video
  -> FFmpeg sampled-frame OCR + bbox
  -> translate
  -> one video encode:
       cleanup + Vietnamese subtitle + branding + aspect
  -> music mix
       video stream is copied (-c:v copy)
       only audio is rebuilt
  -> delete temporary subbed container
```

The final music file therefore does not encode the video a second time.

## Music

Place licensed/royalty-free tracks under `assets/music/`. Docker Compose mounts that folder read-only into `/app/assets/music`.

You can also force one track:

```env
OCR_MUSIC_FILE=/app/assets/music/track.mp3
```

By default, a missing music track fails the `ocr_music` job with a clear error (`OCR_MUSIC_REQUIRED=true`) instead of silently producing a video without music. This setting does not affect `ocr_subtitles`.

## Main settings

Both OCR modes share the same detection/translation settings:

```env
OCR_MODEL_SIZE=small
OCR_FPS=3
OCR_MIN_CONFIDENCE=0.65
OCR_SUBTITLE_REGION=auto
OCR_TEXT_SIMILARITY=0.78
OCR_IGNORE_PERSISTENT_SEC=12
OCR_TRANSLATE=true
OCR_REMOVE_SOURCE_TEXT=true

# Final OCR video quality is intentionally high on every hardware tier.
OCR_RENDER_CRF=18
OCR_RENDER_PRESET=
OCR_KEEP_SUBBED_INTERMEDIATE=false
```

Music-only settings:

```env
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

This keeps OCR CPU-only and avoids loading Whisper in advance. Low still uses `OCR_RENDER_CRF=18`; it saves work by reducing concurrency and redundant encode/decode passes, not by lowering final video quality. Increase `OCR_FPS` or switch to `small` if very short/fast-changing captions are missed.

## Troubleshooting

If OCR returns no timed segments:

1. Set a manual `OCR_SUBTITLE_REGION` around the captions.
2. Lower `OCR_MIN_CONFIDENCE` gradually, e.g. `0.60`.
3. Increase `OCR_FPS` to `3` or `4`.
4. Check whether text is highly stylized, animated, vertical, or heavily occluded.

If static product labels are becoming subtitles, lower `OCR_IGNORE_PERSISTENT_SEC` so long unchanging text is discarded sooner.
