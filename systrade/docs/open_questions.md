# Open Questions

Answer inline under each question (edit this file directly). Defaults in
*italics* are what the code assumes until you say otherwise. Questions marked
**[blocking]** gate go-live; the rest gate specific features.

## A. Capital & risk

1. **[blocking] Trading capital (₹)?** The number you can afford to lose
   entirely — not net worth, not savings earmarked for anything. Drives the
   four-block test, hence the entire instrument universe. *Default in demo: ₹50L.*
   > 30L

2. **[blocking] Volatility target?** After reading the Ch-9 loss tables: at 20%
   on ₹50L expect a ₹1L losing day every month and a ₹2.2L losing week every
   year. Comfortable? *Default: 20%, hard cap 25% (half-Kelly on deflated SR
   0.4–0.5).*
   > 20%

3. Will profits be rolled up (compounding, Kelly-style) or withdrawn
   periodically? *Default: full roll-up, recomputed daily.*
   > full roll-up, recomputed daily

4. Is there a drawdown level (₹ or %) at which you know you would lose nerve
   and intervene? Better to encode it now as a lower vol target than to meddle
   later. 
   > 40%

## B. Broker, execution & data

5. ~~Which broker?~~ **ANSWERED: Dhan** (client 1106536894), auth reused from
   stockey's CDP login, client in `internal/broker/dhan`.
   > Dhan

   **Fee schedule (confirmed 2026-08-02 from dhan.co/pricing, Phase 3 of the
   pure-TA migration):** stockey's `advisory/cost_model.py` is a research-prior
   heuristic (28bps flat "statutory" + modeled spread/impact), NOT Dhan's real
   schedule, and has no F&O/MCX numbers at all — do not port its coefficients
   as fee truth, only as the spread/impact shape.

   | Segment | Brokerage | STT | Exch. txn | SEBI | Stamp duty | GST |
   |---|---|---|---|---|---|---|
   | Equity delivery | ₹0 | 0.1% buy & sell | NSE 0.0030699% / BSE 0.00375% | 0.0001% | 0.015% (buy) | 18% on brokerage+txn+SEBI+IPFT |
   | Equity intraday/ETF | min(₹20, 0.03% of trade value) per order | 0.025% (sell only) | NSE 0.0030699% / BSE 0.00375% | 0.0001% | 0.003% (buy) | 18% |
   | F&O (equity + commodity, incl. MCX) | ₹20 per executed order (₹20/order flat on expiry/exercise/assignment too) | regulatory rates apply (not broken out per-leg on the pricing page) | regulatory rates apply | regulatory rates apply | regulatory rates apply | 18% on brokerage+charges |
   | DP / pledge | ₹12.50/instruction/ISIN (DP) + GST; ₹15/txn/ISIN (pledge) + GST | — | — | — | — | — |

   Dhan's page does not split F&O regulatory rates (STT/exchange/SEBI) by
   futures vs. options vs. MCX-commodity the way it does for equity — only the
   flat ₹20/order brokerage is segment-uniform. If `internal/data` cost metas
   need per-leg F&O/MCX regulatory bps precision beyond the flat brokerage,
   that still needs a dedicated NSE/MCX circular lookup; the open item now is
   narrowly that, not "which broker" or "roughly what does it cost."

6. ~~Daily OHLCV source?~~ **ANSWERED: stockey → systrade mirror** (stocks) +
   **`systrader_ohlcv_daily` Dhan backfill** (ETFs, index spot, futures, MCX —
   2026-07-24). Remaining sub-questions:
   (a) ~~delisted stocks?~~ **ANSWERED 2026-07-26: yes** —
   `advisory_adjusted_ohlcv_daily` (now primary, CA-adjusted, 2013+) carries
   1,201 symbols whose last bar predates 2026; universe is survivorship-honest.
   (b/c/d) futures near+next & MCX: backfilled, BUT Dhan serves continuous
   slot-splices, not per-contract series — carry is usable now, EWMAC needs
   Panama first (`docs/data_notes.md`). True contract-wise history would need
   NSE/MCX bhavcopy archives if we ever want exact roll-date basis.
   > 

7. History depth: **improved 2026-07-26** — adjusted equity series now spans
   2013-07→present (13yrs) for the older names; `dhan_ohlcv_daily` (2015-11+)
   is fallback. Still worth asking: NSE bhavcopy back to ~2000 for the
   longest-lived symbols? Per Ch-3 tables, 13yrs supports slow-rule claims
   but t-stats on anything fast remain weak.
   > 

8. Execution mode for v1: you place orders manually from a daily report, or
   API auto-execution from day one? *Default: manual from report — matches
   "diligent in design, lazy in operation" and lets us calibrate real slippage.*
   > 

9. When do we compute and trade? *Default: compute after close from EOD data;
   place orders next morning (opening range). Cleaner backtest: assume next-day
   execution, cost model includes overnight gap slippage.*
   > assume next-day execution

## C. Instruments (see docs/instruments_india.md for analysis)

10. **[blocking] Do you have F&O trading approval** (income proof etc.) and an
    MCX-enabled account?
    > yes. Dhan account.

11. Comfortable holding **short** futures positions overnight? (Cash equities
    can't be shorted overnight in India, so shorts exist only in F&O; a
    long-only ETF sleeve is the fallback.)
    > 

12. Any instruments you refuse to trade (religious/ethical/practical —
    e.g. agri commodities, crude)? 
    > No 

13. Roll policy for futures: we must pick roll dates (e.g. T-3 before expiry)
    and build back-adjusted ("Panama-stitched") series. OK to let the system
    own this? *Default: yes, roll 3 sessions before expiry.*
    > 

## D. Taxes & accounting

14. Trading as individual or through an entity? F&O is business income in
    India (affects tax on churn); cash-equity STT changes the ETF-sleeve cost
    model. Any CA constraints we should encode (e.g. avoid intraday
    classification)?
    > 

## E. Research preferences

15. How much daily/weekly time do you want operations to take? *Default
    design target: <15 min/day, one deeper weekly review.*
    > 

16. Paper-trading period before real money: how long can you stand? *Default:
    3 months minimum of frozen-rule forward running.*
    > 

17. For the TimesFM experiment: OK running a Python sidecar service (the model
    is Python/JAX; Go will call it over HTTP)? GPU available, or CPU-only
    (slower, still fine for daily bars)?
    > 

18. GitHub repo (private?) for this project — module path is currently
    `github.com/ranedk/systrader`; confirm or change.
    > 

## F. Libraries (heads-up, not really questions)

19. `techan` and `cinar/indicator` exist for Go; **`ta4g` does not appear to
    exist** (you may be thinking of Java's `ta4j`). v1 deliberately has ZERO
    external deps: EWMA/vol/EWMAC/carry are ~100 lines we must control and
    test ourselves (look-ahead safety). `cinar/indicator/v2` will be added
    when we explore extra technicals (breakout, RSI-class rules). Object if
    you feel strongly.
    > 

20. Anything you want changed about the repo layout / naming before it
    calcifies?
    > 
