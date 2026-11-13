<script setup lang="ts">
import type { PtSummary } from '~/types/paperTracks'
// One row per book: the strategy and the controls it is measured against.
defineProps<{ summaries: PtSummary[] }>()
</script>

<template>
  <div class="overflow-x-auto">
    <table class="w-full min-w-[34rem] text-sm">
      <thead>
        <tr class="border-b border-default text-xs text-muted">
          <th class="py-2 pr-3 text-left font-medium">Book</th>
          <th class="px-3 py-2 text-right font-medium">Return</th>
          <th class="px-3 py-2 text-right font-medium">
            <UTooltip text="The biggest fall from a high point to a later low.">
              <span class="cursor-help underline decoration-dotted underline-offset-2">Worst drop</span>
            </UTooltip>
          </th>
          <th class="px-3 py-2 text-right font-medium">Stocks held</th>
          <th class="py-2 pl-3 text-right font-medium">
            <UTooltip text="Trading costs as a share of the book, per year.">
              <span class="cursor-help underline decoration-dotted underline-offset-2">Costs / year</span>
            </UTooltip>
          </th>
        </tr>
      </thead>
      <tbody class="divide-y divide-default">
        <tr v-for="s in summaries" :key="s.book">
          <td class="py-2 pr-3">
            <UTooltip :text="paperBookDef(s.book)?.hint" :disabled="!paperBookDef(s.book)">
              <span class="inline-flex items-center gap-2" :class="s.book === 'strategy' ? 'font-medium text-highlighted' : ''">
                <span class="h-0.5 w-3.5 shrink-0 rounded" :style="{ background: paperBookDef(s.book)?.color ?? 'var(--chart-axis)' }" />
                {{ paperBookDef(s.book)?.label ?? s.book }}
              </span>
            </UTooltip>
          </td>
          <td class="px-3 py-2 text-right"><Change :value="s.total_return" /></td>
          <td class="num px-3 py-2 text-right" :class="signClass(s.max_drawdown)">{{ pct(s.max_drawdown, 1) }}</td>
          <td class="num px-3 py-2 text-right text-muted">{{ num(s.holdings) }}</td>
          <td class="num py-2 pl-3 text-right text-muted">{{ pct(s.ann_cost, 2) }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
