"""Price-data sanity audit -- institutionalizes the price/adjustment bug hunt (2026-07-13).

Every backtest depends on nseindia_ohlcv. This session found five silent data issues that quietly
distorted results: corporate-action splits on UNADJUSTED prices counted as real -90% losses; EQ->BE
(Trade-to-Trade) migrations dropped by EQ-only queries; cross-source (nseindia vs dhan) adjustment
mismatches; an unpopulated adjusted-price table; and benchmark-calendar gaps that misalign excess
windows. This audit surfaces all five on the raw data, so any consumer knows the hazards BEFORE it
computes returns -- rather than each backtest rediscovering them by hand.

Read-only. Model: scripts/funnel_invariants.py (Finding / build_report / format_text_report / main).
Run: `python scripts/price_data_sanity.py --format text`. DB checks are best-effort (a clean skip if
the DB is unavailable). Findings are WARNINGs by default (they describe hazards to guard against, not
crashes); only a broken benchmark calendar is an ERROR.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass

# circuit-implausible single-day step = split/bonus/data artifact on unadjusted prices
CA_STEP_LOW = 0.65
CA_STEP_HIGH = 1.5
LOOKBACK_DAYS = 250
CROSS_SOURCE_MISMATCH_PCT = 0.10
ADJUSTED_TABLE_MIN_SYMBOLS = 100  # below this, advisory_adjusted_ohlcv_daily is unusable for adjustment


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {"severity": self.severity, "code": self.code, "detail": self.detail}


def _one(sql: str, params: dict | None = None):
    from utils.db import sql_to_df
    return sql_to_df(sql, params=params)


def build_report(*, lookback_days: int = LOOKBACK_DAYS) -> dict[str, object]:
    findings: list[Finding] = []
    db_status = "checked"
    try:
        gmax = _one("SELECT MAX(date)::date d FROM nseindia_ohlcv WHERE series='EQ'").iloc[0]["d"]
    except Exception as exc:  # pragma: no cover - db guard
        return {"status": "ok", "db_status": "unavailable", "errors": 0, "warnings": 1,
                "findings": [Finding("warning", "db_unavailable", f"skipped: {exc}").as_dict()]}
    since = f"(SELECT MAX(date) - INTERVAL '{int(lookback_days)} days' FROM nseindia_ohlcv WHERE series='EQ')"

    # 1. corporate-action steps on unadjusted prices (splits/bonuses -> fake huge returns)
    try:
        r = _one(f"""
            WITH s AS (SELECT symbol, close/NULLIF(LAG(close,1) OVER (PARTITION BY symbol ORDER BY date),0) r
                       FROM nseindia_ohlcv WHERE series='EQ' AND date >= {since})
            SELECT COUNT(*) FILTER (WHERE r < {CA_STEP_LOW} OR r > {CA_STEP_HIGH}) steps,
                   COUNT(DISTINCT symbol) FILTER (WHERE r < {CA_STEP_LOW} OR r > {CA_STEP_HIGH}) syms FROM s""")
        steps, syms = int(r.iloc[0]["steps"]), int(r.iloc[0]["syms"])
        if steps:
            findings.append(Finding("warning", "unadjusted_corporate_actions",
                f"{steps} circuit-implausible single-day steps across {syms} symbols (splits/bonuses on "
                "UNADJUSTED prices). Backtests computing returns must guard these or use adjusted prices."))
    except Exception as exc:
        findings.append(Finding("warning", "corporate_action_check_failed", str(exc)[:150]))

    # 2. EQ->BE migrations (symbols still trading in BE after their EQ series ends)
    try:
        r = _one(f"""
            WITH eq AS (SELECT symbol, MAX(date) ld FROM nseindia_ohlcv WHERE series='EQ' GROUP BY symbol),
                 be AS (SELECT symbol, MAX(date) ld FROM nseindia_ohlcv WHERE series='BE' GROUP BY symbol)
            SELECT COUNT(*) n FROM eq JOIN be USING (symbol)
            WHERE be.ld > eq.ld AND eq.ld < (SELECT MAX(date) FROM nseindia_ohlcv WHERE series='EQ') - INTERVAL '5 days'""")
        n = int(r.iloc[0]["n"])
        if n:
            findings.append(Finding("warning", "eq_to_be_migrations",
                f"{n} symbols still trade in BE (Trade-to-Trade) after their EQ series ended. EQ-only "
                "queries lose their forward bars -- use series IN ('EQ','BE')."))
    except Exception as exc:
        findings.append(Finding("warning", "eq_be_check_failed", str(exc)[:150]))

    # 3. cross-source adjustment mismatch (nseindia unadjusted vs dhan adjusted)
    try:
        r = _one(f"""
            SELECT COUNT(DISTINCT n.symbol) syms FROM nseindia_ohlcv n JOIN dhan_ohlcv_daily d
              ON d.ticker=n.symbol AND d.date=n.date
            WHERE n.series='EQ' AND n.date >= {since} AND ABS(n.close/NULLIF(d.close,0)-1) > {CROSS_SOURCE_MISMATCH_PCT}""")
        syms = int(r.iloc[0]["syms"])
        if syms:
            findings.append(Finding("warning", "cross_source_adjustment_mismatch",
                f"{syms} symbols where nseindia (unadjusted) and dhan (adjusted) close disagree >10% -- "
                "mixing the two into one series (e.g. bhavcopy gap-fill) creates a scale jump."))
    except Exception as exc:
        findings.append(Finding("warning", "cross_source_check_failed", str(exc)[:150]))

    # 4. split/bonus-adjusted table populated (advisory.price_adjustment) -- the 'proper fix' for #1/#3
    try:
        r = _one("SELECT COUNT(DISTINCT symbol) syms FROM advisory_adjusted_ohlcv_daily")
        syms = int(r.iloc[0]["syms"])
        if syms < ADJUSTED_TABLE_MIN_SYMBOLS:
            findings.append(Finding("warning", "adjusted_price_table_unpopulated",
                f"advisory_adjusted_ohlcv_daily covers only {syms} symbols -- run `python -m "
                "advisory.price_adjustment` so consumers can read adj_close."))
    except Exception as exc:
        findings.append(Finding("warning", "adjusted_table_check_failed", str(exc)[:150]))

    # 5. benchmark-calendar gaps: EQ trading days with no NIFTY bar in EITHER source (nseindia_indices or
    # dhan's NIFTY INDEX, which _load_benchmark now gap-fills from). A residual gap is a genuine data hole.  [ERROR]
    try:
        r = _one(f"""
            SELECT COUNT(*) gaps FROM (SELECT DISTINCT date::date d FROM nseindia_ohlcv WHERE series='EQ' AND date >= {since}) e
            WHERE NOT EXISTS (SELECT 1 FROM nseindia_indices i WHERE i.index_name ILIKE 'nifty 50' AND i.date::date = e.d)
              AND NOT EXISTS (SELECT 1 FROM dhan_ohlcv_daily d WHERE d.ticker = 'NIFTY' AND d.instrument = 'INDEX' AND d.date::date = e.d)
              AND NOT EXISTS (SELECT 1 FROM dhan_ohlcv_intraday x WHERE x.ticker = 'NIFTY' AND x.exchange_segment = 'IDX_I' AND x.timestamp::date = e.d)""")
        gaps = int(r.iloc[0]["gaps"])
        if gaps:
            findings.append(Finding("error", "benchmark_calendar_gaps",
                f"{gaps} EQ trading days have no NIFTY bar in nseindia_indices OR dhan -- a genuine data hole "
                "(needs an NSE index re-fetch); benchmark/excess windows misalign on those days."))
    except Exception as exc:
        findings.append(Finding("warning", "benchmark_calendar_check_failed", str(exc)[:150]))

    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity == "warning"]
    return {"status": "ok" if not errors else "error", "db_status": db_status,
            "as_of": str(gmax), "errors": len(errors), "warnings": len(warnings),
            "findings": [f.as_dict() for f in findings]}


def format_text_report(report: dict[str, object]) -> str:
    lines = [f"status: {report['status']}", f"db: {report['db_status']}", f"as_of: {report.get('as_of')}",
             f"errors: {report['errors']}", f"warnings: {report['warnings']}"]
    if report["findings"]:
        lines.append("findings:")
        for item in report["findings"]:
            lines.append(f"- {item['severity']} {item['code']}: {item['detail']}")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit the price data every backtest depends on for adjustment/coverage/alignment hazards. Read-only.")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    parser.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS)
    parser.add_argument("--strict", action="store_true", help="Exit non-zero on errors.")
    parser.add_argument("--strict-warnings", action="store_true", help="Exit non-zero on warnings too.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(lookback_days=args.lookback_days)
    print(json.dumps(report, indent=2, sort_keys=True) if args.format == "json" else format_text_report(report))
    if args.strict and report["errors"]:
        return 1
    if args.strict_warnings and (report["errors"] or report["warnings"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
