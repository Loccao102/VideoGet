-- VideoGet Localization Intelligence V2
-- PostgreSQL + pgvector foundation. The first embedding profile targets
-- multilingual/nomic-style 768-d vectors; migrate dimension together with the
-- configured embedding model if that changes later.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS translation_profiles (
    id              BIGSERIAL PRIMARY KEY,
    key             TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    source_language TEXT NOT NULL DEFAULT 'zh',
    target_language TEXT NOT NULL DEFAULT 'vi',
    instructions    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS translation_terms (
    id              BIGSERIAL PRIMARY KEY,
    source_term     TEXT NOT NULL,
    target_term     TEXT NOT NULL,
    domain          TEXT NOT NULL DEFAULT 'global',
    series_key      TEXT,
    channel_key     TEXT,
    priority        INTEGER NOT NULL DEFAULT 0,
    approved        BOOLEAN NOT NULL DEFAULT false,
    notes           TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_translation_terms_scope
    ON translation_terms (domain, series_key, channel_key, approved, priority DESC);

CREATE TABLE IF NOT EXISTS translation_entities (
    id              BIGSERIAL PRIMARY KEY,
    entity_type     TEXT NOT NULL,
    canonical_name  TEXT NOT NULL,
    target_name     TEXT NOT NULL,
    aliases         JSONB NOT NULL DEFAULT '[]'::jsonb,
    domain          TEXT NOT NULL DEFAULT 'global',
    series_key      TEXT,
    channel_key     TEXT,
    approved        BOOLEAN NOT NULL DEFAULT false,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_translation_entities_scope
    ON translation_entities (domain, series_key, channel_key, approved);

CREATE TABLE IF NOT EXISTS translation_memory (
    id                  BIGSERIAL PRIMARY KEY,
    source_language     TEXT NOT NULL DEFAULT 'zh',
    target_language     TEXT NOT NULL DEFAULT 'vi',
    source_text         TEXT NOT NULL,
    target_text         TEXT NOT NULL,
    source_embedding    vector(768),
    domain              TEXT NOT NULL DEFAULT 'global',
    series_key          TEXT,
    channel_key         TEXT,
    source_video_id     TEXT,
    source_segment_id   INTEGER,
    profile_key         TEXT,
    quality_score       REAL,
    approval_status     TEXT NOT NULL DEFAULT 'generated'
        CHECK (approval_status IN ('generated','auto_checked','human_approved','rejected')),
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_translation_memory_scope
    ON translation_memory (source_language, target_language, domain, series_key, channel_key, approval_status);

CREATE INDEX IF NOT EXISTS idx_translation_memory_embedding_hnsw
    ON translation_memory USING hnsw (source_embedding vector_cosine_ops)
    WHERE source_embedding IS NOT NULL AND approval_status = 'human_approved';

CREATE TABLE IF NOT EXISTS semantic_chunks (
    id                  BIGSERIAL PRIMARY KEY,
    source_video_id     TEXT NOT NULL,
    source_segment_id   INTEGER,
    start_sec           DOUBLE PRECISION NOT NULL,
    end_sec             DOUBLE PRECISION NOT NULL,
    source_text         TEXT NOT NULL,
    translated_text     TEXT,
    topic               TEXT,
    entities            JSONB NOT NULL DEFAULT '[]'::jsonb,
    embedding           vector(768),
    series_key          TEXT,
    channel_key         TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_semantic_chunks_video
    ON semantic_chunks (source_video_id, start_sec);

CREATE INDEX IF NOT EXISTS idx_semantic_chunks_embedding_hnsw
    ON semantic_chunks USING hnsw (embedding vector_cosine_ops)
    WHERE embedding IS NOT NULL;

CREATE TABLE IF NOT EXISTS translation_reviews (
    id                  BIGSERIAL PRIMARY KEY,
    source_video_id     TEXT NOT NULL,
    source_segment_id   INTEGER NOT NULL,
    source_text         TEXT NOT NULL,
    generated_text      TEXT NOT NULL,
    final_text          TEXT NOT NULL,
    reviewer            TEXT NOT NULL DEFAULT 'human',
    quality_score       REAL,
    approved            BOOLEAN NOT NULL DEFAULT false,
    feedback            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_translation_reviews_video
    ON translation_reviews (source_video_id, source_segment_id, approved);
