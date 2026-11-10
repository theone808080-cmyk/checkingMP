CREATE TABLE IF NOT EXISTS sources (
    id SERIAL PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('rss', 'html', 'telegram')),
    url TEXT NOT NULL,
    trust TEXT NOT NULL CHECK (trust IN ('official', 'regulator', 'industry')),
    scope TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT true,
    last_checked_at TIMESTAMPTZ,
    last_error TEXT
);

ALTER TABLE sources ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'industry';

CREATE TABLE IF NOT EXISTS runs (
    id SERIAL PRIMARY KEY,
    service TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('started', 'finished', 'failed')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    items INTEGER NOT NULL DEFAULT 0,
    note TEXT
);

CREATE TABLE IF NOT EXISTS materials (
    id SERIAL PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    run_id INTEGER REFERENCES runs(id),
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    text TEXT,
    published_at TIMESTAMPTZ,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    fingerprint TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'done', 'skipped', 'failed'))
);

CREATE TABLE IF NOT EXISTS relevance_assessments (
    id BIGSERIAL PRIMARY KEY,
    material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    rules_version TEXT NOT NULL,
    classification TEXT NOT NULL CHECK (classification IN (
        'practical_change', 'seller_action_or_risk', 'industry_context', 'irrelevant', 'uncertain'
    )),
    score SMALLINT NOT NULL CHECK (score BETWEEN 0 AND 100),
    rule_codes TEXT[] NOT NULL DEFAULT '{}',
    platforms TEXT[] NOT NULL DEFAULT '{}',
    explanation TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (material_id, rules_version)
);

CREATE TABLE IF NOT EXISTS relevance_labels (
    material_id INTEGER PRIMARY KEY REFERENCES materials(id) ON DELETE CASCADE,
    label TEXT NOT NULL CHECK (label IN (
        'practical_change', 'seller_action_or_risk', 'industry_context', 'irrelevant', 'uncertain'
    )),
    labeled_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS events (
    id SERIAL PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    topic TEXT NOT NULL,
    importance SMALLINT NOT NULL CONSTRAINT events_importance_check CHECK (importance BETWEEN 1 AND 5),
    summary TEXT NOT NULL,
    recommended_action TEXT NOT NULL,
    verification TEXT NOT NULL CHECK (verification IN ('confirmed', 'needs_check')),
    effective_date DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'events' AND column_name = 'severity'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'events' AND column_name = 'importance'
    ) THEN
        ALTER TABLE events DROP CONSTRAINT IF EXISTS events_severity_check;
        ALTER TABLE events RENAME COLUMN severity TO importance;
        ALTER TABLE events ALTER COLUMN importance TYPE SMALLINT USING
            CASE importance
                WHEN 'info' THEN 1
                WHEN 'important' THEN 3
                WHEN 'critical' THEN 5
                ELSE NULL
            END::SMALLINT;
        ALTER TABLE events ADD CONSTRAINT events_importance_check CHECK (importance BETWEEN 1 AND 5);
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS event_materials (
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    material_id INTEGER NOT NULL UNIQUE REFERENCES materials(id) ON DELETE CASCADE,
    PRIMARY KEY (event_id, material_id)
);

CREATE TABLE IF NOT EXISTS feed_snapshots (
    id UUID PRIMARY KEY,
    period TEXT NOT NULL,
    platform TEXT NOT NULL,
    material_ids INTEGER[] NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS finding_snapshots (
    id UUID PRIMARY KEY,
    filters_hash TEXT NOT NULL,
    event_ids INTEGER[] NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS notification_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    initialized BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_batches (
    id BIGSERIAL PRIMARY KEY,
    count INTEGER NOT NULL CHECK (count > 0),
    sent_at TIMESTAMPTZ,
    telegram_message_id BIGINT
);

CREATE TABLE IF NOT EXISTS notification_materials (
    material_id INTEGER PRIMARY KEY REFERENCES materials(id),
    batch_id BIGINT REFERENCES notification_batches(id)
);

