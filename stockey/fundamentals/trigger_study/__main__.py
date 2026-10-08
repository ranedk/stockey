"""python -m fundamentals.trigger_study <step>

  events     compute momentum entries + outcomes, pick the pilot (--sample full: mark the rest)
  fetch      NSE filing lists for the sample's stocks
  results    NSE XBRL quarterly results (Sep 2019 on) for the sample's stocks
  queue      keyword pre-filter + blind tagging batches
  documents  download attachments for vague / amount-less notable filings, queue them
  run        tag pending batches through the Claude CLI (night window unless --any-time)
  auto       what cron calls: run; once tagging is done, queue documents and run again
  report     write logs/trigger_study_<sample>.json (+ _triggers.csv, _events.csv)
  status     queue and token counts
"""
from __future__ import annotations

import argparse
import json

from utils.db import sql_to_df

from fundamentals.trigger_study.events import samples


def status() -> dict:
    from fundamentals.trigger_study.llm_runner import BATCHES_TABLE, STATE_TABLE, ensure_tables
    ensure_tables()
    b = sql_to_df(f"""SELECT pass, status, count(*) AS batches, sum(input_tokens) AS in_tok,
                             sum(output_tokens) AS out_tok, round(sum(list_cost_usd)::numeric, 2) AS list_usd
                        FROM {BATCHES_TABLE} GROUP BY 1, 2 ORDER BY 1, 2""")
    st = sql_to_df(f"SELECT resume_at, reason FROM {STATE_TABLE}")
    return {"batches": b.to_dict("records"), "state": st.to_dict("records")}


def auto(max_calls: int, any_time: bool) -> dict:
    from fundamentals.trigger_study import llm_runner, queue
    first = llm_runner.run(max_calls=max_calls, any_time=any_time)
    out = {"tag_run": first}
    pending_tag = int(sql_to_df(f"SELECT count(*) AS n FROM {llm_runner.BATCHES_TABLE} "
                                "WHERE pass = 'tag' AND status = 'pending'").iloc[0]["n"])
    if first.get("status") == "ok" and pending_tag == 0:
        out["documents"] = queue.build_document_queue(limit=200)
        left = max_calls - int(first.get("calls", 0))
        if left > 0:
            out["document_run"] = llm_runner.run(max_calls=left, any_time=any_time)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m fundamentals.trigger_study")
    ap.add_argument("step", choices=["events", "fetch", "results", "queue", "documents", "run", "auto", "report", "status"])
    ap.add_argument("--sample", default="pilot")
    ap.add_argument("--per-group", type=int, default=20)
    ap.add_argument("--max-calls", type=int, default=40)
    ap.add_argument("--any-time", action="store_true")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args(argv)

    if a.step == "events":
        from fundamentals.trigger_study.events import build, mark_full
        out = mark_full() if a.sample == "full" else build(per_group=a.per_group)
    elif a.step == "fetch":
        from fundamentals.trigger_study.events import EVENTS_TABLE
        from fundamentals.trigger_study.filings import fetch
        syms = sql_to_df(f"SELECT DISTINCT symbol FROM {EVENTS_TABLE} WHERE sample = ANY(%s)",
                         params=(samples(a.sample),))["symbol"].tolist()
        out = fetch(syms)
    elif a.step == "results":
        from fundamentals.trigger_study.events import EVENTS_TABLE
        from fundamentals.trigger_study.nse_results import fetch as fetch_results
        syms = sql_to_df(f"SELECT DISTINCT symbol FROM {EVENTS_TABLE} WHERE sample = ANY(%s)",
                         params=(samples(a.sample),))["symbol"].tolist()
        out = fetch_results(syms)
    elif a.step == "queue":
        from fundamentals.trigger_study.queue import build_tag_queue
        out = build_tag_queue(a.sample)
    elif a.step == "documents":
        from fundamentals.trigger_study.queue import build_document_queue
        out = build_document_queue(limit=a.limit)
    elif a.step == "run":
        from fundamentals.trigger_study.llm_runner import run
        out = run(max_calls=a.max_calls, any_time=a.any_time)
    elif a.step == "auto":
        out = auto(a.max_calls, a.any_time)
    elif a.step == "report":
        from fundamentals.trigger_study.report import write
        out = {"written": write(a.sample)}
    else:
        out = status()
    print(json.dumps(out, default=str, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
