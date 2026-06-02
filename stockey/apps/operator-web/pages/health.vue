<script setup lang="ts">
import type { Dict } from '~/types/api'

const api = useOperatorApi()
const [{ data: summary }, { data: details, refresh }] = await Promise.all([
  useAsyncData('summary-health', () => api.getSummary()),
  useAsyncData('operator-health-details', () => api.getHealthDetails())
])

const sections = computed(() => details.value?.sections || {})
const tableFreshness = computed(() => asList(sections.value.table_freshness))
const cronLogs = computed(() => asList(sections.value.cron_logs))
const optionalDeps = computed(() => asList(sections.value.optional_dependencies))
const database = computed(() => asDict(sections.value.database))
const operatorApi = computed(() => asDict(sections.value.operator_api))
const redis = computed(() => asDict(sections.value.redis))
const dhan = computed(() => asDict(sections.value.dhan))
const dhanCache = computed(() => asDict(sections.value.dhan_cache))
const frontend = computed(() => asDict(sections.value.frontend))
const operatorSnapshot = computed(() => asDict(sections.value.operator_snapshot))
const slowOperations = computed(() => asDict(sections.value.slow_operations))
const syncStateFailures = computed(() => asList(sections.value.sync_state_failures))
const fixHints = computed(() => asList(details.value?.fix_hints))
const healthFilter = ref('all')
const healthFilters = [
  { key: 'all', label: 'All' },
  { key: 'error', label: 'Errors' },
  { key: 'warn', label: 'Warnings' },
  { key: 'recovered', label: 'Recovered' },
  { key: 'ok', label: 'OK' }
]
const filteredCronLogs = computed(() => cronLogs.value.filter((row) => matchesHealthFilter(row, healthFilter.value)))
const filteredFixHints = computed(() => fixHints.value.filter((row) => matchesHealthFilter(row, healthFilter.value)))
const healthFilterCounts = computed(() => {
  const rows = [...cronLogs.value, ...fixHints.value, ...syncStateFailures.value]
  return Object.fromEntries(healthFilters.map((item) => [item.key, rows.filter((row) => matchesHealthFilter(row, item.key)).length]))
})

function asDict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Dict : {}
}

function asList(value: unknown): Dict[] {
  return Array.isArray(value) ? value.filter((item) => typeof item === 'object' && item !== null) as Dict[] : []
}

function asStringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)) : []
}

function statusClass(value: unknown) {
  const status = String(value || '').toLowerCase()
  if (status === 'ok') return 'bg-moss text-paper'
  if (status === 'error') return 'bg-rust text-paper'
  return 'bg-sun text-ink'
}

function statusText(value: unknown) {
  return String(value || 'unknown').toUpperCase()
}

function secondsText(value: unknown) {
  const seconds = Number(value)
  if (Number.isNaN(seconds)) return '-'
  if (seconds < 0) return 'expired'
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`
  return `${Math.round(seconds / 86400)}d`
}

function isRecovered(row: Dict) {
  const status = String(row.latest_run_status || row.title || '').toLowerCase()
  return status.includes('recovered') || status.includes('ok_after_historical_errors') || String(row.title || '').toLowerCase().includes('historical cron errors recovered')
}

function matchesHealthFilter(row: Dict, filter: string) {
  if (filter === 'all') return true
  if (filter === 'recovered') return isRecovered(row)
  if (filter === 'ok') return String(row.status || '').toLowerCase() === 'ok'
  if (filter === 'error') return String(row.status || '').toLowerCase() === 'error'
  if (filter === 'warn') return String(row.status || '').toLowerCase() === 'warn' && !isRecovered(row)
  return true
}
</script>

<template>
  <section class="rounded-[2rem] bg-ink p-8 text-paper shadow-soft">
    <p class="text-sm font-semibold uppercase tracking-[0.35em] text-paper/55">Operator Health</p>
    <div class="mt-4 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 class="max-w-4xl text-5xl font-black tracking-tight">Runtime checks before debugging strategy output.</h1>
        <p class="mt-5 max-w-3xl text-lg leading-8 text-paper/70">
          Generated {{ details?.generated_at || '-' }}. Dashboard payload generated {{ summary?.generated_at || '-' }}.
        </p>
      </div>
      <button class="rounded-full bg-paper px-5 py-3 text-sm font-black text-ink" type="button" @click="refresh()">
        Refresh
      </button>
    </div>
  </section>

  <section class="mt-6 grid gap-4 md:grid-cols-9">
    <MetricTile label="Overall" :value="statusText(details?.status)" note="Worst current check" />
    <MetricTile label="DB" :value="statusText(database.status)" :note="String(database.message || '-')" />
    <MetricTile label="API" :value="statusText(operatorApi.status)" :note="operatorApi.latency_ms ? `${operatorApi.latency_ms} ms` : String(operatorApi.message || '-')" />
    <MetricTile label="Snapshot" :value="statusText(operatorSnapshot.status)" :note="operatorSnapshot.age_seconds ? `${Math.round(Number(operatorSnapshot.age_seconds) / 60)}m old` : String(operatorSnapshot.message || '-')" />
    <MetricTile label="Slowlog" :value="statusText(slowOperations.status)" :note="`${slowOperations.returned_count ?? 0} open`" />
    <MetricTile label="Redis" :value="statusText(redis.status)" :note="String(redis.message || '-')" />
    <MetricTile label="Dhan" :value="statusText(dhan.status)" :note="String(dhan.message || '-')" />
    <MetricTile label="Token" :value="statusText(dhanCache.status)" :note="`expires ${secondsText(dhanCache.seconds_to_expiry)}`" />
    <MetricTile label="Frontend" :value="statusText(frontend.status)" :note="String(frontend.message || '-')" />
  </section>

  <section class="mt-8 glass-panel rounded-3xl p-5">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Fix Hints</p>
        <h2 class="mt-2 text-2xl font-black">What to do next</h2>
        <p class="mt-2 max-w-3xl text-sm leading-6 text-ink/60">
          Generated from failing or stale health checks. Start with error hints, then warnings.
        </p>
      </div>
      <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(details?.status)">{{ statusText(details?.status) }}</span>
    </div>
    <div class="mt-5 flex flex-wrap gap-2">
      <button
        v-for="item in healthFilters"
        :key="item.key"
        class="rounded-full px-4 py-2 text-sm font-black transition"
        :class="healthFilter === item.key ? 'bg-ink text-paper' : 'bg-white/80 text-ink/60 hover:bg-white'"
        type="button"
        @click="healthFilter = item.key"
      >
        {{ item.label }} · {{ healthFilterCounts[item.key] || 0 }}
      </button>
    </div>
    <div class="mt-5 grid gap-3 lg:grid-cols-2">
      <article v-for="hint in filteredFixHints" :key="String(hint.title || hint.reason)" class="rounded-2xl bg-white/75 p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="font-black text-ink">{{ hint.title }}</p>
            <p class="mt-1 text-sm leading-6 text-ink/60">{{ hint.reason }}</p>
          </div>
          <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(hint.status)">{{ statusText(hint.status) }}</span>
        </div>
        <div v-if="asStringList(hint.commands).length" class="mt-3 space-y-2">
          <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/40">Commands</p>
          <code v-for="command in asStringList(hint.commands)" :key="command" class="block overflow-auto rounded-xl bg-ink px-3 py-2 text-xs text-paper">
            {{ command }}
          </code>
        </div>
        <details v-if="Object.keys(asDict(hint.details)).length" class="mt-3">
          <summary class="cursor-pointer text-sm font-black text-moss">Show hint details</summary>
          <pre class="mt-3 max-h-56 overflow-auto rounded-2xl bg-paper/80 p-3 text-xs">{{ JSON.stringify(hint.details, null, 2) }}</pre>
        </details>
      </article>
      <p v-if="!filteredFixHints.length" class="rounded-2xl bg-white/75 p-4 text-sm text-ink/60">No fix hints for this filter.</p>
    </div>
  </section>

  <section class="mt-8 grid gap-6 lg:grid-cols-[1.1fr_0.9fr]">
    <div class="glass-panel rounded-3xl p-5">
      <div class="flex items-start justify-between gap-3">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Data Freshness</p>
          <h2 class="mt-2 text-2xl font-black">Key table timestamps</h2>
        </div>
        <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(details?.status)">{{ statusText(details?.status) }}</span>
      </div>
      <div class="mt-5 space-y-3">
        <article v-for="row in tableFreshness" :key="String(row.name || row.table)" class="rounded-2xl bg-white/70 p-4">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p class="font-black text-ink">{{ row.name || row.table }}</p>
              <p class="text-xs text-ink/50">{{ row.table }} · {{ row.latest_at || 'no timestamp' }}</p>
            </div>
            <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(row.status)">{{ statusText(row.status) }}</span>
          </div>
          <p class="mt-2 text-sm text-ink/60">{{ row.message }}</p>
          <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Rows:</b> {{ row.row_count ?? '-' }}</p>
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Age hours:</b> {{ row.age_hours ?? '-' }}</p>
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Limit:</b> {{ row.max_age_hours ?? '-' }}</p>
          </div>
        </article>
      </div>
    </div>

    <div class="space-y-6">
      <div class="glass-panel rounded-3xl p-5">
        <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Runtime Endpoints</p>
        <h2 class="mt-2 text-2xl font-black">API, snapshot, and broker token</h2>
        <div class="mt-4 space-y-3">
          <article class="rounded-2xl bg-white/70 p-4">
            <div class="flex items-start justify-between gap-3">
              <div>
                <p class="font-black text-ink">Operator API</p>
                <p class="mt-1 break-all text-xs text-ink/50">{{ operatorApi.url || '-' }}</p>
              </div>
              <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(operatorApi.status)">{{ statusText(operatorApi.status) }}</span>
            </div>
            <div class="mt-3 grid gap-2 text-sm md:grid-cols-2">
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Latency:</b> {{ operatorApi.latency_ms ?? '-' }} ms</p>
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Status code:</b> {{ operatorApi.status_code ?? '-' }}</p>
            </div>
            <p class="mt-2 text-sm text-ink/60">{{ operatorApi.message }}</p>
          </article>
          <article class="rounded-2xl bg-white/70 p-4">
            <div class="flex items-start justify-between gap-3">
              <div>
                <p class="font-black text-ink">Operator snapshot</p>
                <p class="mt-1 break-all text-xs text-ink/50">{{ operatorSnapshot.generated_at || '-' }}</p>
              </div>
              <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(operatorSnapshot.status)">{{ statusText(operatorSnapshot.status) }}</span>
            </div>
            <div class="mt-3 grid gap-2 text-sm md:grid-cols-2">
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Age:</b> {{ secondsText(operatorSnapshot.age_seconds) }}</p>
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Bytes:</b> {{ operatorSnapshot.payload_bytes ?? '-' }}</p>
            </div>
            <p class="mt-2 text-sm text-ink/60">{{ operatorSnapshot.message }}</p>
          </article>
          <article class="rounded-2xl bg-white/70 p-4">
            <div class="flex items-start justify-between gap-3">
              <div>
                <p class="font-black text-ink">Slow-operation log</p>
                <p class="mt-1 break-all text-xs text-ink/50">{{ slowOperations.state_file || '-' }}</p>
              </div>
              <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(slowOperations.status)">{{ statusText(slowOperations.status) }}</span>
            </div>
            <div class="mt-3 grid gap-2 text-sm md:grid-cols-2">
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Issues:</b> {{ slowOperations.issue_count ?? 0 }}</p>
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Open shown:</b> {{ slowOperations.returned_count ?? 0 }}</p>
            </div>
            <details v-if="asList(slowOperations.issues).length" class="mt-3">
              <summary class="cursor-pointer text-sm font-black text-rust">Show slow issues</summary>
              <pre class="mt-3 max-h-72 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ JSON.stringify(slowOperations.issues, null, 2) }}</pre>
            </details>
          </article>
          <article class="rounded-2xl bg-white/70 p-4">
            <div class="flex items-start justify-between gap-3">
              <div>
                <p class="font-black text-ink">Dhan token cache</p>
                <p class="mt-1 break-all text-xs text-ink/50">{{ dhanCache.cache_path || '-' }}</p>
              </div>
              <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(dhanCache.status)">{{ statusText(dhanCache.status) }}</span>
            </div>
            <div class="mt-3 grid gap-2 text-sm md:grid-cols-2">
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Expires:</b> {{ dhanCache.expires_at || '-' }}</p>
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Time left:</b> {{ secondsText(dhanCache.seconds_to_expiry) }}</p>
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Cache age:</b> {{ secondsText(dhanCache.cache_age_seconds) }}</p>
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Env token:</b> {{ dhanCache.has_direct_env_token ? 'yes' : 'no' }}</p>
            </div>
            <p class="mt-2 text-sm text-ink/60">{{ dhanCache.message }}</p>
          </article>
        </div>
      </div>

      <div class="glass-panel rounded-3xl p-5">
        <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Cron Logs</p>
        <h2 class="mt-2 text-2xl font-black">Latest run status</h2>
        <div class="mt-4 space-y-3">
          <article v-for="row in filteredCronLogs" :key="String(row.log_file || row.message)" class="rounded-2xl bg-white/70 p-4">
            <div class="flex items-start justify-between gap-3">
              <div>
                <p class="break-all text-sm font-black text-ink">{{ row.log_file || 'cron' }}</p>
                <p class="mt-1 text-xs text-ink/50">{{ row.latest_run_status || 'unknown' }} · modified {{ row.modified_at || '-' }}</p>
              </div>
              <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(row.status)">{{ statusText(row.status) }}</span>
            </div>
            <p class="mt-2 text-sm text-ink/60">{{ row.message }}</p>
            <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Current errors:</b> {{ row.current_error_count ?? row.recent_error_count ?? '-' }}</p>
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Historical:</b> {{ row.historical_error_count ?? '-' }}</p>
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Analyzed lines:</b> {{ row.analyzed_lines ?? '-' }}</p>
            </div>
            <p v-if="row.latest_success_line" class="mt-3 rounded-xl bg-moss/10 px-3 py-2 text-xs text-ink/70">
              <b>Latest success:</b> {{ row.latest_success_line }}
            </p>
            <p v-if="row.latest_error_line" class="mt-2 rounded-xl bg-rust/10 px-3 py-2 text-xs text-ink/70">
              <b>Latest error:</b> {{ row.latest_error_line }}
            </p>
            <p v-if="asDict(row.latest_script_marker).status" class="mt-2 rounded-xl bg-paper/70 px-3 py-2 text-xs text-ink/70">
              <b>Script marker:</b> {{ JSON.stringify(row.latest_script_marker) }}
            </p>
            <details v-if="asStringList(row.recent_errors).length" class="mt-3">
              <summary class="cursor-pointer text-sm font-black text-rust">Show current errors</summary>
              <pre class="mt-3 max-h-72 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ JSON.stringify(row.recent_errors, null, 2) }}</pre>
            </details>
            <details v-if="asStringList(row.historical_errors).length" class="mt-3">
              <summary class="cursor-pointer text-sm font-black text-moss">Show historical errors</summary>
              <pre class="mt-3 max-h-72 overflow-auto rounded-2xl bg-paper/80 p-4 text-xs leading-5 text-ink">{{ JSON.stringify(row.historical_errors, null, 2) }}</pre>
            </details>
            <details v-if="asList(row.recovery_outputs).length" class="mt-3">
              <summary class="cursor-pointer text-sm font-black text-moss">Show recovery evidence</summary>
              <pre class="mt-3 max-h-72 overflow-auto rounded-2xl bg-paper/80 p-4 text-xs leading-5 text-ink">{{ JSON.stringify(row.recovery_outputs, null, 2) }}</pre>
            </details>
          </article>
          <p v-if="!filteredCronLogs.length" class="rounded-2xl bg-white/70 p-4 text-sm text-ink/60">No cron logs for this filter.</p>
        </div>
      </div>

      <div class="glass-panel rounded-3xl p-5">
        <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Optional Dependencies</p>
        <h2 class="mt-2 text-2xl font-black">OCR, Codex, Node, TimesFM</h2>
        <div class="mt-4 flex flex-wrap gap-2">
          <span v-for="row in optionalDeps" :key="String(row.module || row.message)" class="rounded-full px-3 py-2 text-xs font-black" :class="statusClass(row.status)">
            {{ row.module || row.message }}: {{ statusText(row.status) }}
          </span>
        </div>
      </div>

      <div class="glass-panel rounded-3xl p-5">
        <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Sync Failures</p>
        <h2 class="mt-2 text-2xl font-black">Watcher/router state</h2>
        <div class="mt-4 space-y-3">
          <article v-for="row in syncStateFailures" :key="String(row.source_name || row.message)" class="rounded-2xl bg-white/70 p-4">
            <div class="flex items-start justify-between gap-3">
              <div>
                <p class="break-all text-sm font-black text-ink">{{ row.source_name || 'sync-state' }}</p>
                <p class="mt-1 text-xs text-ink/50">{{ row.updated_at || '-' }}</p>
              </div>
              <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(row.status)">{{ statusText(row.status) }}</span>
            </div>
            <p class="mt-2 text-sm text-ink/60">{{ row.error || row.message }}</p>
            <details v-if="row.state_json" class="mt-3">
              <summary class="cursor-pointer text-sm font-black text-rust">Show state payload</summary>
              <pre class="mt-3 max-h-72 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ row.state_json }}</pre>
            </details>
          </article>
        </div>
      </div>
    </div>
  </section>

  <section class="mt-8 grid gap-6 lg:grid-cols-3">
    <div class="glass-panel rounded-3xl p-5">
      <h2 class="font-black">Runtime Processes</h2>
      <pre class="mt-4 max-h-96 overflow-auto text-xs">{{ JSON.stringify(summary?.runtime_processes || [], null, 2) }}</pre>
    </div>
    <div class="glass-panel rounded-3xl p-5">
      <h2 class="font-black">Sync State</h2>
      <pre class="mt-4 max-h-96 overflow-auto text-xs">{{ JSON.stringify(summary?.sync_state || [], null, 2) }}</pre>
    </div>
    <div class="glass-panel rounded-3xl p-5">
      <h2 class="font-black">Raw Health</h2>
      <pre class="mt-4 max-h-96 overflow-auto text-xs">{{ JSON.stringify(details || {}, null, 2) }}</pre>
    </div>
  </section>
</template>
