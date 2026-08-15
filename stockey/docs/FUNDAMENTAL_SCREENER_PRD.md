# Fundamental Screener — Build PRD (stockey)

Companion to `fundamental_basic_goal.md` (the source spec, kept as-is at repo
root — this document translates it into a concrete build plan against
stockey's actual codebase, conventions, and infrastructure). Decision date
2026-08-10.

**Decisions locked 2026-08-10** (resolves §11's open questions from the first
draft — kept here as the record of *why*, not just *what*):

- **OCR**: GLM-OCR (0.9B params), self-hosted locally. Fits comfortably
  alongside Postgres/Redis on this machine's 32GB RAM. Runs as a persistent
  local inference service the OCR pipeline calls, not loaded per-call (a
  daily batch job re-loading a model per document would waste most of its
  own runtime on load time alone).
- **Triage**: OpenAI `gpt-5.4-mini` — matches the model naming already used
  for Codex CLI (`CODEX_CLI_MODEL`) elsewhere in `.env.example`. Credentials
  already present (`OPENAI_API_KEY`).
- **Crawl-time filtering, not filter-after-crawl**: screener.in's custom
  query language and BSE/NSE's own search filters do most of L1's job
  directly at the source — no reason to crawl ~5,000+ listed companies'
  full financials just to discard most of them in SQL afterward. This
  reshapes L1 (§8 below).
- **One cron, not a separate file**: fundamentals jobs join the existing
  `config/stockey.crontab.template`, in a clearly-labeled block, not a
  second crontab file. The "never merge" rule (§1) is about decision
  pipelines, not about which file a cron entry lives in — one `go-crond`
  instance is simpler ops with no loss of the separation that actually
  matters.

## 1. Why this exists, and why it's not systrader

Two products, two standards of evidence:

- **systrader** — systematic, backtestable, Carver-framework trading.
  ~20-30% of the portfolio. A signal only earns a place there after it
  survives walk-forward + holdout + FDR discipline (`research/LEDGER.md`).
- **This screener** — long-term (12-30mo) fundamental investing. ~70-80% of
  the portfolio. Explicitly *not* trying to prove backtested alpha —
  `fundamental_basic_goal.md` §5 measures forecast **calibration**, not
  returns, and keeps a human at L4 committing to a falsifiable thesis before
  any capital moves.

A prior attempt at this failed by conflating four different epistemic
categories into one automated pipeline: raw collection, fundamental state,
backtested technical signals, and LLM-assisted judgment, all feeding one
execution engine — no robust cross-regime selection alpha, and LLM-driven
news/announcement pipelines burned tokens with no backtestable output. This
PRD keeps those categories separate on purpose.

**The one rule that matters more than any other in this document: this
screener and systrader's systematic engine never merge into one decision
pipeline.** No shared "final action" table, no engine blending a systematic
score with a fundamental flag. If a name is interesting from both angles
someday, that's a human synthesizing two independent read-outs — never a
system doing it.

## 2. What's genuinely different this time

Hold the line on these four disciplines — they're *why* the old system
became unmaintainable, not incidental complexity:

1. **No sub-daily latency.** `fundamental_basic_goal.md` §2 already excludes
   "anything sub-daily." The old system had 10-minute watchers and fast
   signal refresh for an 18-30 month thesis horizon — pure complexity with
   no payoff. L3 is one daily batch, ~8am. Nothing polls faster than that.
2. **No general announcement ingestion.** OCR only the specific filing types
   tied to the four L3 triggers (rating actions, PIT/SAST, shareholding
   deltas, results). Not the firehose. This alone removes most of what made
   the old announcement pipeline expensive and unreliable.
3. **No LLM makes a capital decision.** See §4 below — the LLM's role is
   bounded to extraction and triage, never sizing or execution. L4 (a human
   committing to a falsifiable, dated prediction) is the only gate capital
   passes through.
4. **Append-only, versioned state, always.** Already in the source PRD §4.
   The old system's mutable state was part of why it couldn't be audited or
   reasoned about after the fact. Every L2 snapshot, every L3 alert, every
   signal-definition version is a new row, never an overwrite.

## 3. The LLM's role — precisely scoped

Two uses, both bounded, both logged, neither touches capital directly:

### 3.1 Structured extraction (low risk)

GLM-OCR (§6) produces raw filing text → an LLM turns it into typed fields
per document type (e.g. a rating-action document → agency, old rating, new
rating, outlook, debt quantum, rationale category). Pure data
transformation. Output is a structured row plus a pointer back to the
source S3 object, never a standalone judgment. Proposed default:
`gpt-5.4-mini`, same as triage (§3.2) — not explicitly confirmed for this
step, but reusing one already-configured provider/credential rather than
adding a second is the sensible default; flag if you'd rather split them.

### 3.2 Event-triggered triage ("has this become interesting")

This is new relative to the source PRD, added per 2026-08-10 discussion.
Functionally: **a second, judgment-based path into L3, running alongside
the deterministic rule-based trigger, not a replacement for it and not a
bypass of L4.**

- Fires only on an actual event (a new L3-relevant filing, or a material
  move in stockey's own raw OHLCV around a name already in L1) — never a
  standing continuous scan. Same "event × primed state" shape as the
  rule-based triggers.
- "Technical" context here means stockey's own already-collected raw price
  data (`nseindia_ohlcv`, `dhan_ohlcv_daily`) — simple, observable price
  action around the event. It does **not** mean importing systrader's
  systematic signals; that would recouple the two repos. stockey already
  owns the raw data needed for this on its own.
- Output is a flagged candidate with the LLM's stated reasoning, written to
  the same alerts surface a rule-based trigger writes to, tagged
  `origin=llm_triage` vs `origin=rule` (mirrors L4's existing
  `origin_tag: systematic_screen | ad_hoc` pattern from the source PRD).
  A human still reviews it and, only if convinced, writes the falsifiable
  L4 thesis. The LLM never opens or sizes a position.
- Model: OpenAI `gpt-5.4-mini` (`OPENAI_API_KEY`, already in `.env`) —
  decided 2026-08-10, confirmed specifically for this step.
- Versioned like everything else: prompt version, model, and the evidence
  bundle it saw are stored with the alert row, so a later review can see
  exactly what the LLM was shown, not just its conclusion.

### 3.3 Explicitly not doing

- No automated decision policy, portfolio construction, or adversarial
  review (all deleted advisory/ concepts — do not reintroduce under a new
  name).
- No continuous/standing LLM polling — every LLM call traces back to a
  specific event or a specific document.
- No LLM call without a stored provenance record (model, prompt version,
  input evidence, timestamp) — an un-audited LLM judgment is worse than no
  judgment.

## 4. Architecture — where this lives in the stockey repo

New top-level package, sibling to `data/`, not inside it:

```
fundamentals/
  collectors/
    security_master.py     # extends dim_security/company_master (see §5) with EQUITY_L.csv + BSE scrip code
    screenerin.py           # screener.in financials/ratios/shareholding (own crawler this time, not the old advisory/ one)
    bse_announcements.py    # bseindia.com/corporates/ann.html
    bse_announcements.py    # also covers PIT-SAST + results-calendar detection (3 of L3's 4 triggers in one crawler) -- built as bse_announcements.py, not the bse_pit_sast.py name planned here
    nse_pit.py               # NSE structured PIT (api/corporates-pit) -- built as nse_pit.py, not the nse_announcements.py name planned here; NSE is secondary/redundant per fundamental_basic_goal.md §3.2
    rating_agencies.py       # CRISIL/ICRA/CARE/India Ratings/Acuité listing+detail (shell-plus-XHR pattern)
    ocr_pipeline.py          # fetch filing -> OCR (pluggable provider) -> LLM structured extraction (see §6)
  screens/
    l1_universe.py           # quarterly universe filter + rejection log (append-only)
    l2_state.py               # per-company state vector, versioned/append-only
    l3_triggers.py             # daily rule-based triggers + LLM triage (see §3.2), one batch ~8am
    l4_thesis.py                # thesis register CRUD + quarterly forecast scoring
```

Reused as-is from existing stockey infrastructure — this is the entire
reason this lives in stockey rather than a new repo:

| Need | Existing module |
|---|---|
| Postgres upsert/retry | `utils/db.py` |
| S3 blob storage | `utils/store.py` (new prefixes: `fundamentals/ocr/`, `fundamentals/filings/`) |
| Browser automation / CDP | same Chrome CDP session `data/nseindia/*` already uses |
| Cross-process rate limiting | `utils/nse_rate_limiter.py` — generalize to `utils/exchange_rate_limiter.py` (parameterized by domain) so BSE gets the same 1-request-floor + no-parallel discipline that just fixed the NSE Akamai block, not a copy-pasted second implementation |
| OCR provider abstraction | `utils/ocr/llm_ocr.py` — already a pluggable `Provider` dispatch (openai/gemini/codex); add a `local` provider for GLM-OCR (§6, decided) — no redesign needed |
| Fallback visibility | `utils/fallback_telemetry.py` — every degraded/failed fetch, every skipped enrichment, recorded the same way `data/` collectors already do it |
| Run-state/status reporting | `STOCKEY_RUN_STATE` + accurate non-hardcoded `status`/exit-code convention (per the 2026-08-10 fix to the NSE collectors — apply from day one here, don't repeat that bug) |
| Cron | Same `go-crond` instance, same `config/stockey.crontab.template` — decided 2026-08-10: one cron for stockey. A clearly-labeled `# ===== FUNDAMENTALS =====` block keeps it visually distinct, but no second file/instance |

## 5. Security master — extend, don't rebuild

`dim_security` already has `isin`, `symbol`, `bse_ticker`, `display_name`.
`company_master` already links `dhan_bse_id`/`dhan_nse_id`/`bse_ticker`/
`nse_ticker`. The source PRD's "Build order step 0" (half a day) is already
~70% done. What's actually missing:

1. Ingest `nsearchives.nseindia.com/content/equities/EQUITY_L.csv` (full
   listed universe + ISIN) and reconcile against `dim_security` — this is
   the explicit starting point you named. Flags any ISIN present in the CSV
   but missing from `dim_security` (a coverage gap in the current identity
   layer) rather than assuming it's already complete.
2. Add a numeric **BSE scrip code** column (distinct from `bse_ticker`) —
   BSE's APIs key on this, not the ticker. Backfill via the same
   `getScripCode()`-style lookup `fundamental_basic_goal.md` §3.2 points at
   (reference logic only, from `BennyThadikaran/BseIndiaApi`'s public
   samples — no runtime dependency, per the source PRD's own instruction).

No new `security_master` table — `dim_security`/`company_master` stay the
one identity layer for the whole repo, fundamentals included.

## 6. OCR pipeline

**Decided 2026-08-10: GLM-OCR (0.9B params), self-hosted locally.** Fits
alongside Postgres/Redis on this machine's 32GB RAM without contention.

`utils/ocr/llm_ocr.py` already supports OpenAI/Gemini/Codex providers via a
`Provider` literal + dispatch function — add `ocr_page_with_local(...)` /
extend the `Provider` type the same way, no pipeline redesign needed. Two
implementation decisions to make when this is actually built (not now):

- **Serving mode**: run GLM-OCR as a small persistent local inference
  server (HTTP, e.g. a lightweight FastAPI/vLLM/Ollama-style wrapper) that
  the OCR pipeline calls per document, rather than loading the model
  in-process per call. This is a daily batch job over a bounded set of
  filings (per §2's "only OCR the specific types tied to L3's triggers"
  discipline) — repeated model-load overhead would dominate runtime
  otherwise. Exact serving stack is yours to set up per your note; the
  pipeline only needs a stable local endpoint to call.
- **Fallback behavior when the local service is down**: OCR failures must
  follow the same visible-fallback discipline as every other stockey
  collector (`utils/fallback_telemetry.py`) — a filing that fails OCR stays
  in `enrichment_status=pending` (§7) and retries the next run, it never
  silently drops or blocks the rest of that day's batch.

Two-stage pipeline, not a choice between the two models: GLM-OCR (local)
does image/PDF → raw text; `gpt-5.4-mini` (§3.2) then does raw text → typed
structured fields (§3.1). GLM-OCR's own output is the *input* to structured
extraction, not an alternative to it.

Storage split (per your instruction):
- Numeric/classification fields (extracted rating, debt quantum, shareholding
  %, etc.) → Postgres, structured columns, versioned like everything else.
- Raw OCR'd long-form text + the source filing (PDF/image) → S3, with the
  Postgres row carrying only the S3 key + a short excerpt
  (`BLOB_TEXT_EXCERPT_CHARS`, already in `.env.example` at 1200 chars) —
  same pattern the deleted `announcement_pipeline_documents` table used
  before it was removed for being fed by an unbounded general-ingestion
  pipeline; the pattern itself was fine, the *scope* (§2) is what's fixed.

## 7. Schema (draft — refine at implementation time, not now)

- `security_master` extensions: `company_master.bse_scrip_code` (built here,
  not on `dim_security` as originally planned), reconciliation flags from
  the `EQUITY_L.csv` diff.
- `fundamentals_events` — one normalized table, `source` as a column
  (mirrors `fundamental_basic_goal.md` §4 exactly): dedupe on
  `(isin, filing_type, disclosure_date, quantity)`, keep earliest
  `announcement_timestamp` across BSE/NSE, `detection_source` +
  `enrichment_status` (pending/fetched/failed) tracked separately so a
  failed agency-site crawl never suppresses the underlying event.
- `fundamentals_l1_universe` — append-only, one row per company per
  quarterly refresh, plus `fundamentals_l1_rejections` (company, reason,
  as-of date) — "log every rejection with reason" (§4 of the source PRD) is
  not optional, it's what makes L1 backtestable-in-spirit later. **Honest
  caveat from the crawl-time-filtering decision (§8 step 3)**: the
  screener.in query itself doesn't hand back *why* a company it excluded
  was excluded, only the ones it returned — full per-company rejection
  reasons are only available for the smaller post-hoc pass (auditor/RPT)
  applied to the query's output, not for the whole listed universe the
  query silently narrowed. Store the query definition itself
  (version + parameters) alongside each refresh so "why wasn't X in the
  universe" is at least reconstructable from the query logic, even without
  a per-company reason row for query-level exclusions.
- `fundamentals_l2_state` — append-only, one row per company per
  refresh/filing-triggered update, `state_vector_version` column.
- `fundamentals_l3_alerts` — `origin` (`rule` | `llm_triage`), trigger type,
  the L2 state snapshot it fired against, and for `llm_triage` rows: model,
  prompt_version, evidence bundle reference.
- `fundamentals_l4_thesis` — falsifiable prediction text, target date,
  invalidation criteria, `origin_tag` (`systematic_screen` | `ad_hoc`),
  signal_definition_version, resolution status + resolved-true/false +
  resolution date (feeds the quarterly forecast-calibration scoring in §5
  of the source PRD).

## 8. Build order

Concretizing `fundamental_basic_goal.md` §6 against what already exists:

0. **Security master** (§5 above) — extend `dim_security`, not rebuild.
   Smallest possible slice; validates nothing else depends on this being
   perfect yet.
1. **`utils/exchange_rate_limiter.py`** generalization from
   `nse_rate_limiter.py` — needed before *any* BSE crawler exists, given
   the 2026-08-09/10 NSE lesson. Cheap, and de-risks every collector after
   it.
2. **Deleveraging screen** — source PRD calls this "highest signal-to-effort,
   ~90% screener-derivable, arithmetic mechanism" and explicitly the
   pipeline-validation step. Build `fundamentals/collectors/screenerin.py`
   scoped to only what this screen needs, not the full financials surface.
   This is the first thing that should produce a real, inspectable output.
   (Built 2026-08-10, scheduled 2026-08-14, retired 2026-08-15 -- its
   output table had zero readers from the day it was scheduled. Validated
   the pipeline as intended; `screenerin.py`'s shared screener.in scraping
   infra it built stays, used by `l1_universe.py`/`l2_state.py` today.)
3. **L1 universe filter — built as a screener.in custom query, not a
   post-hoc SQL pass.** Decided 2026-08-10: filter at crawl time.
   screener.in's query language can express most of the source PRD's L1
   table directly (mcap band, liquidity floor, and derived ratios like cash
   conversion/RoCE are exactly what that query language is for — see the
   `ad_hoc_query` pattern already used elsewhere in this repo's history for
   custom queries like `"Market capitalization > 500 AND ... Return on
   capital employed > 22%"`). Two filters need checking rather than
   assuming either way when this is actually built: **auditor changes** and
   **contingent-liabilities/RPT-as-%-of-revenue** may not be pre-computed
   ratios screener.in exposes as query fields — if not, those two stay a
   smaller post-hoc pass over the query's *output* universe (still far
   cheaper than filtering the full listed universe). "Every later signal is
   worthless without this filter" (source PRD) — still true, just cheaper
   to apply now.
4. **L2 state store** — append-only, versioned, per §7.
5. **L3 feeds, in this order**: BSE crawler first (primary per §3.2 of the
   source PRD — no session/cookie gate, and covers BSE-only microcaps NSE
   misses), NSE archive second (redundant, `nsearchives`-first), then rating
   detection + enrichment (detection via exchange feed must be reliable;
   enrichment via agency site may fail silently and retry tomorrow — do not
   build seven robust agency crawlers, build one reliable detector). BSE's
   own search filters (§3.2 of the source PRD's page list) scope these
   crawls to the step-3 universe directly, same crawl-time-filtering
   decision — no reason to pull PIT/SAST/announcement history for names L1
   already excluded.
6. **OCR + structured extraction** (§6) — wire in once L3 is producing real
   filing references to OCR.
7. **LLM triage** (§3.2) — deliberately *after* the rule-based L3 triggers
   exist and are trusted; triage augments a working alert surface, it
   doesn't bootstrap one.
8. **L4 thesis register + quarterly forecast scoring.**
9. Sector capital-cycle aggregation (source PRD step 6).
10. *(Conditional, likely far out)* HS-code mapping table (source PRD step 7)
    — "build after the strategy shows evidence of working, not to find out
    whether it does."

Each step should produce something inspectable before the next starts —
the old system's failure mode was building interconnected pieces before any
single one was validated end to end.

## 9. `CLAUDE.md` boundary rewrite (do this alongside step 0, not after)

Current text ("Stockey is a **pure data platform**... does no research,
fundamental analysis...") becomes: stockey has two independent product
lines — (1) the pure-TA data platform feeding systrader's systematic
trading (unchanged, still governed by `docs/DATA_INVENTORY.md` and
`DATA_CONTRACT.md`), and (2) this fundamental screener, governed by this
document. Rule that survives from the old text unchanged: **the two must
never be blended into one decision pipeline.** `fundamentals/` tables are
never added to `DATA_CONTRACT.md`'s systrader-sync set.

## 10. Explicitly not building (carried over from the source PRD §2, unchanged)

- General BSE/NSE announcement classifier.
- Concall transcript NLP.
- HS-code → company mapping table (until the strategy shows evidence of
  working).
- Anything sub-daily.
- (New, from this document) Any bridge/shared table between this screener
  and systrader's decision pipeline.

## 11. Decisions log

All three open questions from the first draft resolved 2026-08-10 (folded
into §1/§3/§4/§6/§8 above; kept here as a flat changelog for quick
scanning):

| Question | Decision |
|---|---|
| OCR provider | GLM-OCR (0.9B), self-hosted locally |
| Structured-extraction model | `gpt-5.4-mini` (proposed default, not yet explicitly confirmed) |
| Triage model | `gpt-5.4-mini` (confirmed) |
| Crawl scope | Filter at crawl time (screener.in query language + BSE/NSE search filters), not filter-after-crawl |
| Cron | Single shared `config/stockey.crontab.template`, labeled block — not a separate file |

One item still genuinely open: whether extraction should use `gpt-5.4-mini`
or something else — flagged in §3.1, not blocking.

---

Nothing above blocks starting. Step 0 (security master extension) and step
1 (rate limiter generalization) can begin immediately.
