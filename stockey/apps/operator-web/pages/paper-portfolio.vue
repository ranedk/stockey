<script setup lang="ts">
import type { Dict } from '~/types/api'

const api = useOperatorApi()
const resetBusy = ref(false)
const resetError = ref('')
const resetResult = ref<Dict | null>(null)

const { data, refresh, pending, error } = await useAsyncData('operator-paper-portfolio', () => api.getOperatorPaperPortfolio(), {
  lazy: true,
  server: false
})

const positions = computed(() => Array.isArray(data.value?.positions) ? data.value.positions as Dict[] : [])
const openPositions = computed(() => Array.isArray(data.value?.open_positions) ? data.value.open_positions as Dict[] : [])
const closedPositions = computed(() => Array.isArray(data.value?.closed_positions) ? data.value.closed_positions as Dict[] : [])
const summary = computed(() => typeof data.value?.summary === 'object' && data.value.summary !== null ? data.value.summary as Dict : {})

function money(value: unknown) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 }).format(num)
}

function pct(value: unknown) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  const sign = num > 0 ? '+' : ''
  return `${sign}${Math.round(num * 100) / 100}%`
}

function text(value: unknown) {
  return String(value || '-')
}

function pnlClass(value: unknown) {
  const num = Number(value)
  if (!Number.isFinite(num)) return 'text-ink/60'
  return num >= 0 ? 'text-moss' : 'text-rust'
}

async function resetPortfolio() {
  if (!window.confirm('Reset the operator paper portfolio? This clears only the paper ledger, not advisory output or broker execution.')) return
  resetBusy.value = true
  resetError.value = ''
  resetResult.value = null
  try {
    resetResult.value = await api.resetOperatorPaperPortfolio({ confirm: true }) as Dict
    await refresh()
  } catch (err: unknown) {
    resetError.value = err instanceof Error ? err.message : String(err)
  } finally {
    resetBusy.value = false
  }
}
</script>

<template>
  <section class="space-y-6">
    <div class="rounded-[2rem] bg-gradient-to-br from-moss to-ink p-6 text-paper shadow-soft">
      <p class="text-xs font-black uppercase tracking-[0.3em] text-sun">Paper portfolio</p>
      <h1 class="mt-3 text-4xl font-black">Operator Portfolio</h1>
      <p class="mt-3 max-w-3xl text-sm leading-6 text-paper/75">
        Simple paper state from recommendation-page buy/sell buttons. It shows only entry price, exit price, current price, and percentage gain/loss.
      </p>
      <div class="mt-5 flex flex-wrap gap-3 text-sm font-bold">
        <span class="rounded-full bg-paper/15 px-4 py-2">Open {{ summary.open_count ?? openPositions.length }}</span>
        <span class="rounded-full bg-paper/15 px-4 py-2">Closed {{ summary.closed_count ?? closedPositions.length }}</span>
        <span class="rounded-full bg-sun px-4 py-2 text-ink">No broker orders</span>
      </div>
    </div>

    <div class="flex flex-wrap gap-3 rounded-3xl bg-white/80 p-4 shadow-soft">
      <button class="rounded-2xl bg-ink px-5 py-3 text-sm font-black text-paper disabled:opacity-50" :disabled="pending" type="button" @click="refresh()">
        Refresh
      </button>
      <NuxtLink class="rounded-2xl bg-moss px-5 py-3 text-sm font-black text-paper" to="/recommendations">Open Recommendations</NuxtLink>
      <button class="rounded-2xl bg-rust px-5 py-3 text-sm font-black text-paper disabled:opacity-50" :disabled="resetBusy" type="button" @click="resetPortfolio">
        Reset Paper Portfolio
      </button>
    </div>

    <ApiErrorBanner v-if="error" title="Paper portfolio failed" :error="error" />
    <div v-if="resetError" class="rounded-3xl border border-rust/20 bg-rust/10 p-4 text-sm font-bold text-rust">{{ resetError }}</div>
    <div v-if="resetResult" class="rounded-3xl border border-moss/20 bg-moss/10 p-4 text-sm font-bold text-moss">
      Reset complete. Deleted {{ resetResult.deleted_rows ?? 0 }} paper-ledger rows.
    </div>

    <div class="grid gap-4">
      <article v-for="row in positions" :key="`${row.symbol}-${row.entry_at}-${row.exit_at || 'open'}`" class="rounded-[1.75rem] bg-white/85 p-5 shadow-soft">
        <div class="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div class="flex flex-wrap items-center gap-2">
              <SymbolLink :symbol="text(row.symbol)" class="text-2xl font-black text-ink" />
              <span class="rounded-full px-3 py-1 text-xs font-black" :class="row.status === 'open' ? 'bg-moss/15 text-moss' : 'bg-paper text-ink/60'">{{ text(row.status) }}</span>
            </div>
            <p class="mt-2 text-sm text-ink/55">Entry {{ text(row.entry_at) }} · Exit {{ text(row.exit_at) }}</p>
          </div>
          <p class="text-3xl font-black" :class="pnlClass(row.pnl_pct)">{{ pct(row.pnl_pct) }}</p>
        </div>
        <div class="mt-4 grid gap-3 text-sm sm:grid-cols-4">
          <p class="rounded-2xl bg-paper/80 p-3"><span class="block text-xs font-black uppercase text-ink/40">Entry price</span>{{ money(row.entry_price) }}</p>
          <p class="rounded-2xl bg-paper/80 p-3"><span class="block text-xs font-black uppercase text-ink/40">Exit price</span>{{ money(row.exit_price) }}</p>
          <p class="rounded-2xl bg-paper/80 p-3"><span class="block text-xs font-black uppercase text-ink/40">Current price</span>{{ money(row.current_price) }}</p>
          <p class="rounded-2xl bg-paper/80 p-3"><span class="block text-xs font-black uppercase text-ink/40">Price source</span>{{ text(row.current_price_source) }}</p>
        </div>
      </article>
      <div v-if="!positions.length" class="rounded-[1.75rem] bg-white/80 p-8 text-center text-sm font-bold text-ink/55 shadow-soft">
        No paper positions yet. Open Recommendations and click Buy to start.
      </div>
    </div>
  </section>
</template>
