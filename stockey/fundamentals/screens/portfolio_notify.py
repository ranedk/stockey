"""Portfolio ACTION email -- what to enter and what to exit, and nothing else.

Replaces the watchlist digest as the outbound email (operator, 2026-09-07: "the email will
only tell when to exit and when to enter"). The watchlist itself is still built, scored and
narrated every night -- it is simply read on the /watchlist page now rather than mailed.

THIS RUNS AT THE END OF THE PORTFOLIO CHAIN, NOT IN THE SCREENER PIPELINE, and the ordering
is the whole reason this module exists separately. The screener sends at 21:00 IST; the
entry/exit decisions are not made until the portfolio job at 22:00. An action email built
inside the screener would therefore have reported YESTERDAY's actions every single night,
while looking perfectly correct.

IT SENDS EVERY RUN, INCLUDING WHEN THERE IS NOTHING TO DO. That is deliberate and it is the
one design point worth defending: an email that only arrives when something happened makes
"quiet day" and "pipeline broken" look identical from the outside. This repo has been
bitten by exactly that twice -- go-crond died silently for 5 days, and Dhan collection died
for 5 days while every freshness check stayed green. A one-line "no action today" is cheap;
a silence you cannot interpret is not.

Actions are read from the DATABASE by IST date rather than passed in from the runner, so
the email is correct whether the chain ran end-to-end or a step was re-run by hand.
"""
from __future__ import annotations

import argparse
import html
import json

import pandas as pd

from fundamentals.screens.notifications import (
    WATCHLIST_ALERT_EMAIL_ENABLED,
    _record_fallback,
    send_email,
)
from utils.db import sql_to_df

SYNC_SOURCE_NAME = "fundamentals.screens.portfolio_notify"

EXIT_REASON_LABEL = {
    "stop_loss": "STOP HIT",
    "thesis_invalidation": "thesis invalidated",
    "target_date": "target date reached",
}

_STYLE = (
    "body{font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif;background:#f8fafc;"
    "color:#0f172a;margin:0;padding:24px}"
    ".card{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:20px;margin-bottom:16px}"
    "table{width:100%;border-collapse:collapse;font-size:14px}"
    "th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.03em;color:#64748b;"
    "border-bottom:1px solid #e2e8f0;padding:8px 10px}"
    "td{padding:10px;border-bottom:1px solid #f1f5f9;vertical-align:top}"
    "tr:last-child td{border-bottom:none}"
    "h1{font-size:18px;margin:0 0 4px}"
    ".sub{color:#64748b;font-size:13px;margin:0 0 10px}"
    ".pos{color:#059669;font-weight:600}.neg{color:#dc2626;font-weight:600}"
    ".enter h1{color:#047857}.exit h1{color:#b91c1c}"
    ".banner{background:#fffbeb;border:1px solid #fde68a;color:#92400e;border-radius:6px;"
    "padding:8px 12px;font-size:12px;margin:0 0 14px}"
    ".quiet{color:#475569;font-size:14px}"
    ".thesis{font-size:12px;color:#334155}"
    ".rule{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;color:#64748b}"
    ".footer{color:#94a3b8;font-size:12px;margin-top:8px}"
)


def _ist_today() -> str:
    return str(sql_to_df("SELECT (now() AT TIME ZONE 'Asia/Kolkata')::date AS d").iloc[0]["d"])


def load_actions(*, as_of=None) -> dict[str, list[dict]]:
    """Today's entries, exits and deferrals, by IST date.

    Vetoed candidates are deliberately absent: a name the adjudicator rejected is not an
    action, it is a non-event. It is still recorded and still scored -- on the /portfolio
    page, not here.
    """
    day = as_of or _ist_today()
    entered = sql_to_df(
        """
        SELECT ticker, entry_price, stop_pct, position_size_rs, target_date,
               prediction_text, metric_name, metric_operator, metric_threshold,
               confluence_count, evaluable_count, stage_at_entry, kind, adjudicator_reason
          FROM fundamentals_portfolio_position
         WHERE entry_decision = 'accept'
           AND (opened_at AT TIME ZONE 'Asia/Kolkata')::date = %s
         ORDER BY ticker
        """,
        params=(day,),
    )
    exited = sql_to_df(
        """
        SELECT ticker, entry_price, exit_price, close_reason, stop_pct, kind,
               opened_at, closed_at, prediction_text
          FROM fundamentals_portfolio_position
         WHERE status = 'closed'
           AND (closed_at AT TIME ZONE 'Asia/Kolkata')::date = %s
         ORDER BY ticker
        """,
        params=(day,),
    )
    deferred = sql_to_df(
        """
        SELECT ticker, deferral_count, entry_price, stop_pct, prediction_text
          FROM fundamentals_portfolio_position
         WHERE status = 'open'
           AND (last_deferred_at AT TIME ZONE 'Asia/Kolkata')::date = %s
         ORDER BY ticker
        """,
        params=(day,),
    )
    return {
        "day": day,
        "entered": [] if entered.empty else entered.to_dict("records"),
        "exited": [] if exited.empty else exited.to_dict("records"),
        "deferred": [] if deferred.empty else deferred.to_dict("records"),
    }


def load_book_summary() -> dict:
    df = sql_to_df(
        """
        SELECT count(*) FILTER (WHERE status = 'open' AND entry_decision = 'accept') AS open_count,
               coalesce(sum(position_size_rs) FILTER (
                   WHERE status = 'open' AND entry_decision = 'accept'), 0) AS committed_rs,
               count(*) FILTER (WHERE status = 'open' AND entry_decision = 'accept'
                                  AND kind = 'real') AS real_count
          FROM fundamentals_portfolio_position
        """
    )
    r = df.iloc[0]
    return {"open_count": int(r["open_count"]), "committed_rs": float(r["committed_rs"]),
            "is_live": int(r["real_count"]) > 0}


def _rupees(v) -> str:
    try:
        return f"Rs {float(v):,.0f}"
    except (TypeError, ValueError):
        return "-"


def _price(v) -> str:
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "-"


def _return_pct(entry, exit_) -> float | None:
    try:
        return (float(exit_) / float(entry) - 1) * 100
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def build_action_email(actions: dict, book: dict) -> tuple[str, str, str]:
    """(subject, text, html). Entries and exits only."""
    day, entered, exited, deferred = (actions["day"], actions["entered"],
                                      actions["exited"], actions["deferred"])
    mode = "LIVE" if book["is_live"] else "RECORD-ONLY"

    bits = []
    if entered:
        bits.append(f"{len(entered)} enter")
    if exited:
        bits.append(f"{len(exited)} exit")
    subject = f"[Portfolio] {', '.join(bits) if bits else 'No action'} -- {day}"

    text = [f"Portfolio actions for {day} ({mode}).", ""]
    cards = []

    if entered:
        text.append(f"ENTER ({len(entered)}):")
        rows = []
        for e in entered:
            rule = (f"{e['metric_name']} {e['metric_operator']} {e['metric_threshold']}"
                    if e.get("metric_name") else "")
            stop_level = None
            try:
                stop_level = float(e["entry_price"]) * (1 - float(e["stop_pct"]) / 100)
            except (TypeError, ValueError):
                pass
            text.append(
                f"  {e['ticker']}: buy {_rupees(e.get('position_size_rs'))} at "
                f"{_price(e.get('entry_price'))} | stop {e.get('stop_pct')}% "
                f"({_price(stop_level)}) | target {e.get('target_date')}"
            )
            if e.get("prediction_text"):
                text.append(f"      thesis: {e['prediction_text']}")
            if rule:
                text.append(f"      resolves on: {rule}")
            rows.append(
                "<tr>"
                f"<td><strong>{html.escape(str(e['ticker']))}</strong></td>"
                f"<td>{html.escape(_rupees(e.get('position_size_rs')))}<br>"
                f"<span class='rule'>at {html.escape(_price(e.get('entry_price')))}</span></td>"
                f"<td>{html.escape(str(e.get('stop_pct')))}%<br>"
                f"<span class='rule'>{html.escape(_price(stop_level))}</span></td>"
                f"<td>{html.escape(str(e.get('target_date')))}</td>"
                f"<td class='thesis'>{html.escape(str(e.get('prediction_text') or '-'))}"
                + (f"<div class='rule'>resolves on: {html.escape(rule)}</div>" if rule else "")
                + "</td></tr>"
            )
        cards.append(
            "<div class='card enter'><h1>Enter</h1>"
            f"<p class='sub'>{len(entered)} position(s) the ruleset passed and the adjudicator accepted.</p>"
            "<table><tr><th>Ticker</th><th>Size</th><th>Stop</th><th>Target</th><th>Thesis</th></tr>"
            f"{''.join(rows)}</table></div>"
        )

    if exited:
        text.append("")
        text.append(f"EXIT ({len(exited)}):")
        rows = []
        for x in exited:
            ret = _return_pct(x.get("entry_price"), x.get("exit_price"))
            ret_s = "-" if ret is None else f"{ret:+.1f}%"
            label = EXIT_REASON_LABEL.get(str(x.get("close_reason")), str(x.get("close_reason")))
            held = ""
            try:
                held = f"{(pd.Timestamp(x['closed_at']) - pd.Timestamp(x['opened_at'])).days}d held"
            except Exception:  # noqa: BLE001
                pass
            text.append(
                f"  {x['ticker']}: SELL -- {label} | {_price(x.get('entry_price'))} -> "
                f"{_price(x.get('exit_price'))} ({ret_s}) {held}"
            )
            tone = "neg" if (ret is not None and ret < 0) else "pos"
            rows.append(
                "<tr>"
                f"<td><strong>{html.escape(str(x['ticker']))}</strong></td>"
                f"<td>{html.escape(label)}</td>"
                f"<td>{html.escape(_price(x.get('entry_price')))} &rarr; "
                f"{html.escape(_price(x.get('exit_price')))}</td>"
                f"<td class='{tone}'>{html.escape(ret_s)}</td>"
                f"<td class='rule'>{html.escape(held)}</td></tr>"
            )
        cards.append(
            "<div class='card exit'><h1>Exit</h1>"
            f"<p class='sub'>{len(exited)} position(s) a mechanical rule closed.</p>"
            "<table><tr><th>Ticker</th><th>Why</th><th>Entry &rarr; Exit</th><th>Return</th>"
            f"<th></th></tr>{''.join(rows)}</table></div>"
        )

    if deferred:
        text.append("")
        text.append(f"DEFERRED ({len(deferred)}) -- exit triggered, adjudicator held one cycle:")
        for d in deferred:
            text.append(f"  {d['ticker']} (deferred {d.get('deferral_count')}x -- exits next run unless it recovers)")
        rows = "".join(
            f"<tr><td><strong>{html.escape(str(d['ticker']))}</strong></td>"
            f"<td class='rule'>deferred {html.escape(str(d.get('deferral_count')))}x &mdash; "
            "exits next run unless it recovers</td></tr>" for d in deferred
        )
        cards.append(
            "<div class='card'><h1>Deferred</h1>"
            "<p class='sub'>An exit rule fired and the adjudicator held it for ONE cycle. "
            "No action today; expect these to close next run.</p>"
            f"<table>{rows}</table></div>"
        )

    if not (entered or exited or deferred):
        text.append("No entries or exits today.")
        cards.append(
            "<div class='card'><h1>No action today</h1>"
            "<p class='quiet'>The ruleset opened nothing and closed nothing. This email is sent "
            "every run precisely so that a quiet day and a broken pipeline do not look the "
            "same from your inbox.</p></div>"
        )

    footer = (f"Book: {book['open_count']} open, {_rupees(book['committed_rs'])} committed.")
    text.extend(["", footer,
                 "" if book["is_live"] else "RECORD-ONLY -- no real money is committed."])

    banner = ("" if book["is_live"] else
              "<div class='banner'>RECORD-ONLY &mdash; no real money is committed. These are "
              "recorded decisions, sized as they would be if they were real.</div>")
    html_body = (
        f"<html><head><meta charset='utf-8'><style>{_STYLE}</style></head><body>"
        f"<div class='card'><h1>Portfolio actions</h1><p class='sub'>{html.escape(day)}</p>"
        f"{banner}<p class='quiet'>{html.escape(footer)}</p></div>"
        f"{''.join(cards)}"
        "<p class='footer'>Generated by the nightly ruleset. Entries and exits only &mdash; "
        "the watchlist lives on the screener's /watchlist page.</p></body></html>"
    )
    return subject, "\n".join(text), html_body


def send_portfolio_actions(*, as_of=None, dry_run: bool = False) -> dict[str, object]:
    if not WATCHLIST_ALERT_EMAIL_ENABLED:
        return {"source": SYNC_SOURCE_NAME, "status": "ok", "sent": 0, "skipped_disabled": 1}
    try:
        actions = load_actions(as_of=as_of)
        book = load_book_summary()
        subject, text_body, html_body = build_action_email(actions, book)
    except Exception as exc:  # noqa: BLE001
        _record_fallback(
            "portfolio_action_email_build_failed",
            reason="Could not build the portfolio action email; no email sent this run.",
            error=exc, severity="error",
        )
        return {"source": SYNC_SOURCE_NAME, "status": "error", "sent": 0, "failed": 1,
                "error": f"{type(exc).__name__}: {exc}"}

    result = {"source": SYNC_SOURCE_NAME, "status": "ok", "day": actions["day"],
              "entered": len(actions["entered"]), "exited": len(actions["exited"]),
              "deferred": len(actions["deferred"]), "subject": subject}
    if dry_run:
        return {**result, "sent": 0, "dry_run": True}
    sent = send_email(subject, text_body, html_body)
    return {**result, "sent": 1 if sent else 0, "failed": 0 if sent else 1}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Email today's portfolio entries and exits.")
    parser.add_argument("--dry-run", action="store_true", help="Build but do not send.")
    parser.add_argument("--as-of", default=None, help="IST date (YYYY-MM-DD), default today.")
    args = parser.parse_args(argv)
    out = send_portfolio_actions(as_of=args.as_of, dry_run=args.dry_run)
    print(json.dumps(out, ensure_ascii=False, default=str), flush=True)
    return 0 if out.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
