<script setup lang="ts">
import type { PaperBooksResponse, PaperBookPosition } from '~/types/api'
import type { PaperNavPoint } from '~/types/systrader'

// Three paper books replayed beside the frozen v3 book from 2026-10-02 (stockey
// fundamentals/screens/portfolio_replay.py; pre-registered as LEDGER row 59). Record-only.
// Judged from 2027-04 at the earliest -- weeks of these numbers are noise, and the page says so.
const api = useApi()
const { data, error, status } = await useAsyncData('paper-books', () => api.get<PaperBooksResponse>('/api/paper-books'))

const COLOURS: Record<string, string> = { v3_replay: '#0f766e', uncapped: '#7c3aed', v4: '#ea580c' }
const selected = ref<string>('uncapped')

const chartBooks = computed(() => (data.value?.books ?? []).map(b => ({ key: b.book, label: b.label, colour: COLOURS[b.book] ?? '#64748b' })))

// P&L in Rs lakh per evening: the books deploy different amounts, so rupees -- not a return on
// an arbitrary base -- is the honest common axis.
const chartPoints = computed<PaperNavPoint[]>(() => {
  const byDate = new Map<string, Record<string, number>>()
  for (const b of data.value?.books ?? []) {
    for (const r of b.series) {
      const d = r.as_of.slice(0, 10)
      const row = byDate.get(d) ?? {}
      row[b.book] = (r.realized_rs + r.unrealized_rs) / 1e5
      byDate.set(d, row)
    }
  }
  return [...byDate.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([date, values]) => ({ date, values, rebalanced: false }))
})

const current = computed(() => data.value?.books.find(b => b.book === selected.value))
const ret = (p: PaperBookPosition) => {
  const exit = p.status === 'closed' ? p.exit_price : p.last_price
  return p.entry_price && exit ? exit / p.entry_price - 1 : null
}
const lakh = (rs: number | null | undefined) => rs === null || rs === undefined ? '—' : `₹${(rs / 1e5).toFixed(2)} L`
</script>

<template>
  <section class="mt-6 rounded-lg border border-slate-200 bg-white p-4">
    <div class="flex flex-wrap items-center gap-3">
      <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Paper books</h2>
      <BadgePill label="record-only" tone="neutral" />
      <span v-if="data?.since" class="text-xs text-slate-400">replayed {{ data.since.slice(0, 10) }} → {{ data.as_of?.slice(0, 10) }}, updated nightly</span>
    </div>
    <p class="mt-1 text-xs text-slate-400">
      The same entry and exit rules as v3, replayed on what was known each evening. They answer two
      questions: what does the 25-name cap leave out (<strong>Uncapped</strong>), and do risk-only
      weights plus a weak-market brake do better (<strong>v4</strong>)? Judged from April 2027 at the
      earliest — a few weeks of these numbers are noise.
    </p>

    <div v-if="error" class="mt-3 rounded border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
      Could not load the paper books ({{ error.message }}).
    </div>
    <div v-else-if="status === 'pending'" class="mt-3 text-sm text-slate-500">Loading…</div>

    <template v-else-if="data && data.books.length">
      <div class="mt-3 grid gap-3 sm:grid-cols-3">
        <button
          v-for="b in data.books" :key="b.book"
          class="rounded-lg border p-3 text-left transition"
          :class="selected === b.book ? 'border-slate-900 bg-slate-50' : 'border-slate-200 hover:border-slate-400'"
          @click="selected = b.book"
        >
          <div class="flex items-center gap-2">
            <span class="inline-block h-2.5 w-2.5 rounded-full" :style="{ background: COLOURS[b.book] }" />
            <span class="text-sm font-semibold text-slate-800">{{ b.label }}</span>
          </div>
          <div class="mt-2 flex items-end gap-4">
            <div>
              <div class="text-[11px] text-slate-500">P&amp;L</div>
              <div class="text-lg font-semibold tabular-nums" :class="b.pnl_rs >= 0 ? 'text-emerald-600' : 'text-rose-600'">{{ lakh(b.pnl_rs) }}</div>
            </div>
            <div>
              <div class="text-[11px] text-slate-500">on money invested</div>
              <ReturnValue :value="b.return_on_deployed" :digits="1" />
            </div>
          </div>
          <div class="mt-1 text-[11px] text-slate-500">{{ b.open_positions }} open · {{ b.entered }} bought · {{ b.closed }} closed</div>
          <p class="mt-1 text-[11px] leading-snug text-slate-400">{{ b.description }}</p>
        </button>
      </div>

      <div class="mt-3 rounded-lg border border-slate-200 p-3">
        <div class="mb-1 text-[11px] text-slate-500">Cumulative P&amp;L, ₹ lakh</div>
        <NavChart :points="chartPoints" :books="chartBooks" :height="180" />
      </div>

      <div v-if="current" class="mt-3">
        <h3 class="mb-1 text-xs font-medium text-slate-600">{{ current.label }} — positions</h3>
        <p v-if="!current.positions.length" class="text-xs text-slate-500">
          No positions.<template v-if="current.book === 'v4'"> The Nifty 50 has been below its 50-session average, so v4's brake has blocked new entries.</template>
        </p>
        <table v-else class="w-full text-xs">
          <thead class="text-left text-[11px] uppercase tracking-wide text-slate-500">
            <tr><th class="py-1">Stock</th><th>Bought</th><th class="text-right">Entry</th><th class="text-right">Size</th><th class="text-right">Score</th><th class="text-right">Return</th><th>Status</th></tr>
          </thead>
          <tbody>
            <tr v-for="p in current.positions" :key="p.ticker + p.opened_on" class="border-t border-slate-100">
              <td class="py-1 font-medium text-slate-800">{{ p.ticker }}</td>
              <td class="text-slate-500">{{ p.opened_on.slice(0, 10) }}</td>
              <td class="text-right tabular-nums">{{ p.entry_price?.toFixed(2) ?? '—' }}</td>
              <td class="text-right tabular-nums">{{ lakh(p.size_rs) }}</td>
              <td class="text-right tabular-nums">{{ p.story_score_at_entry?.toFixed(0) ?? '—' }}</td>
              <td class="text-right"><ReturnValue :value="ret(p)" :digits="1" /></td>
              <td class="text-slate-500">{{ p.status === 'open' ? 'open' : `${p.exit_reason} ${p.closed_on?.slice(0, 10)}` }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
    <p v-else-if="data" class="mt-3 text-xs text-slate-500">No replay has run yet.</p>
  </section>
</template>
