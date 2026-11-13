<script setup lang="ts">
import type { RulesetPosition } from '~/types/api'

defineProps<{ rows: RulesetPosition[] }>()

const expanded = ref<string | null>(null)

function toggle(id: string) {
  expanded.value = expanded.value === id ? null : id
}

/** The price a position is judged at: its exit price once closed, today's price while
 *  open. Closed rows used to show P&L against TODAY's price (2026-09-23 audit). */
function markPrice(p: RulesetPosition): number | null {
  return p.status === 'closed' ? (p.exit_price ?? null) : p.last_price
}

/** Gain since entry. The stop-room bar below says how much rope is left; this
 *  says whether the position is actually working. */
function positionReturn(p: RulesetPosition): number | null {
  const mark = markPrice(p)
  if (p.entry_price === null || mark === null || p.entry_price <= 0) return null
  return mark / p.entry_price - 1
}

function axes(p: RulesetPosition): string {
  if (p.confluence_count === null || p.evaluable_count === null) return '—'
  return `${p.confluence_count}/${p.evaluable_count}`
}

/**
 * How much further the price can fall before the stop trips, as a share of the stop
 * distance. 100% = just entered, 0% = at the stop. This is the number that decides
 * whether a position is about to be closed, so it is worth more space than P&L.
 */
function roomToStop(p: RulesetPosition): number | null {
  // A closed position has no stop left to approach.
  if (p.status === 'closed') return null
  if (p.entry_price === null || p.stop_pct === null || p.last_price === null) return null
  const stopLevel = p.entry_price * (1 - p.stop_pct / 100)
  const distance = p.entry_price - stopLevel
  if (distance <= 0) return null
  return Math.max(0, Math.min(100, ((p.last_price - stopLevel) / distance) * 100))
}

function roomTone(room: number | null): string {
  if (room === null) return 'bg-slate-200'
  if (room <= 25) return 'bg-rose-500'
  if (room <= 60) return 'bg-amber-500'
  return 'bg-emerald-500'
}

function daysToTarget(p: RulesetPosition): number | null {
  if (!p.target_date) return null
  const ms = new Date(p.target_date).getTime() - Date.now()
  return Math.round(ms / 86_400_000)
}
</script>

<template>
  <div class="overflow-x-auto rounded-lg border border-slate-200">
    <table class="w-full text-xs">
      <thead class="border-b border-slate-200 bg-slate-50 text-left text-slate-500">
        <tr>
          <th class="px-3 py-2 font-medium">Ticker</th>
          <th class="px-3 py-2 text-right font-medium">P&amp;L</th>
          <th class="px-3 py-2 font-medium">Opened</th>
          <th class="px-3 py-2 text-right font-medium">Entry</th>
          <th class="px-3 py-2 text-right font-medium">Last</th>
          <th class="px-3 py-2 text-right font-medium">Size</th>
          <th class="px-3 py-2 font-medium" style="min-width: 9rem">Room to stop</th>
          <th class="px-3 py-2 font-medium">Target</th>
          <th class="px-3 py-2 text-center font-medium">Axes</th>
          <th class="px-3 py-2 font-medium">Thesis</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="p in rows" :key="p.position_id">
          <tr
            class="cursor-pointer border-b border-slate-100 align-top hover:bg-slate-50"
            @click="toggle(p.position_id)"
          >
            <td class="px-3 py-2 font-medium">
              <NuxtLink
                :to="`/watchlist/${encodeURIComponent(p.company_master_id)}`"
                class="hover:underline"
                @click.stop
              >
                {{ p.ticker }}
              </NuxtLink>
              <span v-if="p.entry_decision === 'reject'" class="ml-1 text-[10px] text-rose-500">vetoed</span>
              <span v-else-if="p.kind === 'shadow'" class="ml-1 text-[10px] text-slate-400">no money</span>
            </td>
            <td class="px-3 py-2 text-right"><ReturnValue :value="positionReturn(p)" :digits="1" /></td>
            <td class="px-3 py-2 text-slate-500">{{ formatDate(p.opened_at) }}</td>
            <td class="px-3 py-2 text-right text-slate-600">
              {{ p.entry_price === null ? '—' : formatPrice(p.entry_price) }}
            </td>
            <td class="px-3 py-2 text-right text-slate-800">
              {{ markPrice(p) === null ? '—' : formatPrice(markPrice(p) as number) }}
            </td>
            <td class="px-3 py-2 text-right text-slate-600">
              <span v-if="p.position_size_rs !== null" :title="p.sizing_basis || ''">
                ₹{{ Math.round(p.position_size_rs).toLocaleString('en-IN') }}
                <span v-if="p.sizing_basis === 'adv_liquidity_cap'" class="text-amber-600">*</span>
              </span>
              <span v-else>—</span>
            </td>
            <td class="px-3 py-2">
              <div v-if="roomToStop(p) !== null" :title="p.stop_basis || ''">
                <div class="h-1.5 w-full overflow-hidden rounded-full bg-slate-200">
                  <div class="h-full rounded-full" :class="roomTone(roomToStop(p))" :style="{ width: `${roomToStop(p)}%` }" />
                </div>
                <div class="mt-1 text-[10px] text-slate-500">
                  {{ Math.round(roomToStop(p) as number) }}% of a {{ p.stop_pct }}% stop
                </div>
              </div>
              <span v-else class="text-slate-400">—</span>
            </td>
            <td class="px-3 py-2 text-slate-500">
              <template v-if="p.target_date">
                {{ formatDate(p.target_date) }}
                <div class="text-[10px] text-slate-400">{{ daysToTarget(p) }}d</div>
              </template>
              <span v-else>—</span>
            </td>
            <td class="px-3 py-2 text-center text-slate-600">{{ axes(p) }}</td>
            <td class="px-3 py-2 text-slate-500">
              <span class="line-clamp-2">{{ p.prediction_text || p.adjudicator_reason || '—' }}</span>
            </td>
          </tr>
          <tr v-if="expanded === p.position_id" class="border-b border-slate-100 bg-slate-50/60">
            <td colspan="10" class="px-3 py-3">
              <dl class="grid gap-3 text-xs sm:grid-cols-3">
                <div>
                  <dt class="font-medium text-slate-700">Prediction</dt>
                  <dd class="mt-0.5 text-slate-600">{{ p.prediction_text || '—' }}</dd>
                </div>
                <div>
                  <dt class="font-medium text-slate-700">Invalidated if</dt>
                  <dd class="mt-0.5 text-slate-600">{{ p.invalidation_criteria || '—' }}</dd>
                </div>
                <div>
                  <dt class="font-medium text-slate-700">Adjudicator</dt>
                  <dd class="mt-0.5 text-slate-600">{{ p.adjudicator_reason || '—' }}</dd>
                  <dd class="mt-1 text-[10px] text-slate-400">
                    {{ p.adjudicator_model || 'no model' }} · prompt v{{ p.adjudicator_prompt_version }} ·
                    ruleset v{{ p.ruleset_version }}
                    <template v-if="p.target_date_basis"> · target {{ p.target_date_basis }}</template>
                  </dd>
                </div>
              </dl>
            </td>
          </tr>
        </template>
      </tbody>
    </table>
  </div>
</template>
