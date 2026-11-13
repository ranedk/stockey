<script setup lang="ts">
import type { ConfluenceDetail } from '~/types/watchlist'

// The per-check view: the badge gives the balance, this says WHICH check disagrees.
const props = defineProps<{ confluence: ConfluenceDetail | null | undefined }>()

const AXIS: Record<string, { label: string, icon: string }> = {
  fundamentals_trajectory: { label: 'Debt and capex trend', icon: 'i-lucide-trending-up' },
  event_corroboration: { label: 'Recent news agrees', icon: 'i-lucide-newspaper' },
  sector_cycle: { label: 'Sector cycle', icon: 'i-lucide-refresh-cw' },
  ownership: { label: 'Who is buying', icon: 'i-lucide-users' },
  valuation: { label: 'Cheap vs its own past', icon: 'i-lucide-tag' },
}

// Contradicting checks first: a "no" is worth reading before a "yes".
const ordered = computed(() => {
  const rank = (v: boolean | null) => (v === false ? 0 : v === true ? 1 : 2)
  return [...(props.confluence?.axes ?? [])].sort((a, b) => rank(a.verdict) - rank(b.verdict))
})
const unknownCount = computed(() => (props.confluence?.axes ?? []).filter(a => a.verdict === null).length)

const V = {
  true: { label: 'Supports', color: 'success' as const, icon: 'i-lucide-circle-check', ring: 'var(--up)' },
  false: { label: 'Contradicts', color: 'error' as const, icon: 'i-lucide-circle-x', ring: 'var(--down)' },
  null: { label: 'Not enough data', color: 'neutral' as const, icon: 'i-lucide-circle-help', ring: 'var(--chart-axis)' },
}
const v = (x: boolean | null) => V[String(x) as 'true' | 'false' | 'null']
</script>

<template>
  <UCard>
    <template #header>
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 class="font-semibold text-highlighted">Five independent checks</h3>
          <p class="text-xs text-muted">Mechanical rules, never an AI judgement. What disagrees is shown next to what agrees.</p>
        </div>
        <WatchlistConfluenceBadge v-if="confluence" :supportive="confluence.confluence_count" :contradicting="confluence.contradicting_count" :evaluable="confluence.evaluable_count" />
      </div>
    </template>

    <UEmpty v-if="!confluence" icon="i-lucide-scale" title="Not scored yet" description="The checks have not run for this company." class="py-6" />

    <template v-else>
      <ul class="grid gap-2">
        <li v-for="a in ordered" :key="a.key" class="flex items-start gap-3 rounded-lg border border-default border-l-4 bg-elevated/30 px-3 py-2.5" :style="{ borderLeftColor: v(a.verdict).ring }">
          <UIcon :name="AXIS[a.key]?.icon || 'i-lucide-circle'" class="mt-0.5 size-4 shrink-0 text-muted" />
          <div class="min-w-0 flex-1">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <span class="text-sm font-medium text-highlighted">{{ AXIS[a.key]?.label ?? a.key }}</span>
              <UBadge :color="v(a.verdict).color" variant="soft" size="sm" :icon="v(a.verdict).icon" :label="v(a.verdict).label" />
            </div>
            <p class="mt-0.5 text-xs text-muted">{{ a.description }}</p>
          </div>
        </li>
      </ul>
      <p class="mt-3 text-xs text-dimmed">
        {{ confluence.evaluable_count }} of 5 checks could be judged<template v-if="unknownCount"> · {{ unknownCount }} lacked data</template><template v-if="confluence.run_date"> · scored {{ dateShort(confluence.run_date) }}</template>.
        A check without data counts neither way, so a company is not penalised for what cannot be known.
      </p>
    </template>
  </UCard>
</template>
