<script setup lang="ts">
import type { WatchlistDetail } from '~/types/watchlist'

const route = useRoute()
const id = computed(() => decodeURIComponent(String(route.params.id)))
const { data, status, error } = useStockey<WatchlistDetail>(() => `/api/watchlist/${encodeURIComponent(id.value)}`)
const w = computed(() => data.value?.watchlist)
const symbol = computed(() => wlSymbol(id.value))
useHead({ title: () => w.value?.company_name || wlTicker(id.value) })

// Positions are opened by the nightly rules and resolved unattended: this is a read-only view.
const openPosition = computed(() => data.value?.portfolio.find(t => t.status === 'open' && t.entry_decision !== 'reject') ?? null)
const vetoed = computed(() => data.value?.portfolio.find(t => t.entry_decision === 'reject') ?? null)
const resolved = computed(() => (data.value?.portfolio ?? []).filter(t => t.resolution_date !== null))

// The detail payload's watchlist row carries no current price; the latest close comes with technicals.
const lastPrice = computed(() => w.value?.current_price ?? (data.value?.technicals as any)?.close ?? null)
const lastAsOf = computed(() => (data.value?.technicals as any)?.as_of_date ?? null)
const priceRet = computed(() => (w.value ? wlChange(w.value.first_seen_price, lastPrice.value) : null))
const strategies = computed(() => (data.value?.signal_pointers ?? []).filter(p => p.signal_type === 'strategy_satisfied'))
const signals = computed(() => (data.value?.signal_pointers ?? []).filter(p => p.signal_type !== 'strategy_satisfied'))

const DIR: Record<string, { color: 'success' | 'error' | 'neutral', icon: string }> = {
  increasing: { color: 'success', icon: 'i-lucide-arrow-up-right' }, new: { color: 'success', icon: 'i-lucide-sparkle' },
  up: { color: 'success', icon: 'i-lucide-arrow-up-right' }, buy: { color: 'success', icon: 'i-lucide-arrow-up-right' },
  decreasing: { color: 'error', icon: 'i-lucide-arrow-down-right' }, down: { color: 'error', icon: 'i-lucide-arrow-down-right' },
  sell: { color: 'error', icon: 'i-lucide-arrow-down-right' }, flat: { color: 'neutral', icon: 'i-lucide-arrow-right' },
}

const l2 = computed(() => (data.value?.l2_state ?? {}) as Record<string, any>)
const tx = computed(() => (data.value?.technicals ?? {}) as Record<string, any>)
const sec = computed(() => data.value?.sector_context as Record<string, any> | null | undefined)
const PHASE: Record<string, { label: string, color: 'success' | 'error' | 'neutral', desc: string }> = {
  capacity_discipline: { label: 'Capacity discipline', color: 'success', desc: 'Demand is growing faster than the sector is adding capacity: good for prices and margins.' },
  capacity_expansion: { label: 'Capacity expansion', color: 'error', desc: 'The sector is adding capacity faster than demand grows: pressure on prices and margins.' },
  balanced: { label: 'Balanced', color: 'neutral', desc: 'Capacity and demand are growing at similar rates.' },
}
const signedCr = (v: any) => (isNum(v) ? `${v > 0 ? '+' : v < 0 ? '−' : ''}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 1 })} Cr` : '—')
const secMax = computed(() => Math.max(1, Math.abs(sec.value?.capacity_growth_pct ?? 0), Math.abs(sec.value?.demand_growth_pct ?? 0)))
</script>

<template>
  <UDashboardPanel id="watchlist-detail">
    <template #header>
      <PageNavbar :title="w ? wlTicker(w.company_master_id) : 'Watchlist'" icon="i-lucide-list-checks">
        <template #right>
          <UButton to="/watchlist" size="xs" color="neutral" variant="ghost" icon="i-lucide-arrow-left" label="Watchlist" />
        </template>
      </PageNavbar>
    </template>

    <template #body>
      <div class="mx-auto w-full max-w-7xl space-y-4">
        <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert"
          :title="`Could not load ${id}`" :description="(error as any).statusCode === 404 ? 'This company is not on the watchlist.' : error.message" />
        <div v-else-if="!data && status === 'pending'" class="space-y-4">
          <USkeleton class="h-32" /><USkeleton class="h-48" /><USkeleton class="h-64" />
        </div>

        <template v-else-if="data && w">
          <!-- header -->
          <UCard>
            <div class="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
              <div class="min-w-0 space-y-2">
                <div class="flex flex-wrap items-center gap-2">
                  <h1 class="font-mono text-2xl font-semibold text-highlighted">{{ wlTicker(w.company_master_id) }}</h1>
                  <UTooltip :text="WL_STATUS[w.status].desc">
                    <UBadge :color="WL_STATUS[w.status].color" variant="subtle" :label="WL_STATUS[w.status].label" />
                  </UTooltip>
                  <UButton v-if="symbol" :to="`/company/${encodeURIComponent(symbol)}`" size="xs" color="neutral" variant="outline" icon="i-lucide-building-2" trailing-icon="i-lucide-arrow-up-right" label="Company page" />
                </div>
                <p class="text-muted">{{ w.company_name || data.l2_state?.company_name }}</p>
                <p v-if="w.status_reason" class="text-sm text-muted"><UIcon name="i-lucide-info" class="mr-1 inline size-3.5 align-[-2px]" />{{ w.status_reason }}</p>
                <div class="flex flex-wrap gap-1.5">
                  <UTooltip v-if="isNum(w.story_score)" :text="`Story score ${w.story_score.toFixed(1)} of 100: how strong the fundamental case is. 80+ qualifies.`">
                    <UBadge :color="w.story_score >= 80 ? 'success' : 'neutral'" variant="subtle" icon="i-lucide-book-open" :label="`Story ${Math.round(w.story_score)}${wlDimension(w.primary_dimension) ? ' · ' + wlDimension(w.primary_dimension) : ''}`" />
                  </UTooltip>
                  <UTooltip v-if="w.entry_basis && WL_ENTRY_BASIS[w.entry_basis]" :text="WL_ENTRY_BASIS[w.entry_basis]!.desc">
                    <UBadge color="neutral" variant="outline" :label="`Via ${WL_ENTRY_BASIS[w.entry_basis]!.label.toLowerCase()}`" />
                  </UTooltip>
                  <WatchlistConfluenceBadge v-if="data.confluence" :supportive="data.confluence.confluence_count" :contradicting="data.confluence.contradicting_count" :evaluable="data.confluence.evaluable_count" size="md" />
                </div>
                <div v-if="strategies.length" class="flex flex-wrap items-center gap-1.5">
                  <span class="text-xs text-muted">Event rules it meets:</span>
                  <NuxtLink v-for="p in strategies" :key="String(p.value)" :to="`/strategies/${encodeURIComponent(String(p.value))}`">
                    <UBadge :color="wlEventColor(String(p.value))" variant="subtle" size="sm" trailing-icon="i-lucide-arrow-up-right" :label="wlEventLabel(String(p.value))" />
                  </NuxtLink>
                </div>
              </div>
              <div class="shrink-0 rounded-lg bg-elevated/60 p-3 md:min-w-56 md:text-right">
                <div class="text-xs text-muted">Price since added</div>
                <div class="num mt-1 text-sm text-muted">{{ inr(w.first_seen_price) }} → <span class="text-lg font-semibold text-highlighted">{{ inr(lastPrice) }}</span></div>
                <div class="mt-1"><Change :value="priceRet" pill class="text-sm" /></div>
                <div v-if="lastAsOf" class="text-[11px] text-dimmed">close of {{ dateShort(lastAsOf) }}</div>
                <div class="mt-2 text-xs text-dimmed">added {{ dateShort(w.first_seen_at) }}</div>
                <div v-if="w.suggested_watch_until" class="text-xs text-dimmed">watch until {{ dateShort(w.suggested_watch_until) }}</div>
              </div>
            </div>
          </UCard>

          <div class="grid items-start gap-4 lg:grid-cols-3">
            <div class="space-y-4 lg:col-span-2">
              <!-- why -->
              <UCard>
                <template #header>
                  <div class="flex items-center gap-2"><UIcon name="i-lucide-message-square-quote" class="size-4 text-primary" /><h3 class="font-semibold text-highlighted">Why this is on the watchlist</h3></div>
                </template>
                <p v-if="w.narrative_text" class="whitespace-pre-line leading-relaxed text-default">{{ w.narrative_text }}</p>
                <p v-else class="text-sm italic text-dimmed">Summary not written yet (the nightly summary job has not covered this company).</p>
                <p v-if="w.narrative_generated_at" class="mt-3 text-xs text-dimmed">Written by an AI model{{ w.narrative_model ? ` (${w.narrative_model})` : '' }} on {{ dateShort(w.narrative_generated_at) }}.</p>
              </UCard>

              <WatchlistChecks :checks="data.checks" :flaws="w.flaws" />

              <!-- portfolio -->
              <UCard>
                <template #header>
                  <div class="flex items-center gap-2"><UIcon name="i-lucide-briefcase" class="size-4 text-primary" /><h3 class="font-semibold text-highlighted">Portfolio</h3></div>
                  <p class="text-xs text-muted">Decided by the nightly rules; nothing here is added by hand.</p>
                </template>

                <div v-if="openPosition" class="space-y-3">
                  <div class="flex flex-wrap gap-1.5">
                    <UBadge color="success" variant="subtle" icon="i-lucide-circle-check" label="Open position" />
                    <UBadge v-if="openPosition.kind === 'shadow'" color="neutral" variant="outline" label="No money committed" />
                  </div>
                  <p v-if="openPosition.prediction_text" class="text-sm text-highlighted">{{ openPosition.prediction_text }}</p>
                  <div class="grid grid-cols-2 gap-3 text-xs sm:grid-cols-3">
                    <div><div class="text-dimmed">Target date</div><div class="num mt-0.5 text-default">{{ dateShort(openPosition.target_date) }}</div></div>
                    <div><UTooltip text="The position closes if the price falls this far below where it was bought"><div class="text-dimmed underline decoration-dotted underline-offset-2">Stop-loss</div></UTooltip><div class="num mt-0.5 text-default">{{ isNum(openPosition.stop_pct) ? `${openPosition.stop_pct}% below entry` : '—' }}</div></div>
                    <div class="col-span-2 sm:col-span-1">
                      <div class="text-dimmed">How it is graded</div>
                      <div class="mt-0.5 text-default">
                        <code v-if="openPosition.metric_name" class="rounded bg-elevated px-1 py-0.5 font-mono text-[11px]">{{ openPosition.metric_name }} {{ openPosition.metric_operator }} {{ openPosition.metric_threshold }}</code>
                        <span v-else>No checkable number; will be judged</span>
                      </div>
                    </div>
                  </div>
                  <p v-if="openPosition.invalidation_criteria" class="text-xs text-muted"><span class="font-medium">Wrong if:</span> {{ openPosition.invalidation_criteria }}</p>
                  <p v-if="openPosition.adjudicator_reason" class="rounded-lg bg-elevated/50 p-3 text-xs text-muted"><span class="font-medium text-default">AI reviewer:</span> {{ openPosition.adjudicator_reason }}</p>
                  <WatchlistSizing :company-master-id="w.company_master_id" />
                </div>

                <div v-else-if="vetoed" class="space-y-2">
                  <UBadge color="warning" variant="subtle" icon="i-lucide-ban" label="Vetoed by the AI reviewer" />
                  <p class="text-xs text-muted">The fixed rules passed this company and the AI reviewer rejected it. It is tracked anyway, so the veto itself can be scored.</p>
                  <p v-if="vetoed.adjudicator_reason" class="text-sm text-default">{{ vetoed.adjudicator_reason }}</p>
                </div>

                <p v-else class="text-sm text-muted">Not in the portfolio. The nightly rules decide this; there is nothing to add by hand.</p>

                <div v-if="resolved.length" class="mt-4 border-t border-default pt-3">
                  <div class="text-xs font-medium text-muted">Graded forecasts</div>
                  <ul class="mt-2 space-y-1.5 text-xs">
                    <li v-for="t in resolved" :key="t.position_id" class="flex flex-wrap items-center gap-2">
                      <UBadge :color="t.resolved_true ? 'success' : 'error'" variant="soft" size="sm" :label="t.resolved_true ? 'Came true' : 'Did not come true'" />
                      <span class="num text-muted">{{ dateShort(t.opened_at) }} → {{ dateShort(t.resolution_date) }}</span>
                      <span v-if="t.failure_attribution" class="text-muted">({{ t.failure_attribution }})</span>
                      <span class="text-dimmed">· {{ t.resolution_method }}</span>
                    </li>
                  </ul>
                </div>
              </UCard>

              <!-- evidence -->
              <UCard>
                <template #header>
                  <div class="flex items-center gap-2"><UIcon name="i-lucide-history" class="size-4 text-primary" /><h3 class="font-semibold text-highlighted">Evidence trail</h3></div>
                  <p class="text-xs text-muted">Every event that led here, newest first</p>
                </template>
                <WatchlistEvidence :alerts="data.alerts" />
              </UCard>
            </div>

            <div class="space-y-4">
              <WatchlistConfluencePanel :confluence="data.confluence" />

              <!-- context facts -->
              <UCard>
                <template #header>
                  <h3 class="font-semibold text-highlighted">Context</h3>
                  <p class="text-xs text-muted">Facts for reading, not signals</p>
                </template>
                <div class="space-y-4">
                  <div>
                    <div class="mb-2 text-xs font-medium uppercase tracking-wide text-dimmed">Balance sheet and owners</div>
                    <dl class="grid grid-cols-2 gap-x-3 gap-y-3 text-sm">
                      <div><dt class="text-xs text-muted">Net debt change, 1 year</dt><dd class="num font-medium" :class="isNum(l2.net_debt_yoy_delta_rscr) ? (l2.net_debt_yoy_delta_rscr > 0 ? 'text-down' : 'text-up') : ''">{{ signedCr(l2.net_debt_yoy_delta_rscr) }}</dd></div>
                      <div><dt class="text-xs text-muted">Promoter stake trend</dt><dd class="font-medium capitalize">{{ l2.promoter_stake_direction ?? '—' }}</dd></div>
                      <div><UTooltip text="Share of promoter shares pledged as loan collateral"><dt class="text-xs text-muted underline decoration-dotted underline-offset-2">Promoter shares pledged</dt></UTooltip><dd class="num font-medium">{{ isNum(l2.pledge_pct) ? `${l2.pledge_pct}%` : '—' }}</dd></div>
                      <div><UTooltip text="Operating profit divided by interest cost: how many times over it can pay its interest"><dt class="text-xs text-muted underline decoration-dotted underline-offset-2">Interest cover</dt></UTooltip><dd class="num font-medium">{{ mult(l2.interest_coverage) }}</dd></div>
                    </dl>
                  </div>
                  <div class="border-t border-default pt-4">
                    <div class="mb-2 text-xs font-medium uppercase tracking-wide text-dimmed">Price and trading</div>
                    <dl class="grid grid-cols-2 gap-x-3 gap-y-3 text-sm">
                      <div><dt class="text-xs text-muted">3-month return</dt><dd><Change :value="wlFrac(tx.return_3m_pct)" /></dd></div>
                      <div><dt class="text-xs text-muted">12-month return</dt><dd><Change :value="wlFrac(tx.return_12m_pct)" /></dd></div>
                      <div><dt class="text-xs text-muted">vs 200-day average</dt><dd><Change :value="wlFrac(tx.pct_vs_dma_200)" /></dd></div>
                      <div><UTooltip text="How far the price sits from its 200-day mean, in standard deviations. Beyond ±2 is unusually stretched."><dt class="text-xs text-muted underline decoration-dotted underline-offset-2">Stretch from 200-day mean</dt></UTooltip><dd class="num font-medium">{{ isNum(tx.z_score_vs_200d_mean) ? `${tx.z_score_vs_200d_mean > 0 ? '+' : ''}${tx.z_score_vs_200d_mean} σ` : '—' }}</dd></div>
                      <div>
                        <UTooltip text="Share of traded shares actually taken into demat accounts (not intraday trades), last 20 days, against its 1-year median"><dt class="text-xs text-muted underline decoration-dotted underline-offset-2">Delivery share, 20 days</dt></UTooltip>
                        <dd class="num font-medium">{{ isNum(tx.delivery_pct_20d) ? `${tx.delivery_pct_20d}%` : '—' }}<span v-if="isNum(tx.delivery_change_pp)" class="ml-1 text-xs" :class="signClass(tx.delivery_change_pp)">{{ tx.delivery_change_pp > 0 ? '+' : '' }}{{ tx.delivery_change_pp }} pts vs 1y</span></dd>
                      </div>
                      <div>
                        <UTooltip text="Days in the last 60 the price hit its daily upper / lower price limit. Not available for stocks with futures."><dt class="text-xs text-muted underline decoration-dotted underline-offset-2">Price-limit hits, 60 days</dt></UTooltip>
                        <dd class="num font-medium">
                          <template v-if="!isNum(tx.delivery_pct_20d)">—</template>
                          <template v-else-if="!isNum(tx.upper_band_hits_60d)"><span class="text-dimmed">n/a (has futures)</span></template>
                          <template v-else><span class="text-up">▲ {{ tx.upper_band_hits_60d }}</span> <span class="text-dimmed">/</span> <span class="text-down">▼ {{ tx.lower_band_hits_60d }}</span></template>
                        </dd>
                      </div>
                    </dl>
                  </div>
                </div>
              </UCard>

              <!-- sector -->
              <UCard v-if="sec">
                <template #header>
                  <div class="flex flex-wrap items-center justify-between gap-2">
                    <div><h3 class="font-semibold text-highlighted">{{ sec.sector_name }}</h3><p class="text-xs text-muted">Sector cycle</p></div>
                    <UTooltip v-if="sec.phase" :text="PHASE[sec.phase]?.desc || ''">
                      <UBadge :color="PHASE[sec.phase]?.color || 'neutral'" variant="subtle" :label="PHASE[sec.phase]?.label || sec.phase" />
                    </UTooltip>
                  </div>
                </template>
                <div class="space-y-2.5">
                  <div v-for="r in [{ label: 'Capacity growth', v: sec.capacity_growth_pct, c: 'var(--series-2)' }, { label: 'Demand growth', v: sec.demand_growth_pct, c: 'var(--series-1)' }]" :key="r.label">
                    <div class="mb-1 flex justify-between text-xs"><span class="text-muted">{{ r.label }}</span><span class="num font-medium text-highlighted">{{ isNum(r.v) ? `${r.v > 0 ? '+' : ''}${r.v}%` : '—' }}</span></div>
                    <div class="h-2 rounded-full bg-elevated"><div class="h-2 rounded-full" :style="{ width: `${Math.abs(r.v ?? 0) / secMax * 100}%`, background: r.c }" /></div>
                  </div>
                </div>
                <p v-if="sec.phase" class="mt-3 text-[11px] text-dimmed">Capacity is read from fixed assets, a weak guide for asset-light sectors (financial services, IT services).</p>
              </UCard>

              <!-- signals -->
              <UCard v-if="signals.length">
                <template #header><h3 class="font-semibold text-highlighted">Signals</h3><p class="text-xs text-muted">Holdings, ratings, insider trades, sector growth</p></template>
                <ul class="divide-y divide-default">
                  <li v-for="p in signals" :key="`${p.signal_type}-${p.value}`" class="flex items-start justify-between gap-3 py-2 first:pt-0 last:pb-0">
                    <span class="min-w-0 text-sm text-default">{{ p.label }}</span>
                    <div class="flex shrink-0 flex-col items-end gap-1">
                      <UBadge v-if="p.direction" :color="DIR[p.direction]?.color || 'neutral'" variant="soft" size="sm" :icon="DIR[p.direction]?.icon" :label="p.direction" class="capitalize" />
                      <span class="num text-[11px] text-dimmed">{{ dateShort(p.as_of_date) }}</span>
                    </div>
                  </li>
                </ul>
              </UCard>
            </div>
          </div>
        </template>
      </div>
    </template>
  </UDashboardPanel>
</template>
