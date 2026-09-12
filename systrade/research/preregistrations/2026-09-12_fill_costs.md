# Measurement design — what fills actually cost

Written 2026-09-12, before any cost below has been measured. This is a
MEASUREMENT, not a hypothesis test (LEDGER trials=0): it checks the one
assumption under every result in the ledger — a flat 50 bps round trip — and
feeds the capacity question (how many names Rs 30 lakh can hold). The methods
are fixed here so they cannot be chosen after seeing which gives the kinder
number.

## 1. What a round trip costs, in four parts

1. **Statutory charges — facts**, from Dhan's schedule (`docs/open_questions.md`
   B.5), equity delivery: STT 0.1% on the buy and on the sell; stamp duty
   0.015% on the buy; NSE transaction charge 0.0030699% each side; SEBI fee
   0.0001% each side; IPFT 0.0001% each side; GST 18% on (brokerage ₹0 +
   transaction + SEBI + IPFT). Plus the depository (DP) charge on every sale:
   ₹12.50 + 18% GST = **₹14.75 per stock per sell day**, fixed in rupees — so
   it is a cost in basis points that depends on position size.
2. **Spread — estimated.** There is no quote data, so the effective spread is
   estimated from 1-minute bars with the Abdi-Ranaldo (2017) close-high-low
   estimator: S² = 4·E[(c_t − η_t)(c_t − η_{t+1})], c the log close and η the
   log mid-range of each bar, over consecutive bars of the first half-hour
   (09:15-09:44 IST), when our orders trade. Pooled per liquidity tier: the
   mean product over every name-day-minute in the tier, floored at zero, then
   the square root. The median of per-name-day estimates (each floored at 0) is
   reported beside it.
3. **Where the fill lands — measured.** The paper tracks fill at the official
   open, which is NSE's pre-open call-auction price; from 2023 the first
   09:15 bar's open equals it exactly (verified on sample days). An order in
   the auction pays no spread. An order placed after the open instead pays the
   spread and lands at the market's price then: reported as the signed and
   absolute gap between the 09:15-09:29 volume-weighted price and the auction
   price.
4. **Market impact — modelled, labelled as a model.** The square-root law,
   one-way impact = Y · σ_daily · √(Q / V_day), with Y = 1 (the conservative end
   of the literature), σ_daily each name's 60-day daily volatility and V_day
   its 60-day median traded value. Reported at position sizes Q of Rs 6,000
   (the combination track at Rs 30 lakh), 12,500 (a quintile book), 20,000,
   1 lakh (30 names) and 3 lakh (10 names). Also reported: the first minute's
   traded value, as a check on whether an auction order is small against it.

## 2. Setup (fixed)

- 1-minute bars from `dhan_ohlcv_intraday`, **2023-06-01 → 2026-08-31** (from
  2023 there are no pre-open bars and the 09:15 open is the auction price;
  2021-22 carry a 09:07 pre-open print and are left out), every 5th trading
  day, first half-hour only. Always read one bounded window per day.
- Minute prices are Dhan-adjusted for later corporate actions; every measure
  here is a ratio within one day's bars, so the scale cancels. Rupee sizes
  (daily value, first-minute value) come from the raw bhavcopy.
- Liquidity tiers by the 60-trading-day median traded value before the day
  (raw bhavcopy): Rs 1-10 crore, 10-25, 25-100, 100-500, 500+. The paper
  tracks trade Rs 10 crore and up.
- A name-day needs a 09:15 bar and at least 20 of the 30 bars; how many do not
  is reported per tier.

## 3. What it reports

Per tier: name-days, coverage, median daily value, median first-minute value,
the spread (pooled and median), the post-open price gap, median daily
volatility. Then the full round trip per tier and position size, two ways —
filled in the auction (statutory + DP + impact) and filled after the open
(+ spread) — against the 50 bps every backtest assumed.

## 4. Ledger

One row, trials=0, whatever it shows.
