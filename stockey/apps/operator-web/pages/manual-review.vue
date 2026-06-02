<script setup lang="ts">
import type { Dict } from '~/types/api'

const api = useOperatorApi()
const { data, refresh, pending, error: loadError } = await useAsyncData('manual-review', () => api.getManualReview(150))

const selectedType = ref('all')
const selectedSeverity = ref('all')
const decisionByItem = ref<Record<string, string>>({})
const rationaleByItem = ref<Record<string, string>>({})
const followUpByItem = ref<Record<string, string>>({})
const operatorId = ref('operator')
const savingItemId = ref('')
const saveError = ref('')
const saveSuccess = ref('')
const decisionOptions = [
  { key: 'needs_more_data', label: 'Needs more data', closes: false },
  { key: 'watch_for_event', label: 'Watch for event', closes: false },
  { key: 'add_operator_note', label: 'Add note', closes: false },
  { key: 'approve_for_manual_config', label: 'Approve manual config', closes: true },
  { key: 'ignore', label: 'Ignore', closes: true },
  { key: 'downgrade_to_no_action', label: 'Downgrade to no action', closes: true },
  { key: 'mark_fixed', label: 'Mark fixed', closes: true }
]

const items = computed(() => asList(data.value?.items))
const summary = computed(() => asDict(data.value?.summary))
const byType = computed(() => asDict(summary.value.by_type))
const bySeverity = computed(() => asDict(summary.value.by_severity))
const skippedSources = computed(() => asList(summary.value.skipped_sources))
const typeFilters = computed(() => [
  { key: 'all', label: 'All', count: items.value.length },
  ...Object.entries(byType.value).map(([key, count]) => ({ key, label: typeLabel(key), count: Number(count || 0) }))
])
const severityFilters = computed(() => [
  { key: 'all', label: 'All', count: items.value.length },
  ...Object.entries(bySeverity.value).map(([key, count]) => ({ key, label: String(key).toUpperCase(), count: Number(count || 0) }))
])
const filteredItems = computed(() => items.value.filter((item) => {
  const typeOk = selectedType.value === 'all' || String(item.item_type || '') === selectedType.value
  const severityOk = selectedSeverity.value === 'all' || String(item.severity || '') === selectedSeverity.value
  return typeOk && severityOk
}))

function asDict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Dict : {}
}

function asList(value: unknown): Dict[] {
  return Array.isArray(value) ? value.filter((item) => typeof item === 'object' && item !== null) as Dict[] : []
}

function display(value: unknown) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  if (typeof value === 'number') return Intl.NumberFormat('en-IN', { maximumFractionDigits: 3 }).format(value)
  return String(value)
}

function typeLabel(value: unknown) {
  const text = String(value || 'unknown').replaceAll('_', ' ')
  return text.replace(/\b\w/g, (char) => char.toUpperCase())
}

function statusClass(value: unknown) {
  const status = String(value || '').toLowerCase()
  if (status === 'error' || status === 'failed' || status.includes('blocked')) return 'bg-rust text-paper'
  if (status === 'warning' || status === 'review' || status.includes('review')) return 'bg-sun text-ink'
  return 'bg-moss text-paper'
}

function itemAccent(value: unknown) {
  const severity = String(value || '').toLowerCase()
  if (severity === 'error') return 'border-rust/60 bg-rust/5'
  if (severity === 'warning') return 'border-sun/80 bg-sun/10'
  return 'border-moss/40 bg-white/75'
}

function tracePath(item: Dict) {
  if (item.unique_id) return `/decision-trace?unique_id=${encodeURIComponent(String(item.unique_id))}`
  if (item.symbol) return `/decision-trace?symbol=${encodeURIComponent(String(item.symbol))}`
  return ''
}

function itemId(item: Dict) {
  return String(item.item_id || '')
}

function latestDecision(item: Dict): Dict {
  return asDict(item.latest_operator_decision)
}

function selectedDecisionMeta(item: Dict) {
  const selected = decisionByItem.value[itemId(item)] || 'needs_more_data'
  return decisionOptions.find((option) => option.key === selected) || decisionOptions[0]
}

async function submitDecision(item: Dict) {
  const id = itemId(item)
  if (!id) return
  saveError.value = ''
  saveSuccess.value = ''
  const decision = decisionByItem.value[id] || 'needs_more_data'
  const rationale = (rationaleByItem.value[id] || '').trim()
  if (decision !== 'add_operator_note' && !rationale) {
    saveError.value = 'Rationale is required before recording this decision.'
    return
  }
  savingItemId.value = id
  try {
    const result = await api.decideManualReview({
      item_id: id,
      item,
      decision,
      rationale,
      follow_up_event: followUpByItem.value[id] || '',
      operator_id: operatorId.value || 'operator'
    })
    saveSuccess.value = `${result.decision} recorded for ${id}.`
    decisionByItem.value[id] = 'needs_more_data'
    rationaleByItem.value[id] = ''
    followUpByItem.value[id] = ''
    await refresh()
  } catch (err) {
    saveError.value = err instanceof Error ? err.message : String(err)
  } finally {
    savingItemId.value = ''
  }
}
</script>

<template>
  <section class="rounded-[2rem] bg-ink p-8 text-paper shadow-soft">
    <p class="text-sm font-semibold uppercase tracking-[0.35em] text-paper/55">Manual Review</p>
    <div class="mt-4 flex flex-wrap items-end justify-between gap-5">
      <div>
        <h1 class="max-w-4xl text-5xl font-black tracking-tight">One queue for decisions that need human judgment.</h1>
        <p class="mt-4 max-w-3xl text-lg leading-8 text-paper/70">
          Consolidates manual action recommendations, event-policy reviews, conflicts, execution blockers, promotion reviews, and extraction failures.
        </p>
      </div>
      <button class="rounded-full bg-paper px-5 py-3 text-sm font-black text-ink disabled:opacity-50" :disabled="pending" type="button" @click="refresh()">
        {{ pending ? 'Refreshing...' : 'Refresh queue' }}
      </button>
    </div>
  </section>

  <section v-if="loadError" class="mt-6">
    <ApiErrorBanner title="Manual review queue failed" :error="loadError" />
  </section>

  <section class="mt-6 grid gap-4 md:grid-cols-5">
    <MetricTile label="Open Items" :value="String(summary.total_items || 0)" :note="`${summary.untrimmed_items || 0} before limit`" />
    <MetricTile label="Errors" :value="String(bySeverity.error || 0)" note="Execution or processing blockers" />
    <MetricTile label="Warnings" :value="String(bySeverity.warning || 0)" note="Conflicts and extraction issues" />
    <MetricTile label="Reviews" :value="String(bySeverity.review || 0)" note="Manual decisions needed" />
    <MetricTile label="Operator Closed" :value="String(summary.closed_by_operator || 0)" :note="`${summary.annotated_by_operator || 0} annotated`" />
  </section>

  <section class="mt-8 glass-panel rounded-3xl p-5">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Filters</p>
        <h2 class="mt-2 text-2xl font-black">Review queue</h2>
      </div>
      <div class="grid gap-2">
        <p class="text-sm font-semibold text-ink/55">Generated {{ data?.generated_at || '-' }}</p>
        <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
          Operator id
          <input v-model="operatorId" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="operator" />
        </label>
      </div>
    </div>
    <p v-if="saveError" class="mt-4 rounded-2xl bg-rust/10 p-3 text-sm font-bold text-rust">{{ saveError }}</p>
    <p v-if="saveSuccess" class="mt-4 rounded-2xl bg-moss/10 p-3 text-sm font-bold text-moss">{{ saveSuccess }}</p>
    <div class="mt-5 flex flex-wrap gap-2">
      <button
        v-for="filter in typeFilters"
        :key="filter.key"
        class="rounded-full px-4 py-2 text-sm font-black transition"
        :class="selectedType === filter.key ? 'bg-ink text-paper' : 'bg-white/80 text-ink/60 hover:bg-white'"
        type="button"
        @click="selectedType = filter.key"
      >
        {{ filter.label }} · {{ filter.count }}
      </button>
    </div>
    <div class="mt-3 flex flex-wrap gap-2">
      <button
        v-for="filter in severityFilters"
        :key="filter.key"
        class="rounded-full px-4 py-2 text-sm font-black transition"
        :class="selectedSeverity === filter.key ? 'bg-moss text-paper' : 'bg-white/80 text-ink/60 hover:bg-white'"
        type="button"
        @click="selectedSeverity = filter.key"
      >
        {{ filter.label }} · {{ filter.count }}
      </button>
    </div>

    <div class="mt-6 space-y-4">
      <article v-for="item in filteredItems" :key="String(item.item_id)" class="rounded-3xl border p-5" :class="itemAccent(item.severity)">
        <div class="flex flex-wrap items-start justify-between gap-4">
          <div class="max-w-4xl">
            <div class="flex flex-wrap gap-2">
              <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(item.severity)">{{ String(item.severity || 'review').toUpperCase() }}</span>
              <span class="rounded-full bg-ink/10 px-3 py-1 text-xs font-black text-ink">{{ typeLabel(item.item_type) }}</span>
              <span class="rounded-full bg-white/80 px-3 py-1 text-xs font-black text-ink/65">{{ display(item.status) }}</span>
            </div>
            <h3 class="mt-3 text-2xl font-black text-ink">{{ item.title || 'Manual review item' }}</h3>
            <p class="mt-2 text-sm leading-6 text-ink/65">{{ item.reason || 'No reason text was provided by the source row.' }}</p>
          </div>
          <NuxtLink v-if="item.symbol" class="rounded-full bg-white px-4 py-2 text-sm font-black text-ink" :to="`/symbols/${encodeURIComponent(String(item.symbol).toUpperCase())}`">
            Open symbol
          </NuxtLink>
          <NuxtLink v-if="tracePath(item)" class="rounded-full bg-ink px-4 py-2 text-sm font-black text-paper" :to="tracePath(item)">
            Open trace
          </NuxtLink>
        </div>
        <div class="mt-4 grid gap-2 text-sm md:grid-cols-4">
          <p class="rounded-xl bg-white/75 px-3 py-2"><b>Symbol:</b> <SymbolLink :symbol="item.symbol" subtle /></p>
          <p class="rounded-xl bg-white/75 px-3 py-2"><b>Setup:</b> {{ display(item.setup_id) }}</p>
          <p class="rounded-xl bg-white/75 px-3 py-2"><b>As of:</b> {{ display(item.asof_date) }}</p>
          <p class="rounded-xl bg-white/75 px-3 py-2"><b>Updated:</b> {{ display(item.updated_at) }}</p>
        </div>
        <p class="mt-3 break-all text-xs font-semibold text-ink/45">{{ item.source_table }} · {{ item.source_key }}</p>
        <div v-if="latestDecision(item).decision" class="mt-4 rounded-2xl bg-ink/5 p-4">
          <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/40">Latest operator annotation</p>
          <p class="mt-2 text-sm font-bold text-ink">{{ latestDecision(item).decision }} · {{ display(latestDecision(item).operator_id) }} · {{ display(latestDecision(item).decided_at) }}</p>
          <p class="mt-1 text-sm leading-6 text-ink/60">{{ latestDecision(item).rationale || 'No rationale recorded.' }}</p>
          <p v-if="latestDecision(item).follow_up_event" class="mt-1 text-sm text-ink/55"><b>Wait for:</b> {{ latestDecision(item).follow_up_event }}</p>
        </div>
        <div class="mt-4 rounded-2xl bg-white/70 p-4">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/40">Record operator decision</p>
              <p class="mt-1 text-sm text-ink/60">
                Closing decisions remove the item from the active queue. Notes and watch decisions keep it visible.
              </p>
            </div>
            <span class="rounded-full px-3 py-1 text-xs font-black" :class="selectedDecisionMeta(item).closes ? 'bg-rust text-paper' : 'bg-sun text-ink'">
              {{ selectedDecisionMeta(item).closes ? 'CLOSES ITEM' : 'ANNOTATES ITEM' }}
            </span>
          </div>
          <div class="mt-4 grid gap-3 lg:grid-cols-[0.8fr_1.2fr_1fr_auto]">
            <label class="grid gap-2 text-sm font-bold text-ink/70">
              Decision
              <select v-model="decisionByItem[itemId(item)]" class="rounded-2xl border border-black/10 bg-white px-4 py-3 text-ink outline-none focus:border-moss">
                <option v-for="option in decisionOptions" :key="option.key" :value="option.key">{{ option.label }}</option>
              </select>
            </label>
            <label class="grid gap-2 text-sm font-bold text-ink/70">
              Rationale
              <input v-model="rationaleByItem[itemId(item)]" class="rounded-2xl border border-black/10 bg-white px-4 py-3 text-ink outline-none focus:border-moss" placeholder="Why this operator decision is safe" />
            </label>
            <label class="grid gap-2 text-sm font-bold text-ink/70">
              Event to wait for
              <input v-model="followUpByItem[itemId(item)]" class="rounded-2xl border border-black/10 bg-white px-4 py-3 text-ink outline-none focus:border-moss" placeholder="Optional future evidence" />
            </label>
            <button class="self-end rounded-full bg-moss px-5 py-3 text-sm font-black text-paper disabled:opacity-50" :disabled="savingItemId === itemId(item)" type="button" @click="submitDecision(item)">
              {{ savingItemId === itemId(item) ? 'Saving...' : 'Save' }}
            </button>
          </div>
        </div>
        <details class="mt-3">
          <summary class="cursor-pointer text-sm font-black text-moss">Show source row</summary>
          <pre class="mt-3 max-h-80 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ JSON.stringify(item.raw || {}, null, 2) }}</pre>
        </details>
      </article>
      <p v-if="!filteredItems.length" class="rounded-2xl bg-white/75 p-4 text-sm text-ink/60">No manual review items match the current filters.</p>
    </div>
  </section>

  <section v-if="skippedSources.length" class="mt-8 glass-panel rounded-3xl p-5">
    <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Skipped Sources</p>
    <h2 class="mt-2 text-2xl font-black">Tables or queries not included</h2>
    <p class="mt-2 max-w-3xl text-sm leading-6 text-ink/60">
      These are visible so missing optional data sources do not silently hide review work.
    </p>
    <div class="mt-4 grid gap-3 md:grid-cols-2">
      <article v-for="source in skippedSources" :key="`${source.source}:${source.error}`" class="rounded-2xl bg-white/75 p-4">
        <p class="font-black text-ink">{{ source.source }}</p>
        <p class="mt-1 text-sm text-ink/60">{{ source.error }}</p>
      </article>
    </div>
  </section>
</template>
