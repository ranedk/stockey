<script setup lang="ts">
import type { HitRateGroup } from '~/types/api'

// PRD §12 todo #7. The whole rationale for a mechanical confluence score is that
// confidence must come "from something checkable after the fact" -- this table IS that
// check. Without it the watchlist lets you filter on a score whose predictive value
// nothing can show, which inverts the design.
const props = defineProps<{
  title: string
  subtitle: string
  groups: Record<string, HitRateGroup>
  keyLabel: string
  // Sort keys numerically where they are numbers ("0".."5"), alphabetically otherwise.
  numericKeys?: boolean
}>()

// The backend deliberately returns hit_rate: null below MIN_SAMPLE_SIZE_FOR_BREAKDOWN
// (5) rather than a percentage computed from 1-2 theses. Rendering that as 0%, or as a
// blank cell, would throw away exactly the honesty it was written to preserve -- so a
// thin sample says so, in words, and always shows its count.
const MIN_SAMPLE_SIZE = 5

const rows = computed(() => {
  const entries = Object.entries(props.groups ?? {})
  entries.sort(([a], [b]) => {
    if (props.numericKeys) {
      // "none" (never scored by confluence_score.py) sorts last, after the counts.
      const na = a === 'none' ? Number.POSITIVE_INFINITY : Number(a)
      const nb = b === 'none' ? Number.POSITIVE_INFINITY : Number(b)
      return na - nb
    }
    return a.localeCompare(b)
  })
  return entries.map(([key, group]) => ({ key, ...group }))
})

const totalCount = computed(() => rows.value.reduce((sum, r) => sum + (r.count ?? 0), 0))
</script>

<template>
  <div>
    <h3 class="text-xs font-medium text-slate-600">{{ title }}</h3>
    <p class="mt-0.5 text-xs text-slate-400">{{ subtitle }}</p>

    <p v-if="rows.length === 0" class="mt-2 text-xs text-slate-400">
      No resolved theses in this breakdown yet.
    </p>

    <table v-else class="mt-2 w-full text-xs">
      <thead class="text-left text-slate-400">
        <tr>
          <th class="py-1 font-medium">{{ keyLabel }}</th>
          <th class="py-1 text-right font-medium">Resolved</th>
          <th class="py-1 text-right font-medium">Hit rate</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="row.key" class="border-t border-slate-100">
          <td class="py-1 text-slate-700">
            {{ row.key === 'none' ? 'never scored' : row.key }}
          </td>
          <td class="py-1 text-right text-slate-600">{{ row.count }}</td>
          <td class="py-1 text-right">
            <span v-if="row.hit_rate !== null" class="font-medium text-slate-900">{{ row.hit_rate }}%</span>
            <span v-else class="text-slate-400" :title="`Fewer than ${MIN_SAMPLE_SIZE} resolved theses -- a percentage here would be noise`">
              too few to say (n={{ row.count }})
            </span>
          </td>
        </tr>
      </tbody>
    </table>

    <p v-if="rows.length && totalCount < MIN_SAMPLE_SIZE" class="mt-1 text-xs text-slate-400">
      Every group is below the {{ MIN_SAMPLE_SIZE }}-thesis threshold — treat this as a
      shape to watch, not a result.
    </p>
  </div>
</template>
