"""One-time remediation for the cross-collector dedup gap fixed in
fundamentals/collectors/events_store.py (2026-08-15 audit, fixed 2026-08-17):
find_dedup_candidate() only ever queried the DB, so two rows sharing an
(isin, filing_type, disclosure_date) key WITHIN one collector run's own batch
(neither yet flushed to the DB) each independently found no match and both got
inserted -- 73 duplicate groups / 167 rows sharing a key were confirmed live
before this script ran.

IMPORTANT, found live 2026-08-17 while building this: most of those 73 groups
are NOT accidental duplicates. The (isin, filing_type, disclosure_date) key
already drops `quantity` by design (events_store.py's own documented, accepted
tradeoff), so two GENUINELY DIFFERENT real disclosures for the same company on
the same day legitimately collide on it -- confirmed live: only 19/73 groups
have an IDENTICAL headline on every row (true duplicates, safe to merge); the
other 54 have meaningfully different headlines (e.g. INE704H01022/pit_sast/
2026-02-25 is 4 distinct real filings for 2 different named individuals plus 2
separate encumbrance notices). Merging those 54 would DELETE real, distinct
disclosure content, not fix a bug. This script therefore only processes a
group when EVERY row's headline matches exactly -- the 54 headline-differing
groups are left untouched; the dedup key needs to get smarter (e.g. also
compare headline similarity) before those can be safely addressed, which is a
separate, harder design task, not a data-cleanup one.

Merges each qualifying group into its earliest-loaded row, using the exact
same merge semantics events_store.py's own upsert-time merge already uses
(_merge_row_fields/_apply_merge, folded across every sibling in load_ts order),
then deletes the now-redundant sibling rows. Idempotent and safe to re-run --
a re-run after a real --apply finds zero qualifying groups left to process.

Defaults to --dry-run (prints the plan, touches nothing); pass --apply to
actually merge+delete."""

from __future__ import annotations

import argparse
import json

from fundamentals.collectors.events_store import _apply_merge, _merge_row_fields
from utils.db import db_session, execute_db_operation, sql_to_df


def find_duplicate_groups() -> list[dict]:
    df = sql_to_df(
        """
        SELECT isin, filing_type, disclosure_date, count(*) AS n
        FROM fundamentals_events
        WHERE isin IS NOT NULL AND filing_type IS NOT NULL AND disclosure_date IS NOT NULL
        GROUP BY isin, filing_type, disclosure_date
        HAVING count(*) > 1
        ORDER BY isin, filing_type, disclosure_date
        """
    )
    return df.to_dict("records")


def load_group_rows(isin: str, filing_type: str, disclosure_date) -> list[dict]:
    return sql_to_df(
        """
        SELECT source, news_id, quantity, insider_name, transaction_type,
               announcement_timestamp, sources, headline, load_ts
        FROM fundamentals_events
        WHERE isin = %s AND filing_type = %s AND disclosure_date = %s
        ORDER BY load_ts ASC NULLS LAST
        """,
        params=(isin, filing_type, str(disclosure_date)),
    ).to_dict("records")


def _is_true_duplicate_group(rows: list[dict]) -> bool:
    """Only a group where EVERY row's headline matches exactly is treated as an
    accidental duplicate -- see module docstring. A group with differing headlines
    is presumed to be genuinely different disclosures colliding on the dedup key's
    documented accepted tradeoff, not a duplicate, and must not be merged here."""
    headlines = {(r.get("headline") or "").strip() for r in rows}
    return len(headlines) == 1


def _delete_row(*, source: str, news_id: str) -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute("DELETE FROM fundamentals_events WHERE source = %s AND news_id = %s", (source, news_id))

    execute_db_operation(_op, operation_name="fundamentals_events:dedupe_delete")


def _count_orphaned_alerts(source: str, news_id: str) -> int:
    df = sql_to_df(
        "SELECT count(*) AS n FROM fundamentals_l3_alerts WHERE source = %s AND news_id = %s",
        params=(source, news_id),
    )
    return int(df.iloc[0]["n"]) if not df.empty else 0


def _delete_orphaned_alerts(*, source: str, news_id: str) -> None:
    """A removed sibling event may already have its own fundamentals_l3_alerts row
    (this run's own earlier re-evaluation of a pit_sast event that turned out to be
    one of these true-duplicate pairs produced exactly this case live) -- a
    duplicate alert for the same real event is the same double-counting problem
    the dedup exists to prevent in the first place, so it's cleaned up here too,
    not left behind as an orphan referencing a now-deleted event row."""

    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute("DELETE FROM fundamentals_l3_alerts WHERE source = %s AND news_id = %s", (source, news_id))

    execute_db_operation(_op, operation_name="fundamentals_l3_alerts:dedupe_delete_orphan")


def dedupe_group(isin: str, filing_type: str, disclosure_date, *, dry_run: bool) -> dict:
    rows = load_group_rows(isin, filing_type, disclosure_date)
    if len(rows) < 2:
        return {"isin": isin, "filing_type": filing_type, "disclosure_date": str(disclosure_date), "canonical": None, "removed": [], "orphaned_alerts_removed": 0, "skipped_reason": None}
    if not _is_true_duplicate_group(rows):
        return {
            "isin": isin, "filing_type": filing_type, "disclosure_date": str(disclosure_date),
            "canonical": None, "removed": [], "orphaned_alerts_removed": 0,
            "skipped_reason": f"headlines differ across {len(rows)} rows -- likely genuinely different disclosures, not merged",
        }

    canonical = rows[0]
    removed = []
    for sibling in rows[1:]:
        merged_fields = _merge_row_fields(canonical, sibling)
        canonical = {**canonical, **merged_fields}
        removed.append((sibling["source"], sibling["news_id"]))

    orphaned_alerts = sum(_count_orphaned_alerts(source, news_id) for source, news_id in removed)

    if not dry_run:
        _apply_merge(
            source=canonical["source"],
            news_id=canonical["news_id"],
            merged_fields={
                "quantity": canonical["quantity"],
                "insider_name": canonical["insider_name"],
                "transaction_type": canonical["transaction_type"],
                "announcement_timestamp": canonical["announcement_timestamp"],
                "sources": canonical["sources"],
            },
        )
        for source, news_id in removed:
            _delete_orphaned_alerts(source=source, news_id=news_id)
            _delete_row(source=source, news_id=news_id)

    return {
        "orphaned_alerts_removed": orphaned_alerts,
        "isin": isin,
        "filing_type": filing_type,
        "disclosure_date": str(disclosure_date),
        "canonical": [canonical["source"], canonical["news_id"]],
        "removed": [list(r) for r in removed],
        "skipped_reason": None,
    }


def run(*, dry_run: bool) -> dict:
    groups = find_duplicate_groups()
    details = [dedupe_group(g["isin"], g["filing_type"], g["disclosure_date"], dry_run=dry_run) for g in groups]
    merged = [d for d in details if d["skipped_reason"] is None and d["canonical"] is not None]
    skipped = [d for d in details if d["skipped_reason"] is not None]
    return {
        "dry_run": dry_run,
        "groups_total": len(groups),
        "groups_merged": len(merged),
        "groups_skipped_differing_headlines": len(skipped),
        "rows_removed": sum(len(d["removed"]) for d in merged),
        "orphaned_alerts_removed": sum(d["orphaned_alerts_removed"] for d in merged),
        "merged": merged,
        "skipped": skipped,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge existing fundamentals_events duplicate groups (pre-dates the 2026-08-17 in-batch dedup fix).")
    parser.add_argument("--apply", action="store_true", help="Actually merge and delete (default is --dry-run).")
    args = parser.parse_args()
    result = run(dry_run=not args.apply)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
