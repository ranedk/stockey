"""Shared fundamentals_events plumbing -- the one normalized L3 events table
(docs/FUNDAMENTAL_SCREENER_PRD.md sec 7, fundamental_basic_goal.md sec 4: "one
normalised events table, source as a column... every disclosure lands on both
exchanges -- treat that as redundancy, not duplication"), written by both
fundamentals/collectors/bse_announcements.py and fundamentals/collectors/nse_pit.py.

Cross-source dedup, without reading any PDF (explicit user instruction 2026-08-10):
the source PRD's full dedup key is (isin, filing_type, disclosure_date, quantity), but
that key can't be applied as a plain SQL UNIQUE constraint / ON CONFLICT clause here --
BSE's announcement-text detection has no `quantity` at all (NULL), NSE's
`corporates-pit` API does, and NULL never equals NULL or a real number under
Postgres's default uniqueness semantics. A naive upsert keyed on all four fields would
simply never fire for exactly the cross-source case it needs to handle -- BSE's
detection-only row and NSE's later structured row for the same disclosure would insert
as two permanently-separate rows.

Instead: `upsert_events_with_dedup()` looks up existing rows on (isin, filing_type,
disclosure_date) -- the three fields available on BOTH a detection-only row and a
structured row -- and MERGES an incoming row into a match (filling in whichever
structured fields the existing row is missing, unioning `sources`, keeping the
earliest `announcement_timestamp` per the source PRD) rather than inserting a
duplicate. Only when no match exists does it fall through to a normal upsert keyed on
(source, news_id).

Accepted tradeoff, not hidden: dropping `quantity` from the match key means two
genuinely different disclosures for the same company on the same day (e.g. two
different insiders both filing PIT trades) would incorrectly merge into one row. This
is judged less harmful than the alternative (silently double-counting the same
disclosure across BSE and NSE) and is cheap to fix later IF a second quantity-bearing,
non-PDF BSE source ever shows up -- there isn't one today (BSE's own PIT/SAST
announcement feed is text-only, confirmed in bse_announcements.py's build).
"""

from __future__ import annotations

import pandas as pd
from psycopg2 import sql

from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db

RESULTS_TABLE = "fundamentals_events"

# Types for the columns find_dedup_candidate's SELECT names -- upsert_to_db's own
# auto-column-add (utils/db.py) only runs at INSERT time, driven by a DataFrame's
# dtypes; it doesn't help the dedup-lookup SELECT that has to run *before* any row
# from a dedup-aware run has ever been inserted. Bootstrapped explicitly instead --
# found live 2026-08-10 as a genuine bug: a fundamentals_events table still at
# bse_announcements.py's original (pre-dedup) schema raised UndefinedColumn on the
# very first call.
_DEDUP_COLUMN_TYPES = {
    "isin": "TEXT",
    "quantity": "DOUBLE PRECISION",
    "insider_name": "TEXT",
    "transaction_type": "TEXT",
    "sources": "TEXT",
    # Also missing from the original table (found in the same live pass): the row
    # builders never included it, so find_dedup_candidate's "earliest-loaded"
    # ORDER BY load_ts had nothing to sort on. Existing rows get NULL here.
    # CORRECTED 2026-08-18 (re-audit): this comment used to claim NULL "sorts first
    # in ASC order" -- live-verified against Postgres's real behavior (ORDER BY x
    # ASC over (1, NULL, 2) returns 1, 2, NULL), Postgres actually sorts NULL LAST
    # in ASC order (NULLS FIRST is the DESC default, not ASC). Still harmless either
    # way -- load_ts only breaks ties among matches, it isn't part of the match
    # itself -- but the old comment's reasoning would have misled the next reader
    # into expecting the opposite tie-breaking preference.
    "load_ts": "TIMESTAMPTZ",
}


def _ensure_events_schema() -> None:
    """Idempotent (IF NOT EXISTS) column bootstrap, see _DEDUP_COLUMN_TYPES. No-op if
    the table doesn't exist yet at all -- upsert_to_db's own DataFrame-driven CREATE
    TABLE already gets every column right in that fresh-install case."""

    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = %s", (RESULTS_TABLE,))
            if cur.fetchone() is None:
                return
            for column, pg_type in _DEDUP_COLUMN_TYPES.items():
                cur.execute(
                    sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} {}").format(
                        sql.Identifier(RESULTS_TABLE), sql.Identifier(column), sql.SQL(pg_type)
                    )
                )

    execute_db_operation(_op, operation_name="fundamentals_events:ensure_schema")


def resolve_isin(company_master_ids: pd.Series) -> pd.Series:
    """company_master_id -> its single most recent ISIN, via dim_security -- same
    DISTINCT-ON-latest pattern as fundamentals/collectors/security_master.py's BSE
    scrip-code resolution, for the same reason (a renamed company can have multiple
    historical ISINs; only the current one is a meaningful dedup key)."""
    lookup_df = sql_to_df(
        """
        SELECT DISTINCT ON (company_master_id) company_master_id, isin
        FROM dim_security
        WHERE isin IS NOT NULL AND company_master_id IS NOT NULL
        ORDER BY company_master_id, last_trade_date DESC NULLS LAST, effective_to DESC NULLS LAST
        """
    )
    isin_by_company_master_id = dict(zip(lookup_df["company_master_id"], lookup_df["isin"]))
    return company_master_ids.map(isin_by_company_master_id)


def resolve_issuer_names(company_master_ids: pd.Series) -> pd.Series:
    """company_master_id -> its single most recent dim_security.display_name -- NSE's
    corporates-pit API requires both `symbol` and `issuer` (the registered company
    name) as query params; same DISTINCT-ON-latest pattern as resolve_isin, same
    table, kept as a separate query rather than widening resolve_isin's return shape
    (callers that only need isin -- and there's an existing tested one -- shouldn't
    have to change)."""
    lookup_df = sql_to_df(
        """
        SELECT DISTINCT ON (company_master_id) company_master_id, display_name
        FROM dim_security
        WHERE display_name IS NOT NULL AND company_master_id IS NOT NULL
        ORDER BY company_master_id, last_trade_date DESC NULLS LAST, effective_to DESC NULLS LAST
        """
    )
    name_by_company_master_id = dict(zip(lookup_df["company_master_id"], lookup_df["display_name"]))
    return company_master_ids.map(name_by_company_master_id)


def find_dedup_candidate(isin: str | None, filing_type: str | None, disclosure_date) -> dict | None:
    """The earliest-loaded existing row (if any) for this (isin, filing_type,
    disclosure_date) -- the merge target for an incoming row that turns out to be the
    same underlying disclosure from a different exchange.

    disclosure_date is compared as text, not date -- found live 2026-08-10:
    fundamentals_events' disclosure_date/announcement_timestamp columns were both
    created TEXT, not DATE/TIMESTAMPTZ (the very first upsert_to_db call that created
    this table inferred column types from a DataFrame whose date/timestamp cells were
    plain Python objects mixed with None, which pandas keeps as dtype=object -- mapped
    to TEXT by the schema generator). Retyping those columns now is a real migration
    against a table other code already reads; comparing as text here instead is the
    smaller, safer fix -- str(date(...)) already matches the stored 'YYYY-MM-DD'
    format exactly.
    """
    if not isin or not filing_type or disclosure_date is None:
        return None
    df = sql_to_df(
        """
        SELECT source, news_id, quantity, insider_name, transaction_type,
               announcement_timestamp, sources
        FROM fundamentals_events
        WHERE isin = %s AND filing_type = %s AND disclosure_date = %s
        ORDER BY load_ts ASC
        LIMIT 1
        """,
        params=(isin, filing_type, str(disclosure_date)),
    )
    return None if df.empty else df.iloc[0].to_dict()


def _merge_row_fields(existing: dict, incoming: dict) -> dict:
    existing_sources = str(existing.get("sources") or "").split(",")
    merged_sources = sorted({s for s in existing_sources if s} | {incoming["source"]})

    # announcement_timestamp is TEXT in the DB (see find_dedup_candidate's docstring),
    # so `existing`'s value arrives as a plain string while `incoming`'s is a fresh
    # pd.Timestamp -- normalize both through pd.to_datetime before comparing (a bare
    # `min(str, Timestamp)` raises TypeError) and store the merged result back as a
    # string, matching what every other row in this column already looks like.
    existing_ts = pd.to_datetime(existing.get("announcement_timestamp"), utc=True, errors="coerce")
    incoming_ts = pd.to_datetime(incoming.get("announcement_timestamp"), utc=True, errors="coerce")
    if pd.notna(existing_ts) and pd.notna(incoming_ts):
        earliest_ts = min(existing_ts, incoming_ts)
    elif pd.notna(existing_ts):
        earliest_ts = existing_ts
    else:
        earliest_ts = incoming_ts
    earliest_ts = earliest_ts.isoformat() if pd.notna(earliest_ts) else None

    return {
        # Fill in a missing structured field from the incoming row; never clobber a
        # value the existing row already has (even if the incoming row disagrees --
        # a genuine conflict here is a data-quality question for later, not something
        # to silently overwrite).
        "quantity": existing.get("quantity") if pd.notna(existing.get("quantity")) else incoming.get("quantity"),
        "insider_name": existing.get("insider_name") or incoming.get("insider_name"),
        "transaction_type": existing.get("transaction_type") or incoming.get("transaction_type"),
        "announcement_timestamp": earliest_ts,
        "sources": ",".join(merged_sources),
    }


def _apply_merge(*, source: str, news_id: str, merged_fields: dict) -> None:
    def _update() -> None:
        with db_session() as (_, cur):
            cur.execute(
                """
                UPDATE fundamentals_events
                   SET quantity = %s,
                       insider_name = %s,
                       transaction_type = %s,
                       announcement_timestamp = %s,
                       sources = %s
                 WHERE source = %s AND news_id = %s
                """,
                (
                    merged_fields["quantity"],
                    merged_fields["insider_name"],
                    merged_fields["transaction_type"],
                    merged_fields["announcement_timestamp"],
                    merged_fields["sources"],
                    source,
                    news_id,
                ),
            )

    execute_db_operation(_update, operation_name="fundamentals_events:merge_update")


def _dedup_key(row: dict) -> tuple | None:
    """Same (isin, filing_type, disclosure_date) match key find_dedup_candidate uses
    against the DB, normalized the same way (disclosure_date as text -- see that
    function's docstring) -- None if this row can't participate in dedup at all
    (matches find_dedup_candidate's own isin/filing_type/disclosure_date gate)."""
    isin, filing_type, disclosure_date = row.get("isin"), row.get("filing_type"), row.get("disclosure_date")
    if not isin or not filing_type or disclosure_date is None:
        return None
    return (isin, filing_type, str(disclosure_date))


def upsert_events_with_dedup(rows: list[dict]) -> dict[str, int]:
    """Insert new fundamentals_events rows, merging into an existing cross-source match
    instead of inserting a duplicate -- see module docstring. Rows without an isin
    (identity couldn't be resolved) skip dedup matching entirely and just insert.

    BUG FOUND LIVE 2026-08-15, fixed here: find_dedup_candidate() only ever queries
    the DB, so two rows sharing a dedup key WITHIN one call's own `rows` batch (a
    company filing the same disclosure twice in one day, or two collectors both
    picking up the same event in one run) each independently found no DB match
    (neither had been flushed yet) and both got inserted separately -- confirmed
    live: 167 orphaned duplicate rows across 73 (isin, filing_type, disclosure_date)
    groups, ~14% of all BSE-sourced events. staged_by_key tracks rows already
    decided as new-inserts EARLIER IN THIS SAME BATCH so a later row in the batch
    merges into the staged row instead of becoming a second insert."""
    if not rows:
        return {"inserted": 0, "merged": 0}

    _ensure_events_schema()

    to_insert: list[dict] = []
    staged_by_key: dict[tuple, int] = {}
    merged = 0

    for row in rows:
        candidate = find_dedup_candidate(row.get("isin"), row.get("filing_type"), row.get("disclosure_date"))
        if candidate is not None and (candidate["source"], candidate["news_id"]) != (row["source"], row["news_id"]):
            merged_fields = _merge_row_fields(candidate, row)
            _apply_merge(source=candidate["source"], news_id=candidate["news_id"], merged_fields=merged_fields)
            merged += 1
            continue

        key = _dedup_key(row)
        staged_idx = staged_by_key.get(key) if key is not None else None
        if staged_idx is not None:
            staged_row = to_insert[staged_idx]
            if (staged_row["source"], staged_row["news_id"]) != (row["source"], row["news_id"]):
                merged_fields = _merge_row_fields(staged_row, row)
                to_insert[staged_idx] = {**staged_row, **merged_fields}
                merged += 1
                continue

        to_insert.append(row)
        if key is not None:
            staged_by_key[key] = len(to_insert) - 1

    if to_insert:
        upsert_to_db(pd.DataFrame(to_insert), RESULTS_TABLE, unique_keys=["source", "news_id"])

    return {"inserted": len(to_insert), "merged": merged}
