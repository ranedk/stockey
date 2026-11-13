<script setup lang="ts">
import type { PtStrategy } from '~/types/paperTracks'
// One price-only replica: its 2016+ backtest (the nested reference) and its live paper record,
// both from systrader GET /api/paper/{name}.
const props = defineProps<{ name: string, label: string }>()
const { data, error, status } = useSystrader<PtStrategy>(`/api/paper/${props.name}`, { key: `paradigm-${props.name}`, lazy: true })

const fwd = computed(() => data.value ?? null)
const ref_ = computed(() => data.value?.reference ?? null)
const books = PAPER_BOOK_DEFS.filter(b => b.key !== 'account').map(b => ({ key: b.key, label: b.label, color: b.color }))

const shownHoldings = computed(() => fwd.value?.holdings?.length ? fwd.value.holdings : ref_.value?.holdings ?? [])
const top = computed(() => [...shownHoldings.value].sort((a, b) => b.weight - a.weight).slice(0, 12))
const holdingsFrom = computed(() => (fwd.value?.holdings?.length ? 'live record' : 'backtest, today'))
const cash = computed(() => {
  if (!shownHoldings.value.length) return null
  const inv = shownHoldings.value.reduce((s, h) => s + h.weight, 0)
  return inv < 0.995 ? 1 - inv : null
})
const refCost = computed(() => paperBook(ref_.value, 'strategy')?.ann_cost)
</script>

<template>
  <div class="rounded-xl p-4 ring ring-default">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <NuxtLink :to="`/paper/${name}`" class="inline-flex items-center gap-1 font-semibold text-highlighted hover:text-primary">
        {{ label }} <UIcon name="i-lucide-arrow-up-right" class="size-3.5 text-dimmed" />
      </NuxtLink>
      <UBadge v-if="data?.spec?.rebalance" color="neutral" variant="subtle" size="sm" icon="i-lucide-calendar-sync" :label="data.spec.rebalance" />
    </div>

    <UAlert v-if="error" class="mt-3" color="error" variant="subtle" icon="i-lucide-plug-zap" title="Could not reach systrader" :description="error.message" />
    <USkeleton v-else-if="status === 'pending' && !data" class="mt-3 h-48" />
    <template v-else-if="data">
      <div class="mt-3 grid grid-cols-2 gap-2 lg:grid-cols-4">
        <div class="rounded-lg bg-elevated/60 p-3">
          <div class="text-[11px] text-muted">Backtest since {{ dateShort(ref_?.start) }}, per year</div>
          <Change :value="paperPerYear(ref_, 'strategy')" class="mt-1 text-lg" />
          <div class="text-[11px] text-dimmed">equal-weight: <Change :value="paperPerYear(ref_, 'equal-weight')" /></div>
        </div>
        <div class="rounded-lg bg-elevated/60 p-3">
          <div class="text-[11px] text-muted">Backtest worst drop</div>
          <div class="num mt-1 text-lg font-semibold" :class="signClass(paperBook(ref_, 'strategy')?.max_drawdown)">{{ pct(paperBook(ref_, 'strategy')?.max_drawdown, 1) }}</div>
          <div class="text-[11px] text-dimmed">equal-weight: <span class="num" :class="signClass(paperBook(ref_, 'equal-weight')?.max_drawdown)">{{ pct(paperBook(ref_, 'equal-weight')?.max_drawdown, 1) }}</span></div>
        </div>
        <div class="rounded-lg bg-elevated/60 p-3">
          <div class="text-[11px] text-muted">Live since {{ dateShort(fwd?.start) }}</div>
          <template v-if="fwd?.days">
            <Change :value="paperBook(fwd, 'strategy')?.total_return" class="mt-1 text-lg" />
            <div class="text-[11px] text-dimmed">equal-weight: <Change :value="paperBook(fwd, 'equal-weight')?.total_return" /> · {{ fwd.days }} days</div>
          </template>
          <div v-else class="mt-1 text-sm text-muted">not started</div>
        </div>
        <div class="rounded-lg bg-elevated/60 p-3">
          <div class="text-[11px] text-muted">Backtest costs per year</div>
          <div class="num mt-1 text-lg font-semibold text-highlighted">{{ pct(refCost, 1) }}</div>
          <div class="text-[11px] text-dimmed">of the book, from trading</div>
        </div>
      </div>

      <div v-if="ref_?.nav?.length" class="mt-3">
        <div class="text-[11px] font-medium text-muted">Backtest: what ₹100 grew to (log scale) · not evidence, the rules were written knowing this history</div>
        <PaperNavChart :points="ref_.nav" :books="books" mode="growth" height="220px" />
      </div>

      <div class="mt-3 text-[11px] text-muted">
        Biggest holdings ({{ holdingsFrom }})<template v-if="cash != null">, {{ pct(cash, 0) }} in cash</template>
      </div>
      <div class="mt-1.5 flex flex-wrap gap-1.5">
        <span v-for="h in top" :key="h.symbol" class="inline-flex items-center gap-1.5 rounded-md bg-elevated px-2 py-0.5 text-xs">
          <NuxtLink :to="`/company/${encodeURIComponent(h.symbol)}`" class="font-medium text-highlighted hover:text-primary">{{ h.symbol }}</NuxtLink>
          <span class="num text-muted">{{ pct(h.weight, 1) }}</span>
          <Change v-if="h.return != null" :value="h.return" :digits="0" />
        </span>
        <span v-if="!top.length" class="text-xs text-dimmed">none yet</span>
      </div>
    </template>
  </div>
</template>
