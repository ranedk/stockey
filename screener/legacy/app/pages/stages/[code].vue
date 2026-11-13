<script setup lang="ts">
import type { RotationIndustryResponse, StoryScoreRow } from '~/types/systrader'

const route = useRoute()
const code = computed(() => String(route.params.code))
const sys = useSystraderApi()
const api = useApi()
const { data, status, error } = await useAsyncData(() => `rotation-${code.value}`,
  () => sys.get<RotationIndustryResponse>(`/api/rotation/industry/${code.value}`), { watch: [code] })
const { data: stories } = await useAsyncData('story-scores', () => api.get<StoryScoreRow[]>('/api/story-scores').catch(() => []))
const storyBySymbol = computed(() => new Map((stories.value ?? []).map(s => [s.symbol, s])))
const storyMedian = computed(() => medianOf((stories.value ?? []).map(s => s.story_score).filter((v): v is number => v !== null)))

const liquidOnly = ref(true)
const stocks = computed(() => (data.value?.stocks ?? [])
  .filter(s => !liquidOnly.value || s.eligible)
  .sort((a, b) => (b.rs26_pct ?? -1e9) - (a.rs26_pct ?? -1e9)))
const FILTER_TONE = { pass: 'good', fail: 'bad', unknown: 'neutral' } as const
</script>

<template>
  <div>
    <NuxtLink to="/stages" class="text-sm text-slate-500 hover:text-slate-700">← Rotation</NuxtLink>
    <div v-if="error" class="mt-6 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
      Could not load this industry ({{ error.message }}).
    </div>
    <div v-else-if="status === 'pending'" class="mt-6 text-sm text-slate-500">Loading…</div>
    <template v-else-if="data">
      <h1 class="mt-2 text-xl font-semibold">{{ data.industry.name || data.industry.code }}</h1>
      <p class="text-sm text-slate-500">{{ data.industry.sector_name }} · week ending {{ formatDate(data.as_of) }}</p>
      <div class="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-5">
        <StatChip label="RS rank" :value="data.industry.rank ? `${data.industry.rank} (4w ago ${data.industry.rank_4w || '—'}, 13w ago ${data.industry.rank_13w || '—'})` : 'not ranked'" />
        <StatChip label="RS 26w vs market" :value="formatPct(data.industry.rs26_pct)" />
        <StatChip label="Industry stage" :value="stageShort(data.industry.stage)" />
        <StatChip label="Liquid members in Stage 2" :value="`${data.industry.stage2_pct}%`" />
        <StatChip label="Leading" :value="data.industry.leading ? `yes, ${data.industry.weeks_leading} weeks` : 'no'" />
      </div>

      <label class="mt-6 flex items-center gap-1.5 text-sm text-slate-600">
        <input v-model="liquidOnly" type="checkbox"> liquid members only (Rs 10 cr+ a day)
      </label>
      <div class="mt-2 overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table class="w-full text-sm">
          <thead class="border-b border-slate-200 bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
            <tr>
              <th class="px-3 py-2">Stock</th>
              <th class="px-3 py-2">Stage</th>
              <th class="px-3 py-2 text-right">RS 26w</th>
              <th class="px-3 py-2 text-right">Weeks in S2</th>
              <th class="px-3 py-2 text-right">Above 30w MA</th>
              <th class="px-3 py-2 text-right">MA slope 4w</th>
              <th class="px-3 py-2 text-right">Volume 10w</th>
              <th class="px-3 py-2 text-right">Close</th>
              <th class="px-3 py-2 text-right">Story</th>
              <th class="px-3 py-2">Filter</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="s in stocks" :key="s.symbol" class="border-b border-slate-100 last:border-0 hover:bg-slate-50"
                :class="s.candidate ? 'bg-emerald-50/50' : ''">
              <td class="px-3 py-2 font-medium">{{ s.symbol }}<BadgePill v-if="s.candidate" class="ml-1" label="candidate" tone="good" /></td>
              <td class="px-3 py-2"><BadgePill :label="stageShort(s.stage)" :tone="STAGE_TONE[s.stage]" /></td>
              <td class="px-3 py-2 text-right">{{ formatPct(s.rs26_pct) }}</td>
              <td class="px-3 py-2 text-right">{{ s.weeks_in_stage2 || '—' }}</td>
              <td class="px-3 py-2 text-right">{{ formatPct(s.above_ma30_pct) }}</td>
              <td class="px-3 py-2 text-right">{{ formatPct(s.slope_pct) }}</td>
              <td class="px-3 py-2 text-right">{{ s.volume_ratio === null ? '—' : `${s.volume_ratio.toFixed(2)}x` }}</td>
              <td class="px-3 py-2 text-right">{{ formatPrice(s.close) }}</td>
              <td class="px-3 py-2 text-right">{{ storyBySymbol.get(s.symbol)?.story_score != null ? Math.round(storyBySymbol.get(s.symbol)!.story_score!) : '—' }}</td>
              <td class="px-3 py-2">
                <BadgePill :label="storyFilter(storyBySymbol.get(s.symbol), storyMedian)"
                           :tone="FILTER_TONE[storyFilter(storyBySymbol.get(s.symbol), storyMedian)]" />
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>
</template>
