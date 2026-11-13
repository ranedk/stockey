<script setup lang="ts">
import type { ThemeDetail, ThemeStock } from '~/types/themes'

// One theme: strength parts, the policy table and every member -- ranked, without a known
// revenue share (greyed, never ranked) and left out, with the reason.

const route = useRoute()
const id = computed(() => String(route.params.id))
const api = useApi()
const { data, status, error } = await useAsyncData(() => `theme-${id.value}`,
  () => api.get<ThemeDetail>(`/api/themes/${id.value}`), { watch: [id] })

const group = (s: ThemeStock['status']) => computed(() => (data.value?.stocks ?? []).filter(x => x.status === s))
const ranked = group('ranked')
const unquantified = group('unquantified')
const excluded = group('excluded')
const STATUS_LABEL: Record<string, string> = { shown: 'on the list', next: 'next up', late: 'late', out: 'not shown', unranked: 'not ranked' }
</script>

<template>
  <div>
    <NuxtLink to="/themes" class="text-sm text-slate-500 hover:text-slate-700">← Themes</NuxtLink>
    <div v-if="error" class="mt-6 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
      Could not load this theme ({{ error.message }}).
    </div>
    <div v-else-if="status === 'pending'" class="mt-6 text-sm text-slate-500">Loading…</div>
    <template v-else-if="data">
      <div class="mt-2 flex flex-wrap items-baseline gap-3">
        <h1 class="text-xl font-semibold">{{ data.theme.name }}</h1>
        <BadgePill :label="STATUS_LABEL[data.theme.status] ?? data.theme.status"
                   :tone="data.theme.status === 'shown' ? 'good' : data.theme.status === 'late' ? 'bad' : 'neutral'" />
      </div>
      <p class="mt-1 text-sm text-slate-600">{{ data.theme.definition }}</p>
      <div class="mt-3 flex flex-wrap gap-2">
        <StatChip label="Strength" :value="data.theme.strength_order ? `${score(data.theme.strength)} (#${data.theme.strength_order})` : '—'" />
        <StatChip label="Policy" :value="score(data.theme.policy_rank)" />
        <StatChip label="Capacity" :value="score(data.theme.capacity_rank)" />
        <StatChip label="Results" :value="score(data.theme.fundamentals_rank)" />
        <StatChip label="Price breadth" :value="score(data.theme.breadth_rank)" />
        <StatChip label="Members rising" :value="data.theme.breadth_stage2_pct === null ? '—' : `${Math.round(data.theme.breadth_stage2_pct)}%`" />
        <StatChip label="Members' sales YoY" :value="signed(data.theme.sales_yoy_pct)" />
      </div>
      <p v-if="data.theme.late_reasons" class="mt-3 rounded-md bg-rose-50 p-3 text-sm text-rose-700">Late: {{ data.theme.late_reasons }}</p>
      <p v-if="data.theme.reasoning" class="mt-3 text-sm text-slate-700">{{ data.theme.reasoning }}</p>
      <p v-if="data.theme.conclusion" class="mt-1 text-sm font-medium">{{ data.theme.conclusion }}</p>

      <h2 class="mt-8 text-sm font-semibold uppercase text-slate-500">Ranked ({{ ranked.length }})</h2>
      <div class="mt-2 overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table class="w-full text-sm">
          <thead class="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase text-slate-500">
            <tr>
              <th class="px-3 py-2">#</th><th class="px-3 py-2">Company</th><th class="px-3 py-2 text-right">Share</th>
              <th class="px-3 py-2">Stage</th><th class="px-3 py-2 text-right">From breakout</th>
              <th class="px-3 py-2 text-right">RS 26w</th><th class="px-3 py-2 text-right">Sales YoY</th>
              <th class="px-3 py-2 text-right">ROCE</th><th class="px-3 py-2 text-right">Ranks E·T·B·C</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="s in ranked" :key="s.company_master_id" class="border-b border-slate-100 last:border-0 align-top"
                :class="s.on_watchlist ? '' : 'text-slate-500'">
              <td class="px-3 py-2"><span class="mr-1 text-slate-400">{{ s.position }}</span>
                <BadgePill :label="s.rating ?? '—'" :tone="RATING_TONE[s.rating ?? ''] ?? 'neutral'" /></td>
              <td class="px-3 py-2"><div class="font-medium">{{ s.company_name }}</div>
                <div class="text-xs text-slate-500">{{ s.ticker }}</div>
                <div v-if="s.conclusion" class="mt-1 text-xs text-slate-700">{{ s.conclusion }}</div></td>
              <td class="px-3 py-2 text-right">{{ s.share_pct === null ? '—' : `${Math.round(s.share_pct)}%` }}<div v-if="s.share_source" class="text-xs text-slate-400">{{ s.share_source }}</div></td>
              <td class="px-3 py-2"><BadgePill :label="stageText(s.stage, s.weeks_in_stage, s.tier)" :tone="s.tier === 4 ? 'good' : STAGE_TONE[s.stage ?? 0]" /></td>
              <td class="px-3 py-2 text-right">{{ signed(s.from_breakout_pct) }}</td>
              <td class="px-3 py-2 text-right">{{ signed(s.rs26_pct) }}</td>
              <td class="px-3 py-2 text-right">{{ signed(s.sales_yoy_pct) }}</td>
              <td class="px-3 py-2 text-right">{{ s.roce_pct === null ? '—' : `${Math.round(s.roce_pct)}%` }}</td>
              <td class="px-3 py-2 text-right font-mono text-xs">{{ score(s.exposure_rank) }}·{{ score(s.trend_rank) }}·{{ score(s.business_rank) }}·{{ score(s.capacity_rank) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <h2 class="mt-8 text-sm font-semibold uppercase text-slate-500">In the theme, revenue share not known ({{ unquantified.length }})</h2>
      <p class="text-xs text-slate-500">Watch only: never ranked until a report gives the share.</p>
      <div class="mt-2 flex flex-wrap gap-2">
        <span v-for="s in unquantified" :key="s.company_master_id" class="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-500">
          {{ s.company_name }} · {{ stageText(s.stage, s.weeks_in_stage, s.tier) }}
        </span>
      </div>

      <h2 class="mt-8 text-sm font-semibold uppercase text-slate-500">Left out ({{ excluded.length }})</h2>
      <div class="mt-2 overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table class="w-full text-sm">
          <tbody>
            <tr v-for="s in excluded" :key="s.company_master_id" class="border-b border-slate-100 last:border-0 align-top">
              <td class="w-1/3 px-3 py-2"><div class="font-medium">{{ s.company_name }}</div><div class="text-xs text-slate-500">{{ s.ticker }}</div></td>
              <td class="px-3 py-2 text-xs text-slate-600">{{ s.reasons }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <h2 class="mt-8 text-sm font-semibold uppercase text-slate-500">Policy</h2>
      <div class="mt-2 overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table class="w-full text-sm">
          <tbody>
            <tr v-for="p in data.policy" :key="p.name" class="border-b border-slate-100 last:border-0 align-top">
              <td class="px-3 py-2"><BadgePill :label="p.direction" :tone="p.direction === 'support' ? 'good' : 'bad'" /></td>
              <td class="px-3 py-2"><a :href="p.source_url" target="_blank" rel="noopener" class="font-medium hover:underline">{{ p.name }}</a>
                <div v-if="p.latest_figure" class="text-xs text-slate-500">{{ p.latest_figure }}</div></td>
              <td class="px-3 py-2 text-xs text-slate-500">{{ p.status }} · {{ p.announced ?? '' }}</td>
              <td class="px-3 py-2 text-right text-xs">{{ p.outlay_cr ? `Rs ${Math.round(p.outlay_cr).toLocaleString('en-IN')} cr` : '' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>
</template>
