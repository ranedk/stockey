<script setup lang="ts">
import type { TraceSummary } from '~/types/api'

const api = useOperatorApi()
const route = useRoute()
const symbol = ref(String(route.query.symbol || '').toUpperCase())
const uniqueId = ref(String(route.query.unique_id || ''))
const trace = ref<TraceSummary | null>(null)
const loading = ref(false)
const error = ref('')
const apiError = ref<unknown>(null)

async function loadTrace() {
  error.value = ''
  apiError.value = null
  trace.value = null
  const normalizedSymbol = symbol.value.trim().toUpperCase()
  const normalizedUniqueId = uniqueId.value.trim()
  if (!normalizedSymbol && !normalizedUniqueId) {
    error.value = 'Enter a symbol or event unique id.'
    return
  }
  loading.value = true
  try {
    if (normalizedUniqueId) {
      trace.value = await api.getEventTraceSummary(normalizedUniqueId)
    } else {
      trace.value = await api.getSymbolTraceSummary(normalizedSymbol, 200)
    }
  } catch (err) {
    apiError.value = err
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

if (symbol.value || uniqueId.value) {
  await loadTrace()
}
</script>

<template>
  <section class="rounded-[2rem] bg-ink p-8 text-paper shadow-soft">
    <p class="text-sm font-semibold uppercase tracking-[0.35em] text-paper/55">Decision Trace</p>
    <h1 class="mt-4 max-w-4xl text-5xl font-black tracking-tight">Follow one symbol or event from evidence to action.</h1>
    <p class="mt-5 max-w-3xl text-lg leading-8 text-paper/70">
      Use this when the action queue is confusing, a news item changed state, or multiple strategies disagree for the same stock.
    </p>
  </section>

  <section class="mt-8 glass-panel rounded-3xl p-6">
    <div class="grid gap-4 md:grid-cols-[1fr_1fr_auto]">
      <label class="grid gap-2 text-sm font-bold text-ink/70">
        Symbol
        <input v-model="symbol" class="rounded-2xl border border-black/10 bg-white/80 px-4 py-3 uppercase text-ink outline-none focus:border-moss" placeholder="RELIANCE" />
      </label>
      <label class="grid gap-2 text-sm font-bold text-ink/70">
        Event unique id
        <input v-model="uniqueId" class="rounded-2xl border border-black/10 bg-white/80 px-4 py-3 text-ink outline-none focus:border-moss" placeholder="Optional announcement/news id" />
      </label>
      <button class="self-end rounded-full bg-moss px-6 py-3 text-sm font-black text-white disabled:opacity-50" :disabled="loading" type="button" @click="loadTrace">
        {{ loading ? 'Loading...' : 'Load Trace' }}
      </button>
    </div>
    <p class="mt-3 text-sm text-ink/55">If both fields are present, event unique id wins.</p>
    <p v-if="error" class="mt-4 rounded-2xl bg-ember/10 p-3 text-sm font-semibold text-ember">{{ error }}</p>
  </section>

  <ApiErrorBanner v-if="apiError" class="mt-5" title="Decision trace failed" :error="apiError" />

  <TraceTimeline v-if="trace" class="mt-8" :trace="trace" sync-url />
  <p v-else-if="!loading" class="mt-8 glass-panel rounded-3xl p-6 text-ink/60">No trace loaded yet.</p>
</template>
