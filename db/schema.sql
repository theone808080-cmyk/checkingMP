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
ALTER TABLE sources ADD COLUMN IF NOT EXISTS managed_by TEXT NOT NULL DEFAULT 'config';

CREATE TABLE IF NOT EXISTS source_candidates (
    id BIGSERIAL PRIMARY KEY,
    publisher_domain TEXT NOT NULL,
    collection_url TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('rss', 'html', 'telegram')),
    category TEXT NOT NULL,
    trust TEXT NOT NULL CHECK (trust IN ('official', 'regulator', 'industry')),
    scope TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'needs_setup', 'approved', 'rejected')),
    probe_status TEXT NOT NULL DEFAULT 'pending',
    probe_error TEXT,
    examples JSONB NOT NULL DEFAULT '[]'::jsonb,
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (publisher_domain, collection_url)
);

CREATE TABLE IF NOT EXISTS source_provider_state (
    provider TEXT PRIMARY KEY,
    pause_until TIMESTAMPTZ,
    pause_reason TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runs (
    id SERIAL PRIMARY KEY,
    service TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('started', 'finished', 'failed')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    items INTEGER NOT NULL DEFAULT 0,
    note TEXT
);

CREATE TABLE IF NOT EXISTS cycles (
    id BIGSERIAL PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'started' CHECK (status IN ('started', 'success', 'failed')),
    collection_status TEXT NOT NULL DEFAULT 'pending' CHECK (collection_status IN ('pending', 'success', 'failed')),
    notification_status TEXT NOT NULL DEFAULT 'pending' CHECK (notification_status IN ('pending', 'sent', 'skipped', 'failed')),
    notification_checked_at TIMESTAMPTZ,
    notification_attempted BOOLEAN NOT NULL DEFAULT false,
    notification_target_material_ids JSONB,
    notification_delivered_material_ids JSONB,
    processing_status TEXT NOT NULL DEFAULT 'pending' CHECK (processing_status IN ('pending', 'success', 'failed')),
    collected_items INTEGER NOT NULL DEFAULT 0 CHECK (collected_items >= 0),
    processed_items INTEGER NOT NULL DEFAULT 0 CHECK (processed_items >= 0),
    source_failures TEXT[] NOT NULL DEFAULT '{}',
    source_failure_signatures JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_stage TEXT,
    error_message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);

ALTER TABLE cycles ADD COLUMN IF NOT EXISTS source_failure_signatures JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE cycles ADD COLUMN IF NOT EXISTS notification_checked_at TIMESTAMPTZ;
ALTER TABLE cycles ADD COLUMN IF NOT EXISTS notification_attempted BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE cycles ADD COLUMN IF NOT EXISTS notification_target_material_ids JSONB;
ALTER TABLE cycles ADD COLUMN IF NOT EXISTS notification_delivered_material_ids JSONB;

CREATE TABLE IF NOT EXISTS watchdog_incidents (
    incident_key TEXT PRIMARY KEY,
    active BOOLEAN NOT NULL DEFAULT true,
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    restart_attempted BOOLEAN NOT NULL DEFAULT false,
    details TEXT NOT NULL,
    opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    alert_sent_at TIMESTAMPTZ,
    recovery_sent_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS backup_runs (
    id BIGSERIAL PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('started', 'success', 'failed')),
    file_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    size_bytes BIGINT CHECK (size_bytes IS NULL OR size_bytes >= 0),
    sha256 TEXT,
    restore_status TEXT NOT NULL DEFAULT 'pending' CHECK (restore_status IN ('pending', 'success', 'failed')),
    restore_checked_at TIMESTAMPTZ,
    error_code TEXT
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

ALTER TABLE materials ADD COLUMN IF NOT EXISTS translation_status TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE materials ADD COLUMN IF NOT EXISTS title_ru TEXT;
ALTER TABLE materials ADD COLUMN IF NOT EXISTS text_ru TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'materials'::regclass AND conname = 'materials_translation_status_check'
    ) THEN
        ALTER TABLE materials ADD CONSTRAINT materials_translation_status_check
            CHECK (translation_status IN ('legacy', 'pending', 'ready'));
    END IF;
END
$$;
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

