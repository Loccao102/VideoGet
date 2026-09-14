# Localization V2

Localization V2 separates three concerns that were previously coupled:

1. speech recognition / source transcript;
2. Vietnamese language intelligence;
3. subtitle + voice rendering.

The first implementation focuses on the editing/rendering quality loop while laying the pgvector schema for semantic translation memory.

## Manual subtitle editor

For any job that already has localized segments, open:

```text
http://localhost:8080/subtitles.html?job=<job-id>
```

The editor provides side-by-side source/current rendered video and one row per subtitle segment:

- source text;
- Vietnamese text;
- start time;
- end time;
- TTS speech-rate policy.

`Save draft` writes `<stem>.subtitle-edit.json` and updates the Vietnamese SRT, but keeps the previous final MP4 for comparison.

`Save + Render again` skips Whisper and translation. It regenerates only:

```text
edited segments
  -> adaptive Vietnamese TTS
  -> Smart Render
  -> <stem>.vi-dubbed.mp4
```

### Adaptive speech rate

Default mode is `auto`.

For each segment, VideoGet compares visible Vietnamese characters against the available timing slot. If the translated sentence is longer than the comfortable speaking density, Edge TTS is requested at a higher rate before any post-processing is applied.

Environment controls:

```env
TTS_TARGET_CHARS_PER_SEC=14
TTS_EDITOR_MAX_RATE_PERCENT=70
TTS_EDITOR_POST_MAX_SPEED=1.35
TTS_EDITOR_CONCURRENCY=3
```

A manual segment can override auto mode with values such as `+20%`, `+40%`, etc.

The post-speed limit is intentionally conservative. The goal is to ask the voice engine to speak faster naturally first and use waveform speedup only for the remaining small mismatch.

## Subtitle editor API

```http
GET /api/jobs/{id}/subtitles
```

Returns editable segments.

```http
PUT /api/jobs/{id}/subtitles
Content-Type: application/json

{
  "segments": [
    {
      "id": 0,
      "start": 1.25,
      "end": 3.60,
      "sourceText": "这个真的很好用",
      "text": "Cái này dùng thực sự rất tiện.",
      "speechRate": "auto"
    }
  ]
}
```

Re-render:

```http
POST /api/jobs/{id}/subtitles/rerender
```

Media preview:

```http
GET /api/jobs/{id}/media/source
GET /api/jobs/{id}/media/output
```

## Localization semantic memory

`db/migrations/001_localization_memory.sql` defines the first PostgreSQL + pgvector schema:

- `translation_profiles` — translation style by workflow/series;
- `translation_terms` — approved terminology/glossary;
- `translation_entities` — brand/person/product/entity normalization;
- `translation_memory` — approved source -> Vietnamese examples + embedding;
- `semantic_chunks` — semantic transcript blocks;
- `translation_reviews` — generated vs manually corrected translations.

The initial embedding dimension is 768 so a local multilingual embedding model can be used without a cloud dependency. The dimension is a schema contract: change it through a migration if the embedding model changes.

### Memory quality rule

Generated translations must **not** automatically become trusted memory.

Recommended lifecycle:

```text
generated
  -> auto_checked
  -> human_approved
  -> eligible for semantic retrieval
```

Manual subtitle corrections are the strongest source for future Translation Memory because they represent the wording actually accepted for publishing.

## Next implementation step

After the editing/render loop is stable, wire the semantic layer into translation:

```text
Whisper
  -> semantic block builder
  -> entity + glossary lookup
  -> pgvector retrieval (same channel > same series > same domain > global)
  -> context-aware translation
  -> QA
  -> subtitle segmentation
```

Series and Affiliate workflows should share this localization core while keeping their discovery/business logic separate.
