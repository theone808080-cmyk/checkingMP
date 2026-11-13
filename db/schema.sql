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
ALTER TABLE sources ADD COLUMN IF NOT EXISTS access_public BOOLEAN;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS access_requires_credentials BOOLEAN;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS access_rate_limit TEXT;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS access_cost TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE sources ADD COLUMN IF NOT EXISTS access_checked_at TIMESTAMPTZ;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS access_check_result TEXT NOT NULL DEFAULT 'unknown';
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='sources'::regclass AND conname='sources_access_cost_check') THEN
        ALTER TABLE sources ADD CONSTRAINT sources_access_cost_check CHECK (access_cost IN ('free','paid','unknown'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='sources'::regclass AND conname='sources_access_check_result_check') THEN
        ALTER TABLE sources ADD CONSTRAINT sources_access_check_result_check CHECK (access_check_result IN ('unknown','passed','failed'));
    END IF;
END $$;

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
ALTER TABLE source_candidates ADD COLUMN IF NOT EXISTS access_public BOOLEAN;
ALTER TABLE source_candidates ADD COLUMN IF NOT EXISTS access_requires_credentials BOOLEAN;
ALTER TABLE source_candidates ADD COLUMN IF NOT EXISTS access_rate_limit TEXT;
ALTER TABLE source_candidates ADD COLUMN IF NOT EXISTS access_cost TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE source_candidates ADD COLUMN IF NOT EXISTS access_checked_at TIMESTAMPTZ;
ALTER TABLE source_candidates ADD COLUMN IF NOT EXISTS access_check_result TEXT NOT NULL DEFAULT 'unknown';
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='source_candidates'::regclass AND conname='source_candidates_access_cost_check') THEN
        ALTER TABLE source_candidates ADD CONSTRAINT source_candidates_access_cost_check CHECK (access_cost IN ('free','paid','unknown'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='source_candidates'::regclass AND conname='source_candidates_access_check_result_check') THEN
        ALTER TABLE source_candidates ADD CONSTRAINT source_candidates_access_check_result_check CHECK (access_check_result IN ('unknown','passed','failed'));
    END IF;
END $$;

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

ALTER TABLE runs ADD COLUMN IF NOT EXISTS cycle_id BIGINT REFERENCES cycles(id);

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

CREATE TABLE IF NOT EXISTS processing_trace (
    id BIGSERIAL PRIMARY KEY,
    cycle_id BIGINT REFERENCES cycles(id),
    run_id INTEGER REFERENCES runs(id),
    source_id INTEGER REFERENCES sources(id),
    material_id INTEGER REFERENCES materials(id),
    stage TEXT NOT NULL CHECK (stage IN ('collection', 'translation', 'relevance', 'analysis')),
    status TEXT NOT NULL CHECK (status IN ('success', 'failed', 'skipped')),
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    error_code TEXT,
    error_summary TEXT,
    provider TEXT,
    model TEXT,
    rule_version TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (finished_at >= started_at),
    CHECK ((status = 'failed' AND error_code IS NOT NULL AND error_summary IS NOT NULL)
        OR (status <> 'failed' AND error_code IS NULL AND error_summary IS NULL))
);

CREATE INDEX IF NOT EXISTS processing_trace_material_idx ON processing_trace (material_id, id);

CREATE OR REPLACE FUNCTION reject_processing_trace_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'processing_trace is append-only';
END;
$$;

DROP TRIGGER IF EXISTS processing_trace_append_only ON processing_trace;
CREATE TRIGGER processing_trace_append_only
    BEFORE UPDATE OR DELETE ON processing_trace
    FOR EACH ROW EXECUTE FUNCTION reject_processing_trace_mutation();

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
    relevance TEXT NOT NULL DEFAULT 'uncertain' CHECK (relevance IN ('relevant', 'irrelevant', 'uncertain')),
    topic TEXT NOT NULL DEFAULT '',
    seller_impact TEXT NOT NULL DEFAULT 'unclear' CHECK (seller_impact IN ('positive', 'negative', 'mixed', 'neutral', 'unclear')),
    score SMALLINT NOT NULL CHECK (score BETWEEN 0 AND 100),
    rule_codes TEXT[] NOT NULL DEFAULT '{}',
    platforms TEXT[] NOT NULL DEFAULT '{}',
    explanation TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (material_id, rules_version)
);
ALTER TABLE relevance_assessments ADD COLUMN IF NOT EXISTS relevance TEXT NOT NULL DEFAULT 'uncertain';
ALTER TABLE relevance_assessments ADD COLUMN IF NOT EXISTS topic TEXT NOT NULL DEFAULT '';
ALTER TABLE relevance_assessments ADD COLUMN IF NOT EXISTS seller_impact TEXT NOT NULL DEFAULT 'unclear';

CREATE TABLE IF NOT EXISTS relevance_labels (
    material_id INTEGER PRIMARY KEY REFERENCES materials(id) ON DELETE CASCADE,
    label TEXT NOT NULL CHECK (label IN (
        'practical_change', 'seller_action_or_risk', 'industry_context', 'irrelevant', 'uncertain',
        'relevant'
    )),
    label_type TEXT NOT NULL DEFAULT 'classification' CHECK (label_type IN ('classification', 'relevance')),
    platforms TEXT[] NOT NULL DEFAULT '{}',
    seller_impact TEXT NOT NULL DEFAULT 'unclear' CHECK (seller_impact IN ('positive', 'negative', 'mixed', 'neutral', 'unclear')),
    labeled_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE relevance_labels ADD COLUMN IF NOT EXISTS platforms TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE relevance_labels ADD COLUMN IF NOT EXISTS seller_impact TEXT NOT NULL DEFAULT 'unclear';
ALTER TABLE relevance_labels ADD COLUMN IF NOT EXISTS label_type TEXT NOT NULL DEFAULT 'classification';
ALTER TABLE relevance_labels DROP CONSTRAINT IF EXISTS relevance_labels_label_check;
ALTER TABLE relevance_labels ADD CONSTRAINT relevance_labels_label_check CHECK (label IN (
    'practical_change', 'seller_action_or_risk', 'industry_context', 'irrelevant', 'uncertain', 'relevant'
));

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'relevance_assessments'::regclass AND conname = 'relevance_assessments_relevance_allowed_check') THEN
        ALTER TABLE relevance_assessments ADD CONSTRAINT relevance_assessments_relevance_allowed_check
            CHECK (relevance IN ('relevant', 'irrelevant', 'uncertain'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'relevance_assessments'::regclass AND conname = 'relevance_assessments_seller_impact_allowed_check') THEN
        ALTER TABLE relevance_assessments ADD CONSTRAINT relevance_assessments_seller_impact_allowed_check
            CHECK (seller_impact IN ('positive', 'negative', 'mixed', 'neutral', 'unclear'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'relevance_labels'::regclass AND conname = 'relevance_labels_seller_impact_allowed_check') THEN
        ALTER TABLE relevance_labels ADD CONSTRAINT relevance_labels_seller_impact_allowed_check
            CHECK (seller_impact IN ('positive', 'negative', 'mixed', 'neutral', 'unclear'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'relevance_labels'::regclass AND conname = 'relevance_labels_label_type_allowed_check') THEN
        ALTER TABLE relevance_labels ADD CONSTRAINT relevance_labels_label_type_allowed_check
            CHECK (label_type IN ('classification', 'relevance'));
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS events (
    id SERIAL PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    topic TEXT NOT NULL,
    importance SMALLINT NOT NULL CONSTRAINT events_importance_check CHECK (importance BETWEEN 1 AND 5),
    seller_impact TEXT NOT NULL DEFAULT 'unclear' CHECK (seller_impact IN ('positive', 'negative', 'mixed', 'neutral', 'unclear')),
    summary TEXT NOT NULL,
    recommended_action TEXT NOT NULL,
    verification TEXT NOT NULL CHECK (verification IN ('confirmed', 'needs_check')),
    effective_date DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE events ADD COLUMN IF NOT EXISTS seller_impact TEXT NOT NULL DEFAULT 'unclear';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'events'::regclass AND conname = 'events_seller_impact_allowed_check') THEN
        ALTER TABLE events ADD CONSTRAINT events_seller_impact_allowed_check
            CHECK (seller_impact IN ('positive', 'negative', 'mixed', 'neutral', 'unclear'));
    END IF;
END
$$;

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

