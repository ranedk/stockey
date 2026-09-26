<script setup lang="ts">
import type { SectorInfo } from '~/types/api'

const api = useApi()
const { data, status, error } = await useAsyncData('sectors', () => api.get<SectorInfo[]>('/api/sectors'))

const phaseTone: Record<string, 'good' | 'bad' | 'neutral'> = {
  capacity_discipline: 'good',
  capacity_expansion: 'bad',
  balanced: 'neutral',
}

const GROWTH_LABELS: Record<string, string> = {
  high_growth: 'High growth',
  medium_growth: 'Medium growth',
  low_growth: 'Low growth',
  no_pattern: 'No pattern',
}
const growthTone: Record<string, 'good' | 'bad' | 'neutral' | 'warn'> = {
  high_growth: 'good',
  medium_growth: 'neutral',
  low_growth: 'warn',
  no_pattern: 'neutral',
}
</script>

<template>
  <div>
    <h1 class="text-xl font-semibold">Sectors</h1>
    <p class="mt-1 text-sm text-slate-600">
      Capacity vs demand growth by sector (capital-cycle phase, L1 universe only), and
      which watchlist companies sit in each one. Descriptive only -- not a ranked
      recommendation.
    </p>
    <PipelineNote layer="L1" title="Where the sector view comes from">
      Capital-cycle phase per sector, over L1-universe companies only. For each company, gross-block
      growth (last year vs the year before) is read against the same company's annual sales growth;
      the phase is the sector's median gap. Capacity well behind demand reads as discipline, well
      ahead as expansion. The phase is one of the five confluence axes on the watchlist, so it
      does feed the portfolio's entry rule; it does not rank sectors.
    </PipelineNote>
    <p class="mt-1 text-xs text-slate-400">
      Financial Services, IT and Realty get no phase: their gross block is offices or land, not
      operating capacity. Their growth badge still shows.
    </p>

    <div v-if="error" class="mt-6 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
      Could not reach the API ({{ error.message }}).
    </div>

    <div v-else-if="data" class="mt-4 grid gap-3">
      <div v-for="sector in data" :key="sector.sector_code" class="rounded-lg border border-slate-200 bg-white p-4">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div class="flex items-center gap-2">
            <h2 class="font-medium">{{ sector.sector_name || sector.sector_code }}</h2>
            <BadgePill v-if="sector.phase" :label="sector.phase" :tone="phaseTone[sector.phase] || 'neutral'" />
            <BadgePill
              v-if="sector.growth_classification"
              :label="GROWTH_LABELS[sector.growth_classification] || sector.growth_classification"
              :tone="growthTone[sector.growth_classification] || 'neutral'"
            />
            <BadgePill v-if="sector.sample_size_confidence === 'low'" label="low sample size" tone="warn" />
          </div>
          <div class="text-xs text-slate-500">
            capacity {{ formatPct(sector.capacity_growth_pct) }} · sales {{ formatPct(sector.demand_growth_pct) }}
            <template v-if="sector.capacity_minus_demand_pts != null"> · gap {{ sector.capacity_minus_demand_pts.toFixed(1) }} pts</template>
            · {{ sector.n_companies_with_demand_data ?? 0 }} of {{ sector.n_companies_in_l1 }} L1 companies
          </div>
        </div>

        <div v-if="sector.watched_companies.length" class="mt-3 grid gap-2 sm:grid-cols-2">
          <NuxtLink
            v-for="wc in sector.watched_companies"
            :key="wc.company_master_id"
            :to="`/watchlist/${encodeURIComponent(wc.company_master_id)}`"
            class="rounded border border-slate-100 bg-slate-50 p-2 text-xs hover:border-slate-300"
          >
            <span class="font-medium text-slate-800">{{ wc.company_master_id.replace('nse:', '') }}</span>
            <span class="ml-1 text-slate-400">({{ wc.alert_count }} events)</span>
            <p v-if="wc.narrative_snippet" class="mt-1 line-clamp-2 text-slate-500">{{ wc.narrative_snippet }}</p>
          </NuxtLink>
        </div>
        <p v-else class="mt-2 text-xs text-slate-400">No watchlist companies in this sector.</p>
      </div>
    </div>

    <p v-else-if="status === 'pending'" class="mt-6 text-sm text-slate-500">Loading…</p>
  </div>
</template>
