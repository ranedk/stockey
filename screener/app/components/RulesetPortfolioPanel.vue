<script setup lang="ts">
import type { RulesetPortfolioResponse, RulesetPosition } from '~/types/api'

// docs/PORTFOLIO_RULESET_PRD.md. A mechanical ruleset picks candidates; an LLM adjudicator
// may only REJECT them. Rejected names are still tracked so the adjudicator itself can be
// scored -- if the rejects outperform the takes, the veto layer is destroying value.
//
// The split below is on entry_decision, NOT on kind. kind answers "is real money at
// risk"; entry_decision answers "what did the adjudicator say". In record-only mode every
// row is kind='shadow' regardless of the verdict, so grouping by kind would show every
// accepted name under a heading that reads "rejected".
const api = useApi()
const { data, status, error } = await useAsyncData(
  'ruleset-portfolio',
  () => api.get<RulesetPortfolioResponse>('/api/ruleset-portfolio'),
)

const showRejected = ref(true)

const accepted = computed(() => (data.value?.positions ?? []).filter(p => p.entry_decision !== 'reject'))
const rejected = computed(() => (data.value?.positions ?? []).filter(p => p.entry_decision === 'reject'))
</script>

<template>
  <section class="mt-6 rounded-lg border border-slate-200 bg-white p-4">
    <div class="flex flex-wrap items-center gap-3">
      <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Ruleset portfolio</h2>
      <BadgePill
        v-if="data"
        :label="data.mode === 'live' ? 'LIVE' : 'record-only'"
        :tone="data.mode === 'live' ? 'good' : 'neutral'"
      />
      <span v-if="data" class="text-xs text-slate-400">
        ruleset v{{ data.ruleset_version }} ·
        {{ data.accepted_count }} accepted · {{ data.rejected_count }} vetoed ·
        {{ data.real_count }} with real money
      </span>
      <span v-if="data" class="text-xs text-slate-500">
        ₹{{ Math.round(data.capital_committed_rs).toLocaleString('en-IN') }} would be
        committed · {{ data.book_used }}/{{ data.book_capacity }} slots
        (₹{{ Math.round(data.capital_per_position_rs).toLocaleString('en-IN') }} each)
      </span>
    </div>

    <p class="mt-1 text-xs text-slate-400">
      A mechanical rule selects candidates; an LLM may only <strong>reject</strong> them, never
      add one. Vetoed names are still tracked as if taken, so the veto can be scored later —
      if the vetoed names outperform the accepted ones, the LLM layer is costing you.
    </p>

    <div v-if="error" class="mt-3 rounded border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
      Could not load the ruleset portfolio ({{ error.message }}).
    </div>
    <div v-else-if="status === 'pending'" class="mt-3 text-sm text-slate-500">Loading…</div>

    <template v-else-if="data">
      <p v-if="data.mode === 'record-only'" class="mt-3 rounded border border-slate-200 bg-slate-50 p-3 text-xs text-slate-600">
        No real money is committed. Every row below is a recorded decision only — a shakedown of
        the machinery before it has consequences.
      </p>

      <p
        v-if="data.book_used >= data.book_capacity"
        class="mt-3 rounded border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800"
      >
        The book is FULL ({{ data.book_capacity }} positions). New candidates the ruleset
        passes are being turned away for want of capital, not for anything about the
        companies — those names are named in the nightly run's output.
      </p>

      <div v-if="accepted.length" class="mt-3">
        <h3 class="mb-1 text-xs font-medium text-slate-600">
          Accepted <span class="font-normal text-slate-400">— the rule passed them and the adjudicator agreed</span>
        </h3>
        <RulesetPositionTable :rows="accepted" />
      </div>

      <div v-if="rejected.length" class="mt-4">
        <button class="text-xs font-medium text-slate-500 hover:text-slate-900" @click="showRejected = !showRejected">
          {{ showRejected ? 'Hide' : 'Show' }} {{ rejected.length }} name(s) the adjudicator vetoed
        </button>
        <RulesetPositionTable v-if="showRejected" :rows="rejected" class="mt-2" />
      </div>
      <p v-else class="mt-4 text-xs text-slate-400">
        The adjudicator has vetoed nothing yet. Until it does, there is no paired comparison
        to score it with.
      </p>
    </template>
  </section>
</template>
