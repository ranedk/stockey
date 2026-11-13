<script setup lang="ts">
import type { BindingConstraint, PositionSizing } from '~/types/watchlist'

// Position size for a company with an open accepted position. The endpoint 404s otherwise, and
// that is a meaningful state (nothing to size), not an error.
const props = defineProps<{ companyMasterId: string }>()
const { data: sizing, error } = useStockey<PositionSizing>(() => `/api/watchlist/${encodeURIComponent(props.companyMasterId)}/sizing`)
const notCommitted = computed(() => (error.value as any)?.statusCode === 404)

const COPY: Record<BindingConstraint, { label: string, color: 'neutral' | 'warning', note: string }> = {
  flat_allocation: { label: 'Flat amount', color: 'neutral', note: 'Trading volume is not the limit: the flat amount is smaller than this stock can absorb.' },
  adv_liquidity_cap: { label: 'Limited by trading volume', color: 'warning', note: 'A full amount would be too large a share of what trades each day to sell cleanly, so it is cut.' },
  flat_allocation_no_adv_data: { label: 'No volume data', color: 'warning', note: 'No trading-volume data, so the volume ceiling could not be checked; this size is uncapped. Treat with caution.' },
}
</script>

<template>
  <div class="rounded-lg border border-default p-4">
    <div class="flex flex-wrap items-center gap-2">
      <h4 class="text-sm font-medium text-highlighted">Position size</h4>
      <span class="text-xs text-dimmed">calculator · no money is committed yet</span>
    </div>

    <p v-if="notCommitted" class="mt-2 text-sm text-muted">No open accepted position for this company, so there is nothing to size.</p>
    <UAlert v-else-if="error" class="mt-3" color="error" variant="subtle" icon="i-lucide-circle-alert" title="Could not reach the sizing endpoint." />
    <template v-else-if="sizing">
      <div class="mt-2 flex flex-wrap items-center gap-3">
        <span class="num text-2xl font-semibold text-highlighted">{{ wlRupees(sizing.recommended_size_rs) }}</span>
        <UBadge :color="COPY[sizing.binding_constraint].color" variant="subtle" :label="COPY[sizing.binding_constraint].label" />
      </div>
      <p class="mt-1 text-xs text-muted">{{ COPY[sizing.binding_constraint].note }}</p>
      <div class="mt-3 grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
        <div>
          <div class="text-dimmed">Flat amount</div>
          <div class="num mt-0.5" :class="sizing.binding_constraint === 'flat_allocation' ? 'font-semibold text-highlighted' : 'text-muted'">{{ wlRupees(sizing.target_capital_rs) }}</div>
        </div>
        <div>
          <UTooltip text="The most we would buy: this share of the average rupee value traded per day">
            <div class="text-dimmed underline decoration-dotted underline-offset-2">Volume ceiling ({{ Math.round(sizing.max_pct_of_adv * 100) }}% of daily value)</div>
          </UTooltip>
          <div class="num mt-0.5" :class="sizing.binding_constraint === 'adv_liquidity_cap' ? 'font-semibold text-highlighted' : 'text-muted'">{{ wlRupees(sizing.adv_cap_rs) }}</div>
        </div>
        <div>
          <div class="text-dimmed">Avg shares traded (1 month)</div>
          <div class="num mt-0.5 text-muted">{{ num(sizing.avg_vol_1mth) }}</div>
        </div>
        <div>
          <div class="text-dimmed">Current price</div>
          <div class="num mt-0.5 text-muted">{{ inr(sizing.cmp_rs) }}</div>
        </div>
      </div>
      <p class="mt-3 text-xs text-dimmed">
        Size = the smaller of {{ wlRupees(sizing.capital_per_position_rs) }} per position and {{ Math.round(sizing.max_pct_of_adv * 100) }}% of the average daily traded value.
        The volume ceiling can only reduce the size, never raise it.
      </p>
    </template>
    <USkeleton v-else class="mt-3 h-16" />
  </div>
</template>
