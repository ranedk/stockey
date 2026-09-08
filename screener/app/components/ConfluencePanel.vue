<script setup lang="ts">
import type { ConfluenceDetail } from '~/types/api'

// The per-axis view. The badge tells you the balance; this tells you WHICH signal
// disagrees -- which is the part that should actually change what you do. All five
// columns already exist in fundamentals_confluence_score; nothing here is computed.
const props = defineProps<{ confluence: ConfluenceDetail | null }>()

const AXIS_LABELS: Record<string, string> = {
  fundamentals_trajectory: 'Fundamentals trajectory',
  event_corroboration: 'Event corroboration',
  sector_cycle: 'Sector cycle',
  ownership: 'Ownership',
  valuation: 'Valuation',
}

// Contradicting axes first: a "no" is the thing worth reading before a "yes".
const ordered = computed(() => {
  const rank = (v: boolean | null) => (v === false ? 0 : v === true ? 1 : 2)
  return [...(props.confluence?.axes ?? [])].sort((a, b) => rank(a.verdict) - rank(b.verdict))
})

const unknownCount = computed(() =>
  (props.confluence?.axes ?? []).filter(a => a.verdict === null).length,
)

function verdictLabel(v: boolean | null): string {
  return v === true ? 'supports' : v === false ? 'contradicts' : 'not enough data'
}
function verdictTone(v: boolean | null): 'good' | 'bad' | 'neutral' {
  return v === true ? 'good' : v === false ? 'bad' : 'neutral'
}
</script>

<template>
  <section class="rounded-lg border border-slate-200 bg-white p-5">
    <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Confluence</h2>

    <p v-if="!confluence" class="mt-3 text-sm text-slate-500">
      Not scored yet — confluence_score.py has not run for this company.
    </p>

    <template v-else>
      <p class="mt-1 text-xs text-slate-400">
        Five independent, mechanical axes — never an LLM judgement. A count is only
        meaningful next to what disagrees with it, so both are shown.
      </p>

      <div class="mt-3 flex flex-wrap items-center gap-3 text-sm">
        <ConfluenceBadge
          :supportive="confluence.confluence_count"
          :contradicting="confluence.contradicting_count"
          :evaluable="confluence.evaluable_count"
        />
        <span class="text-xs text-slate-400">
          {{ confluence.evaluable_count }} of 5 axes evaluable
          <template v-if="unknownCount"> · {{ unknownCount }} lacked data</template>
          <template v-if="confluence.run_date"> · scored {{ formatDate(confluence.run_date) }}</template>
        </span>
      </div>

      <ul class="mt-3 space-y-2">
        <li
          v-for="axis in ordered"
          :key="axis.key"
          class="rounded border p-3"
          :class="axis.verdict === false ? 'border-rose-200' : axis.verdict === true ? 'border-emerald-200' : 'border-slate-200'"
        >
          <div class="flex flex-wrap items-center gap-2">
            <BadgePill :label="verdictLabel(axis.verdict)" :tone="verdictTone(axis.verdict)" />
            <span class="text-sm font-medium">{{ AXIS_LABELS[axis.key] ?? axis.key }}</span>
          </div>
          <p class="mt-1 text-xs text-slate-500">{{ axis.description }}</p>
        </li>
      </ul>

      <p class="mt-3 text-xs text-slate-400">
        An axis with no verdict is deliberately not counted either way — a company with 3
        knowable axes all supporting scores 3 for · none against, and is not penalised for
        the two it could not evaluate.
      </p>
    </template>
  </section>
</template>
