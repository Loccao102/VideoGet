# VideoGet processing map

VideoGet treats **download** as the core operation. Subtitle generation and Vietnamese TTS are optional processing layers selected per job.

## 1. Source selection

The web UI exposes every registered source explicitly:

- Douyin
- Bilibili
- Kuaishou
- Xiaohongshu
- Weibo
- Xigua
- Haokan
- Toutiao
- AcFun
- Meipai
- Weishi

When the request contains `sources`, `/api/search` searches exactly those providers. It no longer silently appends the free-source list behind the user's selection.

## 2. Processing modes

### `download`

```text
Search -> Preview -> Download -> Source MP4 -> Done
```

No Whisper, translation, subtitle generation, TTS or final render.

### `subtitles` (default in the UI)

```text
Search -> Download -> Extract audio -> Whisper -> Translate Vietnamese
       -> original.srt + vi.srt -> Smart subtitle render -> Final MP4
```

The original audio is kept. Edge TTS is not called.

### `dub`

```text
Search -> Download -> Whisper -> Translate Vietnamese -> TTS
       -> Smart render with Vietnamese voice + subtitles -> Final MP4
```

This remains the legacy VideoGet behavior and is intentionally opt-in in the new UI.

### `ocr_subtitles`

```text
Search -> Download
       -> FFmpeg sampled-frame decode
       -> RapidOCR text + bbox in one pass
       -> Translate Vietnamese
       -> cleanup + sub + branding + requested aspect
       -> ONE video encode
       -> copy original audio
       -> Final MP4
```

The normal OCR path does not create a full-video H.264 compatibility proxy and does not run a second OCR pass just for bbox placement.

### `ocr_music`

```text
Search -> Download
       -> FFmpeg sampled-frame OCR + bbox
       -> Translate Vietnamese
       -> cleanup + sub + branding + requested aspect
       -> ONE video encode
       -> music mix with video stream copy
       -> Final MP4
```

The temporary subbed container is removed after the music output is complete unless `OCR_KEEP_SUBBED_INTERMEDIATE=true`.

## 3. Subtitle editor and re-render

A completed subtitle/dub job exposes its Vietnamese SRT through:

```text
GET /api/jobs/{id}/subtitle
PUT /api/jobs/{id}/subtitle
POST /api/jobs/{id}/subtitle/render
```

Workflow:

```text
Completed job
   -> Open Subtitle Editor
   -> Edit .vi.srt
   -> Save
   -> Render again
       -> reuse SourceOutput
       -> reuse edited .vi.srt
       -> smart cleanup + burn subtitle
       -> keep original audio
       -> NO download
       -> NO Whisper
       -> NO translation
       -> NO TTS
```

The first edited version also keeps a `.bak` copy of the generated Vietnamese SRT.

## 4. Persistence

Each job now persists:

- `processing_mode`: `download`, `subtitles`, or `dub`
- `subtitle_revision`: revision counter for manual SRT edits

Existing databases are migrated automatically. Existing jobs without an explicit mode retain legacy `dub` semantics.

## 5. UI rule

The web UI defaults to **Sub Việt · không TTS**. Users must explicitly choose `Sub + voice Việt (TTS)` when dubbing is desired.
