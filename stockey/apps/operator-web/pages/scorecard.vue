<script setup lang="ts">
type Dict = Record<string, unknown>

const api = useOperatorApi()
const { data, pending, error: loadError, refresh } = await useAsyncData('scorecard', () => api.getScorecard())

const summary = computed(() => asDict(data.value?.summary))
const byEventClass = computed(() => asList(data.value?.by_event_class))
const bySufficiency = computed(() => asList(data.value?.by_sufficiency_path))
const byHypothesis = computed(() => asList(data.value?.by_hypothesis))
const skipped = computed(() => asList(data.value?.skipped))
const matured = computed(() => Number(summary.value.matured || 0))

function asDict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Dict : {}
}
function asList(value: unknown): Dict[] {
  return Array.isArray(value) ? value.filter((item) => typeof item === 'object' && item !== null) as Dict[] : []
}
function pct(value: unknown) {
  return typeof value === 'number' ? `${Math.round(value * 100)}%` : '-'
}
function excess(value: unknown) {
  if (typeof value !== 'number') return '-'
  const bps = Math.round(value * 10000)
  return `${bps > 0 ? '+' : ''}${bps} bps`
}
function excessClass(value: unknown) {
  if (typeof value !== 'number') return 'text-ink/50'
  if (value > 0) return 'text-moss font-semibold'
  if (value < 0) return 'text-rust font-semibold'
  return 'text-ink/55'
}
</script>

<template>
  <section class="space-y-6">
    <header class="rounded-3xl border border-ink/10 bg-ink px-8 py-7 text-paper">
      <p class="text-xs font-black uppercase tracking-[0.2em] text-paper/50">Read-only · is the system any good?</p>
      <h1 class="mt-2 text-3xl font-black tracking-tight">Scorecard</h1>
      <p class="mt-2 max-w-2xl text-paper/70">
        The matured track record of the system's directional decisions — benchmark-excess after cost vs
        NIFTY — sliced by event class, sufficiency path, and hypothesis. The trust signal behind the
        recommendations.
      </p>
    </header>

    <ApiErrorBanner v-if="loadError" :error="loadError" title="Could not load the scorecard" />
    <p v-if="pending" class="text-sm text-ink/40">loading…</p>

    <div v-if="!matured && !pending" class="rounded-2xl border border-ink/15 bg-white/70 px-5 py-5 text-sm text-ink/65">
      <span class="font-black text-ink/80">No matured outcomes yet.</span>
      Outcomes mature once a directional (BUY/SELL) decision's horizon elapses and the labeler runs
      (nightly, after advisory). With LLM authority off, decisions are deterministic WATCH and are not
      scored — so this view fills in as the LLM is enabled and its calls mature.
      <span v-if="skipped.length" class="block mt-2 text-ink/45">({{ skipped.map(s => String(s.error)).join(', ') }})</span>
    </div>

    <template v-else-if="matured">
      <!-- headline -->
      <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div class="rounded-2xl border border-ink/10 bg-white/60 px-5 py-4">
          <p class="text-xs font-black uppercase tracking-wide text-ink/45">Matured</p>
          <p class="mt-1 text-2xl font-black text-ink">{{ matured }}</p>
        </div>
        <div class="rounded-2xl border border-ink/10 bg-white/60 px-5 py-4">
          <p class="text-xs font-black uppercase tracking-wide text-ink/45">Excess hit rate</p>
          <p class="mt-1 text-2xl font-black text-ink">{{ pct(summary.excess_hit_rate) }}</p>
        </div>
        <div class="rounded-2xl border border-ink/10 bg-white/60 px-5 py-4">
          <p class="text-xs font-black uppercase tracking-wide text-ink/45">Mean excess</p>
          <p class="mt-1 text-2xl font-black" :class="excessClass(summary.mean_excess_after_cost)">{{ excess(summary.mean_excess_after_cost) }}</p>
        </div>
        <div class="rounded-2xl border border-ink/10 bg-white/60 px-5 py-4">
          <p class="text-xs font-black uppercase tracking-wide text-ink/45">Beta-only rate</p>
          <p class="mt-1 text-2xl font-black text-ink">{{ pct(summary.beta_only_rate) }}</p>
        </div>
      </div>

      <section v-for="grp in [
                 { title: 'By hypothesis', rows: byHypothesis, link: true },
                 { title: 'By event class', rows: byEventClass, link: false },
                 { title: 'By sufficiency path', rows: bySufficiency, link: false }
               ]" :key="grp.title" class="rounded-2xl border border-ink/10 bg-white/60 p-5">
        <h2 class="text-lg font-black tracking-tight text-ink">{{ grp.title }}</h2>
        <div class="mt-3 overflow-x-auto">
          <table class="w-full text-sm">
            <thead class="text-left text-xs font-black uppercase tracking-wide text-ink/45">
              <tr class="border-b border-ink/10">
                <th class="py-2 pr-4">Group</th>
                <th class="py-2 pr-4">Matured</th>
                <th class="py-2 pr-4">Hit rate</th>
                <th class="py-2 pr-4">Mean excess</th>
                <th class="py-2">Beta-only</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(r, idx) in grp.rows" :key="idx" class="border-b border-ink/5">
                <td class="py-2 pr-4 font-semibold text-ink/70">
                  <NuxtLink v-if="grp.link && r.group !== 'unattributed'" :to="`/hypotheses?hypothesis_id=${encodeURIComponent(String(r.group))}`" class="underline-offset-2 hover:underline">{{ r.group }}</NuxtLink>
                  <span v-else>{{ r.group }}</span>
                </td>
                <td class="py-2 pr-4 text-ink/70">{{ r.matured }}</td>
                <td class="py-2 pr-4 text-ink/70">{{ pct(r.excess_hit_rate) }}</td>
                <td class="py-2 pr-4" :class="excessClass(r.mean_excess_after_cost)">{{ excess(r.mean_excess_after_cost) }}</td>
                <td class="py-2 text-ink/55">{{ pct(r.beta_only_rate) }}</td>
              </tr>
              <tr v-if="!grp.rows.length"><td colspan="5" class="py-4 text-center text-ink/40">No data.</td></tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>

    <div class="flex justify-end">
      <button class="rounded-full border border-ink/15 bg-white/70 px-4 py-2 text-sm font-semibold hover:bg-white" @click="refresh()">Refresh</button>
    </div>
  </section>
</template>
