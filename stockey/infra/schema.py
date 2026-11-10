"""Tables the orchestrator owns. Only the orchestrator (and the autoscaler beside it on the main
server) reads or writes them; workers never connect to Postgres."""
from __future__ import annotations

from utils.db import db_session, execute_db_operation

TASK = "infra_task"
ATTEMPT = "infra_task_attempt"
MACHINE = "infra_machine"
IP_DOMAIN = "infra_ip_domain"
DOMAIN = "infra_domain_state"
ANNOUNCEMENT = "infra_announcement"
DOCUMENT = "infra_document"
RECORD = "infra_record"
FACT = "infra_fact"
PASSAGE = "infra_passage"

_DDL = [
    f"""CREATE TABLE IF NOT EXISTS {TASK} (
        id BIGSERIAL PRIMARY KEY,
        kind TEXT NOT NULL,
        payload JSONB NOT NULL,
        dedup_key TEXT UNIQUE,                 -- the same work is never queued twice
        pool TEXT NOT NULL,                    -- hetzner / runpod / main
        domain TEXT,                           -- rate-limited site the task hits (nse, bse), or NULL
        priority INT NOT NULL DEFAULT 50,      -- lower runs first: 10 user question, 30 daily, 50 history
        status TEXT NOT NULL DEFAULT 'pending',  -- pending / running / done / failed / cancelled
        attempt INT NOT NULL DEFAULT 0,
        max_attempts INT NOT NULL DEFAULT 3,
        machine_id TEXT,
        lease_until TIMESTAMPTZ,
        not_before TIMESTAMPTZ,
        parent_id BIGINT REFERENCES {TASK}(id),
        run_tag TEXT,                          -- groups a run (e.g. "pilot-2026-11-09")
        result JSONB,
        error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ)""",
    f"CREATE INDEX IF NOT EXISTS {TASK}_ready ON {TASK} (pool, status, priority, id)",
    f"""CREATE TABLE IF NOT EXISTS {ATTEMPT} (
        task_id BIGINT NOT NULL REFERENCES {TASK}(id), attempt INT NOT NULL, machine_id TEXT,
        ip TEXT, started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ,
        outcome TEXT,                          -- done / failed / blocked / lost
        error TEXT, seconds DOUBLE PRECISION,
        PRIMARY KEY (task_id, attempt))""",
    f"""CREATE TABLE IF NOT EXISTS {MACHINE} (
        id TEXT PRIMARY KEY,                   -- our id; also the server/pod name
        provider TEXT NOT NULL,                -- hetzner / runpod / local
        provider_id TEXT, pool TEXT NOT NULL, ip TEXT,
        token_sha256 TEXT NOT NULL,
        hourly_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
        status TEXT NOT NULL,                  -- creating / ready / busy / deleting / deleted / failed
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        registered_at TIMESTAMPTZ, last_seen TIMESTAMPTZ, idle_since TIMESTAMPTZ,
        deleted_at TIMESTAMPTZ, note TEXT)""",
    f"""CREATE TABLE IF NOT EXISTS {IP_DOMAIN} (
        ip TEXT NOT NULL, domain TEXT NOT NULL,
        next_at TIMESTAMPTZ NOT NULL DEFAULT now(),   -- earliest next dispatch for this IP and site
        busy_task BIGINT,                             -- one task per IP and site at a time
        held_until TIMESTAMPTZ, hold_reason TEXT,     -- after a block
        PRIMARY KEY (ip, domain))""",
    f"""CREATE TABLE IF NOT EXISTS {DOMAIN} (
        domain TEXT PRIMARY KEY, paused_until TIMESTAMPTZ, reason TEXT, updated_at TIMESTAMPTZ)""",
    f"""CREATE TABLE IF NOT EXISTS {ANNOUNCEMENT} (
        source TEXT NOT NULL, filing_id TEXT NOT NULL, symbol TEXT NOT NULL,
        announced_at TIMESTAMP, category TEXT, subject TEXT, text TEXT, attachment_url TEXT,
        raw JSONB, task_id BIGINT, loaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (source, filing_id))""",
    f"""CREATE TABLE IF NOT EXISTS {DOCUMENT} (
        url TEXT PRIMARY KEY, source TEXT, symbol TEXT, filing_id TEXT,
        status TEXT NOT NULL,                  -- fetched / not_found / not_pdf / failed
        blob_key TEXT, bytes INT, pages INT,
        text_layer TEXT, text_layer_chars INT,
        ocr_status TEXT,                       -- pending / probe_done / more_queued / rest_skipped / done / failed / not_needed
        ocr_text TEXT, ocr_pages INT, ocr_seconds DOUBLE PRECISION, ocr_model TEXT,
        task_id BIGINT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    # One record per disclosure: the NSE and BSE copies of the same document are merged.
    f"""CREATE TABLE IF NOT EXISTS {RECORD} (
        record_key TEXT PRIMARY KEY, symbol TEXT NOT NULL, announced_at TIMESTAMP,
        category TEXT, subject TEXT,
        sources JSONB NOT NULL,                -- [{{source, filing_id, url}}] -- every exchange copy
        doc_url TEXT, routine BOOLEAN NOT NULL DEFAULT false,
        text_sha TEXT, text_chars INT,         -- a changed text (e.g. OCR arrived) re-extracts
        summary TEXT,
        status TEXT NOT NULL DEFAULT 'pending',  -- pending / done / failed
        version INT, chars_read INT, truncated BOOLEAN, extracted_at TIMESTAMPTZ, error TEXT,
        embedded_sha TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    f"CREATE INDEX IF NOT EXISTS {RECORD}_sym ON {RECORD} (symbol, announced_at)",
    f"ALTER TABLE {RECORD} ADD COLUMN IF NOT EXISTS kind TEXT",      # decided from content (infra/catalog.py)
    f"""CREATE TABLE IF NOT EXISTS {FACT} (
        id BIGSERIAL PRIMARY KEY,
        record_key TEXT NOT NULL REFERENCES {RECORD}(record_key) ON DELETE CASCADE,
        symbol TEXT NOT NULL, fact_type TEXT NOT NULL, event_date DATE, period TEXT,
        summary TEXT,
        amount_cr DOUBLE PRECISION,            -- in Rs crore, converted at the RBI rate if foreign
        amount_raw TEXT, amount_unit TEXT, currency TEXT,
        pct DOUBLE PRECISION, shares DOUBLE PRECISION, price_inr DOUBLE PRECISION,
        counterparty TEXT, person TEXT, role TEXT, direction TEXT,
        details JSONB, quote TEXT,
        verified BOOLEAN NOT NULL,             -- quote found in the text and the numbers in the quote
        note TEXT, version INT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    f"CREATE INDEX IF NOT EXISTS {FACT}_q ON {FACT} (symbol, fact_type, event_date)",
    f"CREATE INDEX IF NOT EXISTS {FACT}_t ON {FACT} (fact_type, event_date)",
    f"""CREATE TABLE IF NOT EXISTS {PASSAGE} (
        record_key TEXT NOT NULL REFERENCES {RECORD}(record_key) ON DELETE CASCADE,
        seq INT NOT NULL, symbol TEXT NOT NULL, announced_at TIMESTAMP,
        text TEXT NOT NULL, embedding vector(384) NOT NULL,
        PRIMARY KEY (record_key, seq))""",
    f"CREATE INDEX IF NOT EXISTS {PASSAGE}_sym ON {PASSAGE} (symbol, announced_at)",
    f"CREATE INDEX IF NOT EXISTS {PASSAGE}_vec ON {PASSAGE} USING hnsw (embedding vector_cosine_ops)",
]


def ensure() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            for ddl in _DDL:
                cur.execute(ddl)

    execute_db_operation(_op, operation_name="infra:ensure")
