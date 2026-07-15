<script setup lang="ts">
type Dict = Record<string, unknown>

const api = useOperatorApi()
const { data, pending, error: loadError, refresh } = await useAsyncData('health-hub', () => api.getHealthHub({ error_limit: 25 }))

const dataHealth = computed(() => asDict(data.value?.data_health))
const blocked = computed(() => asDict(data.value?.blocked_on_data))
const apiErrors = computed(() => asDict(data.value?.api_errors))
const monitor = computed(() => asDict(data.value?.llm_monitor))
const skipped = computed(() => asList(data.value?.skipped))
const errorRows = computed(() => asList(apiErrors.value.errors))
const syncRows = computed(() => asList(dataHealth.value.sync_state))
const cronRows = computed(() => asList(dataHealth.value.cron_status))

function asDict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Dict : {}
}
function asList(value: unknown): Dict[] {
  return Array.isArray(value) ? value.filter((item) => typeof item === 'object' && item !== null) as Dict[] : []
}
function display(value: unknown) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(2)
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  if (typeof value === 'object') return JSON.stringify(value).slice(0, 120)
  return String(value)
}
</script>

<template>
  <section class="space-y-6">
    <header class="rounded-3xl border border-ink/10 bg-ink px-8 py-7 text-paper">
      <p class="text-xs font-black uppercase tracking-[0.2em] text-paper/50">Read-only · one diagnostics hub</p>
      <h1 class="mt-2 text-3xl font-black tracking-tight">Health</h1>
      <p class="mt-2 max-w-2xl text-paper/70">
        Data freshness, cron/pipeline status, API errors, and the LLM systematic-error monitor — every
        degradation in one place.
      </p>
    </header>

    <ApiErrorBanner v-if="loadError" :error="loadError" title="Could not load the health hub" />
    <p v-if="pending" class="text-sm text-ink/40">loading…</p>

    <div v-if="skipped.length" class="rounded-2xl border border-rust/20 bg-rust/5 px-5 py-4 text-sm text-rust">
      Degraded sources: <span v-for="(s, i) in skipped" :key="i">{{ display(s.source) }} ({{ display(s.error) }}){{ i < skipped.length - 1 ? ', ' : '' }}</span>
    </div>

    <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
      <div class="rounded-2xl border border-ink/10 bg-white/60 px-5 py-4">
        <p class="text-xs font-black uppercase tracking-wide text-ink/45">Blocked on data</p>
        <p class="mt-1 text-2xl font-black text-ink">{{ display(blocked.count) }}</p>
      </div>
      <div class="rounded-2xl border border-ink/10 bg-white/60 px-5 py-4">
        <p class="text-xs font-black uppercase tracking-wide text-ink/45">API errors (recent)</p>
        <p class="mt-1 text-2xl font-black text-ink">{{ display(asDict(apiErrors.summary).total) }}</p>
      </div>
      <div class="rounded-2xl border border-ink/10 bg-white/60 px-5 py-4">
        <p class="text-xs font-black uppercase tracking-wide text-ink/45">LLM monitor</p>
        <p class="mt-1 text-2xl font-black" :class="monitor.has_systematic_error ? 'text-rust' : 'text-ink'">
          {{ monitor.has_systematic_error ? 'systematic error' : 'ok' }}
          <span class="text-sm font-semibold text-ink/50">· {{ display(monitor.alert_count ?? 0) }} alerts</span>
        </p>
      </div>
    </div>

    <section v-if="syncRows.length" class="rounded-2xl border border-ink/10 bg-white/60 p-5">
      <h2 class="text-lg font-black tracking-tight text-ink">Sync state</h2>
      <div class="mt-3 overflow-x-auto">
        <table class="w-full text-sm">
          <tbody>
            <tr v-for="(row, idx) in syncRows" :key="idx" class="border-b border-ink/5">
              <td class="py-2 pr-4 font-semibold text-ink/70">{{ display(row.source_name) }}</td>
              <td class="py-2 pr-4" :class="String(row.status) === 'error' ? 'text-rust font-semibold' : 'text-ink/55'">{{ display(row.status) }}</td>
              <td class="py-2 text-ink/45">{{ display(row.last_success_at ?? row.updated_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section v-if="cronRows.length" class="rounded-2xl border border-ink/10 bg-white/60 p-5">
      <h2 class="text-lg font-black tracking-tight text-ink">Cron status</h2>
      <div class="mt-3 overflow-x-auto">
        <table class="w-full text-sm">
          <tbody>
            <tr v-for="(row, idx) in cronRows" :key="idx" class="border-b border-ink/5">
              <td class="py-2 pr-4 font-semibold text-ink/70">{{ display(row.log_file) }}</td>
              <td class="py-2 text-ink/55">{{ display(row.modified_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section v-if="errorRows.length" class="rounded-2xl border border-ink/10 bg-white/60 p-5">
      <h2 class="text-lg font-black tracking-tight text-ink">Recent API errors</h2>
      <div class="mt-3 space-y-2">
        <div v-for="(row, idx) in errorRows" :key="idx" class="rounded-xl border border-rust/15 bg-rust/5 px-4 py-2.5 text-sm">
          <div class="flex items-center justify-between">
            <span class="font-semibold text-rust">{{ display(row.route ?? row.endpoint ?? row.path) }}</span>
            <span class="text-xs text-ink/45">{{ display(row.status_code) }} · {{ display(row.occurred_at) }}</span>
          </div>
          <p class="mt-1 text-ink/60">{{ display(row.error_message) }}</p>
        </div>
      </div>
    </section>

    <div class="flex justify-end">
      <button class="rounded-full border border-ink/15 bg-white/70 px-4 py-2 text-sm font-semibold hover:bg-white" @click="refresh()">Refresh</button>
    </div>
  </section>
</template>
