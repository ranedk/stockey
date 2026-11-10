"""Queue work for the fleet, and look at it.

    python -m infra.cli history SOLARINDS KAYNES --years 2 --tag pilot-2026-11-09
    python -m infra.cli runs [--tag TAG]
    python -m infra.cli add-local --pool test      # a worker token for this machine (testing)
"""
from __future__ import annotations

import argparse
import json
import secrets
from datetime import date, timedelta

from infra import schema, tasks
from infra.fleet import _sha
from utils.db import db_session, sql_to_df


def history(symbols: list[str], years: float, tag: str, ocr: bool = True) -> dict:
    schema.ensure()
    end = date.today()
    start = end - timedelta(days=int(365 * years))
    cm = sql_to_df("""SELECT DISTINCT ON (nse_ticker) nse_ticker AS nse_symbol, bse_scrip_code FROM company_master
                       WHERE nse_ticker = ANY(%(s)s) ORDER BY nse_ticker, bse_scrip_code NULLS LAST""",
                   params={"s": symbols})
    scrip = dict(zip(cm["nse_symbol"], cm["bse_scrip_code"]))
    q = []
    for sym in symbols:
        q.append(tasks.new_task("nse_announcements", {"symbol": sym, "from": start.strftime("%d-%m-%Y"),
                                                      "to": end.strftime("%d-%m-%Y"), "ocr": ocr},
                                dedup_key=f"nse_ann:{sym}:{start}:{end}", run_tag=tag))
        if scrip.get(sym):
            q.append(tasks.new_task("bse_announcements", {"symbol": sym, "scrip": str(int(float(scrip[sym]))),
                                                          "from": str(start), "to": str(end), "ocr": ocr},
                                    dedup_key=f"bse_ann:{sym}:{start}:{end}", run_tag=tag))
    return {"queued": tasks.enqueue(q), "bse_scrip": scrip}


def runs(tag: str | None) -> list[dict]:
    where = "WHERE run_tag = %(t)s" if tag else ""
    return sql_to_df(f"""SELECT run_tag, kind, status, count(*) AS n, round(avg(attempt), 2) AS avg_attempts
                           FROM {schema.TASK} {where} GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""",  # noqa: S608
                     params={"t": tag}).to_dict("records")


def requeue_failed(tag: str, extra_attempts: int = 3) -> int:
    """Failed tasks of a run go back to pending with more attempts; the attempt counter keeps
    counting (resetting it would collide with the recorded attempts)."""
    with db_session() as (_, cur):
        cur.execute(f"""UPDATE {schema.TASK} SET status = 'pending', max_attempts = attempt + %s, error = NULL,
                               not_before = NULL, finished_at = NULL WHERE run_tag = %s AND status = 'failed'""",
                    (extra_attempts, tag))
        return cur.rowcount


def add_local(pool: str) -> dict:
    schema.ensure()
    token = secrets.token_urlsafe(32)
    mid = f"local-{pool}-{secrets.token_hex(2)}"
    with db_session() as (_, cur):
        cur.execute(f"""INSERT INTO {schema.MACHINE} (id, provider, pool, ip, token_sha256, status)
                        VALUES (%s, 'local', %s, '127.0.0.1', %s, 'creating')""", (mid, pool, _sha(token)))
    return {"id": mid, "token": token}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("history")
    h.add_argument("symbols", nargs="+")
    h.add_argument("--years", type=float, default=2.0)
    h.add_argument("--tag", required=True)
    h.add_argument("--no-ocr", action="store_true")
    r = sub.add_parser("runs")
    r.add_argument("--tag")
    rq = sub.add_parser("requeue-failed")
    rq.add_argument("--tag", required=True)
    lo = sub.add_parser("add-local")
    lo.add_argument("--pool", default="test")
    a = ap.parse_args(argv)
    out = (history(a.symbols, a.years, a.tag, not a.no_ocr) if a.cmd == "history" else
           runs(a.tag) if a.cmd == "runs" else
           {"requeued": requeue_failed(a.tag)} if a.cmd == "requeue-failed" else add_local(a.pool))
    print(json.dumps(out, default=str, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
