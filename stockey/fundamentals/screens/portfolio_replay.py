"""Paper-portfolio replay -- the uncapped record and ruleset v4 beside the frozen v3 book
(LEDGER row 59, systrader research/preregistrations/2026-10-16_portfolio_v4_and_uncapped.md).

Re-runs the books from V3_GO_LIVE to the latest close with POINT-IN-TIME inputs, using v3's own
rule functions (portfolio_v3): daily story scores, systrader's stage as of the day, L2 valuation
and hard alerts as of the day, stop sizes from closes up to the day. Every night it replays
from scratch; the expensive inputs are cached, so the history is identical run to run:
  fundamentals_replay_stage    the stage API's answer per day
  fundamentals_replay_verdict  the adjudicator's verdict per (company, day): v3's recorded one
                               where v3 asked that name that day, else asked once and kept

Books (record-only, Rs 1 crore):
  v3_replay  v3 sizing -- shows the engine reproduces the real v3 book (differences listed)
  uncapped   every accepted name at Rs 4 lakh, no position or capital limit
  v4         inverse-volatility weights 2-8%, sector <= 25%, 25 names; no new entries while the
             Nifty 50 closes below its 50-session average

Known approximations (stated in the pre-registration): watchlist membership is rebuilt from
first_seen_at and status_updated_at (a name whose status changed twice is read by its last
change); the live runner's catch-up cohort is not replayed; a veto stands 20 sessions
(live: until the evidence moves); a story_fading exit is not deferred by the exit adjudicator.
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field

import pandas as pd

from utils.db import db_session, sql_to_df, upsert_to_db

from fundamentals.screens import portfolio_v3 as v3

V3_GO_LIVE = pd.Timestamp("2026-10-02")
CAPITAL_RS = 10_000_000.0
TARGET_POSITIONS = 25
UNCAPPED_SIZE_RS = CAPITAL_RS / TARGET_POSITIONS
STOP_COOLDOWN_DAYS = 14
VETO_STANDS_SESSIONS = 20
BAND_ENTER, BAND_STAY = 80.0, v3.BAND_EXIT
FLOOR_INDEX, FLOOR_WINDOW = "Nifty 50", 50
STAGE_API = os.environ.get("SYSTRADER_API", "http://localhost:8090")
BOOKS = ("v3_replay", "uncapped", "v4")

STAGE_TABLE = "fundamentals_replay_stage"
VERDICT_TABLE = "fundamentals_replay_verdict"
POSITION_TABLE = "fundamentals_replay_position"
NAV_TABLE = "fundamentals_replay_nav"


def ensure_tables() -> None:
    with db_session() as (_, cur):
        cur.execute(f"""CREATE TABLE IF NOT EXISTS {STAGE_TABLE} (
            as_of DATE NOT NULL, ticker TEXT NOT NULL, stage INT, PRIMARY KEY (as_of, ticker))""")
        cur.execute(f"""CREATE TABLE IF NOT EXISTS {VERDICT_TABLE} (
            company_master_id TEXT NOT NULL, as_of DATE NOT NULL, decision TEXT NOT NULL, reason TEXT,
            model TEXT, target_date DATE, source TEXT NOT NULL, decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (company_master_id, as_of))""")
        cur.execute(f"""CREATE TABLE IF NOT EXISTS {POSITION_TABLE} (
            book TEXT NOT NULL, ticker TEXT NOT NULL, opened_on DATE NOT NULL, company_master_id TEXT,
            entry_price DOUBLE PRECISION, shares DOUBLE PRECISION, size_rs DOUBLE PRECISION,
            weight DOUBLE PRECISION, story_score_at_entry DOUBLE PRECISION, sector_code TEXT,
            stop_pct DOUBLE PRECISION, daily_vol_pct DOUBLE PRECISION, status TEXT NOT NULL,
            closed_on DATE, exit_price DOUBLE PRECISION, exit_reason TEXT, realized_rs DOUBLE PRECISION,
            trim_count INT NOT NULL DEFAULT 0, replayed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (book, ticker, opened_on))""")
        cur.execute(f"""CREATE TABLE IF NOT EXISTS {NAV_TABLE} (
            book TEXT NOT NULL, as_of DATE NOT NULL, open_positions INT, deployed_rs DOUBLE PRECISION,
            market_value_rs DOUBLE PRECISION, realized_rs DOUBLE PRECISION, unrealized_rs DOUBLE PRECISION,
            entries INT, exits INT, PRIMARY KEY (book, as_of))""")


# --- point-in-time inputs ----------------------------------------------------------------------

def trading_days(start, end) -> list[pd.Timestamp]:
    df = sql_to_df("SELECT date FROM dim_trading_days WHERE date BETWEEN %s AND %s ORDER BY date",
                   params=(str(pd.Timestamp(start).date()), str(pd.Timestamp(end).date())))
    return [pd.Timestamp(d).tz_localize(None) if pd.Timestamp(d).tzinfo else pd.Timestamp(d) for d in df["date"]]


def stages_on(day: pd.Timestamp) -> dict[str, int]:
    """Cached per day; the API is asked once per day ever."""
    d = str(day.date())
    df = sql_to_df(f"SELECT ticker, stage FROM {STAGE_TABLE} WHERE as_of = %s", params=(d,))
    if not df.empty:
        return dict(zip(df["ticker"], df["stage"].astype(int)))
    with urllib.request.urlopen(f"{STAGE_API}/api/stage?as_of={d}", timeout=600) as resp:
        rows = json.loads(resp.read().decode())["rows"]
    out = {r["ticker"]: int(r["stage"]) for r in rows if r.get("stage")}
    if not out:
        raise RuntimeError(f"stage API returned nothing for {d} -- refusing to replay a day on no stages")
    upsert_to_db(pd.DataFrame({"as_of": d, "ticker": list(out), "stage": list(out.values())}),
                 STAGE_TABLE, ["as_of", "ticker"], on_conflict="nothing")
    return out


@dataclass
class Inputs:
    days: list[pd.Timestamp]
    scores: pd.DataFrame            # date, company_master_id, symbol, story_score, has_flaw
    closes: pd.DataFrame            # date x symbol adjusted close
    valuation: dict                 # company -> [(run_date, ratio)] ascending
    alerts: pd.DataFrame            # company_master_id, trigger_type, load_ts, alert_date
    sectors: dict
    narratives: pd.DataFrame
    reads: pd.DataFrame
    floor_ok: dict                  # day -> bool (index at/above its moving average)
    metrics: list = field(default_factory=list)


def load_inputs(end: pd.Timestamp | None = None) -> Inputs:
    from fundamentals.screens.portfolio_adjudicator import numeric_l2_metrics

    scores = sql_to_df("""SELECT date, company_master_id, symbol, story_score, has_flaw
                            FROM fundamentals_story_filter_daily WHERE date >= '2026-09-29' AND symbol IS NOT NULL""")
    # A score's `date` is the IST date it was COMPUTED, and the nightly run often finishes after
    # midnight: the 10-05 evening's scores are dated 10-06 (00:19 IST). Each score set is placed
    # on the EVENING its run belongs to (last write minus 6 h), and that evening's close is the
    # price -- otherwise the replay would trade on the next day's close. A date written twice
    # keeps only the later run (10-07's evening was overwritten by 10-08's), so that evening is
    # simply absent.
    runs = sql_to_df("""SELECT as_of_date, max(load_ts) AS last_write FROM fundamentals_story_score
                         WHERE as_of_date >= '2026-09-29' GROUP BY 1""")
    evening = {pd.Timestamp(r.as_of_date): (pd.Timestamp(r.last_write).tz_convert("Asia/Kolkata")
                                            - pd.Timedelta(hours=6)).normalize().tz_localize(None)
               for r in runs.itertuples()}
    scores["date"] = pd.to_datetime(scores["date"]).map(lambda d: evening.get(d, d))
    last = scores["date"].max() if end is None else min(scores["date"].max(), pd.Timestamp(end))
    # the portfolio runs on weekday evenings, holidays included (10-02 used the 10-01 close)
    days = sorted(d for d in scores["date"].unique() if V3_GO_LIVE <= d <= last and pd.Timestamp(d).weekday() < 5)
    days = [pd.Timestamp(d) for d in days]
    syms = sorted(scores["symbol"].unique())
    px = sql_to_df("""SELECT symbol, date::date AS date, adj_close FROM advisory_adjusted_ohlcv_daily
                       WHERE symbol = ANY(%s) AND date >= '2026-07-01' AND date < %s::date + 1""",
                   params=(syms, str(last.date())))
    px["date"] = pd.to_datetime(px["date"])
    closes = px.pivot_table(index="date", columns="symbol", values="adj_close", aggfunc="last").sort_index()
    val = sql_to_df("""SELECT m.company_master_id, s.run_date, s.valuation_vs_own_history_ratio AS ratio
                         FROM fundamentals_l2_state s
                         JOIN (SELECT DISTINCT ON (screener_company_id) screener_company_id, company_master_id
                                 FROM fundamentals_snapshot_daily ORDER BY screener_company_id, as_of_date DESC) m
                           ON m.screener_company_id::bigint = s.company_id
                        WHERE s.run_date >= '2026-06-01'""")
    valuation: dict = {}
    for r in val.sort_values("run_date").itertuples():
        valuation.setdefault(str(r.company_master_id), []).append((pd.Timestamp(r.run_date).tz_localize(None)
                                                                  if pd.Timestamp(r.run_date).tzinfo else pd.Timestamp(r.run_date), r.ratio))
    alerts = sql_to_df("""SELECT company_master_id, trigger_type, load_ts, alert_date FROM fundamentals_l3_alerts
                           WHERE trigger_type = ANY(%s) AND load_ts >= '2026-08-01'""",
                       params=(list(v3.HARD_NEGATIVE_TRIGGERS),))
    sectors = dict(sql_to_df("SELECT company_master_id, sector_code FROM fundamentals_company_sector").to_numpy())
    narratives = sql_to_df("""SELECT company_master_id, narrative_text, first_seen_at, status, status_updated_at
                                FROM fundamentals_watchlist""").set_index("company_master_id")
    reads = sql_to_df("""SELECT company_master_id, read_at, story, deal_breaker, direction, materiality
                           FROM fundamentals_story_read WHERE read_at >= '2026-08-15'""")
    idx = sql_to_df("SELECT date, close FROM nseindia_indices WHERE index_name = %s AND date >= '2026-05-01' ORDER BY date",
                    params=(FLOOR_INDEX,))
    idx["date"] = pd.to_datetime(idx["date"]).dt.tz_localize(None) if pd.to_datetime(idx["date"]).dt.tz is not None else pd.to_datetime(idx["date"])
    idx = idx.drop_duplicates("date").set_index("date")["close"].astype(float)
    sma = idx.rolling(FLOOR_WINDOW).mean()
    floor_ok = {d: bool(idx.asof(d) >= sma.asof(d)) if pd.notna(sma.asof(d)) else True for d in days}
    return Inputs(days, scores, closes, valuation, alerts, sectors, narratives, reads, floor_ok, numeric_l2_metrics())


def close_on(inp: Inputs, sym: str, day) -> float | None:
    if sym not in inp.closes.columns:
        return None
    s = inp.closes[sym].loc[:day].dropna()
    return float(s.iloc[-1]) if len(s) and (day - s.index[-1]).days <= 5 else None


def on_watchlist(inp: Inputs, cmid: str, day) -> bool:
    """Active on the watchlist that evening: added by then, and either still active or its
    status changed only later (a name that faded on 10-12 was active on 10-09)."""
    if cmid not in inp.narratives.index:
        return False
    w = inp.narratives.loc[cmid]
    end = _ist_end(day)
    if pd.isna(w["first_seen_at"]):
        return False
    first = pd.Timestamp(w["first_seen_at"])
    first = first.tz_localize("Asia/Kolkata") if first.tzinfo is None else first   # a DATE column: IST day
    if first > end:
        return False
    if w["status"] == "active":
        return True
    changed = w["status_updated_at"]
    return pd.notna(changed) and pd.Timestamp(changed) > end


def valuation_on(inp: Inputs, cmid: str, day) -> float | None:
    hist = [r for d, r in inp.valuation.get(cmid, []) if d <= day]
    return None if not hist or hist[-1] is None or pd.isna(hist[-1]) else float(hist[-1])


def _ist_end(day) -> pd.Timestamp:
    return (pd.Timestamp(day) + pd.Timedelta(hours=23, minutes=59)).tz_localize("Asia/Kolkata").tz_convert("UTC")


def hard_events(inp: Inputs, cmid: str, upto) -> list[dict]:
    a = inp.alerts[(inp.alerts["company_master_id"] == cmid)]
    a = a[pd.to_datetime(a["load_ts"], utc=True) <= _ist_end(upto)]
    return a[["trigger_type", "load_ts", "alert_date"]].to_dict("records")


def band_states(inp: Inputs) -> pd.DataFrame:
    """In-band per (day, company) with the live hysteresis: enter at 80, stay while >= 65."""
    s = inp.scores.sort_values("date")
    inband, prev = [], {}
    for r in s.itertuples():
        was = prev.get(r.company_master_id, False)
        now = bool(r.story_score >= BAND_ENTER or (was and r.story_score >= BAND_STAY))
        prev[r.company_master_id] = now
        inband.append(now)
    return s.assign(in_band=inband)


# --- verdicts ----------------------------------------------------------------------------------

def recorded_v3_verdicts() -> dict:
    df = sql_to_df("""SELECT d.company_master_id, d.decided_at, d.decision, d.reason, d.model, p.target_date
                        FROM fundamentals_portfolio_decision d
                        LEFT JOIN fundamentals_portfolio_position p
                          ON p.company_master_id = d.company_master_id AND p.ruleset_version >= 3
                         AND abs(extract(epoch FROM p.opened_at - d.decided_at)) < 600
                       WHERE d.phase = 'entry' AND d.ruleset_version >= 3""")
    out = {}
    for r in df.itertuples():
        day = (pd.Timestamp(r.decided_at).tz_convert("Asia/Kolkata") - pd.Timedelta(hours=6)).normalize().tz_localize(None)
        out[(str(r.company_master_id), day)] = {"decision": r.decision, "reason": r.reason, "model": r.model,
                                                "target_date": r.target_date}
    return out


def verdict_for(candidate: dict, day: pd.Timestamp, recorded: dict, ask_llm: bool) -> dict | None:
    """v3's recorded verdict for that name that day, else the cached replay verdict, else (if
    allowed) the adjudicator asked now with the candidate as of that day."""
    from fundamentals.screens.portfolio_adjudicator import adjudicate_entry

    cmid = candidate["company_master_id"]
    rec = recorded.get((cmid, day))
    if rec:
        return {**rec, "source": "v3_recorded"}
    df = sql_to_df(f"SELECT decision, reason, model, target_date, source FROM {VERDICT_TABLE} "
                   "WHERE company_master_id = %s AND as_of = %s", params=(cmid, str(day.date())))
    if not df.empty:
        return df.iloc[0].to_dict()
    if not ask_llm:
        return None
    v = adjudicate_entry(candidate)
    row = {"company_master_id": cmid, "as_of": str(day.date()), "decision": v["decision"],
           "reason": (v.get("reason") or "")[:2000], "model": v.get("model"),
           "target_date": v.get("target_date"), "source": "replay_llm"}
    upsert_to_db(pd.DataFrame([row]), VERDICT_TABLE, ["company_master_id", "as_of"], on_conflict="nothing")
    return row


# --- books -------------------------------------------------------------------------------------

@dataclass
class Book:
    name: str
    open: dict = field(default_factory=dict)       # ticker -> position dict
    closed: list = field(default_factory=list)
    cooldown: dict = field(default_factory=dict)   # ticker -> blocked until (day)
    log: list = field(default_factory=list)

    @property
    def capped(self) -> bool:
        return self.name != "uncapped"


def _pos_obj(p: dict):
    return pd.Series({"entry_price": p["entry_price"], "stop_pct": p["stop_pct"], "opened_at": p["opened_on"],
                      "daily_vol_pct": p["daily_vol_pct"], "story_score_at_entry": p["story_score_at_entry"],
                      "trim_count": p["trim_count"], "target_date": p.get("target_date")})


def _close(book: Book, t: str, day, price: float, reason: str) -> None:
    p = book.open.pop(t)
    p.update(status="closed", closed_on=day, exit_price=price, exit_reason=reason,
             realized_rs=p.get("realized_rs", 0.0) + p["shares"] * (price - p["entry_price"]))
    book.closed.append(p)
    if reason in ("stop_loss", "trailing_stop"):
        book.cooldown[t] = day + pd.Timedelta(days=STOP_COOLDOWN_DAYS)
    book.log.append((day, t, "exit", reason))


def run_exits(book: Book, inp: Inputs, day, today_scores: dict, recent: dict) -> int:
    n = 0
    for t, p in list(book.open.items()):
        price = close_on(inp, t, day)
        if price is None:
            continue
        hist = inp.closes[t].loc[p["opened_on"]:day].dropna()
        trail_high = float(hist.max()) if len(hist) else None
        sc = today_scores.get(p["company_master_id"])
        live = None if sc is None else {"story_score": sc[0], "flaws": "flaw" if sc[1] else ""}
        act = v3.v3_exit_action(_pos_obj(p), price=price, trail_high=trail_high, live=live,
                                hard_events=[e for e in hard_events(inp, p["company_master_id"], day)
                                             if pd.Timestamp(e["load_ts"]) > _ist_end(p["opened_on"])],
                                recent_scores=recent.get(p["company_master_id"], []),
                                valuation_ratio=valuation_on(inp, p["company_master_id"], day), today=day)
        if not act or act["action"] == "wait":
            continue
        if act["action"] == "trim":
            sold = p["shares"] * v3.TRIM_FRACTION
            p["realized_rs"] = p.get("realized_rs", 0.0) + sold * (price - p["entry_price"])
            p["shares"] -= sold
            p["trim_count"] += 1
            book.log.append((day, t, "trim", act["detail"]))
        else:
            _close(book, t, day, price, act["reason"])
            n += 1
    return n


def candidates_on(inp: Inputs, band: pd.DataFrame, day, stages: dict) -> list[dict]:
    from fundamentals.screens.portfolio_ruleset import compute_stop_pct

    today = band[(band["date"] == day) & band["in_band"] & ~band["has_flaw"].astype(bool)]
    out = []
    for r in today.itertuples():
        t, cmid = str(r.symbol), str(r.company_master_id)
        if not on_watchlist(inp, cmid, day):
            continue
        if stages.get(t) != 2 or close_on(inp, t, day) is None:
            continue
        vr = valuation_on(inp, cmid, day)
        if vr is not None and vr >= v3.TRIM_VALUATION_RATIO:
            continue
        recent_alert = [e for e in hard_events(inp, cmid, day)
                        if pd.Timestamp(e["load_ts"]) >= _ist_end(day) - pd.Timedelta(days=v3.HARD_ALERT_COOLDOWN_DAYS)]
        if recent_alert:
            continue
        rd = inp.reads[(inp.reads["company_master_id"] == cmid)
                       & (pd.to_datetime(inp.reads["read_at"], utc=True) <= _ist_end(day))
                       & (pd.to_datetime(inp.reads["read_at"], utc=True) >= _ist_end(day) - pd.Timedelta(days=30))]
        rd = rd.sort_values("read_at").iloc[-1] if len(rd) else None
        out.append({"company_master_id": cmid, "ticker": t, "stage": 2, "story_score": float(r.story_score),
                    "primary_dimension": None, "sector_code": inp.sectors.get(cmid),
                    "story_read": None if rd is None else {"story": rd["story"], "deal_breaker": rd["deal_breaker"],
                                                           "direction": rd["direction"],
                                                           "materiality": None if pd.isna(rd["materiality"]) else int(rd["materiality"])},
                    "narrative_text": inp.narratives["narrative_text"].get(cmid) if cmid in inp.narratives.index else None,
                    "confluence_count": None, "contradicting_count": None, "evaluable_count": None, "axes": None,
                    "available_l2_metrics": inp.metrics, "valuation_ratio": vr})
    stops = compute_stop_pct([c["ticker"] for c in out], as_of=day) if out else {}
    for c in out:
        c["stop"] = stops.get(c["ticker"])
        c["daily_vol_pct"] = (c["stop"] or {}).get("daily_vol_pct")
    return sorted(out, key=lambda c: -c["story_score"])


def run_entries(book: Book, inp: Inputs, day, cands: list[dict], today_scores: dict, verdicts, vetoes: dict,
                recorded: dict | None = None) -> int:
    if book.name == "v4" and not inp.floor_ok.get(day, True):
        book.log.append((day, "-", "floor", f"{FLOOR_INDEX} below its {FLOOR_WINDOW}-session average: no entries"))
        return 0
    weights = {}
    if book.capped:
        pool = [{"company_master_id": p["company_master_id"], "story_score": today_scores.get(p["company_master_id"], (p["story_score_at_entry"],))[0],
                 "daily_vol_pct": p["daily_vol_pct"], "sector_code": p["sector_code"]} for p in book.open.values()]
        pool += [{k: c[k] for k in ("company_master_id", "story_score", "daily_vol_pct", "sector_code")}
                 for c in cands if c["ticker"] not in book.open]
        if book.name == "v4":   # risk-only: the same caps, no score tilt
            pool = [{**b, "story_score": 1.0} for b in pool]
        weights = v3.target_weights(pool, TARGET_POSITIONS)
    entered = 0
    for c in cands:
        t = c["ticker"]
        if t in book.open or book.cooldown.get(t, day - pd.Timedelta(days=1)) >= day:
            continue
        # A replay veto stops the replay asking again; it never overrides what v3 actually decided
        # that evening (a name the replay vetoed on 10-02, v3 itself accepted on 10-05).
        if vetoes.get(c["company_master_id"], pd.Timestamp.min) >= day and (c["company_master_id"], day) not in (recorded or {}):
            continue
        if book.capped and c["company_master_id"] not in weights:
            continue
        full = book.capped and len(book.open) >= TARGET_POSITIONS
        weakest = None
        if full:
            df = pd.DataFrame([{"position_id": tk, "ticker": tk, "company_master_id": p["company_master_id"],
                                "opened_at": p["opened_on"], "entry_price": p["entry_price"],
                                "story_score": today_scores.get(p["company_master_id"], (None,))[0]}
                               for tk, p in book.open.items()])
            weakest = v3.weakest_replaceable(df, c["story_score"], day)
            if weakest is None:
                continue
        v = verdicts(c, day)
        if v is None:
            continue
        if v["decision"] != "accept":
            vetoes[c["company_master_id"]] = day + pd.offsets.BDay(VETO_STANDS_SESSIONS)
            continue
        if weakest is not None:
            _close(book, weakest["ticker"], day, close_on(inp, weakest["ticker"], day), "replaced")
        price = close_on(inp, t, day)
        size = UNCAPPED_SIZE_RS if not book.capped else weights[c["company_master_id"]] * CAPITAL_RS
        book.open[t] = {"book": book.name, "ticker": t, "company_master_id": c["company_master_id"], "opened_on": day,
                        "entry_price": price, "shares": size / price, "size_rs": size,
                        "weight": weights.get(c["company_master_id"]), "story_score_at_entry": c["story_score"],
                        "sector_code": c["sector_code"], "stop_pct": (c["stop"] or {}).get("stop_pct"),
                        "daily_vol_pct": c["daily_vol_pct"], "status": "open", "trim_count": 0,
                        "target_date": v.get("target_date"), "realized_rs": 0.0}
        book.log.append((day, t, "enter", f"score {c['story_score']:.0f}, {v.get('source')}"))
        entered += 1
    return entered


def replay(end=None, ask_llm: bool = True) -> dict:
    ensure_tables()
    inp = load_inputs(end)
    band = band_states(inp)
    recorded = recorded_v3_verdicts()
    books = {n: Book(n) for n in BOOKS}
    vetoes: dict = {}
    nav_rows = []
    cache: dict = {}

    def verdicts(c, day):
        key = (c["company_master_id"], day)
        if key not in cache:
            cache[key] = verdict_for(c, day, recorded, ask_llm)
        return cache[key]

    for day in inp.days:
        stages = stages_on(day)
        sc = inp.scores[inp.scores["date"] == day]
        today_scores = {str(r.company_master_id): (float(r.story_score), bool(r.has_flaw)) for r in sc.itertuples()}
        past = inp.scores[inp.scores["date"] <= day].sort_values("date", ascending=False)
        recent = past.groupby("company_master_id")["story_score"].apply(lambda s: list(s.head(v3.FADE_RUNS))).to_dict()
        cands = candidates_on(inp, band, day, stages)
        for b in books.values():
            exits = run_exits(b, inp, day, today_scores, recent)
            entries = run_entries(b, inp, day, cands, today_scores, verdicts, vetoes, recorded)
            mv = sum(p["shares"] * (close_on(inp, t, day) or p["entry_price"]) for t, p in b.open.items())
            cost = sum(p["shares"] * p["entry_price"] for p in b.open.values())
            realized = sum(p.get("realized_rs", 0.0) for p in b.closed) + sum(p.get("realized_rs", 0.0) for p in b.open.values())
            nav_rows.append({"book": b.name, "as_of": day.date(), "open_positions": len(b.open), "deployed_rs": round(cost),
                             "market_value_rs": round(mv), "realized_rs": round(realized),
                             "unrealized_rs": round(mv - cost), "entries": entries, "exits": exits})
    _store(books, nav_rows)
    return summarise(books, nav_rows)


def _store(books: dict, nav_rows: list) -> None:
    with db_session() as (_, cur):
        cur.execute(f"DELETE FROM {POSITION_TABLE}")   # a replay is recomputed whole every run
        cur.execute(f"DELETE FROM {NAV_TABLE}")
    rows = []
    for b in books.values():
        for p in list(b.open.values()) + b.closed:
            rows.append({k: p.get(k) for k in ("book", "ticker", "opened_on", "company_master_id", "entry_price", "shares",
                                                "size_rs", "weight", "story_score_at_entry", "sector_code", "stop_pct",
                                                "daily_vol_pct", "status", "closed_on", "exit_price", "exit_reason",
                                                "realized_rs", "trim_count")})
    if rows:
        df = pd.DataFrame(rows)
        for c in ("opened_on", "closed_on"):
            df[c] = pd.to_datetime(df[c]).dt.date
        upsert_to_db(df, POSITION_TABLE, ["book", "ticker", "opened_on"])
    upsert_to_db(pd.DataFrame(nav_rows), NAV_TABLE, ["book", "as_of"])


def summarise(books: dict, nav_rows: list) -> dict:
    nav = pd.DataFrame(nav_rows)
    out = {}
    for name, b in books.items():
        last = nav[nav["book"] == name].iloc[-1]
        pnl = float(last["realized_rs"] + last["unrealized_rs"])
        deployed = sum(p["size_rs"] for p in list(b.open.values()) + b.closed)
        out[name] = {"open": int(last["open_positions"]), "entered": len(b.open) + len(b.closed), "closed": len(b.closed),
                     "pnl_rs": round(pnl), "return_on_capital_pct": round(100 * pnl / CAPITAL_RS, 2),
                     "return_on_deployed_pct": round(100 * pnl / deployed, 2) if deployed else None,
                     "exit_reasons": pd.Series([p["exit_reason"] for p in b.closed]).value_counts().to_dict()}
    return {"from": str(nav["as_of"].min()), "to": str(nav["as_of"].max()), "books": out}


def reconcile_v3() -> dict:
    """The replayed v3 book against the real one: which names differ, and on which side."""
    rep = sql_to_df(f"SELECT ticker, opened_on FROM {POSITION_TABLE} WHERE book = 'v3_replay'")
    real = sql_to_df("""SELECT ticker, (opened_at AT TIME ZONE 'Asia/Kolkata')::date AS opened_on
                          FROM fundamentals_portfolio_position
                         WHERE ruleset_version >= 3 AND entry_decision = 'accept'""")
    a, b = set(rep["ticker"]), set(real["ticker"])
    return {"both": len(a & b), "only_replay": sorted(a - b), "only_real": sorted(b - a)}


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Replay the paper books (v3_replay, uncapped, v4).")
    ap.add_argument("--no-llm", action="store_true", help="never call the adjudicator (cached verdicts only)")
    a = ap.parse_args(argv)
    out = replay(ask_llm=not a.no_llm)
    out["reconcile_v3"] = reconcile_v3()
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
