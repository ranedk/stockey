<script setup lang="ts">
import type { DataHealthFinding, DataHealthResponse } from '~/types/api'

// Human-readable names for the check ids the API returns. Kept here rather than in the
// API so the backend payload stays a stable machine contract (the same one the nightly
// cron gate consumes) and the wording can change without a backend deploy.
const CHECK_LABELS: Record<string, string> = {
  bhavcopy: 'NSE bhavcopy',
  adjustment_join: 'Adjustment factors',
  dhan_daily: 'Dhan daily breadth',
  dhan_daily_freshness: 'Dhan daily freshness',
  intraday_breadth: 'Dhan intraday breadth',
  intraday_freshness: 'Dhan intraday freshness',
  intraday_truncated: 'Session completeness',
  universe_coverage: 'Universe coverage',
  trading_days: 'Trading calendar',
}

const LEVEL_TONE: Record<string, 'good' | 'warn' | 'bad'> = {
  ok: 'good',
  warn: 'warn',
  error: 'bad',
}

const LEVEL_LABEL: Record<string, string> = {
  ok: 'OK',
  warn: 'Warning',
  error: 'Failing',
}

const windowDays = ref(30)
const refreshing = ref(false)

const api = useApi()
const { data, status, error, refresh } = await useAsyncData(
  () => `data-health-${windowDays.value}`,
  () => api.get<DataHealthResponse>(`/api/data-health?window_days=${windowDays.value}`),
  { watch: [windowDays] },
)

// The intraday checks scan a 30-day window of a 528M-row compressed hypertable and take
// ~20s, so the API caches for 5 minutes. `refresh=true` forces a recompute.
async function forceRefresh() {
  refreshing.value = true
  try {
    data.value = await api.get<DataHealthResponse>(
      `/api/data-health?window_days=${windowDays.value}&refresh=true`,
    )
  }
  finally {
    refreshing.value = false
  }
}

const ordered = computed<DataHealthFinding[]>(() => {
  const rank: Record<string, number> = { error: 0, warn: 1, ok: 2 }
  return [...(data.value?.findings ?? [])].sort((a, b) => rank[a.level] - rank[b.level])
})

function metricEntries(f: DataHealthFinding): [string, string | number][] {
  return Object.entries(f.metrics ?? {})
}
</script>

<template>
  <div>
    <h1 class="text-xl font-semibold">Data Health</h1>
    <p class="mt-1 text-sm text-slate-600">
      Is the data actually <em>there</em>, across the whole universe? Every check measures
      <strong>breadth against the recent norm</strong>, not just how recent the newest row is —
      a feed can look current while almost every symbol has stopped collecting. Same
      implementation as the nightly cron gate, so this page and the gate cannot disagree.
    </p>

    <div v-if="error" class="mt-6 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
      Could not reach the stockey API ({{ error.message }}). Is the fundamentals API running on port 8000?
    </div>

    <div v-else-if="status === 'pending'" class="mt-6 text-sm text-slate-500">
      Running checks… this takes ~20s when the cache is cold.
    </div>

    <template v-else-if="data">
      <div class="mt-4 flex flex-wrap items-center gap-3">
        <BadgePill
          :label="data.status === 'ok' ? 'All checks passing' : data.status === 'warn' ? `${data.warn_count} warning${data.warn_count === 1 ? '' : 's'}` : `${data.error_count} failing`"
          :tone="LEVEL_TONE[data.status]"
        />
        <span class="text-xs text-slate-400">
          checked {{ formatDate(data.checked_at) }}
          <template v-if="data.cached">· cached {{ data.age_seconds }}s ago</template>
          · {{ data.window_days }}-day window
        </span>

        <div class="ml-auto flex items-center gap-2">
          <select
            v-model.number="windowDays"
            class="rounded-md border border-slate-200 px-2 py-1.5 text-sm focus:border-slate-400 focus:outline-none"
          >
            <option :value="7">7 days</option>
            <option :value="30">30 days</option>
            <option :value="60">60 days</option>
          </select>
          <button
            class="rounded-md border border-slate-200 px-3 py-1.5 text-sm font-medium text-slate-600 transition hover:text-slate-900 disabled:opacity-50"
            :disabled="refreshing"
            @click="forceRefresh"
          >
            {{ refreshing ? 'Rechecking…' : 'Recheck' }}
          </button>
        </div>
      </div>

      <div class="mt-4 space-y-2">
        <div
          v-for="f in ordered"
          :key="f.check"
          class="rounded-lg border bg-white p-4"
          :class="f.level === 'error' ? 'border-rose-200' : f.level === 'warn' ? 'border-amber-200' : 'border-slate-200'"
        >
          <div class="flex flex-wrap items-center gap-3">
            <BadgePill :label="LEVEL_LABEL[f.level]" :tone="LEVEL_TONE[f.level]" />
            <span class="font-medium">{{ CHECK_LABELS[f.check] ?? f.check }}</span>
            <code class="text-xs text-slate-400">{{ f.check }}</code>
          </div>
          <p class="mt-2 text-sm text-slate-700">{{ f.message }}</p>
          <dl v-if="metricEntries(f).length" class="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs text-slate-500">
            <div v-for="[k, v] in metricEntries(f)" :key="k" class="flex gap-1">
              <dt class="text-slate-400">{{ k }}:</dt>
              <dd class="font-medium text-slate-600">{{ v }}</dd>
            </div>
          </dl>
        </div>
      </div>

      <CollectorsPanel />

      <SchedulerPanel />

      <PlatformIssuesPanel />

      <CoveragePanel />

      <p class="mt-6 text-xs text-slate-400">
        Read-only. This page never repairs anything — repairs live in
        <code>data_readiness --fix</code> and the collectors themselves.
      </p>
    </template>
  </div>
</template>
