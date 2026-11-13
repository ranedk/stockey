<script setup lang="ts">
import type { HitRateGroup } from '~/types/watchlist'

// Hit rate per group. The API returns hit_rate null below 5 graded forecasts; that is shown in
// words with its count, never as 0% or a blank.
const props = defineProps<{ title: string, subtitle: string, groups: Record<string, HitRateGroup>, keyLabel: string, numericKeys?: boolean, names?: Record<string, string> }>()
const MIN = 5
const rows = computed(() => {
  const e = Object.entries(props.groups ?? {})
  e.sort(([a], [b]) => {
    if (props.numericKeys) {
      const na = a === 'none' ? Infinity : Number(a), nb = b === 'none' ? Infinity : Number(b)
      return na - nb
    }
    return a.localeCompare(b)
  })
  return e.map(([key, g]) => ({ key, ...g }))
})
const total = computed(() => rows.value.reduce((s, r) => s + (r.count ?? 0), 0))
const name = (k: string) => (k === 'none' ? 'never scored' : props.names?.[k] ?? k)
</script>

<template>
  <div>
    <h4 class="text-sm font-medium text-highlighted">{{ title }}</h4>
    <p class="mt-0.5 text-xs text-muted">{{ subtitle }}</p>
    <p v-if="!rows.length" class="mt-3 rounded-lg bg-elevated/50 p-3 text-xs text-dimmed">No graded forecasts in this breakdown yet.</p>
    <table v-else class="mt-2 w-full text-xs">
      <thead class="text-left text-dimmed">
        <tr><th class="py-1 font-medium">{{ keyLabel }}</th><th class="py-1 text-right font-medium">Graded</th><th class="py-1 text-right font-medium">Came true</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.key" class="border-t border-default">
          <td class="py-1.5 text-default">{{ name(r.key) }}</td>
          <td class="num py-1.5 text-right text-muted">{{ r.count }}</td>
          <td class="py-1.5 text-right">
            <span v-if="r.hit_rate !== null" class="num font-medium text-highlighted">{{ r.hit_rate }}%</span>
            <UTooltip v-else :text="`Fewer than ${MIN} graded forecasts: a percentage would be noise`"><span class="text-dimmed">too few (n={{ r.count }})</span></UTooltip>
          </td>
        </tr>
      </tbody>
    </table>
    <p v-if="rows.length && total < MIN" class="mt-1 text-xs text-dimmed">Every group is below {{ MIN }} forecasts: a shape to watch, not a result.</p>
  </div>
</template>
