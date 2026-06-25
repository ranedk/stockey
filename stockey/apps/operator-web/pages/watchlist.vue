<script setup lang="ts">
type Dict = Record<string, unknown>

const api = useOperatorApi()
const sectionFilter = ref<'active' | 'matched' | 'expired' | 'closed'>('active')
const whySymbol = ref<string | null>(null)

const { data, pending, error: loadError, refresh } = await useAsyncData(
  'watchlist',
  () => api.getWaitSignals({ limit: 200 })
)

const summary = computed(() => asDict(data.value?.summary))
const sections = computed(() => asDict(data.value?.sections))
const rows = computed(() => asList(sections.value[sectionFilter.value]))
const sourceWarnings = computed(() => asList(data.value?.source_warnings))

function asDict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Dict : {}
}
function asList(value: unknown): Dict[] {
  return Array.isArray(value) ? value.filter((item) => typeof item === 'object' && item !== null) as Dict[] : []
}
function display(value: unknown) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  return String(value)
}
// "why watching" -> operator_summary / hypothesis title / source label; "waiting for" -> condition_summary / wait_question.
function whyWatching(row: Dict) {
  return String(row.operator_summary || row.hypothesis_title || row.source_label || row.signal_type || '-')
}
function waitingFor(row: Dict) {
  return String(row.condition_summary || row.wait_question || row.condition_issue || '-')
}
function sourceLabel(row: Dict) {
  return String(row.source_label || row.generated_by || '')
}
const sectionCounts = computed(() => ({
  active: Number(summary.value.active || 0),
  matched: Number(summary.value.matched || 0),
  expired: Number(summary.value.expired || 0),
  closed: Number(summary.value.closed || 0)
}))
</script>

<template>
  <section class="space-y-6">
    <header class="rounded-3xl border border-ink/10 bg-ink px-8 py-7 text-paper">
      <p class="text-xs font-black uppercase tracking-[0.2em] text-paper/50">Review-only · what we're waiting for</p>
      <h1 class="mt-2 text-3xl font-black tracking-tight">Watchlist</h1>
      <p class="mt-2 max-w-2xl text-paper/70">
        Symbols on watch and the explicit condition that would promote each into a recommendation. When the
        reason is a hypothesis, it's named — open the "why" for the full evidence.
      </p>
    </header>

    <RegimeBanner />

    <div v-if="loadError" class="rounded-2xl border border-rust/25 bg-rust/10 px-5 py-4 text-sm text-rust">
      Could not load the watchlist.
    </div>
    <div v-if="sourceWarnings.length" class="rounded-2xl border border-sun/30 bg-sun/10 px-5 py-4 text-sm text-ink/70">
      <span v-for="(w, i) in sourceWarnings" :key="i">{{ display(w.message) }}{{ i < sourceWarnings.length - 1 ? ' · ' : '' }}</span>
    </div>

    <div class="flex flex-wrap items-center gap-2">
      <button v-for="s in (['active','matched','expired','closed'] as const)" :key="s"
              class="rounded-full border px-4 py-1.5 text-sm font-semibold"
              :class="sectionFilter === s ? 'border-ink/40 bg-ink text-paper' : 'border-ink/15 bg-white/70 text-ink/55 hover:bg-white'"
              @click="sectionFilter = s">
        {{ s }}: {{ sectionCounts[s] }}
      </button>
      <button class="ml-auto rounded-full border border-ink/15 bg-white/70 px-4 py-1.5 text-sm font-semibold hover:bg-white" @click="refresh()">Refresh</button>
      <span v-if="pending" class="text-sm text-ink/40">loading…</span>
    </div>

    <div class="overflow-x-auto rounded-2xl border border-ink/10 bg-white/60">
      <table class="w-full text-sm">
        <thead class="text-left text-xs font-black uppercase tracking-wide text-ink/45">
          <tr class="border-b border-ink/10">
            <th class="px-4 py-3">Symbol</th>
            <th class="px-4 py-3">Why watching</th>
            <th class="px-4 py-3">What we're waiting for</th>
            <th class="px-4 py-3">Source</th>
            <th class="px-4 py-3">Expected</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, idx) in rows" :key="idx" class="border-b border-ink/5 hover:bg-white/80 align-top">
            <td class="px-4 py-3 font-black text-ink">
              <button class="underline-offset-2 hover:underline" @click="whySymbol = String(row.symbol)">{{ display(row.symbol) }}</button>
              <span v-if="row.is_playbook_signal || row.hypothesis_id"
                    class="ml-2 rounded-full border border-moss/25 bg-moss/10 px-2 py-0.5 text-[10px] font-bold uppercase text-moss">hypothesis</span>
            </td>
            <td class="px-4 py-3 text-ink/70">{{ whyWatching(row) }}</td>
            <td class="px-4 py-3 text-ink/70">{{ waitingFor(row) }}</td>
            <td class="px-4 py-3 text-ink/50">{{ sourceLabel(row) }}</td>
            <td class="px-4 py-3 font-semibold text-ink/60">{{ display(row.expected_action) }}</td>
          </tr>
          <tr v-if="!rows.length && !pending">
            <td colspan="5" class="px-4 py-8 text-center text-ink/40">No {{ sectionFilter }} watch signals.</td>
          </tr>
        </tbody>
      </table>
    </div>

    <DecisionWhy v-if="whySymbol" :symbol="whySymbol" @close="whySymbol = null" />
  </section>
</template>
