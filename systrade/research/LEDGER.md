# Research Ledger (append-only)

Every rule/variation/parameter combination ever evaluated against market data
gets one row — including failures, including "just looking." M = row count of
distinct variations per dataset; the Bonferroni bar in backtest reports derives
from it. Deleting rows is self-deception.

| # | Date | Rule / variation | Story (one line) | Dataset & period | Result (t, SR, corr w/ incumbents) | Decision |
|---|------|------------------|------------------|------------------|------------------------------------|----------|
| 1 | (pending) | EWMAC 16/64, 32/128, 64/256 | Under-reaction → trends persist (prospect theory) | pending real data | — | Adopted a priori from Carver (fitted on 35yr, 40+ instruments, out-of-sample) |
| 2 | (pending) | Carry (futures basis / yield − funding) | Paid for bearing skew + providing liquidity to hedgers | pending real data | — | Adopted a priori from Carver |

Notes:
- Rows 1–2 are inherited, ideas-first rules — the book's own out-of-sample
  evidence is their admission ticket; our backtests of them are *verification*,
  not selection, so they don't inflate M for selection purposes.
- Everything we invent or tune locally DOES count toward M.
