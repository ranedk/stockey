"""Low-priority OCR queue for research documents.

The daily OCR job (`all_fundamentals_ocr.sh`, one process under a lock) is the only thing that
loads the OCR model, so the machine never holds two copies of it. Research code that needs a
scanned page read does not run OCR itself: it drops the PDF here and the daily job reads it
ONLY when its own queue is empty, re-checking that queue before every research document so a
new filing waits at most one short research document (operator, 2026-10-09).

PDF bytes are kept on local disk, not fetched by the OCR job, so the job never makes a
research request against an exchange's rate gate.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd

from utils.db import db_session, sql_to_df

TABLE = "fundamentals_ocr_research_queue"
PDF_DIR = Path(__file__).resolve().parents[2] / ".cache" / "ocr_research_pdfs"
DEFAULT_MAX_PAGES = 2
MAX_ATTEMPTS = 2


def ensure_table() -> None:
    with db_session() as (_, cur):
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                doc_id TEXT PRIMARY KEY, requester TEXT NOT NULL, pdf_path TEXT NOT NULL,
                max_pages INT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INT NOT NULL DEFAULT 0, text TEXT, pages_model INT, last_error TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(), done_at TIMESTAMPTZ
            )""")


def enqueue(doc_id: str, requester: str, pdf_bytes: bytes, *, max_pages: int = DEFAULT_MAX_PAGES) -> bool:
    """True if newly queued. Re-enqueueing a known doc_id is a no-op."""
    ensure_table()
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    path = PDF_DIR / f"{hashlib.sha1(doc_id.encode()).hexdigest()}.pdf"
    if not path.exists():
        path.write_bytes(pdf_bytes)
    with db_session() as (_, cur):
        cur.execute(f"""INSERT INTO {TABLE} (doc_id, requester, pdf_path, max_pages) VALUES (%s, %s, %s, %s)
                        ON CONFLICT (doc_id) DO NOTHING""", (doc_id, requester, str(path), max_pages))
        return cur.rowcount == 1


def next_pending() -> dict | None:
    ensure_table()
    df = sql_to_df(f"""SELECT doc_id, pdf_path, max_pages FROM {TABLE}
                        WHERE status = 'pending' ORDER BY created_at LIMIT 1""")
    return None if df.empty else df.iloc[0].to_dict()


def pending_count() -> int:
    ensure_table()
    return int(sql_to_df(f"SELECT count(*) AS n FROM {TABLE} WHERE status = 'pending'").iloc[0]["n"])


def mark(doc_id: str, *, text: str | None = None, pages_model: int | None = None, error: str | None = None) -> None:
    with db_session() as (_, cur):
        if error is None:
            cur.execute(f"""UPDATE {TABLE} SET status = 'done', text = %s, pages_model = %s, attempts = attempts + 1,
                                   done_at = now(), last_error = NULL WHERE doc_id = %s""", (text, pages_model, doc_id))
        else:
            cur.execute(f"""UPDATE {TABLE} SET attempts = attempts + 1, last_error = %s,
                                   status = CASE WHEN attempts + 1 >= %s THEN 'failed' ELSE 'pending' END
                             WHERE doc_id = %s""", (error[:500], MAX_ATTEMPTS, doc_id))


def done_for(requester: str) -> pd.DataFrame:
    ensure_table()
    return sql_to_df(f"SELECT doc_id, text FROM {TABLE} WHERE requester = %s AND status = 'done'", params=(requester,))
