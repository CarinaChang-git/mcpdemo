CREATE EXTENSION IF NOT EXISTS vector;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sec_ingest') THEN
        CREATE ROLE sec_ingest NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sec_query') THEN
        CREATE ROLE sec_query NOLOGIN;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS companies (
    cik text PRIMARY KEY CHECK (cik ~ '^[0-9]{10}$'),
    ticker text NOT NULL UNIQUE CHECK (ticker ~ '^[A-Z][A-Z0-9.-]{0,9}$'),
    company_name text NOT NULL,
    exchange text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS filings (
    accession_number text PRIMARY KEY
        CHECK (accession_number ~ '^[0-9]{10}-[0-9]{2}-[0-9]{6}$'),
    cik text NOT NULL REFERENCES companies(cik),
    form text NOT NULL CHECK (form IN ('10-K', '10-Q')),
    filed_date date NOT NULL,
    period_end date NOT NULL,
    primary_document text NOT NULL,
    source_url text NOT NULL CHECK (source_url LIKE 'https://www.sec.gov/%'),
    raw_path text,
    content_sha256 text CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    processing_status text NOT NULL DEFAULT 'discovered'
        CHECK (processing_status IN (
            'discovered', 'downloaded', 'parsed', 'indexed', 'failed', 'quarantined'
        )),
    parser_version text,
    error_code text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS filings_scope_idx
    ON filings (cik, form, filed_date DESC, accession_number);

CREATE TABLE IF NOT EXISTS filing_sections (
    section_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    accession_number text NOT NULL REFERENCES filings(accession_number) ON DELETE CASCADE,
    section_code text NOT NULL,
    section_title text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    content_text text NOT NULL,
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    parse_confidence numeric(5, 4) NOT NULL CHECK (
        parse_confidence >= 0 AND parse_confidence <= 1
    ),
    parse_status text NOT NULL CHECK (parse_status IN ('parsed', 'failed')),
    UNIQUE (accession_number, section_code, ordinal)
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    section_id uuid NOT NULL REFERENCES filing_sections(section_id) ON DELETE CASCADE,
    chunk_index integer NOT NULL CHECK (chunk_index >= 0),
    content_text text NOT NULL,
    token_count integer NOT NULL CHECK (token_count > 0),
    search_vector tsvector GENERATED ALWAYS AS (
        to_tsvector('english', content_text)
    ) STORED,
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    UNIQUE (section_id, chunk_index),
    UNIQUE (section_id, content_sha256)
);

CREATE INDEX IF NOT EXISTS chunks_search_vector_idx
    ON chunks USING gin (search_vector);

CREATE TABLE IF NOT EXISTS index_builds (
    index_build_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    embedding_provider text NOT NULL,
    embedding_model text NOT NULL,
    embedding_dimension integer NOT NULL CHECK (embedding_dimension = 1536),
    chunker_version text NOT NULL,
    status text NOT NULL CHECK (status IN ('building', 'ready', 'failed')),
    is_active boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS index_builds_one_active_idx
    ON index_builds (is_active)
    WHERE is_active;

CREATE TABLE IF NOT EXISTS chunk_embeddings (
    chunk_id uuid NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    index_build_id uuid NOT NULL REFERENCES index_builds(index_build_id) ON DELETE CASCADE,
    chunk_text_sha256 text NOT NULL CHECK (chunk_text_sha256 ~ '^[0-9a-f]{64}$'),
    embedding vector(1536) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chunk_id, index_build_id)
);

ALTER TABLE chunk_embeddings
    ADD COLUMN IF NOT EXISTS chunk_text_sha256 text;

UPDATE chunk_embeddings AS embeddings
SET chunk_text_sha256 = chunks.content_sha256
FROM chunks
WHERE embeddings.chunk_id = chunks.chunk_id
  AND embeddings.chunk_text_sha256 IS NULL;

ALTER TABLE chunk_embeddings
    ALTER COLUMN chunk_text_sha256 SET NOT NULL;

CREATE TABLE IF NOT EXISTS xbrl_facts (
    fact_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cik text NOT NULL REFERENCES companies(cik),
    accession_number text NOT NULL REFERENCES filings(accession_number),
    taxonomy text NOT NULL,
    concept text NOT NULL,
    unit text NOT NULL,
    value numeric NOT NULL,
    start_date date,
    end_date date NOT NULL,
    fiscal_year integer NOT NULL,
    fiscal_period text NOT NULL,
    form text NOT NULL CHECK (form IN ('10-K', '10-Q')),
    filed_date date NOT NULL,
    frame text,
    UNIQUE NULLS NOT DISTINCT (
        cik,
        taxonomy,
        concept,
        unit,
        start_date,
        end_date,
        form,
        filed_date,
        accession_number
    )
);

CREATE INDEX IF NOT EXISTS xbrl_facts_lookup_idx
    ON xbrl_facts (cik, taxonomy, concept, end_date DESC, form);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scope jsonb NOT NULL,
    intent_hash text NOT NULL UNIQUE CHECK (intent_hash ~ '^[0-9a-f]{64}$'),
    status text NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    counts jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

CREATE TABLE IF NOT EXISTS ingestion_items (
    run_id uuid NOT NULL REFERENCES ingestion_runs(run_id) ON DELETE CASCADE,
    accession_number text NOT NULL REFERENCES filings(accession_number) ON DELETE CASCADE,
    stage text NOT NULL CHECK (stage IN (
        'discovered', 'downloaded', 'parsed', 'indexed', 'failed', 'quarantined'
    )),
    status text NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    error_code text,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, accession_number)
);

GRANT USAGE ON SCHEMA public TO sec_ingest, sec_query;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO sec_ingest;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO sec_ingest;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO sec_query;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sec_ingest;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO sec_ingest;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO sec_query;
