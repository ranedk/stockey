<script setup lang="ts">
import type { PaperOptionsResponse } from '~/types/systrader'

// Nifty option paper tracks (systrader LEDGER 78). Optional: an API older than the endpoint
// shows nothing rather than an error.
const api = useSystraderApi()
const { data } = await useAsyncData('paper-options',
  () => api.get<PaperOptionsResponse>('/api/paper/options').catch(() => null))

const bps = (v: number | null | undefined) => v === null || v === undefined ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)} bps`
const tone = (v: number | null | undefined) => v === null || v === undefined ? 'text-slate-400' : v >= 0 ? 'text-emerald-700' : 'text-rose-700'
const memberTitle = (m: Record<string, number | null>) => Object.entries(m).map(([k, v]) => `${k}: ${bps(v)}`).join('\n')
</script>

<template>
  <section v-for="t in data?.tracks ?? []" :key="t.name" class="mt-6 rounded-lg border border-slate-200 bg-white p-4">
    <div class="flex flex-wrap items-center gap-3">
      <h2 class="text-sm font-semibold text-slate-900">Options · {{ paperStrategyName(t.name) }}</h2>
      <BadgePill label="paper only" tone="neutral" />
      <span class="text-xs text-slate-400">from {{ formatDate(t.start) }} · {{ t.judged }}</span>
    </div>
    <p class="mt-1 max-w-3xl text-xs text-slate-500">{{ t.rule }}. Costs: {{ t.costs }}.</p>

    <div class="mt-3 flex flex-wrap gap-6 text-sm">
      <div>
        <div class="text-xs text-slate-500">Average per expiry</div>
        <div :class="['text-lg font-semibold', tone(t.mean_bps)]">{{ bps(t.mean_bps) }}</div>
        <div class="text-xs text-slate-400">of notional, after costs · check period {{ bps(t.check_bps) }}</div>
      </div>
      <div>
        <div class="text-xs text-slate-500">Expiries recorded</div>
        <div class="text-lg font-semibold text-slate-900">{{ t.count }} <span class="text-sm font-normal text-slate-500">of 52</span></div>
        <div class="text-xs text-slate-400">{{ t.positive }} positive</div>
      </div>
    </div>

    <div v-if="!t.sessions.length" class="mt-3 text-sm text-slate-500">
      No expiry recorded yet — each one appears the evening after it, once the option data lands.
    </div>
    <table v-else class="mt-3 w-full text-sm">
      <thead>
        <tr class="text-left text-xs text-slate-500">
          <th class="py-1 font-medium">Expiry</th>
          <th class="py-1 font-medium">ATM strike (09:20)</th>
          <th class="py-1 text-right font-medium">Basket</th>
          <th class="py-1 text-right font-medium">Running total</th>
          <th class="py-1 text-right font-medium">Legs missing</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="s in [...t.sessions].reverse()" :key="s.date" class="border-t border-slate-100" :title="memberTitle(s.members)">
          <td class="py-1">{{ formatDate(s.date) }}</td>
          <td class="py-1 text-slate-600">{{ s.strike ?? '—' }}</td>
          <td :class="['py-1 text-right', tone(s.basket_bps)]">{{ bps(s.basket_bps) }}</td>
          <td :class="['py-1 text-right', tone(s.cum_bps)]">{{ bps(s.cum_bps) }}</td>
          <td class="py-1 text-right text-slate-500">{{ s.missing_legs }}</td>
        </tr>
      </tbody>
    </table>
    <p class="mt-2 text-xs text-slate-400">
      1 bp of notional is about 2.5 Nifty points per straddle at Nifty 25,000. Hover a row for the six versions.
    </p>
  </section>
</template>
