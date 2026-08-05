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

## Phase 2 — Promote data producers out of advisory/ (1 session) — DONE 2026-07-28

1. `advisory/price_adjustment.py` → `data/nseindia/price_adjustment.py`
   (imports fixed, table name unchanged). Verified: the 4 price-adjustment
   regression tests pass against the new path.
2. Audit imports: does anything under `data/` import `advisory.*`? Full
   grep audit found the real set differed from the guessed candidate list:
   `data_sync`, `data_readiness`, `current_prices`, `event_data_quality` are
   NOT imported anywhere under `data/` (false positives — left in
   `advisory/`, archived with it in Phase 4). The real promoted set is
   `fallback_telemetry`, `sync_state`, `identity_issues`, `advisory_date`,
   `external_task_queue` — all moved to `utils/` (fallback_telemetry has
   zero advisory-internal deps; the other four depend only on it +
   `utils.*`). `advisory/` keeps thin re-export shims (`from utils.X import
   *`, plus a `main()` passthrough for the 3 with CLI entrypoints) so its
   ~130 existing internal callers and `python -m advisory.X` cron
   invocations (`all_watchers.sh`, `all_context_to_entry_repair.sh`,
   `all_advisory.sh`, `all_external_workers.sh`) keep working unchanged
   until Phase 4 deletes the package outright. `utils/*` files that
   already imported these from `advisory.*` (a pre-existing layering
   inversion — `redaction.py`, `redis_utils.py`, `sync.py`,
   `ingestion_state.py`, `http.py`, `redis_bkp_restore.py`, `date.py`,
   `display_time.py`, `codex_cli.py`, `company_master.py`,
   `transcribe/llm_transcribe.py`) were repointed to the new `utils.*`
   location directly, not the shim. Checked whether downloaders read
   `advisory_sync_state`: **yes** — `data/download_runner.py` persists to
   it via `persist_sync_state`; it is load-bearing and excluded from the
   Phase 5 drop list (see `DATA_INVENTORY.md`).
3. Pointed `all_price_adjustment.sh` at the new module path.

**Gate:** price-adjustment tests green on the new path; all `data/` and
`utils/` imports of the promoted modules point at `utils.*`, zero imports
of `advisory.*` remain under `data/` or `utils/`
(`advisory/*.py` itself still imports the shims, expected until Phase 4).

## Phase 3 — Evidence export (before anything is archived) — DONE 2026-08-05

1. Fee schedule mined from `advisory/cost_model.py` + Dhan's real published
   pricing (2026-08-02) and documented in `systrader/docs/open_questions.md`
   Q5 — done earlier.
2. Exported `advisory_research_runs`, `advisory_factor_ic_sweep`,
   `advisory_subscore_ic`, and the 3 signal-quality/threshold eval summary
   tables to `systrader/research/imports/*.csv` (cloud DB reachable directly
   from this session as of 2026-08-05, unlike earlier attempts).
3. Reconciled against `research/LEDGER.md`: rows 3–5 already covered the
   named experiments. Found 4 uncounted batches, added as rows 6–9: TimesFM
   forecast paper-portfolio (trials=15, inconclusive), weekly factor-IC +
   subscore-IC drift monitor (trials=81, reinforces row 4's null), weekly
   technical-threshold calibration (trials=13, **candidate but NOT
   promoted** — beats baseline on early large samples but eligible-count
   shrinks hard on later runs, smells like re-selection on a thinning pool,
   needs a dedicated re-run before trusting it), and the recurring
   signal-quality health monitors (trials=585+174, no promotion signal, the
   split monitor actively recommends against broad promotion in >half its
   rows). M grew from ~124 to ~889; nothing shrunk.

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
   - Two registry-level scope leaks found during the 2026-08-04 KEEP-script
     live run-through, both currently non-fatal but must be cut here:
     `data/download_runner.py`'s `PARSER_STEPS` still dispatches
     `advisory.event_evidence_store` and `advisory.context_overlay_refresh`
     under `complete_data.sh --phase all`; `all_data_readiness.sh` still
     calls `advisory.fundamentals_refresh` (Sharpely statements pull) as a
     second, `|| non-fatal` step after `advisory.data_readiness --fix`.
     Remove all three call sites along with the rest of `advisory/`.
2. Crontab: regenerate with ONLY `complete_data.sh`,
   `all_downloaders_queue.sh`, `all_external_workers.sh`,
   `all_price_adjustment.sh`, `all_ohlcv_reconcile.sh`,
   `all_data_readiness.sh`, log rotation. (`all_external_workers.sh` added
   2026-08-02: it drains the `nse`/`dhan` queues `all_downloaders_queue.sh`
   feeds — see `DATA_INVENTORY.md`.)
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
   `advisory_adjusted_ohlcv_daily` and `advisory_sync_state` (Phase 2
   confirmed it load-bearing — `data/download_runner.py` writes to it),
   plus `announcement_*`, `screenerin_*`, `stmt_*`,
   `shareholding_*`, `sharpely_*`/`master_sharpely_*` (except none —
   `historical_mcap` is its own table and stays), `features_*`,
   `economictimes_rss_items`, `nseindia_insider_deals`,
   `nseindia_{block,bulk}_deals`, `mospi_cpi`, `eaindustry_wpi`,
   `macro_usa*`, `macro_india_gdp`, `fii_*`.
   NEVER in the list: anything in DATA_INVENTORY's KEEP table, explicitly
   including `events_dividend`, `events_capital_change`,
   `rbi_currency_rates`, `dhan_ohlcv_intraday`, `advisory_sync_state`.
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
