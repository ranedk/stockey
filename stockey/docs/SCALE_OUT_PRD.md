# Scale-out PRD -- central orchestrator, crawl fleet, GPU OCR, research tasks

Written 2026-11-09 and revised the same day with the operator's decisions:
- **Crawling** scales out on on-demand Hetzner Cloud servers, **at most 8**. Each has its own IP
  and keeps the 10 s gap. The data is public disclosure data, and NSE offers no bulk route
  for history.
- **OCR** (GLM-OCR) runs on RunPod GPUs, **at most 3**. Hetzner Cloud has no GPUs.
- **Claude stays** for all reading, classification and analysis. No local LLM replaces it.
- **Orchestration is central.** A worker gets ONE task at a time and reports back after it
  (and its retries) finishes; the orchestrator then gives it the next one. No bulk hand-outs,
  so a crash loses at most one task.
- **Workers live in a separate Hetzner project** with their own token (`HETZNER_KEY`, checked
  2026-11-09: it sees no servers, so it cannot touch `stockey-ubuntu`). GPUs use
  `RUNPOD_API_KEY`.
- **The code stays private; the output becomes a service.** People ask questions on a site that
  answers from our data: deep analysis of companies, their announcements, boards and founders,
  with web search and global news. The same orchestrator runs those tasks.
- **Spend caps: under USD 100 a month for each pool** (Hetzner, RunPod).
- **Search uses free sources** and the local Chrome; first test on a couple of stocks.

Status: proposal. Nothing here is built.

## 1. Facts (checked 2026-11-09)

- Hetzner Cloud: 26 server types, all CPU, billed hourly, created or deleted in under a minute
  (cx23: 2 vCPU / 4 GB, about EUR 0.01/hour). Hetzner's GPUs are dedicated servers with a
  month's rent as a setup fee on each order, so they don't fit bursts.
- RunPod: billed by the second. The key works (pod list: empty).
- GLM-OCR on our CPU: about 6 minutes a page. 2,385 filings have no OCR status yet.
- NSE and BSE: we allow one request per 10 s per domain from one IP. History for ~1,800
  companies (announcement lists, attachments, reports) is on the order of 100k-200k requests:
  12-23 days from one IP, about 2-3 days from 8.
- Daily archives (bhavcopy, daily announcements) are one download a day and stay as they are.

## 2. Architecture

```
            research interface / cron / backfill scripts
                          | create tasks
                          v
   +---------------- ORCHESTRATOR (main server) ----------------+
   | task table + graph | dispatcher | rate policy | autoscaler |
   +-----^-------------------------^---------------------^------+
         | next / heartbeat / report (HTTPS, one task at a time)
   +-----+------+          +-------+--------+     +------+------+
   | main pool  |          | hetzner pool   |     | runpod pool |
   | (Dhan,     |          | crawl, fetch,  |     | OCR         |
   | daily,     |          | web/news, text |     | max 3 GPUs  |
   | Claude)    |          | max 8 servers  |     |             |
   +------------+          +----------------+     +-------------+
```

### 2.1 The orchestrator

A service on the main server (FastAPI, like the fundamentals API). It is the only thing that
reads or writes the task tables.

- **Task table** `infra_task`: id, kind, payload, pool, domain, priority, status (`pending`,
  `running`, `done`, `failed`, `cancelled`), attempt, max_attempts, worker, lease expiry,
  parent id, result reference, error, timestamps. Every attempt is kept in `infra_task_attempt`.
- **Task graph:** a task's handler can return follow-up tasks when it is reported done. For
  example: an announcement list leads to one fetch per attachment, each fetched PDF to an OCR
  task, and each OCR'd text to a Claude reading task. A user's question becomes a tree, and its
  answer is ready when the tree is done.
- **Priorities:** a user's question first, then daily work, then history backfills.

### 2.2 The worker protocol (one task at a time)

1. **Start.** A new server or pod starts the worker image with its pool, a one-time token
   issued at creation, and the orchestrator's address. It registers its IP and abilities.
2. **`POST /next`.** The orchestrator gives it exactly one task it may run now (2.3), or "wait
   N seconds".
3. **Heartbeat** every 30 s while the task runs.
4. **Retries inside the task.** Each task kind sets its own retries (e.g. 2 for a fetch, with
   backoff), counted as one attempt.
5. **`POST /report`.** Success with a result reference, failure with the error, or a block
   signal. Then back to step 2.
6. **Lost tasks.** If the heartbeat stops, the lease expires (task kind's timeout + 60 s). The
   orchestrator marks the attempt lost and puts the task back as pending, up to max_attempts;
   after that the task is failed and shown. At most one task per worker is ever in doubt.

Workers never connect to Postgres. Results go through the orchestrator: small results in the
report, large files to the blob store through a short-lived upload link the orchestrator hands
out. This keeps the database off the internet and lets RunPod pods outside Hetzner work the
same way.

### 2.3 Rate policy (central)

- The orchestrator keeps, for each (worker IP, domain), the earliest time the next request may
  go. It only hands a worker a task for a domain whose time has come for that worker's IP.
  The 10 s gap per IP is therefore enforced in one place. The worker's own gate is a second
  check.
- Per-domain settings: gap per IP (NSE, BSE 10 s; screener.in, SEBI as today; a default for
  company websites and news sites), maximum IPs at once (NSE 8), and active hours (history
  pauses during the main server's NSE/BSE windows, 07:10-08:30 and 19:15-20:30 IST).
- **Blocks:** a report of 403/429, a captcha or block page puts that IP on hold for that
  domain. Two IPs blocked on one domain within an hour pause the domain fleet-wide and alert
  the operator. The main server never runs history tasks, so the daily data keeps its own
  clean IP.

### 2.4 Pools and the autoscaler

| Pool | Where | Cap | Runs |
|---|---|---|---|
| main | stockey-ubuntu | (existing) | Dhan, daily collection, Claude calls, the orchestrator |
| hetzner | Hetzner Cloud cx23, worker project | **8** | NSE/BSE history, attachments, company and news pages, web search calls, pdftotext |
| runpod | RunPod 24 GB GPU | **3** | GLM-OCR |

- The autoscaler runs every minute. For each pool it counts tasks that could run now (for the
  hetzner pool, limited by free IP-domain slots) and creates servers or pods up to the cap. It
  deletes any that have been idle 10 minutes.
- Daily spend caps per pool: past a cap it stops creating and alerts.
- Every server and pod is labelled with its pool. A sweep deletes any without a heartbeat for
  15 minutes, and any not in the orchestrator's list.
- Claude calls run on the main pool at a fixed concurrency (CLI now; the API for the public
  service, 5.2).

### 2.5 The worker image

- One Docker image from this repo, pushed to GitHub Container Registry. It holds the Python
  env, pdftotext, headless Chrome (NSE needs a browser session) and the worker loop. The task
  handlers are the same code the collectors use, in one task registry (kind -> handler,
  retries, timeout, pool, domain, follow-ups).
- The GPU variant adds CUDA and GLM-OCR; weights are cached on the pod's volume.
- Workers never hold Dhan credentials, DB credentials or the cloud API keys. They hold only
  their one-time orchestrator token.

## 3. First uses

1. **OCR backlog.** The 2,385 filings go to the runpod pool, after a 100-page trial against
   the CPU output (pages a minute, agreement, cost per 1,000 pages).
2. **History crawl.** 1-2 years of NSE and BSE announcements, their attachments and annual
   reports. Then OCR, then Claude reading.

## 4. Research tasks (for the open interface)

The same orchestrator runs the task kinds a deep company question needs. A question is
planned by Claude into a task tree, run, and answered by Claude from the gathered sources,
every claim linked to its source.

| Task kind | Source | Pool |
|---|---|---|
| filings for a company | our tables; NSE/BSE fetch if missing | hetzner |
| board and founders | exchange filings (director changes, shareholding), annual reports, SEBI orders (`sebi_orders`), MCA public data | hetzner |
| web search | free sources: Google Programmable Search JSON API (100 queries/day free), Wikipedia, company websites; not scraped Google result pages (Google blocks them and its terms forbid it) | hetzner |
| global news | GDELT (free, global), news RSS, ET (already collected) | hetzner |
| page fetch | company sites, news articles (default gap per domain) | hetzner |
| read / analyse | Claude | main |

### 4.1 Records: every filing stored as a summary, typed facts and searchable passages

Decided 2026-11-10. Questions will be about orders, preferential shares, founder equity,
investments, boards and more, often across many filings or companies. So each filing is
read once and stored three ways (`infra/records.py`):

- **Record** (`infra_record`): one per disclosure. The NSE and BSE copies of one document
  merge, keyed on the document's text layer (a scan keys on its size and pages). Pilot:
  842 filings -> 427 records. Each record keeps Claude's 2-4 sentence summary. A record whose
  text changes (OCR arriving) goes back to pending and is read again.
- **Facts** (`infra_fact`): one row per thing that happened, typed from a fixed list
  (`records.FACT_TYPES`: order won, order book, L1 bid, results, guidance, preferential issue,
  warrants, public issue, debt, ESOP, buyback, dividend, bonus/split, promoter stake, pledge,
  insider trade, major holder, investment, acquisition, divestment, subsidiary/JV, capex,
  partnership, approval, credit rating, director and KMP joined/left, auditor change,
  litigation, regulatory action, related party, other). Common columns: date, period, amount
  in Rs crore (foreign amounts at the RBI rate), percentage, shares, price, counterparty,
  person, role, direction; anything else in `details`. Every fact carries a verbatim quote
  and is **verified** only when the quote is in the text and its numbers are in the quote
  (the order-win rule). Unverified facts are kept for inspection, never used in answers.
  Each fact records the extraction version, so a new type re-reads only what it needs.
- **Passages** (`infra_passage`): the full text cut into ~1,200-character pieces, each with a
  384-number vector from a small open embedding model on the CPU (`infra/embed.py`,
  bge-small-en-v1.5) in pgvector. The model only finds similar text; it writes and judges
  nothing, so the "no local LLM" rule holds. This replaces cutting each filing at 6,000
  characters, which lost the June 2026 KAYNES order book in the first test.
- Routine notices (trading window, newspaper copies, depository certificates) get a summary
  only. A long document is read as its first 12k characters plus up to 18k of the passages
  most like a material fact.

Answering (`infra/research.py`): Claude plans the fact types, text searches and dates; the
code pulls the verified facts and the closest passages; Claude answers citing records [R#];
citations of anything it was not given are removed. Cross-company questions ("preferential
allotments to promoters above Rs 50 crore in 2025") are plain SQL over `infra_fact` -- not
built yet.

Extraction runs in the 22:00-08:00 IST window like the other Claude jobs.

## 5. Before the public service

1. **Secrets.** The repo stays private; workers still get no keys (2.5), and the site never
   exposes the orchestrator or the DB.
2. **Claude for the public.** The CLI runs on a personal subscription and cannot serve other
   people. The public service needs the Anthropic API with its own key, a budget per user and
   per day, and a queue.
3. **Data terms.** Releasing the code is one question; serving NSE/BSE-derived data to the
   public is another, and their terms on redistribution should be read first.
4. **People.** Analysis of directors and founders uses public records only (filings, regulator
   orders, reported news), states each claim with its source, and does not collect private
   details. This limits privacy and defamation risk.
5. **Abuse limits.** User questions create tasks, so each user gets a task and cost budget.
   The rate policy (2.3) holds whatever users ask.

## 6. Build order

1. **Measure** (no spend): per-stage time and backlog.
2. **Orchestrator + task tables + worker loop**, with a local worker on the main server first.
   Port one collector (NSE announcement list for a company) and OCR as the first task kinds.
3. **Worker image** (CPU and GPU variants) in the registry.
4. **RunPod pool:** 100-page OCR trial, then the 2,385-filing backlog.
5. **Hetzner pool:** pilot with 2 servers and 50 companies, with the block detector checked by
   hand, then 8.
6. **History run.**
7. **Research task kinds** (search API, news, page fetch, board/founder) and a separate PRD for
   the public interface.

## 7. Decisions for the operator

- Decided: under USD 100/month per pool; free search sources; start with two stocks.

## 8. Risks

- **Hetzner IP ranges blocked** reaching the main server. Mitigations: block detection, domain
  pause, quiet hours, the main server never crawls history.
- **Runaway cost or forgotten machines.** Caps, labels, the heartbeat sweep.
- **The orchestrator is a single point.** It holds all state in Postgres, so a restart resumes:
  running leases expire and the tasks go back to pending.
- **GPU OCR differs from CPU OCR.** The 100-page trial comes first.
