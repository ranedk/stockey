<script setup lang="ts">
import type { DraftThesis } from '~/types/api'

// /api/drafts was reachable and unread -- "the same data the digest email already
// sends". Drafts are the ENTRY to the promotion funnel, so surfacing them closes the
// loop at the top the way the sizing panel closes it at the bottom.
const api = useApi()
const { data, status } = await useAsyncData('drafts', () => api.get<DraftThesis[]>('/api/drafts'))

const expanded = ref(false)
const drafts = computed(() => data.value ?? [])
// Highest LLM confidence first -- but see the caveat rendered below: this orders the
// list, it does not rank conviction.
const sorted = computed(() =>
  [...drafts.value].sort((a, b) => (b.confidence_score ?? 0) - (a.confidence_score ?? 0)),
)
</script>

<template>
  <section v-if="status !== 'pending' && drafts.length" class="mt-4 rounded-lg border border-slate-200 bg-white p-4">
    <div class="flex flex-wrap items-center gap-3">
      <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Candidate notes</h2>
      <BadgePill :label="`${drafts.length} candidates`" tone="neutral" />
      <button class="ml-auto text-xs font-medium text-slate-500 hover:text-slate-900" @click="expanded = !expanded">
        {{ expanded ? 'Hide' : 'Show' }}
      </button>
    </div>

    <p class="mt-1 text-xs text-slate-400">
      Machine-written notes on watchlist names — the same ones the digest email sends.
      They are <strong>reading, not decisions</strong>, and nothing here waits on you: the
      nightly ruleset decides positions on its own, from the confluence axes and the stage
      read, and never consults these notes. The confidence score is the model's own and is
      not evidence of anything until the calibration table on Portfolio says otherwise.
    </p>

    <ul v-if="expanded" class="mt-3 space-y-2">
      <li v-for="d in sorted" :key="d.company_master_id" class="rounded border border-slate-200 p-3">
        <div class="flex flex-wrap items-center gap-2">
          <NuxtLink
            :to="`/watchlist/${encodeURIComponent(d.company_master_id)}`"
            class="text-sm font-medium text-slate-900 hover:underline"
          >
            {{ d.company_master_id.replace('nse:', '') }}
          </NuxtLink>
          <span v-if="d.confidence_score !== null" class="text-xs text-slate-400">
            model confidence {{ d.confidence_score }}
          </span>
          <span v-if="d.source_alert_trigger_type" class="text-xs text-slate-400">
            · {{ d.source_alert_trigger_type }}
          </span>
        </div>
        <p class="mt-1 text-sm text-slate-700">{{ d.prediction_text }}</p>
        <p class="mt-1 text-xs text-slate-500">
          invalidation: {{ d.invalidation_criteria }}
          <template v-if="d.target_date"> · target {{ formatDate(d.target_date) }}</template>
        </p>
      </li>
    </ul>
  </section>
</template>
