# Localization V2

Localization V2 separates three concerns that were previously coupled:

1. speech recognition / source transcript;
2. Vietnamese language intelligence;
3. subtitle + voice rendering.

The main rule is: **Whisper timing segments are not sentences**. A one-second ASR fragment may be only half of a sentence, so translation and TTS must first recover the semantic/utterance context instead of treating every segment independently.

## Context-aware translation V3

The current smart worker uses a two-pass localization pipeline:

```text
Whisper transcript
  -> semantic source blocks
  -> whole-video context analysis
       - content type
       - plot/topic summary
       - character/name aliases
       - relationships / kinship
       - terminology
       - high-confidence ASR correction candidates
       - tone
  -> contextual block translation
       - full story/context bible
       - previous translated dialogue
       - following source dialogue
  -> per-timing-cue Vietnamese
       - utteranceId
       - speaker (when sufficiently clear)
       - sourceCorrected (reviewable, never silently replaces source)
       - translationConfidence
  -> deterministic QA
  -> contextual TTS
  -> Smart Render
```

This is specifically designed for content such as Chinese short drama where Whisper often produces many tiny contiguous fragments and names/kinship terms only make sense from the surrounding scene.

The translator is explicitly instructed to:

- never translate a raw Whisper fragment in isolation;
- keep character names and forms of address consistent across the video;
- translate idioms by meaning rather than word-for-word;
- avoid leaving Chinese characters inside Vietnamese output;
- preserve facts, numbers, brands and product models;
- treat possible ASR repairs as reviewable suggestions, not source truth;
- keep one output entry for each original timing ID so subtitle timing remains editable;
- assign the same `utteranceId` to adjacent timing cues that form one spoken sentence.

Important controls:

```env
TRANSLATE_PROFILE=auto
TRANSLATE_CONTEXT_HINT=
TRANSLATE_CONTEXT_ANALYSIS_CHARS=10000
TRANSLATE_CONTEXT_ANALYSIS_REQUIRED=false
TRANSLATE_ROLLING_CONTEXT_SEGMENTS=8
```

Profiles currently accepted by the retranslation API/editor are:

```text
auto | drama | short_drama | affiliate | tutorial | general
```

`auto` lets the context analysis classify the content before translation. The editor can also provide a temporary instruction such as known character names/relationships for a difficult video.

The generated context bible is persisted as:

```text
<stem>.translation-context.json
```

## Re-translate without running Whisper again

The Subtitle Studio can re-run only the language-intelligence layer from the cached source transcript. Existing downloaded media and the old rendered MP4 are preserved.

```text
cached transcript
  -> rebuild story/context bible
  -> contextual retranslation
  -> Vietnamese SRT + reviewable draft
  -> QA
```

The user can retranslate:

- the whole video; or
- selected subtitle IDs. Selected IDs automatically cause the containing semantic block to be retranslated so half-sentence fixes do not create another context boundary.

API:

```http
POST /api/jobs/{id}/subtitles/retranslate
Content-Type: application/json

{
  "profile": "drama",
  "instruction": "Gia Nghi is the female lead. Keep family forms of address consistent.",
  "segmentIds": [6, 7, 8]
}
```

Omit `segmentIds` to retranslate the whole transcript.

The operation intentionally invalidates generated TTS but keeps the current MP4 for comparison. Review the new text first, then explicitly regenerate voice/render.

## Subtitle Studio

Open:

```text
http://localhost:8080/subtitles.html?job=<job-id>
```

The Studio provides:

- source/current rendered video side by side;
- source ASR text and optional `sourceCorrected` suggestion;
- Vietnamese text editing;
- start/end timing editing;
- detected speaker / utterance / confidence metadata;
- per-cue TTS rate policy;
- QA warnings highlighted on the affected cue;
- selection of QA-problem cues;
- **Dịch lại đoạn chọn**;
- **Dịch lại toàn bộ**;
- **Lưu draft**;
- **Tạo lại TTS + Render**.

Manual edits preserve contextual metadata such as `utteranceId` so saving a corrected line does not accidentally revert the TTS pipeline to robotic per-segment speech.

## Contextual TTS

The first V2 implementation synthesized every subtitle timing segment separately. This is safe for timing but sounds unnatural on short drama where Whisper can create one-second fragments.

Localization V2 now groups adjacent cues that share the same `utteranceId` and compatible speaker/rate policy:

```text
cue 120 ┐
cue 121 ├─ same utteranceId -> one Edge TTS request -> fitted to combined slot
cue 122 ┘
```

The subtitle cues still keep their original timestamps. Only speech synthesis is grouped.

If the Vietnamese sentence is too long for its combined slot, VideoGet first asks Edge TTS to speak faster naturally. A conservative waveform speed-up is used only for the remaining mismatch.

Controls:

```env
TTS_TARGET_CHARS_PER_SEC=14
TTS_EDITOR_MIN_RATE_PERCENT=-20
TTS_EDITOR_MAX_RATE_PERCENT=70
TTS_EDITOR_POST_MAX_SPEED=1.35
TTS_EDITOR_CONCURRENCY=3
TTS_GROUP_CONTEXTUAL_UTTERANCES=true
TTS_UTTERANCE_MAX_DURATION_SEC=8
TTS_UTTERANCE_MAX_CHARS=180
TTS_UTTERANCE_MAX_GAP_SEC=0.45
```

A manual cue can override auto rate with values such as `+20%` or `+40%`. Cues with different manual rate policies are not merged into the same TTS request.

Regenerate voice/render API:

```http
POST /api/jobs/{id}/subtitles/regenerate-tts
```

The older endpoint remains as a compatibility alias:

```http
POST /api/jobs/{id}/subtitles/rerender
```

Both paths skip Whisper and translation:

```text
current subtitle draft
  -> contextual/adaptive Vietnamese TTS
  -> Translation QA
  -> Smart Render
  -> <stem>.vi-dubbed.mp4
```

## Translation QA and semantic blocks

The worker emits:

```text
<stem>.translation-qa.json
<stem>.semantic-blocks.json
```

Translation QA is deterministic and advisory. It checks:

- empty Vietnamese text;
- suspicious untranslated Chinese remaining in Vietnamese;
- numbers/units present in source but missing in translation;
- Latin brand/model/technical tokens that disappear;
- reading-speed pressure in characters per second;
- suspicious duplicate translations on adjacent different source segments.

The report returns `pass`, `warning`, or `error` plus per-segment issue codes. It does **not** automatically overwrite generated or manually corrected text.

Default controls:

```env
TRANSLATION_QA_WARN_CPS=18
TRANSLATION_QA_ERROR_CPS=28
TRANSLATION_QA_MAX_CJK_RATIO=0.18
```

Semantic blocks preserve the original timing segment IDs and are now used by contextual translation. They also remain the retrieval unit for future Translation Memory/pgvector integration.

```env
SEMANTIC_BLOCK_MAX_DURATION_SEC=12
SEMANTIC_BLOCK_MAX_SOURCE_CHARS=140
SEMANTIC_BLOCK_MAX_GAP_SEC=1.0
```

## Subtitle API

Read editable subtitles:

```http
GET /api/jobs/{id}/subtitles
```

Read QA:

```http
GET /api/jobs/{id}/subtitles/quality
```

Save manual draft:

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
      "text": "Cái này dùng thật sự rất tiện.",
      "speechRate": "auto"
    }
  ]
}
```

Media preview:

```http
GET /api/jobs/{id}/media/source
GET /api/jobs/{id}/media/output
```

## Localization semantic memory

`db/migrations/001_localization_memory.sql` defines the PostgreSQL + pgvector foundation:

- `translation_profiles` — translation style by workflow/series;
- `translation_terms` — approved terminology/glossary;
- `translation_entities` — brand/person/product/entity normalization;
- `translation_memory` — approved source -> Vietnamese examples + embedding;
- `semantic_chunks` — semantic transcript blocks;
- `translation_reviews` — generated vs manually corrected translations.

Generated translations must **not** automatically become trusted memory.

Recommended lifecycle:

```text
generated
  -> auto_checked
  -> human_approved
  -> eligible for semantic retrieval
```

Manual subtitle corrections are the strongest source for future Translation Memory because they represent wording accepted for publishing.

## Next localization step

The next phase should build on the contextual pipeline rather than replacing it:

```text
whole-video context bible
  + glossary/entity lookup
  + approved Translation Memory retrieval
      same channel > same series > same domain > global
  -> contextual block translation
  -> deterministic QA
  -> human review/approval
  -> semantic cue split/merge
  -> contextual TTS
```

Series and Affiliate workflows share this localization core while keeping discovery/business policy separate.
