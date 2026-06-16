<script setup lang="ts">
import type { Dict, ScreenerPreviewPayload } from '~/types/api'

const api = useOperatorApi()
const { data: coverage, refresh: refreshCoverage, error: coverageError } = await useAsyncData('screener-coverage', () => api.getScreenerCoverage({ lookback_days: 30, limit: 50 }))
const { data: failures, refresh: refreshFailures, error: failuresError } = await useAsyncData('screener-failures', () => api.getScreenerFailures({ hours: 24, limit: 25 }))
const queryName = ref('Operator Screener Preview')
const queryText = ref(`Market capitalization > 1000 AND
Current price > 50 AND
Current price > DMA 50 AND
Current price > DMA 200`)
const rowLimit = ref(25)
const loading = ref('')
const errorText = ref('')
const result = ref<ScreenerPreviewPayload | null>(null)

const validationIssues = computed(() => asList(result.value?.validation_issues))
const rows = computed(() => asList(result.value?.rows))
const coverageRows = computed(() => asList(coverage.value?.screeners))
const coverageSummary = computed(() => asDict(coverage.value?.summary))
const failureRows = computed(() => asList(failures.value?.rows))
const failureBoundary = computed(() => asDict(failures.value?.operator_boundary))
const meta = computed(() => asDict(result.value?.meta))
const boundary = computed(() => asDict(result.value?.operator_boundary))
const hasResult = computed(() => Boolean(result.value))

function asList(value: unknown): Dict[] {
  return Array.isArray(value) ? value.filter((item) => typeof item === 'object' && item !== null) as Dict[] : []
}

function asDict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Dict : {}
}

function display(value: unknown) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  if (typeof value === 'number') return Intl.NumberFormat('en-IN', { maximumFractionDigits: 3 }).format(value)
  return String(value)
}

function statusTone(status: unknown): 'success' | 'warning' | 'danger' | 'neutral' | 'info' {
  const text = String(status || '').toLowerCase()
  if (text === 'ok' || text === 'valid') return 'success'
  if (text === 'invalid') return 'danger'
  if (text === 'error') return 'danger'
  if (text === 'warn') return 'warning'
  return 'neutral'
}

function companyMetrics(row: Dict): Dict {
  return asDict(row.metrics)
}

function pct(value: unknown) {
  if (value === null || value === undefined || value === '') return '-'
  const num = Number(value)
  if (Number.isNaN(num)) return '-'
  return `${Intl.NumberFormat('en-IN', { maximumFractionDigits: 1 }).format(num)}%`
}

async function runPreview(fetchRows: boolean) {
  errorText.value = ''
  loading.value = fetchRows ? 'fetch' : 'validate'
  try {
    result.value = await api.previewScreener({
      query_name: queryName.value,
      query_text: queryText.value,
      fetch_rows: fetchRows,
      row_limit: rowLimit.value
    })
  } catch (err) {
    errorText.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = ''
  }
}
</script>

<template>
  <section class="rounded-[2rem] bg-ink p-8 text-paper shadow-soft">
    <p class="text-sm font-semibold uppercase tracking-[0.35em] text-paper/55">Screener workbench</p>
    <div class="mt-4 flex flex-wrap items-end justify-between gap-5">
      <div>
        <h1 class="max-w-4xl text-5xl font-black tracking-tight">Preview Screener.in queries before promoting them.</h1>
        <p class="mt-4 max-w-3xl text-lg leading-8 text-paper/70">
          Validate syntax locally first. Fetch preview rows only when you want to test the authenticated Screener.in session. Nothing here registers a production screener or changes recommendations.
        </p>
      </div>
    </div>
  </section>

  <section class="mt-6 grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
    <form class="glass-panel rounded-3xl p-6" @submit.prevent="runPreview(false)">
      <div class="grid gap-4 md:grid-cols-2">
        <label class="grid gap-2">
          <span class="text-sm font-black text-ink">Query name</span>
          <input v-model="queryName" class="rounded-2xl border border-black/10 bg-white px-4 py-3 text-sm font-semibold outline-none focus:border-moss" />
        </label>
        <label class="grid gap-2">
          <span class="text-sm font-black text-ink">Preview row limit</span>
          <input v-model.number="rowLimit" min="1" max="100" type="number" class="rounded-2xl border border-black/10 bg-white px-4 py-3 text-sm font-semibold outline-none focus:border-moss" />
        </label>
      </div>
      <label class="mt-5 grid gap-2">
        <span class="text-sm font-black text-ink">Screener.in raw query</span>
        <textarea v-model="queryText" rows="10" class="rounded-3xl border border-black/10 bg-white p-4 font-mono text-sm leading-6 text-ink outline-none focus:border-moss"></textarea>
      </label>
      <div class="mt-5 flex flex-wrap gap-3">
        <button class="rounded-full bg-ink px-5 py-3 text-sm font-black text-paper disabled:opacity-50" type="submit" :disabled="Boolean(loading)">
          {{ loading === 'validate' ? 'Validating...' : 'Validate syntax' }}
        </button>
        <button class="rounded-full bg-moss px-5 py-3 text-sm font-black text-paper disabled:opacity-50" type="button" :disabled="Boolean(loading)" @click="runPreview(true)">
          {{ loading === 'fetch' ? 'Fetching preview...' : 'Fetch preview rows' }}
        </button>
      </div>
      <p class="mt-4 text-sm leading-6 text-ink/60">
        Use Screener syntax such as <code class="rounded bg-white px-1 py-0.5">DMA 50</code> and <code class="rounded bg-white px-1 py-0.5">DMA 200</code>. Avoid aliases like “50 Day Moving Average”.
      </p>
    </form>

    <aside class="rounded-3xl border border-sky/20 bg-sky/10 p-6">
      <p class="text-xs font-black uppercase tracking-[0.3em] text-sky">Operator boundary</p>
      <h2 class="mt-2 text-2xl font-black text-ink">Safe preview only</h2>
      <ul class="mt-4 grid gap-3 text-sm leading-6 text-ink/70">
        <li>Validation-only mode does not open Chrome or hit Screener.in.</li>
        <li>Fetch preview uses the authenticated session but sets <code class="rounded bg-white px-1 py-0.5">persist=false</code>.</li>
        <li>Failures are recorded as operational Screener failures for Health and debugging.</li>
        <li>Promotion into production still requires explicit screener registry action.</li>
      </ul>
    </aside>
  </section>

  <ApiErrorBanner v-if="errorText" class="mt-6" title="Screener preview failed" :error="errorText" />
  <ApiErrorBanner v-if="coverageError" class="mt-6" title="Screener coverage failed" :error="coverageError" />
  <ApiErrorBanner v-if="failuresError" class="mt-6" title="Screener failure audit failed" :error="failuresError" />

  <section class="mt-8 glass-panel rounded-3xl p-6">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <p class="text-xs font-bold uppercase tracking-[0.3em] text-rust">Operational failures</p>
        <h2 class="mt-2 text-2xl font-black">Recent Screener.in failures</h2>
        <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/60">
          Source-specific audit rows from the last {{ failures?.window_hours || 24 }} hours. These are operational repair signals, not investment Manual Review items.
        </p>
      </div>
      <button class="rounded-full bg-rust px-4 py-2 text-sm font-black text-paper" type="button" @click="refreshFailures()">Refresh failures</button>
    </div>
    <div class="mt-5 grid gap-4 md:grid-cols-5">
      <MetricTile label="Status" :value="String(failures?.status || '-').toUpperCase()" :note="failures?.message || 'Screener failure audit'" />
      <MetricTile label="Active" :value="display(failures?.active_count)" note="Recent failure rows" />
      <MetricTile label="Validation" :value="display(failures?.validation_count)" note="Local syntax/query issues" />
      <MetricTile label="Fetch" :value="display(failures?.fetch_count)" note="Auth/session/network" />
      <MetricTile label="Parse" :value="display(failures?.parse_count)" note="HTML/results-table changes" />
    </div>
    <div class="mt-5 rounded-2xl border border-black/10 bg-white/70 p-4">
      <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/40">Repair boundary</p>
      <p class="mt-2 text-sm leading-6 text-ink/65">
        {{ failureBoundary.operator_action || 'Fix the Screener query/session/parser issue, rerun the screener workflow, then refresh this page.' }}
      </p>
      <p class="mt-2 text-sm leading-6 text-ink/55">
        {{ failureBoundary.side_effects || 'This read-only panel does not clear failures, register screeners, change recommendations, or submit orders.' }}
      </p>
    </div>
    <div v-if="failureRows.length" class="mt-5 grid gap-4">
      <article v-for="row in failureRows" :key="String(row.failure_id || row.observed_at || row.screener_url)" class="rounded-3xl border border-rust/20 bg-rust/5 p-5">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-lg font-black text-ink">{{ row.query_name || row.failure_stage || 'Screener failure' }}</p>
            <p class="mt-1 text-sm text-ink/55">{{ display(row.observed_at) }} · {{ display(row.failure_stage) }} · {{ display(row.error_type) }}</p>
          </div>
          <StatusPill tone="danger">{{ row.failure_stage || 'failure' }}</StatusPill>
        </div>
        <p class="mt-3 rounded-2xl bg-white/80 px-4 py-3 text-sm leading-6 text-ink/70">{{ row.error_message || 'No error message recorded.' }}</p>
        <div class="mt-3 grid gap-3 md:grid-cols-2">
          <p class="break-words rounded-2xl bg-white/80 px-4 py-3 text-sm"><b>URL:</b> {{ display(row.screener_url || row.final_url) }}</p>
          <p class="break-words rounded-2xl bg-white/80 px-4 py-3 text-sm"><b>Query:</b> {{ display(row.query_text || row.query_hash) }}</p>
        </div>
        <p v-if="row.body_excerpt" class="mt-3 rounded-2xl bg-white/80 px-4 py-3 text-sm leading-6 text-ink/60">
          <b>Page excerpt:</b> {{ row.body_excerpt }}
        </p>
      </article>
    </div>
    <p v-else class="mt-5 rounded-2xl bg-white/70 p-4 text-sm font-semibold text-ink/60">
      No recent Screener.in failures in this window.
    </p>
  </section>

  <section class="mt-8 glass-panel rounded-3xl p-6">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <p class="text-xs font-bold uppercase tracking-[0.3em] text-ink/45">Coverage metrics</p>
        <h2 class="mt-2 text-2xl font-black">Which screeners feed useful candidates?</h2>
        <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/60">
          Read-only attribution over {{ coverage?.lookback_days || 30 }} days. Rows use Screener provenance preserved in constituents, candidates, and final action context.
        </p>
      </div>
      <button class="rounded-full bg-ink px-4 py-2 text-sm font-black text-paper" type="button" @click="refreshCoverage()">Refresh coverage</button>
    </div>
    <div class="mt-5 grid gap-4 md:grid-cols-5">
      <MetricTile label="Screeners" :value="display(coverageSummary.screener_count)" :note="`${display(coverageSummary.returned_count)} shown`" />
      <MetricTile label="Constituent Rows" :value="display(coverageSummary.constituent_rows)" note="Window total" />
      <MetricTile label="Candidate Rows" :value="display(coverageSummary.candidate_rows)" note="Attributed to screeners" />
      <MetricTile label="Action Rows" :value="display(coverageSummary.action_rows)" note="Attributed final actions" />
      <MetricTile label="Window" :value="String(coverage?.window?.start_date || '-')" :note="String(coverage?.window?.end_date || '-')" />
    </div>
    <div class="mt-6 grid gap-4">
      <article v-for="row in coverageRows" :key="String(row.screener_slug)" class="rounded-3xl bg-white/80 p-5 shadow-soft">
        <div class="flex flex-wrap items-start justify-between gap-4">
          <div>
            <p class="text-lg font-black text-ink">{{ row.screener_name || row.screener_slug }}</p>
            <p class="mt-1 text-sm text-ink/55">
              {{ row.screener_slug }} · latest constituents {{ display(row.latest_constituent_date) }}
            </p>
          </div>
          <div class="flex flex-wrap justify-end gap-2">
            <StatusPill tone="info" title="Symbols from this screener that reached the candidate table.">
              candidates {{ display(row.candidate_symbols) }}
            </StatusPill>
            <StatusPill tone="action" title="Symbols from this screener that reached final consolidated actions.">
              actions {{ display(row.action_symbols) }}
            </StatusPill>
          </div>
        </div>
        <div class="mt-4 grid gap-3 md:grid-cols-4">
          <MetricTile label="Universe" :value="display(row.constituent_symbols)" :note="`${display(row.constituent_rows)} rows`" />
          <MetricTile label="Candidate Coverage" :value="pct(row.candidate_symbol_coverage_pct)" :note="`${display(row.candidate_rows)} candidate rows`" />
          <MetricTile label="Action Coverage" :value="pct(row.action_symbol_coverage_pct)" :note="`${display(row.action_rows)} action rows`" />
          <MetricTile label="Positive Rate" :value="pct(row.positive_action_rate_pct)" :note="`${display(row.positive_action_rows)} buy/add rows`" />
        </div>
        <div class="mt-4 flex flex-wrap gap-2">
          <StatusPill tone="neutral">PASS_NOW {{ display(row.pass_now_rows) }}</StatusPill>
          <StatusPill tone="neutral">WATCH {{ display(row.watch_rows) }}</StatusPill>
          <StatusPill tone="neutral">MANUAL {{ display(row.manual_review_rows) }}</StatusPill>
          <StatusPill tone="neutral">EXITS {{ display(row.exit_action_rows) }}</StatusPill>
        </div>
      </article>
      <p v-if="!coverageRows.length" class="rounded-2xl bg-white/70 p-4 text-sm font-semibold text-ink/60">
        No screener coverage rows returned for the selected window.
      </p>
    </div>
  </section>

  <section v-if="hasResult" class="mt-8 grid gap-4 md:grid-cols-5">
    <MetricTile label="Status" :value="String(result?.status || '-').toUpperCase()" :note="result?.generated_at || '-'" />
    <MetricTile label="Rows" :value="String(result?.row_count || 0)" :note="`${meta.returned_rows ?? rows.length} shown`" />
    <MetricTile label="Validation" :value="validationIssues.length ? 'ISSUES' : 'PASS'" :note="`${validationIssues.length} local issue(s)`" />
    <MetricTile label="Persisted" :value="display(meta.persisted)" note="Query results are never stored by preview" />
    <MetricTile label="Hash" :value="String(result?.query_hash || '-').slice(0, 10)" :note="String(result?.query_slug || '-')" />
  </section>

  <section v-if="hasResult" class="mt-8 glass-panel rounded-3xl p-6">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <p class="text-xs font-bold uppercase tracking-[0.3em] text-ink/45">Preview result</p>
        <h2 class="mt-2 text-2xl font-black">{{ result?.query_name }}</h2>
        <a v-if="result?.screener_url" :href="result.screener_url" target="_blank" rel="noreferrer" class="mt-2 inline-block text-sm font-black text-moss underline decoration-moss/40 underline-offset-4">
          Open Screener.in URL
        </a>
      </div>
      <StatusPill :tone="statusTone(result?.status)">{{ result?.status }}</StatusPill>
    </div>

    <div v-if="validationIssues.length" class="mt-5 grid gap-3">
      <article v-for="issue in validationIssues" :key="`${issue.code}-${issue.text}`" class="rounded-2xl border border-rust/20 bg-rust/10 p-4">
        <p class="font-black text-rust">{{ issue.code }}</p>
        <p class="mt-1 text-sm text-ink/70">{{ issue.text }}</p>
        <p class="mt-2 text-sm font-bold text-ink">{{ issue.suggestion }}</p>
      </article>
    </div>

    <div v-else-if="rows.length" class="mt-5 grid gap-4">
      <article v-for="row in rows" :key="`${row.ticker || row.company_name}-${row.rank}`" class="rounded-3xl bg-white/80 p-5 shadow-soft">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-lg font-black text-ink">{{ row.company_name || row.ticker || 'Unknown company' }}</p>
            <p class="mt-1 text-sm text-ink/55">Rank {{ display(row.rank) }} · {{ row.company_url || '-' }}</p>
          </div>
          <SymbolLink v-if="row.ticker" :symbol="String(row.ticker)" />
        </div>
        <div class="mt-4 flex flex-wrap gap-2">
          <StatusPill v-for="[key, value] in Object.entries(companyMetrics(row)).slice(0, 8)" :key="key" tone="neutral" :title="key">
            {{ key }}: {{ display(value) }}
          </StatusPill>
        </div>
      </article>
    </div>

    <p v-else class="mt-5 rounded-2xl bg-white/70 p-4 text-sm font-semibold text-ink/60">
      No preview rows returned. If status is valid, click “Fetch preview rows” to test the authenticated Screener.in query.
    </p>

    <details class="mt-5 rounded-2xl border border-black/10 bg-white/60 px-4 py-3">
      <summary class="cursor-pointer text-sm font-black text-moss">Show raw preview payload</summary>
      <pre class="mt-3 max-h-96 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ JSON.stringify({ result, boundary }, null, 2) }}</pre>
    </details>
  </section>
</template>
