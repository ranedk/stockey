# Trigger study

Operator idea, 2026-10-08. When a stock enters the momentum list, what filings and results
separate the ones that keep running from the ones that fail? Which triggers matter in each
sector, and how long do they take to play out? Live use: take high-momentum stocks, wait for
a trigger, hold for its known timeline.

Code: `fundamentals/trigger_study/`. Ledger: systrader `research/LEDGER.md` row 56
(exploration, trials=0).

## Design

| Piece | Rule |
|---|---|
| Event | point-in-time entry into the top tenth by 26-week return vs the market; Rs 5 cr/day liquidity (previous 12 weeks), 2 years listed, no +300% year before, financials and diversified excluded |
| Outcome | next 26 weeks vs its own industry (equal-weight, 58 Sharpely industries): **continued** = +20% and never 30% down; **failed** = -10% or worse |
| Years | discovery on entries 2016-07 -> 2022-12; entries from 2023 are the check, untagged until the trigger list is frozen |
| Filings | each stock's NSE announcement list, one request per company; window = 365 days before entry to 91 days after |
| Pre-filter | keywords on the TEXT; the company's own category is ignored (often wrong). A material word always sends a filing to the tagger. Results filings and call/presentation filings are classed but not tagged |
| Tagger | Claude Haiku through the Claude CLI on the operator's plan, thinking off, a FIXED trigger list (`prompts.py`); it never sees the outcome, batches mix both groups |
| Documents | page 1-2 of the attachment, only for vague filings or notable triggers with no stated amount |
| Free signals | sales growth, its acceleration, margin change, profit growth (screener.in quarters, public 60 days after quarter end) |
| Report | per trigger: share of continued vs failed events with it, before vs after entry, stock vs industry 4 weeks before and 13 weeks after the filing, order size vs trailing sales |

## Running

```sh
python -m fundamentals.trigger_study events      # entries + outcomes, picks the pilot
python -m fundamentals.trigger_study fetch       # NSE filing lists
python -m fundamentals.trigger_study queue       # keyword filter + batches
python -m fundamentals.trigger_study run --any-time   # tag now (otherwise cron, nights only)
python -m fundamentals.trigger_study report      # logs/trigger_study_pilot*.{json,csv}
python -m fundamentals.trigger_study status
```

Cron runs `all_trigger_study.sh` every 15 minutes. It calls only 22:00-08:00 IST. When the
CLI reports the plan's usage limit, the runner stores the reset time and every run until then
exits at once, so work resumes by itself.

## Known limits

- Point-in-time membership is today's industry list applied to the past.
- Quarter results are screener.in's current figures (may include later corrections) and their
  public date is approximated as quarter end + 60 days.
- NSE returned no filings for ABBOTINDIA for 2015-2023; such stocks are left out of the report.
- No shareholding history or surveillance history before 2025, so those signals are absent.
- The momentum labelling is a research label. Anything that graduates is re-implemented in
  systrader as a storied rule (stockey does not grow its own TA).
