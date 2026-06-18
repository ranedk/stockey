<script setup lang="ts">
import type { Dict } from '~/types/api'

const api = useOperatorApi()
const symbol = ref('')
const limit = ref(100)
const actionBusy = ref<Record<string, boolean>>({})
const actionError = ref('')
const actionResult = ref<Dict | null>(null)

const { data, refresh, pending, error } = await useAsyncData(
  'operator-paper-recommendations',
  () => api.getOperatorPortfolioRecommendations({ limit: limit.value, symbol: symbol.value.trim().toUpperCase() }),
  { watch: [limit] }
)

const rows = computed(() => Array.isArray(data.value?.recommendations) ? data.value.recommendations as Dict[] : [])
const summary = computed(() => typeof data.value?.summary === 'object' && data.value.summary !== null ? data.value.summary as Dict : {})

function text(value: unknown) {
  return String(value || '-')
}

function money(value: unknown) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 }).format(num)
}

function pct(value: unknown) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return `${Math.round(num * 10) / 10}%`
}

function rowKey(row: Dict) {
  return String(row.recommendation_id || `${row.symbol}-${row.action_code}-${row.published_on}`)
}

function rowPrice(row: Dict) {
  return Number(row.current_price || row.reference_price || 0)
}

function actionText(row: Dict) {
  return String(row.operator_display_action || row.operator_recommended_action || row.action_code || '-')
}

function buttonAction(row: Dict) {
  return String(row.operator_recommended_action || '')
}

function buttonClass(row: Dict) {
  const action = buttonAction(row)
  if (action.startsWith('sell')) return 'bg-rust text-paper'
  if (action.includes('50')) return 'bg-sun text-ink'
  return 'bg-moss text-paper'
}

function buttonLabel(row: Dict) {
  const action = buttonAction(row)
  if (!action) return 'No action'
  return action.toUpperCase()
}

function buttonDisabled(row: Dict) {
  return Boolean(row.operator_action_disabled_reason || !buttonAction(row) || actionBusy.value[rowKey(row)])
}

async function applyAction(row: Dict) {
  const action = buttonAction(row)
  const key = rowKey(row)
  actionBusy.value[key] = true
  actionError.value = ''
  actionResult.value = null
  try {
    actionResult.value = await api.applyOperatorPortfolioAction({
      symbol: row.symbol,
      action,
      price: rowPrice(row),
      recommendation_id: row.recommendation_id,
      recommendation_action: row.action_code,
      source: row,
      operator_id: 'operator',
      note: `Operator ${action} from recommendations page`
    }) as Dict
    await refresh()
  } catch (err: unknown) {
    actionError.value = err instanceof Error ? err.message : String(err)
  } finally {
    actionBusy.value[key] = false
  }
}

async function refreshFiltered() {
  await refresh()
}
</script>

<template>
  <section class="space-y-6">
    <div class="rounded-[2rem] bg-gradient-to-br from-ink to-moss p-6 text-paper shadow-soft">
      <p class="text-xs font-black uppercase tracking-[0.3em] text-sun">Operator paper ledger</p>
      <h1 class="mt-3 text-4xl font-black">Recommendations</h1>
      <p class="mt-3 max-w-3xl text-sm leading-6 text-paper/75">
        Use these buttons to add/remove stocks from the operator paper portfolio. This does not change advisory recommendations, broker execution rows, or live orders.
      </p>
      <div class="mt-5 flex flex-wrap gap-3 text-sm font-bold">
        <span class="rounded-full bg-paper/15 px-4 py-2">Recommendations {{ summary.recommendation_count ?? rows.length }}</span>
        <span class="rounded-full bg-paper/15 px-4 py-2">Open positions {{ summary.open_position_count ?? 0 }}</span>
        <span class="rounded-full bg-sun px-4 py-2 text-ink">Paper only</span>
      </div>
    </div>

    <div class="flex flex-wrap items-end gap-3 rounded-3xl bg-white/80 p-4 shadow-soft">
      <label class="flex flex-col gap-1 text-sm font-bold text-ink/70">
        Symbol
        <input v-model="symbol" class="rounded-2xl border border-black/10 px-4 py-3 text-ink outline-none focus:border-moss" placeholder="AMIORG" @keyup.enter="refreshFiltered" />
      </label>
      <label class="flex flex-col gap-1 text-sm font-bold text-ink/70">
        Limit
        <input v-model.number="limit" class="w-28 rounded-2xl border border-black/10 px-4 py-3 text-ink outline-none focus:border-moss" type="number" min="1" max="500" />
      </label>
      <button class="rounded-2xl bg-ink px-5 py-3 text-sm font-black text-paper disabled:opacity-50" type="button" :disabled="pending" @click="refreshFiltered">
        Refresh
      </button>
      <NuxtLink class="rounded-2xl bg-moss px-5 py-3 text-sm font-black text-paper" to="/paper-portfolio">Open Paper Portfolio</NuxtLink>
    </div>

    <ApiErrorBanner v-if="error" title="Recommendations failed" :error="error" />
    <div v-if="actionError" class="rounded-3xl border border-rust/20 bg-rust/10 p-4 text-sm font-bold text-rust">{{ actionError }}</div>
    <div v-if="actionResult" class="rounded-3xl border border-moss/20 bg-moss/10 p-4 text-sm font-bold text-moss">Saved paper portfolio action.</div>

    <div class="grid gap-4">
      <article v-for="row in rows" :key="rowKey(row)" class="rounded-[1.75rem] bg-white/85 p-5 shadow-soft">
        <div class="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div class="flex flex-wrap items-center gap-2">
              <SymbolLink :symbol="text(row.symbol)" class="text-2xl font-black text-ink" />
              <span class="rounded-full bg-sun px-3 py-1 text-xs font-black text-ink">{{ actionText(row) }}</span>
              <span class="rounded-full bg-paper px-3 py-1 text-xs font-black text-ink/60">{{ text(row.operator_position_status) }}</span>
            </div>
            <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/65">{{ text(row.action_reason || row.action_detail) }}</p>
          </div>
          <div class="flex flex-col items-end gap-2">
            <button
              class="rounded-2xl px-5 py-3 text-sm font-black disabled:opacity-40"
              :class="buttonClass(row)"
              type="button"
              :disabled="buttonDisabled(row)"
              @click="applyAction(row)"
            >
              {{ buttonLabel(row) }}
            </button>
            <p v-if="row.operator_action_disabled_reason" class="max-w-56 text-right text-xs font-bold text-ink/45">
              {{ row.operator_action_disabled_reason }}
            </p>
          </div>
        </div>
        <div class="mt-4 flex gap-3 text-sm">
          <p class="rounded-2xl bg-paper/80 p-3"><span class="block text-xs font-black uppercase text-ink/40">Current</span>{{ money(row.current_price) }}</p>
          <p class="rounded-2xl bg-paper/80 p-3"><span class="block text-xs font-black uppercase text-ink/40">Reference</span>{{ money(row.reference_price) }}</p>
          <p class="rounded-2xl bg-paper/80 p-3"><span class="block text-xs font-black uppercase text-ink/40">Target</span>{{ money(row.recommended_target_price) }}</p>
          <p class="rounded-2xl bg-paper/80 p-3"><span class="block text-xs font-black uppercase text-ink/40">Stop</span>{{ money(row.recommended_stop_price || row.stop_price || row.invalidation_price) }}</p>
          <p class="rounded-2xl bg-paper/80 p-3"><span class="block text-xs font-black uppercase text-ink/40">Score</span>{{ pct(row.invest_score_pct) }}</p>
        </div>
      </article>
      <div v-if="!rows.length" class="rounded-[1.75rem] bg-white/80 p-8 text-center shadow-soft">
        <p class="text-lg font-black text-ink">No applicable paper action recommendations.</p>
        <p class="mx-auto mt-2 max-w-3xl text-sm leading-6 text-ink/60">
          The backend found {{ summary.raw_actionable_recommendation_count ?? 0 }} action-capable advisory rows, but hid
          {{ summary.hidden_not_applicable_count ?? 0 }} because they do not match the current paper portfolio state.
          After a reset, sell and partial-sell rows are hidden until a symbol is open in the paper ledger.
        </p>
      </div>
    </div>
  </section>
</template>
