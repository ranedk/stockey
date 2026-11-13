<script setup lang="ts">
import type { BindingConstraint, PositionSizing } from '~/types/api'

// PRD §12 todo #8's calculator. Renders only where the company has an open ACCEPTED
// position -- the endpoint 404s otherwise.
//
// This used to ask the operator for a sleeve size and a target position count, because
// the endpoint refused to assume either. Sizing became a FLAT Rs 1,00,000 per position on
// 2026-09-04, set directly by the operator, so there is nothing left to ask: the inputs
// and the Calculate button are gone and this just shows what the size is and what bound
// it.
const props = defineProps<{ companyMasterId: string }>()

const config = useRuntimeConfig()
const { data: sizing, error } = await useAsyncData(
  () => `sizing-${props.companyMasterId}`,
  () => $fetch<PositionSizing>(
    `${config.public.apiBase}/api/watchlist/${encodeURIComponent(props.companyMasterId)}/sizing`,
  ),
  { watch: [() => props.companyMasterId] },
)

// 404 is a MEANINGFUL state, not a failure: no open accepted position, so nothing to
// size. Collapsing it into "something broke" would report an error when the pipeline is
// correctly refusing to size a name it did not take.
const notCommitted = computed(() => {
  const e = error.value as { statusCode?: number, response?: { status?: number } } | null
  return (e?.statusCode ?? e?.response?.status) === 404
})

const CONSTRAINT_COPY: Record<BindingConstraint, { label: string, tone: 'good' | 'warn' | 'neutral', note: string }> = {
  flat_allocation: {
    label: 'Flat allocation',
    tone: 'neutral',
    note: 'Liquidity is not the limit here — the flat allocation is smaller than this name can absorb.',
  },
  adv_liquidity_cap: {
    label: 'ADV liquidity cap',
    tone: 'warn',
    note: 'Liquidity binds: a full allocation would be too large a share of this name\'s daily traded value to exit cleanly.',
  },
  flat_allocation_no_adv_data: {
    label: 'No ADV data',
    tone: 'warn',
    note: 'No liquidity data for this company, so the ADV ceiling could NOT be computed — this size is uncapped. Treat with caution.',
  },
}

function rupees(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return `₹${Math.round(v).toLocaleString('en-IN')}`
}
</script>

<template>
  <div class="mt-4 border-t border-slate-100 pt-4">
    <div class="flex items-center gap-2">
      <h3 class="text-xs font-medium text-slate-500">Position sizing</h3>
      <span class="text-xs text-slate-400">calculator — no money is committed yet</span>
    </div>

    <div v-if="notCommitted" class="mt-3 rounded border border-slate-200 bg-slate-50 p-3 text-sm text-slate-600">
      No open accepted position for this company, so there is nothing to size.
    </div>

    <div v-else-if="error" class="mt-3 rounded border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
      Could not reach the sizing endpoint.
    </div>

    <div v-else-if="sizing" class="mt-3 rounded-lg border border-slate-200 p-4">
      <div class="flex flex-wrap items-baseline gap-3">
        <span class="text-2xl font-semibold text-slate-900">{{ rupees(sizing.recommended_size_rs) }}</span>
        <BadgePill
          :label="CONSTRAINT_COPY[sizing.binding_constraint].label"
          :tone="CONSTRAINT_COPY[sizing.binding_constraint].tone"
        />
      </div>
      <p class="mt-1 text-xs text-slate-500">{{ CONSTRAINT_COPY[sizing.binding_constraint].note }}</p>

      <!-- Both inputs to the min() are shown, not just the winner: which constraint bound
           is the actionable information, and a number without its ceiling cannot be
           sanity-checked. -->
      <dl class="mt-3 grid grid-cols-2 gap-x-6 gap-y-1 text-xs sm:grid-cols-4">
        <div>
          <dt class="text-slate-400">Flat allocation</dt>
          <dd :class="sizing.binding_constraint === 'flat_allocation' ? 'font-semibold text-slate-900' : 'text-slate-600'">
            {{ rupees(sizing.target_capital_rs) }}
          </dd>
        </div>
        <div>
          <dt class="text-slate-400">ADV cap ({{ Math.round(sizing.max_pct_of_adv * 100) }}% of ADV)</dt>
          <dd :class="sizing.binding_constraint === 'adv_liquidity_cap' ? 'font-semibold text-slate-900' : 'text-slate-600'">
            {{ rupees(sizing.adv_cap_rs) }}
          </dd>
        </div>
        <div>
          <dt class="text-slate-400">Avg vol (1m)</dt>
          <dd class="text-slate-600">{{ sizing.avg_vol_1mth === null ? '—' : sizing.avg_vol_1mth.toLocaleString('en-IN') }}</dd>
        </div>
        <div>
          <dt class="text-slate-400">CMP</dt>
          <dd class="text-slate-600">{{ sizing.cmp_rs === null ? '—' : formatPrice(sizing.cmp_rs) }}</dd>
        </div>
      </dl>

      <p class="mt-3 text-xs text-slate-400">
        Size = min(₹{{ Math.round(sizing.capital_per_position_rs).toLocaleString('en-IN') }} per position,
        {{ Math.round(sizing.max_pct_of_adv * 100) }}% of average daily traded value).
        ADV is a liquidity ceiling — it can only reduce the size, never raise it.
      </p>
    </div>
  </div>
</template>
