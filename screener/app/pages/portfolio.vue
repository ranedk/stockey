<script setup lang="ts">
import type { PortfolioScoring } from '~/types/api'

// The human forecast register was removed 2026-09-04 ("only machine created portfolio and
// forecast"). Everything on this page is opened, judged and resolved unattended; there is
// no create/resolve form because there is no human act left to perform.
const api = useApi()
const { data: scoring, status, error } = await useAsyncData(
  'portfolio-scoring',
  () => api.get<PortfolioScoring>('/api/portfolio/scoring'),
)
</script>

<template>
  <div>
    <h1 class="text-xl font-semibold">Portfolio</h1>
    <p class="mt-1 text-sm text-slate-600">
      Built and graded by machine. A mechanical ruleset selects candidates, an LLM
      adjudicator may veto them, and every forecast is resolved at its target date without
      anyone confirming it.
    </p>
    <PipelineNote layer="L4 → L5" title="How a position gets here, and how it is sized">
      <p>
        <strong>Selection</strong> is mechanical: the ruleset proposes, an LLM adjudicator may only
        <em>reject</em> — it can never add a name or enlarge a position — and forecasts resolve at
        their target date by machine, with no one confirming the result.
      </p>
      <p class="mt-2">
        <strong>L4</strong> used to be a human thesis register that gated capital. It was deleted on
        2026-09-04 when the portfolio became fully machine-run; what remains under that name is
        per-company written reading, which promotes to nothing.
      </p>
      <p class="mt-2">
        <strong>L5</strong> is a sizing calculator, not a decision-maker. It computes a size only for
        a company that <em>already</em> holds an open position — ruleset passed, adjudicator did not
        veto. No open position, no size; it will not guess at a number for a name nothing has
        committed to.
      </p>
    </PipelineNote>

    <div v-if="error" class="mt-6 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
      Could not reach the API ({{ error.message }}).
    </div>

    <template v-else-if="scoring">
      <section class="mt-6 rounded-lg border border-slate-200 bg-white p-4">
        <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Scoring</h2>
        <p class="mt-1 text-xs text-slate-400">
          Forecast accuracy, not returns — as of {{ formatDate(scoring.as_of_date) }}.
        </p>

        <div v-if="scoring.resolved === 0" class="mt-3 text-sm text-slate-500">
          Nothing resolved yet ({{ scoring.open }} open). Forecasts resolve at their target
          date, so the first numbers here are months out by construction.
        </div>
        <div v-else class="mt-3 flex flex-wrap items-center gap-4">
          <StatChip label="Hit rate" :value="scoring.hit_rate !== null ? `${scoring.hit_rate}%` : null" />
          <StatChip label="Resolved" :value="scoring.resolved" />
          <StatChip label="Open" :value="scoring.open" />
          <StatChip
            v-if="scoring.time_to_confirmation_days?.median_days !== undefined"
            label="Median days to confirm"
            :value="scoring.time_to_confirmation_days.median_days"
          />
          <div v-if="Object.keys(scoring.failure_attribution_breakdown).length" class="flex flex-wrap gap-2">
            <BadgePill
              v-for="(count, reason) in scoring.failure_attribution_breakdown"
              :key="reason"
              :label="`${reason}: ${count}`"
              tone="warn"
            />
          </div>
        </div>
      </section>

      <section class="mt-4 rounded-lg border border-slate-200 bg-white p-4">
        <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Accepted vs vetoed, on price</h2>
        <p class="mt-1 text-xs text-slate-400">
          Closed positions only. Vetoed names are tracked as shadows and take the same exits, so their
          return is the price of the veto. Averages appear from 5 closed positions per arm.
        </p>
        <div v-if="!Object.keys(scoring.price_return_by_entry_decision ?? {}).length" class="mt-3 text-sm text-slate-500">
          No closed positions yet.
        </div>
        <table v-else class="mt-3 w-full text-sm">
          <thead class="text-left text-xs text-slate-400">
            <tr><th class="py-1">Decision</th><th>Closed</th><th>Mean</th><th>Median</th><th>Share positive</th></tr>
          </thead>
          <tbody>
            <tr v-for="(arm, decision) in scoring.price_return_by_entry_decision" :key="decision" class="border-t border-slate-100">
              <td class="py-1 font-medium">{{ decision === 'reject' ? 'Vetoed' : decision === 'accept' ? 'Accepted' : decision }}</td>
              <td>{{ arm.closed }}</td>
              <td>{{ arm.mean_return_pct !== null ? `${arm.mean_return_pct}%` : '—' }}</td>
              <td>{{ arm.median_return_pct !== null ? `${arm.median_return_pct}%` : '—' }}</td>
              <td>{{ arm.share_positive_pct !== null ? `${arm.share_positive_pct}%` : '—' }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="mt-4 rounded-lg border border-slate-200 bg-white p-4">
        <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Calibration</h2>
        <p class="mt-1 text-xs text-slate-400">
          A hit rate only appears once a group has at least 5 resolved forecasts — below
          that the sample says so rather than showing a number that would be noise.
        </p>

        <div v-if="scoring.resolved === 0" class="mt-3 text-sm text-slate-500">
          Not enough history yet — {{ scoring.total_forecasts }} forecast(s) recorded,
          {{ scoring.open }} open, none resolved.
        </div>

        <div v-else class="mt-4 grid gap-6 md:grid-cols-3">
          <CalibrationBreakdown
            title="By resolution method"
            subtitle="Data deciding vs a model grading prose. A large gap is self-grading bias."
            key-label="Method"
            :groups="scoring.hit_rate_by_resolution_method"
          />
          <CalibrationBreakdown
            title="Accepted vs vetoed"
            subtitle="Does the adjudicator's veto add value, or destroy it?"
            key-label="Entry decision"
            :groups="scoring.hit_rate_by_entry_decision"
          />
          <CalibrationBreakdown
            title="By confluence count"
            subtitle="The one that tests §12's core claim."
            key-label="Axes agreeing"
            :groups="scoring.hit_rate_by_confluence_count"
            numeric-keys
          />
        </div>

        <p class="mt-4 rounded border border-slate-200 bg-slate-50 p-3 text-xs text-slate-600">
          <strong>Why the first column matters.</strong> A model that both writes and grades
          its own forecasts reports a flattering hit rate, and that number is worse than no
          number because it looks like evidence. Forecasts that reduced to a checkable
          comparison against fundamental state resolve <em>mechanically</em> — the model's
          prose is never consulted. The rest are judged. If the judged hit rate runs well
          above the mechanical one, that gap is the bias, measured rather than argued about.
        </p>
      </section>

      <RulesetPortfolioPanel />
    </template>

    <p v-else-if="status === 'pending'" class="mt-6 text-sm text-slate-500">Loading…</p>
  </div>
</template>
