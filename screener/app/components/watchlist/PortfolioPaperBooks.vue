<script setup lang="ts">
import type { PaperBookPosition, PaperBooksResponse } from '~/types/watchlist'

// Paper books replayed beside the frozen v3 book (stockey portfolio_replay.py; LEDGER row 59).
// Record-only; judged from April 2027 at the earliest.
const { data, error, status } = useStockey<PaperBooksResponse>('/api/paper-books')
const SLOT: Record<string, string> = { v3_replay: 'var(--series-1)', uncapped: 'var(--series-7)', v4: 'var(--series-2)' }
const selected = ref('uncapped')
const current = computed(() => data.value?.books.find(b => b.book === selected.value))
const ret = (p: PaperBookPosition) => wlChange(p.entry_price, p.status === 'closed' ? p.exit_price : p.last_price)

const { t, base, axis, tooltip, resolve } = useChartTheme()
// Profit in ₹ lakh per evening: the books deploy different amounts, so rupees are the fair common scale.
const option = computed(() => {
  const books = data.value?.books ?? []
  const dates = [...new Set(books.flatMap(b => b.series.map(r => r.as_of.slice(0, 10))))].sort()
  return {
    ...base.value,
    grid: { left: 4, right: 12, top: 12, bottom: 4, containLabel: true },
    tooltip: { ...tooltip.value, trigger: 'axis', valueFormatter: (v: number) => (isNum(v) ? `₹${v.toFixed(2)} L` : '—') },
    xAxis: { type: 'category', data: dates, boundaryGap: false, ...axis.value, splitLine: { show: false }, axisLabel: { ...axis.value.axisLabel, formatter: (d: string) => new Date(d).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }) } },
    yAxis: { type: 'value', ...axis.value, axisLine: { show: false }, splitNumber: 4, axisLabel: { ...axis.value.axisLabel, formatter: (v: number) => `₹${v} L` } },
    series: books.map((b) => {
      const m = new Map(b.series.map(r => [r.as_of.slice(0, 10), (r.realized_rs + r.unrealized_rs) / 1e5]))
      return { name: b.label, type: 'line', showSymbol: false, lineStyle: { width: b.book === selected.value ? 2.5 : 2 }, color: resolve(SLOT[b.book] || 'var(--series-3)'),
        data: dates.map(d => m.get(d) ?? null), connectNulls: true,
        markLine: b === books[0] ? { silent: true, symbol: 'none', label: { show: false }, lineStyle: { color: t.value['chart-axis'], type: 'dashed', width: 1 }, data: [{ yAxis: 0 }] } : undefined }
    }),
  }
})
</script>

<template>
  <UCard>
    <template #header>
      <div class="flex flex-wrap items-center gap-2">
        <UIcon name="i-lucide-notebook-pen" class="size-4 text-primary" />
        <h3 class="font-semibold text-highlighted">Paper books</h3>
        <UBadge color="neutral" variant="subtle" label="Record-only" />
        <span v-if="data?.since" class="text-xs text-dimmed">replayed {{ dateShort(data.since) }} → {{ dateShort(data.as_of) }}, updated nightly</span>
      </div>
      <p class="mt-1 text-xs text-muted">
        The same buy and sell rules as v3, replayed on what was known each evening. They answer two questions: what does the
        25-name cap leave out (<strong>Uncapped</strong>), and do risk-based weights plus a weak-market brake do better
        (<strong>v4</strong>)? Judged from April 2027 at the earliest; a few weeks of these numbers are noise.
      </p>
    </template>

    <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" title="Could not load the paper books" :description="error.message" />
    <USkeleton v-else-if="!data && status === 'pending'" class="h-64" />
    <UEmpty v-else-if="!data?.books.length" icon="i-lucide-notebook" title="No replay has run yet" class="py-8" />

    <div v-else class="space-y-4">
      <div class="grid gap-3 sm:grid-cols-3">
        <button v-for="b in data.books" :key="b.book" class="rounded-lg border p-3 text-left transition"
          :class="selected === b.book ? 'border-primary bg-primary/5 ring-1 ring-primary/30' : 'border-default hover:border-accented'" @click="selected = b.book">
          <div class="flex items-center gap-2">
            <span class="size-2.5 rounded-full" :style="{ background: SLOT[b.book] }" />
            <span class="text-sm font-semibold text-highlighted">{{ b.label }}</span>
          </div>
          <div class="mt-2 flex items-end gap-5">
            <div>
              <div class="text-[11px] text-muted">Profit / loss</div>
              <div class="num text-lg font-semibold" :class="signClass(b.pnl_rs)">{{ arrow(b.pnl_rs) }} {{ wlLakh(b.pnl_rs) }}</div>
            </div>
            <div>
              <div class="text-[11px] text-muted">on money invested</div>
              <Change :value="b.return_on_deployed" :digits="1" />
            </div>
          </div>
          <div class="mt-1 text-[11px] text-muted">{{ b.open_positions }} open · {{ b.entered }} bought · {{ b.closed }} sold</div>
          <p class="mt-1 text-[11px] leading-snug text-dimmed">{{ b.description }}</p>
        </button>
      </div>

      <div>
        <div class="mb-1 flex flex-wrap items-center justify-between gap-2">
          <span class="text-xs font-medium text-muted">Running profit, ₹ lakh</span>
          <div class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
            <span v-for="b in data.books" :key="b.book" class="inline-flex items-center gap-1.5"><span class="h-0.5 w-3 rounded" :style="{ background: SLOT[b.book] }" />{{ b.label }}</span>
          </div>
        </div>
        <EChart :option="option" height="220px" />
      </div>

      <div v-if="current">
        <h4 class="mb-2 text-sm font-medium text-highlighted">{{ current.label }}: positions</h4>
        <p v-if="!current.positions.length" class="rounded-lg bg-elevated/50 p-3 text-xs text-muted">
          No positions.<template v-if="current.book === 'v4'"> The Nifty 50 has been below its 50-session average, so v4's brake has blocked new buys.</template>
        </p>
        <div v-else class="max-h-[420px] overflow-auto rounded-lg border border-default">
          <table class="w-full min-w-[640px] text-sm">
            <thead class="sticky top-0 z-10 bg-elevated text-left text-xs text-muted">
              <tr>
                <th class="px-3 py-2 font-medium">Stock</th><th class="px-3 py-2 font-medium">Bought</th>
                <th class="px-3 py-2 text-right font-medium">Entry</th><th class="px-3 py-2 text-right font-medium">Size</th>
                <th class="px-3 py-2 text-right font-medium"><UTooltip text="Story score when bought"><span class="underline decoration-dotted underline-offset-2">Score</span></UTooltip></th>
                <th class="px-3 py-2 text-right font-medium">Return</th><th class="px-3 py-2 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="p in current.positions" :key="p.ticker + p.opened_on" class="border-t border-default/60">
                <td class="px-3 py-1.5 font-mono font-medium text-highlighted">{{ p.ticker }}</td>
                <td class="num px-3 py-1.5 text-xs text-muted">{{ dateShort(p.opened_on) }}</td>
                <td class="num px-3 py-1.5 text-right">{{ inr(p.entry_price) }}</td>
                <td class="num px-3 py-1.5 text-right text-muted">{{ wlLakh(p.size_rs) }}</td>
                <td class="num px-3 py-1.5 text-right text-muted">{{ isNum(p.story_score_at_entry) ? p.story_score_at_entry.toFixed(0) : '—' }}</td>
                <td class="px-3 py-1.5 text-right"><Change :value="ret(p)" /></td>
                <td class="px-3 py-1.5 text-xs">
                  <UBadge v-if="p.status === 'open'" color="success" variant="soft" size="xs" label="open" />
                  <span v-else class="text-muted">sold {{ dateShort(p.closed_on) }}<template v-if="p.exit_reason"> · {{ wlCloseReason(p.exit_reason) }}</template></span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </UCard>
</template>
