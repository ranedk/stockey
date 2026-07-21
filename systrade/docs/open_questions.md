# Open Questions

Answer inline under each question (edit this file directly). Defaults in
*italics* are what the code assumes until you say otherwise. Questions marked
**[blocking]** gate go-live; the rest gate specific features.

## A. Capital & risk

1. **[blocking] Trading capital (₹)?** The number you can afford to lose
   entirely — not net worth, not savings earmarked for anything. Drives the
   four-block test, hence the entire instrument universe. *Default in demo: ₹50L.*
   > 

2. **[blocking] Volatility target?** After reading the Ch-9 loss tables: at 20%
   on ₹50L expect a ₹1L losing day every month and a ₹2.2L losing week every
   year. Comfortable? *Default: 20%, hard cap 25% (half-Kelly on deflated SR
   0.4–0.5).*
   > 

3. Will profits be rolled up (compounding, Kelly-style) or withdrawn
   periodically? *Default: full roll-up, recomputed daily.*
   > 

4. Is there a drawdown level (₹ or %) at which you know you would lose nerve
   and intervene? Better to encode it now as a lower vol target than to meddle
   later. 
   > 

## B. Broker, execution & data

5. **[blocking] Which broker(s)?** Zerodha (Kite Connect), Upstox, Fyers,
   Dhan, IBKR India…? Determines API for later automation and actual fee
   schedule for the cost model.
   > 

6. **[blocking] Daily OHLCV source?** You mentioned you'll provide access —
   which source (broker API historicals, NSE bhavcopy archive, a vendor)?
   Does it include: (a) delisted stocks, (b) futures continuous/contract-wise
   prices, (c) both near and next-month contracts (needed for carry), (d) MCX?
   > 

7. How far back does the history go? <10 years materially weakens what we can
   claim statistically (Ch 3 tables).
   > 

8. Execution mode for v1: you place orders manually from a daily report, or
   API auto-execution from day one? *Default: manual from report — matches
   "diligent in design, lazy in operation" and lets us calibrate real slippage.*
   > 

9. When do we compute and trade? *Default: compute after close from EOD data;
   place orders next morning (opening range). Cleaner backtest: assume next-day
   execution, cost model includes overnight gap slippage.*
   > 

## C. Instruments (see docs/instruments_india.md for analysis)

10. **[blocking] Do you have F&O trading approval** (income proof etc.) and an
    MCX-enabled account?
    > 

11. Comfortable holding **short** futures positions overnight? (Cash equities
    can't be shorted overnight in India, so shorts exist only in F&O; a
    long-only ETF sleeve is the fallback.)
    > 

12. Any instruments you refuse to trade (religious/ethical/practical —
    e.g. agri commodities, crude)? 
    > 

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
