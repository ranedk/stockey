# Pre-registration — fundamental portfolio: an uncapped record and ruleset v4 (paper books)

Written 2026-10-16, before the replay below has produced any number. Operator, 2026-10-16:
the v3 book is full at 25 names, so newer recommendations go unmeasured; also move away from
the score-tilted weights. v3 (LEDGER row 52) stays the frozen official book, untouched.

## 1. Books (record-only, stockey `fundamentals/screens/portfolio_replay.py`)

Common to all three: v3's entry and exit rules exactly (`portfolio_v3.py`) — story band
(enter 80 / stay 65, daily scores), no flaw, Weinstein stage 2 (systrader `/api/stage?as_of=`),
valuation below 2x its own history, no hard negative alert in 14 days, 14-day re-entry cooldown
after a stop, the adjudicator may veto (a veto stands until 20 sessions pass); exits: vol-scaled
stop, trailing stop, thesis broken, story fading, valuation trim (a third, once), tax guard.
Entries and exits at that session's adjusted close. Rs 1 crore.

- **v3-replay** — v3's sizing (story score / volatility, 2-8%, sector <= 25%, 25 names,
  replacement +15 points after 20 sessions). Exists only to show the replay reproduces the real
  v3 book; differences are listed, not hidden.
- **uncapped** — no position limit: every accepted name enters at Rs 4 lakh (1/25 of capital)
  with no capital limit. Measures every recommendation. Return = P&L / capital deployed.
- **v4** — 25 names; weight = 1 / daily volatility (no score tilt — factor research found no
  selection edge in fundamental scores), clipped 2-8%, sector <= 25%; replacement as v3;
  **market floor:** no new entries on a day the Nifty 50 closes below its 50-session average
  (the trend floor is the one deployment edge the research confirmed; this is its milder
  no-new-entries form, not a full move to cash).

## 2. Replay and inputs

From 2026-10-02 (v3 go-live) to the latest close, then nightly. Point in time: daily story scores
(`fundamentals_story_filter_daily`), stage as of the day, L2 valuation and alerts as of the day,
stop sizes from closes up to the day (`compute_stop_pct(as_of=)`). The adjudicator reuses v3's
recorded verdict where v3 asked that name that day; otherwise it is asked now with the
candidate as of that day (operator's choice). Known approximations: watchlist membership is
taken as the story band; the catch-up cohort rules of the live runner are not replayed.

## 3. Judging

Not before 2027-04-16 (6 months), preferably 2027-10-16. Monthly returns of v4 and uncapped vs
v3, paired; max drawdown; turnover. v4 replaces v3 only if ahead after costs at no deeper
drawdown. The uncapped book answers "what did the cap cost" — it is a record, not a strategy
(it has no capital limit). The first two weeks of replay are not evidence for anything.

LEDGER row 59, trials = 2 (v4, uncapped).
