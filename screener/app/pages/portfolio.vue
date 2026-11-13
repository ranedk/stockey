<script setup lang="ts">
import type { PortfolioScoring } from '~/types/watchlist'

useHead({ title: 'Portfolio' })
// Fully machine-run since 2026-09-04: opened, reviewed and graded unattended. No forms here.
const { data: s, status, error } = useStockey<PortfolioScoring>('/api/portfolio/scoring')
const howOpen = ref(false)

const ARM_NAME: Record<string, string> = { accept: 'Accepted', reject: 'Vetoed' }
const arms = computed(() => Object.entries(s.value?.price_return_by_entry_decision ?? {}).map(([k, a]) => ({ key: k, name: ARM_NAME[k] ?? k, ...a })))
const failures = computed(() => Object.entries(s.value?.failure_attribution_breakdown ?? {}))
</script>

<template>
  <UDashboardPanel id="portfolio">
    <template #header>
      <PageNavbar title="Portfolio" icon="i-lucide-briefcase">
        <template #right>
          <span v-if="s?.as_of_date" class="hidden text-xs text-muted sm:inline">As of {{ dateShort(s.as_of_date) }}</span>
        </template>
      </PageNavbar>
    </template>

    <template #body>
      <div class="mx-auto w-full max-w-7xl space-y-4">
        <div class="space-y-2">
          <p class="max-w-4xl text-sm text-muted">
            Built and graded by machine. Fixed rules pick candidates, an AI reviewer may veto them, and every forecast is graded
            at its target date without anyone confirming it.
          </p>
          <UButton size="xs" variant="link" color="neutral" class="px-0" icon="i-lucide-info" :label="howOpen ? 'Hide how a position gets here' : 'How a position gets here, and how it is sized'" @click="howOpen = !howOpen" />
          <div v-if="howOpen" class="max-w-4xl space-y-2 rounded-lg border border-default bg-elevated/40 p-3 text-xs leading-relaxed text-muted">
            <p><strong class="text-highlighted">Selection</strong> is mechanical: the rules propose, the AI reviewer may only <em>reject</em> (it can never add a name or make a position bigger), and forecasts are graded at their target date by machine, with no one confirming the result.</p>
            <p><strong class="text-highlighted">Written notes.</strong> A human forecast register used to gate money. It was deleted on 2026-09-04 when the portfolio became fully machine-run; what remains is per-company written reading, which leads to nothing.</p>
            <p><strong class="text-highlighted">Sizing</strong> is a calculator, not a decision-maker. It sizes only a company that already holds an open position (rules passed, reviewer did not veto). No open position, no size.</p>
          </div>
        </div>

        <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" title="Could not reach the portfolio API" :description="error.message" />
        <div v-else-if="!s && status === 'pending'" class="grid gap-3 lg:grid-cols-2"><USkeleton class="h-48" /><USkeleton class="h-48" /></div>

        <template v-else-if="s">
          <div class="grid items-start gap-4 lg:grid-cols-2">
            <!-- forecast scoring -->
            <UCard class="h-full">
              <template #header>
                <h3 class="font-semibold text-highlighted">Forecast accuracy</h3>
                <p class="text-xs text-muted">Did the forecasts come true? This is about being right, not about returns.</p>
              </template>
              <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <div class="rounded-lg bg-elevated/60 p-3">
                  <div class="text-xs text-muted">Came true</div>
                  <div class="num mt-1 text-2xl font-semibold text-highlighted">{{ s.hit_rate !== null ? `${s.hit_rate}%` : '—' }}</div>
                </div>
                <div class="rounded-lg bg-elevated/60 p-3">
                  <div class="text-xs text-muted">Graded</div>
                  <div class="num mt-1 text-2xl font-semibold text-highlighted">{{ s.resolved }}</div>
                </div>
                <div class="rounded-lg bg-elevated/60 p-3">
                  <div class="text-xs text-muted">Still open</div>
                  <div class="num mt-1 text-2xl font-semibold text-highlighted">{{ s.open }}</div>
                </div>
                <div class="rounded-lg bg-elevated/60 p-3">
                  <div class="text-xs text-muted">Median days to confirm</div>
                  <div class="num mt-1 text-2xl font-semibold text-highlighted">{{ s.time_to_confirmation_days?.median_days ?? '—' }}</div>
                </div>
              </div>
              <p v-if="s.resolved === 0" class="mt-3 rounded-lg bg-elevated/50 p-3 text-xs text-muted">
                Nothing graded yet ({{ s.open }} open of {{ s.total_forecasts }} recorded). Forecasts are graded at their target date, so the
                first numbers here are months away by design.
              </p>
              <div v-if="failures.length" class="mt-3">
                <div class="mb-1 text-xs text-muted">Why forecasts failed</div>
                <div class="flex flex-wrap gap-1.5">
                  <UBadge v-for="[reason, n] in failures" :key="reason" color="warning" variant="subtle" :label="`${reason}: ${n}`" />
                </div>
              </div>
            </UCard>

            <!-- accepted vs vetoed on price -->
            <UCard class="h-full">
              <template #header>
                <h3 class="font-semibold text-highlighted">Accepted vs vetoed, on price</h3>
                <p class="text-xs text-muted">Closed positions only. Vetoed names take the same exits, so their return is the price of the veto. Averages appear from 5 closed positions per side.</p>
              </template>
              <UEmpty v-if="!arms.length" icon="i-lucide-scale" title="No closed positions yet" class="py-6" />
              <div v-else class="grid gap-3 sm:grid-cols-2">
                <div v-for="a in arms" :key="a.key" class="rounded-lg border border-default p-3">
                  <div class="flex items-center justify-between gap-2">
                    <UBadge :color="a.key === 'reject' ? 'warning' : 'success'" variant="subtle" :icon="a.key === 'reject' ? 'i-lucide-ban' : 'i-lucide-circle-check'" :label="a.name" />
                    <span class="text-xs text-muted"><span class="num font-medium text-highlighted">{{ a.closed }}</span> closed</span>
                  </div>
                  <div class="mt-3 grid grid-cols-3 gap-2">
                    <div><div class="text-[11px] text-muted">Average</div><Change :value="wlFrac(a.mean_return_pct)" class="text-base" /></div>
                    <div><div class="text-[11px] text-muted">Median</div><Change :value="wlFrac(a.median_return_pct)" class="text-base" /></div>
                    <div>
                      <div class="text-[11px] text-muted">Share positive</div>
                      <div class="num text-base font-medium text-highlighted">{{ isNum(a.share_positive_pct) ? `${a.share_positive_pct}%` : '—' }}</div>
                    </div>
                  </div>
                  <UProgress v-if="isNum(a.share_positive_pct)" :model-value="a.share_positive_pct" size="xs" color="success" class="mt-2" />
                </div>
              </div>
            </UCard>
          </div>

          <!-- calibration -->
          <UCard>
            <template #header>
              <h3 class="font-semibold text-highlighted">Hit rate by group</h3>
              <p class="text-xs text-muted">A hit rate appears only once a group has at least 5 graded forecasts; below that the table says so instead of showing noise.</p>
            </template>
            <p v-if="s.resolved === 0" class="mb-4 rounded-lg bg-elevated/50 p-3 text-xs text-muted">
              Not enough history yet: {{ s.total_forecasts }} forecast(s) recorded, {{ s.open }} open, none graded.
            </p>
            <div class="grid gap-6 md:grid-cols-3">
              <PortfolioHitRates title="By how it was graded" subtitle="Checked against data vs an AI grading prose. A big gap is the AI flattering itself." key-label="Method" :groups="s.hit_rate_by_resolution_method" />
              <PortfolioHitRates title="Accepted vs vetoed" subtitle="Does the AI reviewer's veto add value, or destroy it?" key-label="Decision" :groups="s.hit_rate_by_entry_decision" :names="{ accept: 'Accepted', reject: 'Vetoed' }" />
              <PortfolioHitRates title="By checks agreeing" subtitle="Tests the core claim: more independent checks agreeing should mean more forecasts come true." key-label="Checks agreeing" :groups="s.hit_rate_by_confluence_count" numeric-keys />
            </div>
            <div class="mt-4 rounded-lg border border-default bg-elevated/40 p-3 text-xs text-muted">
              <strong class="text-highlighted">Why the first table matters.</strong> A model that both writes and grades its own forecasts reports a
              flattering hit rate, and that number is worse than none because it looks like evidence. Forecasts that reduce to a checkable
              number are graded <em>mechanically</em>, without reading the model's prose; the rest are judged. If the judged hit rate runs well
              above the mechanical one, that gap is the bias, measured rather than argued about.
            </div>
          </UCard>
        </template>

        <PortfolioRuleset />
        <PortfolioPaperBooks />
      </div>
    </template>
  </UDashboardPanel>
</template>
