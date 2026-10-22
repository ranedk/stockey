# e1-expiry-straddle (frozen 2026-10-22)

**What:** on each weekly Nifty expiry session only, sell the at-the-money WEEK1 straddle six
ways — entry 09:20 / 10:00 / 11:00 x a stop on each leg at +25% / +50% of its entry price (on the
minute bar's high, filled at the stop or the bar's open if worse) — and buy back whatever is left
at 15:15. The record is the equal-weight average of the six, net of costs (Rs 20/order, STT,
exchange, SEBI, GST, stamp, 0.5 point slippage per leg per transaction), in bps of notional.

**Why:** an option loses its last time value fastest on its expiry day, and the stop caps the
trending expiry. LEDGER 77 found it in-sample (2021-24, +2.86 bps, fading); LEDGER 78 pre-registered
it and it PASSED the unseen 2024-11..2026-09 check: +2.73 bps per expiry (n 104, t 2.5), both
halves positive, max drawdown −7% at 10% vol. The same straddles lose ~1 bp on every other day.

**Mechanics:** `scripts/paper_e1_expiry_straddle.py` (called from `scripts/paper_daily.sh`),
using `research/scripts/2026-10-22_nifty_algo_library.py`'s `short_straddle()` unchanged.
Recomputed from the start every run into `systrader_paper_option_e1` (one row per expiry x
member). Data: Dhan rolling-option minute bars and the NSE F&O bhavcopy, kept current by stockey's
`all_option_data_daily.sh` (morning, Tue-Sat), so each expiry is recorded the day after. Paper
only: nothing goes to a broker, and the dry-run order sheets do not include it.

**Starts:** 2026-10-23, the first session after the rules were frozen and checked. (The
2026-10-01..10-19 expiries were computed once in a smoke test of this script — not part of the
record, not a test: the rules were already frozen.)

**Judged:** after 52 expiry sessions (about a year): kept, and eligible for a small live
allocation, if the basket's mean is above zero; otherwise closed. Watched along the way, not
gated: the per-member spread, missing legs, and whether the effect keeps fading (check halves
+3.91 then +1.68). Rough size: ~6-7 Nifty points per straddle per expiry after costs; on one lot's
margin that is roughly 10%/yr (an estimate, not measured). LEDGER row 78.
