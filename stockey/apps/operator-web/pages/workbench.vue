<script setup lang="ts">
type Dict = Record<string, unknown>

const api = useOperatorApi()
const { data, pending, error: loadError, refresh } = await useAsyncData('workbench', () => api.getWorkbench({ top_n: 8 }))

const recommendations = computed(() => asDict(data.value?.recommendations))
const holdings = computed(() => asDict(data.value?.holdings))
const health = computed(() => asDict(data.value?.health))
const alerts = computed(() => asDict(data.value?.alerts))
const recItems = computed(() => asList(recommendations.value.items))
const holdItems = computed(() => asList(holdings.value.items))
const alertItems = computed(() => asList(alerts.value.items))
const recSummary = computed(() => asDict(recommendations.value.summary))
const whySymbol = ref<string | null>(null)

function alertTime(value: unknown): string {
  if (typeof value !== 'string' || !value) return ''
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return ''
  return parsed.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

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
  return String(value)
}
</script>

<template>
  <section class="space-y-6">
    <header class="rounded-3xl border border-ink/10 bg-ink px-8 py-7 text-paper">
      <p class="text-xs font-black uppercase tracking-[0.2em] text-paper/50">Review-only · what needs me now</p>
      <h1 class="mt-2 text-3xl font-black tracking-tight">Workbench</h1>
      <p class="mt-2 max-w-2xl text-paper/70">
        The top recommendations to action, the holdings you are tracking, and anything wrong with the
        system — one screen.
      </p>
    </header>

    <RegimeBanner />

    <div v-if="loadError" class="rounded-2xl border border-rust/25 bg-rust/10 px-5 py-4 text-sm text-rust">
      Could not load the workbench.
    </div>
    <p v-if="pending" class="text-sm text-ink/40">loading…</p>

    <!-- Health strip -->
    <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
      <NuxtLink to="/health-hub" class="rounded-2xl border border-ink/10 bg-white/60 px-5 py-4 hover:bg-white">
        <p class="text-xs font-black uppercase tracking-wide text-ink/45">Blocked on data</p>
        <p class="mt-1 text-2xl font-black text-ink">{{ display(health.blocked_on_data) }}</p>
      </NuxtLink>
      <NuxtLink to="/health-hub" class="rounded-2xl border border-ink/10 bg-white/60 px-5 py-4 hover:bg-white">
        <p class="text-xs font-black uppercase tracking-wide text-ink/45">API errors</p>
        <p class="mt-1 text-2xl font-black text-ink">{{ display(health.api_errors) }}</p>
      </NuxtLink>
      <div class="rounded-2xl border border-ink/10 bg-white/60 px-5 py-4">
        <p class="text-xs font-black uppercase tracking-wide text-ink/45">Recommendations</p>
        <p class="mt-1 text-2xl font-black text-ink">{{ display(recSummary.total) }}
          <span class="text-sm font-semibold text-rust">{{ recSummary.conflict ? `· ${recSummary.conflict} conflict` : '' }}</span>
        </p>
      </div>
    </div>

    <!-- Top recommendations -->
    <section class="rounded-2xl border border-ink/10 bg-white/60 p-5">
      <div class="flex items-center justify-between">
        <h2 class="text-lg font-black tracking-tight text-ink">Top recommendations</h2>
        <NuxtLink to="/recommendations-unified" class="text-sm font-semibold text-ink/55 hover:underline">See all →</NuxtLink>
      </div>
      <div class="mt-3 space-y-2">
        <div v-for="(row, idx) in recItems" :key="idx" class="flex items-center justify-between gap-3 rounded-xl border border-ink/5 bg-paper px-4 py-2.5">
          <button class="font-black text-ink underline-offset-2 hover:underline" @click="whySymbol = String(row.symbol)">{{ display(row.symbol) }}</button>
          <span class="flex items-center gap-3 text-sm">
            <span class="font-semibold">{{ display(row.action) }}</span>
            <span v-if="row.conflict" class="rounded-full border border-rust/30 bg-rust/10 px-2 py-0.5 text-xs font-semibold text-rust">conflict</span>
            <span v-else-if="row.agree" class="rounded-full border border-moss/25 bg-moss/10 px-2 py-0.5 text-xs font-semibold text-moss">agree</span>
          </span>
        </div>
        <p v-if="!recItems.length && !pending" class="py-4 text-center text-sm text-ink/40">No open recommendations.</p>
      </div>
    </section>

    <!-- Live alerts (intraday, review-only) -->
    <section class="rounded-2xl border border-ink/10 bg-white/60 p-5">
      <div class="flex items-center justify-between">
        <h2 class="text-lg font-black tracking-tight text-ink">Live alerts
          <span class="text-sm font-semibold text-ink/45">· intraday, review-only</span>
        </h2>
        <span v-if="Number(alerts.material_announcement_count) > 0" class="rounded-full border border-ink bg-ink px-2.5 py-0.5 text-xs font-black text-paper">
          {{ display(alerts.material_announcement_count) }} material filing{{ Number(alerts.material_announcement_count) === 1 ? '' : 's' }}
        </span>
      </div>
      <div class="mt-3 space-y-2">
        <div v-for="(row, idx) in alertItems" :key="idx" class="flex items-center justify-between gap-3 rounded-xl border border-ink/5 bg-paper px-4 py-2.5">
          <div class="flex min-w-0 items-center gap-3">
            <button class="shrink-0 font-black text-ink underline-offset-2 hover:underline" @click="whySymbol = String(row.symbol)">{{ display(row.symbol) }}</button>
            <span
              class="shrink-0 rounded-full px-2 py-0.5 text-xs font-black"
              :class="row.alert_type === 'MATERIAL_ANNOUNCEMENT' ? 'bg-ink text-paper' : 'border border-ink/15 text-ink/55'"
            >{{ display(row.alert_type) }}</span>
            <span class="truncate text-sm text-ink/60">{{ display(row.alert_reason) }}</span>
          </div>
          <span class="shrink-0 text-xs text-ink/40">{{ alertTime(row.observed_at) }}</span>
        </div>
        <p v-if="!alertItems.length && !pending" class="py-4 text-center text-sm text-ink/40">No live alerts in the last 36h.</p>
      </div>
    </section>

    <!-- Holdings -->
    <section class="rounded-2xl border border-ink/10 bg-white/60 p-5">
      <div class="flex items-center justify-between">
        <h2 class="text-lg font-black tracking-tight text-ink">Tracked holdings</h2>
        <NuxtLink to="/positions" class="text-sm font-semibold text-ink/55 hover:underline">See all →</NuxtLink>
      </div>
      <div class="mt-3 space-y-2">
        <div v-for="(row, idx) in holdItems" :key="idx" class="flex items-center justify-between gap-3 rounded-xl border border-ink/5 bg-paper px-4 py-2.5">
          <button class="font-black text-ink underline-offset-2 hover:underline" @click="whySymbol = String(row.symbol)">{{ display(row.symbol) }}</button>
          <span class="flex items-center gap-3 text-sm text-ink/60">
            <span>entry {{ display(row.entry_price) }}</span>
            <span>now {{ display(row.current_price) }}</span>
            <span :class="typeof row.change_pct_since_entry === 'number' && (row.change_pct_since_entry as number) < 0 ? 'text-rust font-semibold' : 'text-moss font-semibold'">
              {{ typeof row.change_pct_since_entry === 'number' ? `${(row.change_pct_since_entry as number) > 0 ? '+' : ''}${row.change_pct_since_entry}%` : '' }}
            </span>
          </span>
        </div>
        <p v-if="!holdItems.length && !pending" class="py-4 text-center text-sm text-ink/40">No tracked holdings yet.</p>
      </div>
    </section>

    <DecisionWhy v-if="whySymbol" :symbol="whySymbol" @close="whySymbol = null" />
  </section>
</template>
