<script setup lang="ts">
import type { RotationResponse, RotationIndustry, StoryScoreRow } from '~/types/systrader'

// Industry rotation, relative strength and early Stage 2 (systrade/docs/SECTOR_ROTATION_PRD.md).
// Replaces the plain stage listing (2026-10-01). Reporting only: the strategy built on it is
// pre-registered and must pass its test before anything trades on it.

const sys = useSystraderApi()
const api = useApi()
const { data, status, error } = await useAsyncData('rotation', () => sys.get<RotationResponse>('/api/rotation'))
const { data: stories } = await useAsyncData('story-scores', () => api.get<StoryScoreRow[]>('/api/story-scores').catch(() => []))

const leadersOnly = ref(false)
const sector = ref('all')
const filterMode = ref<'all' | 'pass'>('all')
const search = ref('')

const storyBySymbol = computed(() => new Map((stories.value ?? []).map(s => [s.symbol, s])))
const storyMedian = computed(() => medianOf((stories.value ?? []).map(s => s.story_score).filter((v): v is number => v !== null)))
const industryByCode = computed(() => new Map((data.value?.industries ?? []).map(i => [i.code, i])))

const sectors = computed(() => {
  const seen = new Map<string, string>()
  for (const i of data.value?.industries ?? []) seen.set(i.sector_code, i.sector_name || i.sector_code)
  return [...seen.entries()].sort((a, b) => a[1].localeCompare(b[1]))
})

const industries = computed(() => (data.value?.industries ?? []).filter(i =>
  (!leadersOnly.value || i.leading) && (sector.value === 'all' || i.sector_code === sector.value)))

const leadingCount = computed(() => (data.value?.industries ?? []).filter(i => i.leading).length)

const candidates = computed(() => {
  const q = search.value.trim().toUpperCase()
  return (data.value?.stocks ?? []).filter((s) => {
    if (q && !s.symbol.includes(q)) return false
    if (filterMode.value === 'pass' && storyFilter(storyBySymbol.value.get(s.symbol), storyMedian.value) !== 'pass') return false
    return true
  })
})

function moveLabel(i: RotationIndustry, before: number): string {
  const m = rankMove(i.rank, before)
  if (m === null) return '—'
  if (m === 0) return '='
  return m > 0 ? `▲${m}` : `▼${-m}`
}
function moveClass(i: RotationIndustry, before: number): string {
  const m = rankMove(i.rank, before)
  if (!m) return 'text-slate-400'
  return m > 0 ? 'text-emerald-600' : 'text-rose-600'
}
// rank history sparkline: rank 1 at the top
function sparkPath(h: number[], ranked: number): string {
  const pts = h.map((r, k) => ({ x: (k / Math.max(h.length - 1, 1)) * 100, r }))
    .filter(p => p.r > 0).map(p => `${p.x.toFixed(1)},${((p.r - 1) / Math.max(ranked - 1, 1) * 24 + 2).toFixed(1)}`)
  return pts.length ? `M${pts.join(' L')}` : ''
}
const rankedCount = computed(() => (data.value?.industries ?? []).filter(i => i.rank > 0).length)
function weeks(v: number | null | undefined): string { return v === null || v === undefined ? '—' : `${Math.round(v)}w` }
const FILTER_TONE = { pass: 'good', fail: 'bad', unknown: 'neutral' } as const
</script>

<template>
  <div>
    <h1 class="text-xl font-semibold">Rotation</h1>
    <p class="mt-1 max-w-3xl text-sm text-slate-600">
      Top-down, weekly: is the market healthy, which industries lead on relative strength (26-week
      return against the market) while their own index is in Weinstein Stage 2, and which liquid
      Stage 2 stocks lead inside them. Reporting only: the strategy built on this failed its
      pre-registered test (LEDGER row 53, 2013-2021) -- buying only inside the leading industries was
      calmer but earned less than plain Stage 2 -- so nothing trades on this page. Plain Stage 2
      ranked by relative strength runs as its own forward paper track:
      <NuxtLink to="/paper/stage2-rs-leaders" class="font-medium text-slate-900 underline">stage2-rs-leaders</NuxtLink>.
    </p>

    <div v-if="error" class="mt-6 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
      Could not load the rotation snapshot ({{ error.message }}).
    </div>
    <div v-else-if="status === 'pending'" class="mt-6 text-sm text-slate-500">Loading…</div>

    <template v-else-if="data">
      <div class="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-5">
        <StatChip label="Market (equal-weight, Rs 10 cr+)" :value="data.market.stage_label" />
        <StatChip label="Breadth: liquid stocks in Stage 2" :value="`${data.market.breadth_pct}% of ${data.market.eligible_count}`" />
        <StatChip label="Market 26-week return" :value="formatPct(data.market.return_26w_pct)" />
        <StatChip label="Leading industries" :value="`${leadingCount} of ${rankedCount}`" />
        <StatChip label="Week ending" :value="formatDate(data.as_of)" />
      </div>

      <div class="mt-3 rounded-lg border border-slate-200 bg-slate-50 px-4 py-3 text-xs text-slate-600">
        <span class="font-medium text-slate-700">How long things last</span> (since {{ data.stats.from }}):
        a Stage 2 run lasts {{ weeks(data.stats.stage2_weeks.p50) }} at the median, {{ weeks(data.stats.stage2_weeks.mean) }} on average,
        and a tenth run {{ weeks(data.stats.stage2_weeks.p90) }}+ ({{ data.stats.stage2_runs }} runs) ·
        an industry stays in the top fifth {{ weeks(data.stats.top_fifth_weeks.p50) }} at the median, {{ weeks(data.stats.top_fifth_weeks.mean) }} on average,
        a tenth {{ weeks(data.stats.top_fifth_weeks.p90) }}+ · about {{ data.stats.new_leaders_per_quarter?.toFixed(0) }} new leaders a quarter.
        Most moves are short; a few run long -- weekly trading on this would mostly chase noise.
      </div>

      <div class="mt-6 flex flex-wrap items-center gap-3">
        <h2 class="text-base font-semibold">Industries</h2>
        <label class="flex items-center gap-1.5 text-sm text-slate-600">
          <input v-model="leadersOnly" type="checkbox"> leaders only
        </label>
        <select v-model="sector" class="rounded-md border border-slate-200 px-2 py-1 text-sm">
          <option value="all">All sectors</option>
          <option v-for="[code, name] in sectors" :key="code" :value="code">{{ name }}</option>
        </select>
      </div>
      <div class="mt-2 overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table class="w-full text-sm">
          <thead class="border-b border-slate-200 bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
            <tr>
              <th class="px-3 py-2 text-right">Rank</th>
              <th class="px-3 py-2">Industry</th>
              <th class="px-3 py-2 text-right">RS 26w</th>
              <th class="px-3 py-2 text-right">vs 4w</th>
              <th class="px-3 py-2 text-right">vs 13w</th>
              <th class="px-3 py-2">26w rank</th>
              <th class="px-3 py-2">Stage</th>
              <th class="px-3 py-2 text-right">Members in S2</th>
              <th class="px-3 py-2 text-right">Liquid / all</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="i in industries" :key="i.code" class="border-b border-slate-100 last:border-0 hover:bg-slate-50"
                :class="i.leading ? 'bg-emerald-50/50' : ''">
              <td class="px-3 py-2 text-right font-medium">{{ i.rank || '—' }}</td>
              <td class="px-3 py-2">
                <NuxtLink :to="`/stages/${i.code}`" class="font-medium hover:underline">{{ i.name || i.code }}</NuxtLink>
                <span class="ml-1 text-xs text-slate-400">{{ i.sector_name }}</span>
                <BadgePill v-if="i.leading" class="ml-1" :label="`leading ${i.weeks_leading}w`" tone="good" />
              </td>
              <td class="px-3 py-2 text-right">{{ formatPct(i.rs26_pct) }}</td>
              <td class="px-3 py-2 text-right" :class="moveClass(i, i.rank_4w)">{{ moveLabel(i, i.rank_4w) }}</td>
              <td class="px-3 py-2 text-right" :class="moveClass(i, i.rank_13w)">{{ moveLabel(i, i.rank_13w) }}</td>
              <td class="px-3 py-2">
                <svg viewBox="0 0 100 28" class="h-6 w-24"><path :d="sparkPath(i.rank_history, rankedCount)" fill="none" stroke="currentColor" stroke-width="1.5" class="text-slate-500" /></svg>
              </td>
              <td class="px-3 py-2"><BadgePill :label="stageShort(i.stage)" :tone="STAGE_TONE[i.stage]" /></td>
              <td class="px-3 py-2 text-right">{{ i.stage2_pct }}%</td>
              <td class="px-3 py-2 text-right text-slate-500">{{ i.eligible_members }} / {{ i.members }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="mt-8 flex flex-wrap items-center gap-3">
        <h2 class="text-base font-semibold">Candidates</h2>
        <span class="text-xs text-slate-500">liquid Stage 2 stocks in leading industries, strongest first</span>
        <select v-model="filterMode" class="rounded-md border border-slate-200 px-2 py-1 text-sm">
          <option value="all">Any fundamentals</option>
          <option value="pass">Fundamental filter: pass only</option>
        </select>
        <input v-model="search" type="text" placeholder="Filter by ticker…"
               class="ml-auto rounded-md border border-slate-200 px-3 py-1.5 text-sm focus:border-slate-400 focus:outline-none">
      </div>
      <p class="mt-1 text-xs text-slate-500">
        Fundamental filter (stockey): no flaw and a story score at or above the median ({{ storyMedian?.toFixed(0) ?? '—' }}).
        It cannot be backtested -- story scores exist only from 2026-09-29 -- so it is tested forward only.
      </p>
      <div class="mt-2 overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table class="w-full text-sm">
          <thead class="border-b border-slate-200 bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
            <tr>
              <th class="px-3 py-2">Stock</th>
              <th class="px-3 py-2">Industry</th>
              <th class="px-3 py-2 text-right">RS 26w</th>
              <th class="px-3 py-2 text-right">Rank in industry</th>
              <th class="px-3 py-2 text-right">Weeks in S2</th>
              <th class="px-3 py-2 text-right">Above 30w MA</th>
              <th class="px-3 py-2 text-right">Volume 10w</th>
              <th class="px-3 py-2 text-right">Story</th>
              <th class="px-3 py-2">Filter</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="s in candidates" :key="s.symbol" class="border-b border-slate-100 last:border-0 hover:bg-slate-50">
              <td class="px-3 py-2 font-medium">{{ s.symbol }}</td>
              <td class="px-3 py-2 text-slate-600">
                <NuxtLink :to="`/stages/${s.industry_code}`" class="hover:underline">{{ industryByCode.get(s.industry_code)?.name || s.industry_code }}</NuxtLink>
              </td>
              <td class="px-3 py-2 text-right">{{ formatPct(s.rs26_pct) }}</td>
              <td class="px-3 py-2 text-right">{{ s.rank_in_industry }}</td>
              <td class="px-3 py-2 text-right">{{ s.weeks_in_stage2 }}</td>
              <td class="px-3 py-2 text-right">{{ formatPct(s.above_ma30_pct) }}</td>
              <td class="px-3 py-2 text-right">{{ s.volume_ratio === null ? '—' : `${s.volume_ratio.toFixed(2)}x` }}</td>
              <td class="px-3 py-2 text-right">
                <template v-if="storyBySymbol.get(s.symbol)?.story_score !== undefined && storyBySymbol.get(s.symbol)?.story_score !== null">
                  {{ Math.round(storyBySymbol.get(s.symbol)!.story_score!) }}
                  <span class="text-xs text-slate-400">{{ storyBySymbol.get(s.symbol)?.primary_dimension?.replace('_', ' ') }}</span>
                </template>
                <span v-else class="text-slate-400">—</span>
              </td>
              <td class="px-3 py-2">
                <BadgePill :label="storyFilter(storyBySymbol.get(s.symbol), storyMedian)"
                           :tone="FILTER_TONE[storyFilter(storyBySymbol.get(s.symbol), storyMedian)]" />
                <span v-if="storyBySymbol.get(s.symbol)?.flaws" class="ml-1 text-xs text-rose-600">{{ storyBySymbol.get(s.symbol)?.flaws }}</span>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="!candidates.length" class="p-4 text-sm text-slate-500">No candidates match.</div>
      </div>
    </template>
  </div>
</template>
