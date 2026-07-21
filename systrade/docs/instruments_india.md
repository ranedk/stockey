# Instrument Selection — Indian Markets

Applying Ch 6 (instruments) + Ch 12 (costs, four-block rule) to what's actually
tradeable from an Indian account. Verify current lot sizes/margins before
go-live — NSE revises lots periodically; numbers below are indicative
(mid-2026) and must be refreshed from the exchange file.

## The governing constraint

Every candidate must pass, at YOUR capital:
1. **Four-block test**: max position = 2 × vol scalar × instrument weight × IDM ≥ 4 blocks.
2. **Speed limit**: standardized cost ≤ 0.01 SR/round trip (so 12.5 turnover
   costs ≤ 0.13 SR/yr).
3. Understandable drivers, no artificially suppressed volatility, exchange-traded.

Indian futures lots are chunky (₹10–20L notional), so — exactly like Carver's
$250k example — the design is a *balance between diversification and lot size*,
and below ~₹1Cr a pure-futures system can't hold enough instruments. Answer:
a **two-sleeve design** (see below).

## Asset-class menu

| Asset class | Best vehicle | Block economics | Cost (SR, est.) | Verdict |
|---|---|---|---|---|
| Equity index | NIFTY futures (lot 75, ~₹19L notional, ~₹1.3L margin) | big blocks | ~0.002–0.004 | Core, if capital allows |
| Equity index (small) | NIFTYBEES etc. ETFs | 1 share ≈ ₹280 — divisible | ~0.02–0.05 | Core for smaller capital / long-only sleeve |
| Single stocks | Stock futures (liquid top-30 names) | ₹10–20L notional each | ~0.003–0.008 | Only at large capital; adds within-class correlation, low priority |
| Single stocks (cash) | Delivery equity | 1 share | ~0.05–0.15 (STT-heavy) | Slow rules only; long-only; for cross-sectional momentum later |
| Bonds / rates | Gilt ETFs (LTGILTBEES-class) or target-maturity debt ETFs | 1 unit, tiny | ~0.05–0.10 | Yes — the bond leg. NSE bond futures are too illiquid. |
| Gold | GOLDM (MCX mini, 100g ≈ ₹9–10L) or GOLDBEES ETF | mini lot / 1 unit | fut ~0.005; ETF ~0.03 | Yes — excellent diversifier for Indian portfolios |
| Silver | SILVERM / SILVERMIC (MCX) or silver ETF | micro = 1kg | ~0.005–0.01 | Optional second commodity |
| Energy | CRUDEOILM (MCX mini, 10 bbl) | ~₹6L notional | ~0.008 | Optional; genuinely uncorrelated |
| FX | USDINR futures | tiny lots ($1,000) | ~0.003 | **Blocked**: RBI's Apr-2024 rules require underlying exposure; speculative volumes collapsed. Monitor for reversal — ideal carry instrument otherwise. |
| International equity | MON100 / MOSPY-class ETFs | 1 unit | ~0.03–0.06 | Nice diversifier; watch premium/discount to iNAV (subscription caps distort it) |
| Options | NSE weekly/monthly | — | — | **Not in v1.** Non-linear payoffs don't fit the forecast framework; short-option carry is concentrated negative skew (Law 15). Research candidate for later, never the core. |
| G-Sec direct | RBI Retail Direct | — | — | No: can't trade systematically, no exit liquidity. Gilt ETFs instead. |

## Recommended architecture: two sleeves

**Sleeve 1 — Dynamic futures (the staunch-systems core).** EWMAC + carry, long
and short, on: NIFTY futures, GOLDM, CRUDEOILM (+ SILVERM or BANKNIFTY at
higher capital). Each additional instrument must pass the four-block test at
its handcrafted weight. Rough capital thresholds (20% vol target): 1 futures
instrument viable from ~₹30–40L; 3 instruments from ~₹80L–1.2Cr; below that
the sleeve shrinks or disappears — honestly, per the book, rather than by
over-leveraging.

**Sleeve 2 — ETF sleeve (long-only, small blocks).** NIFTYBEES + gilt ETF +
GOLDBEES + MON100. Two modes per your choice: pure asset-allocator ("no-rule"
constant forecast +10, Ch 14 style) or dynamic with forecasts clipped to
0…+20 (long-or-flat trend/carry — the book's adaptation for unshortable
instruments). Costs ~0.03–0.08 SR ⇒ slow rules only (EWMAC 32/128, 64/256,
carry; turnover ≲ 2–4). This sleeve is what makes the system diversified even
at modest capital, and it's where the four-block rule never bites (blocks are
1 share).

Handcrafting then treats sleeves' instruments in one tree: equities {NIFTY fut,
NIFTYBEES…}, bonds {gilt ETF}, metals {GOLDM/GOLDBEES}, energy {CRUDEOILM},
international {MON100} — one instrument per class first (Law 14).

## India-specific cost notes for the cost model

- Futures round trip: brokerage (flat ~₹20/order) + exchange txn charges + STT
  0.02% on sell (futures) + stamp + GST + slippage (1 tick minimum, more for
  MCX minis). Model as SpreadPoints + FeePerBlock + PercentValueFee.
- Cash delivery: STT 0.1% **each side** — dominant; keeps cash-equity rules slow.
- ETFs: quoted spread is the real cost; use only high-ADV ETFs and limit orders.
- MCX evening session exists; we still trade once daily at a fixed time.

## Explicit rejections (with the Law they break)

- Weekly index options selling: Law 15 (concentrated negative skew) + Law 13.
- Illiquid stock futures beyond top names: Law 13 + four-block.
- Micro-cap cash momentum churn: Law 13 (STT), Law 3 (baseline = beta anyway).
- Anything whose volatility is administratively suppressed: Law 11.
