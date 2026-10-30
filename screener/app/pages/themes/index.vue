<script setup lang="ts">
import type { ThemesResponse, ThemeRow, ThemeStock } from '~/types/themes'

// Thematic watchlist (stockey docs/THEMATIC_WATCHLIST_PRD.md): the 5 strongest themes that are
// not late, 10 stocks each, ranked on four equal parts; refreshed every morning. Reporting only.

const api = useApi()
const { data, status, error } = await useAsyncData('themes', () => api.get<ThemesResponse>('/api/themes'))

const byStatus = (s: ThemeRow['status']) => computed(() => (data.value?.themes ?? []).filter(t => t.status === s))
const shown = byStatus('shown')
const nextUp = byStatus('next')
const late = byStatus('late')
const out = byStatus('out')
const unranked = byStatus('unranked')

const stocksByTheme = computed(() => {
  const m = new Map<string, ThemeStock[]>()
  for (const s of data.value?.stocks ?? []) m.set(s.theme_id, [...(m.get(s.theme_id) ?? []), s])
  return m
})

const parts: [keyof ThemeRow, string][] = [
  ['policy_rank', 'Policy'], ['capacity_rank', 'Capacity'], ['fundamentals_rank', 'Results'], ['breadth_rank', 'Price breadth'],
]
const open = ref<Record<string, boolean>>({})
function toggle(k: string) { open.value[k] = !open.value[k] }
</script>

<template>
  <div>
    <h1 class="text-xl font-semibold">Themes</h1>
    <p class="mt-1 text-sm text-slate-600">
      Real end markets with money flowing in. Each theme is ranked on four equal parts — government policy,
      capacity being built, members' latest results and how many members are rising in price — and the five
      strongest that are not <em>late</em> (more capacity coming than demand) are shown, ten stocks each.
      Stocks are ranked on revenue share in the theme, price trend (an early breakout ranks above an extended run),
      business results and capacity being built. Companies named in a SEBI order, with an auditor resignation,
      a default, promoter pledge over 10% or a story flaw are left out; so are weak businesses (return on capital
      under 8% or negative operating cash flow over three years) unless their quarterly losses are shrinking.
      <span v-if="data?.as_of" class="text-slate-500">As of {{ formatDate(data.as_of) }}.</span>
    </p>

    <div v-if="error" class="mt-6 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
      Could not reach the API ({{ error.message }})
    </div>
    <p v-else-if="status === 'pending'" class="mt-6 text-sm text-slate-500">Loading…</p>
    <div v-else-if="data && !data.themes.length" class="mt-6 text-sm text-slate-500">No theme list yet.</div>

    <template v-else-if="data">
      <section v-for="t in shown" :key="t.theme_id" class="mt-6 rounded-lg border border-slate-200 bg-white">
        <div class="border-b border-slate-100 p-4">
          <div class="flex flex-wrap items-baseline gap-3">
            <span class="text-sm font-semibold text-slate-400">#{{ t.position }}</span>
            <NuxtLink :to="`/themes/${t.theme_id}`" class="text-lg font-semibold hover:underline">{{ t.name }}</NuxtLink>
            <span class="text-sm text-slate-500">strength {{ score(t.strength) }}</span>
            <BadgePill v-if="t.kept_by_buffer" label="kept (still in top 8)" tone="neutral" />
          </div>
          <div class="mt-2 flex flex-wrap gap-2">
            <StatChip v-for="[k, label] in parts" :key="k" :label="label" :value="score(t[k] as number | null)" />
          </div>
          <p v-if="t.reasoning" class="mt-3 text-sm text-slate-700">{{ t.reasoning }}</p>
          <p v-if="t.conclusion" class="mt-1 text-sm font-medium text-slate-900">{{ t.conclusion }}</p>
        </div>
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead class="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase text-slate-500">
              <tr>
                <th class="px-3 py-2">#</th>
                <th class="px-3 py-2">Company</th>
                <th class="px-3 py-2 text-right">Share</th>
                <th class="px-3 py-2">Stage</th>
                <th class="px-3 py-2 text-right">From breakout</th>
                <th class="px-3 py-2 text-right">Sales YoY</th>
                <th class="px-3 py-2 text-right" title="Exposure / Trend / Business / Capacity, 0-100 within the theme">Ranks E·T·B·C</th>
              </tr>
            </thead>
            <tbody>
              <template v-for="s in stocksByTheme.get(t.theme_id) ?? []" :key="s.company_master_id">
                <tr class="cursor-pointer border-b border-slate-100 align-top hover:bg-slate-50" @click="toggle(t.theme_id + s.company_master_id)">
                  <td class="px-3 py-2">
                    <span class="mr-1 text-slate-400">{{ s.position }}</span>
                    <BadgePill :label="s.rating ?? '—'" :tone="RATING_TONE[s.rating ?? ''] ?? 'neutral'" />
                  </td>
                  <td class="px-3 py-2">
                    <div class="font-medium">{{ s.company_name }}</div>
                    <div class="text-xs text-slate-500">{{ s.ticker }}</div>
                    <div v-if="s.conclusion" class="mt-1 text-xs text-slate-700">{{ s.conclusion }}</div>
                  </td>
                  <td class="px-3 py-2 text-right">{{ s.share_pct === null ? '—' : `${Math.round(s.share_pct)}%` }}<div v-if="s.share_source" class="text-xs text-slate-400">{{ s.share_source }}</div></td>
                  <td class="px-3 py-2">
                    <BadgePill :label="stageText(s.stage, s.weeks_in_stage, s.tier)" :tone="s.tier === 4 ? 'good' : STAGE_TONE[s.stage ?? 0]" />
                  </td>
                  <td class="px-3 py-2 text-right">{{ signed(s.from_breakout_pct) }}</td>
                  <td class="px-3 py-2 text-right">{{ signed(s.sales_yoy_pct) }}</td>
                  <td class="px-3 py-2 text-right font-mono text-xs">
                    {{ score(s.exposure_rank) }}·{{ score(s.trend_rank) }}·{{ score(s.business_rank) }}·{{ score(s.capacity_rank) }}
                  </td>
                </tr>
                <tr v-if="open[t.theme_id + s.company_master_id]" class="border-b border-slate-100 bg-slate-50">
                  <td />
                  <td colspan="6" class="px-3 py-2 text-sm text-slate-700">
                    <p>{{ s.reasoning ?? 'No write-up yet (written overnight).' }}</p>
                    <p v-if="s.reasons" class="mt-1 text-xs text-slate-500">{{ s.reasons.replaceAll('note: ', '') }}</p>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
          <p v-if="!(stocksByTheme.get(t.theme_id) ?? []).length" class="p-4 text-sm text-slate-500">
            No company passes all the checks with a known revenue share yet.
          </p>
        </div>
      </section>

      <div class="mt-8 grid gap-4 md:grid-cols-2">
        <section class="rounded-lg border border-slate-200 bg-white p-4">
          <h2 class="text-sm font-semibold uppercase text-slate-500">Next up</h2>
          <ul class="mt-2 space-y-1 text-sm">
            <li v-for="t in nextUp" :key="t.theme_id" class="flex justify-between">
              <NuxtLink :to="`/themes/${t.theme_id}`" class="hover:underline">{{ t.name }}</NuxtLink>
              <span class="text-slate-500">strength {{ score(t.strength) }}</span>
            </li>
          </ul>
        </section>
        <section class="rounded-lg border border-slate-200 bg-white p-4">
          <h2 class="text-sm font-semibold uppercase text-slate-500">Late — more capacity than demand</h2>
          <ul class="mt-2 space-y-2 text-sm">
            <li v-for="t in late" :key="t.theme_id">
              <NuxtLink :to="`/themes/${t.theme_id}`" class="font-medium hover:underline">{{ t.name }}</NuxtLink>
              <span class="text-slate-500"> — {{ t.late_reasons }}</span>
            </li>
          </ul>
        </section>
        <section class="rounded-lg border border-slate-200 bg-white p-4">
          <h2 class="text-sm font-semibold uppercase text-slate-500">Other ranked themes</h2>
          <ul class="mt-2 space-y-1 text-sm">
            <li v-for="t in out" :key="t.theme_id" class="flex justify-between">
              <NuxtLink :to="`/themes/${t.theme_id}`" class="hover:underline">{{ t.name }}</NuxtLink>
              <span class="text-slate-500">#{{ t.strength_order }} · {{ score(t.strength) }}</span>
            </li>
          </ul>
        </section>
        <section class="rounded-lg border border-slate-200 bg-white p-4">
          <h2 class="text-sm font-semibold uppercase text-slate-500">Not ranked — under 5 listed members with results</h2>
          <ul class="mt-2 space-y-1 text-sm">
            <li v-for="t in unranked" :key="t.theme_id">
              <NuxtLink :to="`/themes/${t.theme_id}`" class="hover:underline">{{ t.name }}</NuxtLink>
              <span class="text-slate-500"> · {{ t.n_members ?? 0 }} members</span>
            </li>
          </ul>
        </section>
      </div>
    </template>
  </div>
</template>
