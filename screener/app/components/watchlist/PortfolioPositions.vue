<script setup lang="ts">
import type { RulesetPosition } from '~/types/watchlist'

// One table of ruleset positions; a row expands to the forecast and the AI reviewer's reason.
defineProps<{ rows: RulesetPosition[] }>()
const expanded = ref<string | null>(null)
const toggle = (id: string) => { expanded.value = expanded.value === id ? null : id }

/** Judged at the exit price once closed, at today's price while open. */
const mark = (p: RulesetPosition) => (p.status === 'closed' ? p.exit_price ?? null : p.last_price)
const ret = (p: RulesetPosition) => wlChange(p.entry_price, mark(p))
/** How much of the drop-to-stop is still left: 100% = just bought, 0% = at the stop. */
function room(p: RulesetPosition): number | null {
  if (p.status === 'closed' || !isNum(p.entry_price) || !isNum(p.stop_pct) || !isNum(p.last_price)) return null
  const stop = p.entry_price * (1 - p.stop_pct / 100)
  const dist = p.entry_price - stop
  if (dist <= 0) return null
  return Math.max(0, Math.min(100, ((p.last_price - stop) / dist) * 100))
}
const roomColor = (r: number) => (r <= 25 ? 'var(--down)' : r <= 60 ? 'var(--series-4)' : 'var(--up)')
</script>

<template>
  <div class="overflow-x-auto rounded-lg border border-default">
    <table class="w-full min-w-[900px] text-sm">
      <thead>
        <tr class="border-b border-default bg-elevated/40 text-left text-xs text-muted">
          <th class="sticky left-0 z-10 bg-elevated px-3 py-2 font-medium">Stock</th>
          <th class="px-3 py-2 text-right font-medium">Return</th>
          <th class="px-3 py-2 font-medium">Bought</th>
          <th class="px-3 py-2 text-right font-medium">Entry</th>
          <th class="px-3 py-2 text-right font-medium">Last / exit</th>
          <th class="px-3 py-2 text-right font-medium">Size</th>
          <th class="px-3 py-2 font-medium"><UTooltip text="How much of the fall to the stop-loss is still left. Full bar = at the buy price; empty = at the stop."><span class="underline decoration-dotted underline-offset-2">Room to stop-loss</span></UTooltip></th>
          <th class="px-3 py-2 font-medium">Target date</th>
          <th class="px-3 py-2 text-center font-medium"><UTooltip text="Independent checks agreeing / checks that could be judged, at entry"><span class="underline decoration-dotted underline-offset-2">Checks</span></UTooltip></th>
          <th class="px-3 py-2 font-medium">Forecast</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="p in rows" :key="p.position_id">
          <tr class="group cursor-pointer border-b border-default/60 align-top hover:bg-elevated/40" @click="toggle(p.position_id)">
            <td class="sticky left-0 z-10 bg-default px-3 py-2 group-hover:bg-elevated">
              <div class="flex items-center gap-1.5">
                <UIcon :name="expanded === p.position_id ? 'i-lucide-chevron-down' : 'i-lucide-chevron-right'" class="size-3.5 text-dimmed" />
                <NuxtLink :to="`/watchlist/${encodeURIComponent(p.company_master_id)}`" class="font-mono font-medium text-highlighted hover:text-primary" @click.stop>{{ p.ticker }}</NuxtLink>
              </div>
              <div class="mt-0.5 flex gap-1 pl-5">
                <UBadge v-if="p.entry_decision === 'reject'" color="warning" variant="soft" size="xs" label="vetoed" />
                <UBadge v-else-if="p.kind === 'shadow'" color="neutral" variant="outline" size="xs" label="no money" />
                <UBadge v-if="p.status === 'closed'" color="neutral" variant="soft" size="xs" :label="`closed${p.close_reason ? ': ' + wlCloseReason(p.close_reason) : ''}`" />
              </div>
            </td>
            <td class="px-3 py-2 text-right"><Change :value="ret(p)" pill /></td>
            <td class="num whitespace-nowrap px-3 py-2 text-xs text-muted">{{ dateShort(p.opened_at) }}</td>
            <td class="num px-3 py-2 text-right text-muted">{{ inr(p.entry_price) }}</td>
            <td class="num px-3 py-2 text-right text-highlighted">{{ inr(mark(p)) }}</td>
            <td class="num whitespace-nowrap px-3 py-2 text-right text-muted">
              <UTooltip v-if="isNum(p.position_size_rs)" :text="p.sizing_basis === 'adv_liquidity_cap' ? 'Cut to fit the stock\'s daily trading volume' : 'Flat amount per position'">
                <span>{{ wlRupees(p.position_size_rs) }}<span v-if="p.sizing_basis === 'adv_liquidity_cap'" class="text-warning">*</span></span>
              </UTooltip>
              <span v-else>—</span>
            </td>
            <td class="px-3 py-2" style="min-width: 8rem">
              <UTooltip v-if="room(p) !== null" :text="p.stop_basis || ''">
                <div class="w-full">
                  <div class="h-1.5 w-full overflow-hidden rounded-full bg-elevated">
                    <div class="h-full rounded-full" :style="{ width: `${room(p)}%`, background: roomColor(room(p)!) }" />
                  </div>
                  <div class="mt-1 text-[11px] text-muted">{{ Math.round(room(p)!) }}% left · stop {{ p.stop_pct }}%</div>
                </div>
              </UTooltip>
              <span v-else class="text-dimmed">—</span>
            </td>
            <td class="whitespace-nowrap px-3 py-2 text-xs text-muted">
              <template v-if="p.target_date">
                {{ dateShort(p.target_date) }}
                <div v-if="p.status === 'open'" class="text-[11px] text-dimmed">{{ wlDaysFromNow(p.target_date) }} days left</div>
              </template>
              <span v-else>—</span>
            </td>
            <td class="num px-3 py-2 text-center text-xs text-muted">{{ isNum(p.confluence_count) && isNum(p.evaluable_count) ? `${p.confluence_count}/${p.evaluable_count}` : '—' }}</td>
            <td class="min-w-48 max-w-xs px-3 py-2 text-xs text-muted"><span class="line-clamp-2">{{ p.prediction_text || p.adjudicator_reason || '—' }}</span></td>
          </tr>
          <tr v-if="expanded === p.position_id" class="border-b border-default/60 bg-elevated/30">
            <td colspan="10" class="px-4 py-3">
              <div class="grid max-w-[calc(100vw-4rem)] gap-4 text-xs sm:grid-cols-3">
                <div><div class="font-medium text-highlighted">Forecast</div><p class="mt-0.5 text-muted">{{ p.prediction_text || '—' }}</p></div>
                <div><div class="font-medium text-highlighted">Wrong if</div><p class="mt-0.5 text-muted">{{ p.invalidation_criteria || '—' }}</p></div>
                <div>
                  <div class="font-medium text-highlighted">AI reviewer</div>
                  <p class="mt-0.5 text-muted">{{ p.adjudicator_reason || '—' }}</p>
                  <p class="mt-1 text-[11px] text-dimmed">{{ p.adjudicator_model || 'no model' }} · prompt v{{ p.adjudicator_prompt_version }} · rules v{{ p.ruleset_version }}<template v-if="p.target_date_basis"> · target: {{ p.target_date_basis }}</template></p>
                </div>
              </div>
            </td>
          </tr>
        </template>
      </tbody>
    </table>
  </div>
</template>
