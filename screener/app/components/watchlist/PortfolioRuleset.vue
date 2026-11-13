<script setup lang="ts">
import type { RulesetPortfolioResponse } from '~/types/watchlist'

// docs/PORTFOLIO_RULESET_PRD.md: fixed rules pick candidates, an AI reviewer may only REJECT.
// Split on entry_decision, not kind: in record-only mode every row is kind='shadow'.
const { data, status, error } = useStockey<RulesetPortfolioResponse>('/api/ruleset-portfolio')
const showRejected = ref(true)
const view = ref<'open' | 'closed' | 'all'>('open')

const accepted = computed(() => (data.value?.positions ?? []).filter(p => p.entry_decision !== 'reject'))
const rejected = computed(() => (data.value?.positions ?? []).filter(p => p.entry_decision === 'reject'))
const byView = <T extends { status: string }>(rows: T[]) => (view.value === 'all' ? rows : rows.filter(r => r.status === view.value))

/** Open accepted book: weighted by position size where sized, equal-weighted otherwise. Unpriced rows are counted, not zeroed. */
const book = computed(() => {
  let wsum = 0, w = 0, up = 0, down = 0, unpriced = 0
  for (const p of accepted.value) {
    if (p.status !== 'open') continue
    const r = wlChange(p.entry_price, p.last_price)
    if (!isNum(r)) { unpriced++; continue }
    const wt = isNum(p.position_size_rs) && p.position_size_rs > 0 ? p.position_size_rs : 1
    wsum += r * wt; w += wt
    r >= 0 ? up++ : down++
  }
  return { ret: w > 0 ? wsum / w : null, up, down, unpriced, sized: accepted.value.some(p => isNum(p.position_size_rs) && p.position_size_rs > 0) }
})
const full = computed(() => !!data.value && data.value.book_used >= data.value.book_capacity)
</script>

<template>
  <UCard>
    <template #header>
      <div class="flex flex-wrap items-center gap-2">
        <UIcon name="i-lucide-briefcase" class="size-4 text-primary" />
        <h3 class="font-semibold text-highlighted">Rule-based portfolio</h3>
        <template v-if="data">
          <UTooltip :text="data.mode === 'live' ? 'Real money is invested' : 'No real money: decisions are recorded to test the machinery first'">
            <UBadge :color="data.mode === 'live' ? 'success' : 'neutral'" variant="subtle" :icon="data.mode === 'live' ? 'i-lucide-radio' : 'i-lucide-notebook-pen'" :label="data.mode === 'live' ? 'Live' : 'Record-only'" />
          </UTooltip>
          <UBadge color="neutral" variant="outline" :label="`Rules v${data.ruleset_version}`" />
        </template>
      </div>
      <p class="mt-1 text-xs text-muted">
        A fixed rule picks candidates; an AI reviewer may only <strong>reject</strong> one, never add one. Rejected names are still
        tracked as if bought, so the veto can be scored: if they do better than the accepted ones, the AI layer is costing money.
      </p>
    </template>

    <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" title="Could not load the rule-based portfolio" :description="error.message" />
    <USkeleton v-else-if="!data && status === 'pending'" class="h-64" />

    <div v-else-if="data" class="space-y-4">
      <div class="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <div class="rounded-lg bg-elevated/60 p-3">
          <div class="text-xs text-muted">Open positions, since bought</div>
          <div class="mt-1 text-2xl"><Change :value="book.ret" :digits="1" /></div>
          <div class="text-[11px] text-dimmed">{{ book.sized ? 'weighted by position size' : 'equal-weighted' }}</div>
        </div>
        <div class="rounded-lg bg-elevated/60 p-3">
          <div class="text-xs text-muted">Positions up / down</div>
          <div class="num mt-1 text-2xl font-semibold"><span class="text-up">▲ {{ book.up }}</span><span class="text-dimmed"> / </span><span class="text-down">▼ {{ book.down }}</span></div>
          <div v-if="book.unpriced" class="text-[11px] text-dimmed">{{ book.unpriced }} not priced yet</div>
        </div>
        <div class="rounded-lg bg-elevated/60 p-3">
          <div class="text-xs text-muted">Slots used (v3)</div>
          <div class="num mt-1 text-2xl font-semibold text-highlighted">{{ data.book_used }}<span class="text-base text-dimmed"> / {{ data.book_capacity }}</span></div>
          <UProgress :model-value="data.book_used / data.book_capacity * 100" size="xs" class="mt-1.5" :color="full ? 'warning' : 'primary'" />
          <div class="mt-1 text-[11px] text-dimmed">{{ wlRupees(data.capital_per_position_rs) }} each</div>
        </div>
        <div class="rounded-lg bg-elevated/60 p-3">
          <div class="text-xs text-muted">Would be committed</div>
          <div class="num mt-1 text-2xl font-semibold text-highlighted">{{ crore(data.capital_committed_rs) }}</div>
          <div class="text-[11px] text-dimmed">{{ wlRupees(data.capital_committed_rs) }} · {{ data.real_count }} with real money</div>
        </div>
      </div>

      <div class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
        <span><span class="num font-medium text-highlighted">{{ data.accepted_count }}</span> accepted</span>
        <span><span class="num font-medium text-highlighted">{{ data.rejected_count }}</span> vetoed</span>
        <span v-if="data.legacy_open"><span class="num font-medium text-highlighted">{{ data.legacy_open }}</span> older v1 position(s) running off, not counted</span>
      </div>

      <UAlert v-if="data.mode === 'record-only'" color="neutral" variant="subtle" icon="i-lucide-notebook-pen" title="No real money is committed"
        description="Every row below is a recorded decision only: a shakedown of the machinery before it has consequences." />
      <UAlert v-if="full" color="warning" variant="subtle" icon="i-lucide-circle-alert" :title="`The book is full (${data.book_capacity} positions)`"
        description="New candidates the rules pass are being turned away for want of capital, not for anything about the companies. The nightly run's output names them." />

      <div class="flex flex-wrap items-center justify-between gap-2">
        <UTabs v-model="view" :items="[{ label: 'Open', value: 'open' }, { label: 'Closed', value: 'closed' }, { label: 'All', value: 'all' }]" size="xs" :content="false" />
        <span class="text-xs text-dimmed">Click a row for the forecast and the reviewer's reason</span>
      </div>

      <div>
        <div class="mb-2 flex items-center gap-2">
          <UBadge color="success" variant="subtle" icon="i-lucide-circle-check" :label="`Accepted · ${byView(accepted).length}`" />
          <span class="text-xs text-muted">the rule passed them and the AI reviewer agreed</span>
        </div>
        <PortfolioPositions v-if="byView(accepted).length" :rows="byView(accepted)" />
        <p v-else class="rounded-lg bg-elevated/50 p-3 text-xs text-dimmed">None in this view.</p>
      </div>

      <div v-if="rejected.length">
        <div class="mb-2 flex flex-wrap items-center gap-2">
          <UBadge color="warning" variant="subtle" icon="i-lucide-ban" :label="`Vetoed · ${byView(rejected).length}`" />
          <span class="text-xs text-muted">the rule passed them, the AI reviewer rejected them; tracked as if bought</span>
          <UButton size="xs" variant="ghost" color="neutral" class="ml-auto" :label="showRejected ? 'Hide' : 'Show'" :trailing-icon="showRejected ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'" @click="showRejected = !showRejected" />
        </div>
        <template v-if="showRejected">
          <PortfolioPositions v-if="byView(rejected).length" :rows="byView(rejected)" />
          <p v-else class="rounded-lg bg-elevated/50 p-3 text-xs text-dimmed">None in this view.</p>
        </template>
      </div>
      <p v-else class="text-xs text-dimmed">The AI reviewer has vetoed nothing yet. Until it does, there is no paired comparison to score it with.</p>
    </div>
  </UCard>
</template>
