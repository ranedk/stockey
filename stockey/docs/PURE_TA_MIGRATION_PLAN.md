# Pure-TA Migration Plan (2026-07-27)

Executes `DATA_INVENTORY.md` + the folder unification. Operator decisions
baked in: **no cloud-DB dump before drops** (LLM-analysis outputs judged not
useful; the 25 keep-tables are mirrored locally and dumped —
`~/systrade_dumps/systrade_20260727`); code is archived via git branch, so
nothing needed for go-forward is destroyed.

Each phase ends with a gate. Do not start the next phase red.

---

## Phase 0 — Freeze & snapshot (10 min)

1. Note SHAs: `git -C stockey rev-parse HEAD`, same for systrader.
2. `git -C stockey branch advisory-archive-2026-07` — the full pre-surgery
   snapshot. This branch is the rollback for every later phase.
3. Confirm local mirror fresh (`sync_from_stockey.sh` green today) and the
   local dump exists.

**Gate:** archive branch exists; sync green.

## Phase 1 — Folder unification (operator, ~30 min)

1. `mkdir -p ~/code/trading && mv ~/code/stockey ~/code/trading/stockey &&
   mv ~/Downloads/books/systrader ~/code/trading/systrader`
   (git histories move intact; two repos, one parent).
2. Fix absolute paths:
   - systrader `.env`: `STOCKEY_DIR`, `DHAN_TOKEN_CACHE`.
   - stockey: crontab template `STOCKEY_DIR`, any launchd/shell wrappers,
     `.env`/config paths. `grep -r "code/stockey\|Downloads/books" --include="*.sh" --include="*.env*" --include="*.template"` in both repos.
3. Reinstall crontab from template so entries point at the new path.
4. Claude session merge: create `~/code/trading/CLAUDE.md` (two-project layout,
   authority rule: stockey side = data ops only; systrader side = design
   code/experiments, never override forecasts (Law 19); pointers to
   DATA_CONTRACT.md, DATA_INVENTORY.md, TRADING_BIBLE.md). Copy memory files
   from BOTH old project slugs
   (`~/.claude/projects/-Users-rane-code-stockey/memory/`,
   `~/.claude/projects/-Users-rane-Downloads-books/memory/`) into the new
   slug's memory dir and merge the two MEMORY.md indexes. Start all future
   sessions from `~/code/trading`.
5. Verify: `cd ~/code/trading/systrader && go test ./... && ./scripts/sync_from_stockey.sh`;
   stockey `./complete_data.sh` smoke run.

**Gate:** tests + sync + downloaders green from new paths; new session at
`~/code/trading` sees both repos and merged memory.

## Phase 2 — Promote data producers out of advisory/ (1 session)

1. `advisory/price_adjustment.py` → `data/nseindia/price_adjustment.py`
   (imports fixed, table name unchanged). Run once; verify
   `advisory_adjusted_ohlcv_daily` max(load_ts) advances.
2. Audit imports: does anything under `data/` import `advisory.*`? Promote
   or inline what's needed (`data_sync`, `sync_state`, `data_readiness`,
   `current_prices`, `identity_issues`, `event_data_quality` are the
   candidates). Check whether downloaders read `advisory_sync_state`.
3. Point `all_price_adjustment.sh` at the new module path.

**Gate:** `complete_data.sh` + price adjustment run green with ZERO imports
from `advisory/`.

## Phase 3 — Evidence export (before anything is archived)

1. Export `advisory_research_runs`, `advisory_factor_ic_sweep`,
   `advisory_subscore_ic`, signal-quality/threshold evaluation summaries →
   flat files in `systrader/research/imports/` (CSV or MD).
2. Reconcile with systrader `research/LEDGER.md` rows 3–5 (already imported
   from session memory); add rows for anything not yet counted
   (`trials=N` batches). M must not shrink.
3. Mine `advisory/cost_model.py` → actual Dhan fee schedule
   (delivery/F&O/MCX) → answer systrader open question #5; encode later in
   `internal/data` cost metas.

**Gate:** LEDGER updated; fee schedule documented.

## Phase 4 — The cut (one commit on main)

1. Delete (they live on the archive branch):
   - `advisory/` minus promoted files
   - `data/announcements/`, `data/economictimes/`, `data/screenerin/`,
     `data/mospi/`, `data/eaindustry/`, `data/fred/`, `data/nsdl/`,
     `data/ininvesting/`
   - `data/sharpelydata/`: keep ONLY the `historical_mcap` path
     (`sharpely_data.py` slim-down or extract), delete fundamentals parts
   - `data/nseindia/insider_deals.py`; leave `earnings_events.py` in place
     but UNSCHEDULED (freeze, per inventory)
   - `apps/`, `live_dashboard/`, frontend + watcher scripts
   - the ~29 dead `all_*.sh` wrappers
2. Crontab: regenerate with ONLY `complete_data.sh`,
   `all_downloaders_queue.sh`, `all_price_adjustment.sh`,
   `all_ohlcv_reconcile.sh`, `all_data_readiness.sh`, log rotation.
3. Remove LLM API keys/config from stockey env — token spend ends
   mechanically, not by policy.
4. Prune tests to the data layer; suite green.
5. Rewrite stockey `CLAUDE.md`: data-platform operating guide; retire the
   LLM-decision-authority section (obsolete); keep the systrader-boundary
   section.

**Gate:** clean install from main runs the 6 cron jobs green for one full
day; `git grep -l "import advisory"` returns nothing outside the archive.

## Phase 5 — Cloud DB cleanup (destructive; NO dump — operator decision)

1. Generate the DROP list mechanically: every `advisory_*` table EXCEPT
   `advisory_adjusted_ohlcv_daily` (and `advisory_sync_state` if Phase 2
   found it load-bearing), plus `announcement_*`, `screenerin_*`, `stmt_*`,
   `shareholding_*`, `sharpely_*`/`master_sharpely_*` (except none —
   `historical_mcap` is its own table and stays), `features_*`,
   `economictimes_rss_items`, `nseindia_insider_deals`,
   `nseindia_{block,bulk}_deals`, `mospi_cpi`, `eaindustry_wpi`,
   `macro_usa*`, `macro_india_gdp`, `fii_*`.
   NEVER in the list: anything in DATA_INVENTORY's KEEP table, explicitly
   including `events_dividend`, `events_capital_change`,
   `rbi_currency_rates`, `dhan_ohlcv_intraday`.
2. Review the generated list by eye against DATA_INVENTORY.md (this review
   IS the safety net in place of the dump).
3. Drop in batches inside transactions; `VACUUM FULL` the small instance
   afterward to reclaim space.
4. Run systrader's sync + `go test ./internal/store/ -count=1` — must be
   green (sync `skip`s the now-absent tables it used to full-copy: fine;
   optionally prune FULL_TABLES of dropped names).

**Gate:** cloud DB holds only KEEP tables (+ postgres internals); sync and
store integration tests green; disk usage down.

## Phase 6 — Steady state (3 trading days)

- Daily: 6 cron jobs green, `advisory_adjusted_ohlcv_daily` fresh, sync
  fresh, systrader integration tests green, zero LLM API calls billed.
- Then delete this plan's checklist items from todo trackers and update
  `DATA_CONTRACT.md` if any table changed shape during the move.

## Rollback

Any phase: `git checkout advisory-archive-2026-07` restores all code;
re-install the old crontab from that branch. Phase 5 is the only
irreversible step (dropped advisory data is gone) — which is why its gate
is a human review of the generated DROP list, and why it runs LAST.

## Explicitly out of scope

systrader's research roadmap (TR benchmark, indicator library, combination
policies) continues in parallel — nothing here blocks it except the folder
move itself (Phase 1), which should happen first so new work lands in the
unified session.
