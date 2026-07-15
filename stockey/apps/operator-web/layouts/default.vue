<script setup lang="ts">
const api = useOperatorApi()
const { data: runtime } = await useAsyncData('operator-runtime', () => api.getRuntime(), {
  server: false,
  lazy: true
})

const staleRuntime = computed(() => Boolean(runtime.value?.stale_code))
const liveTradingStatusKnown = computed(() => Boolean(runtime.value))
const liveTradingEnabled = computed(() => Boolean(runtime.value?.live_trading_enabled))
const liveTradingLabel = computed(() => {
  if (!liveTradingStatusKnown.value) return 'Live trading status unknown'
  return liveTradingEnabled.value ? 'Live trading enabled' : 'Live trading disabled'
})
const liveTradingNote = computed(() => runtime.value?.live_trading_operator_note || 'Broker submission stays disabled unless explicitly enabled.')
const runtimeLabel = computed(() => 'Operator API is running older code — restart it to serve current data')
const runtimeDetail = computed(() => {
  const rev = runtime.value?.git_rev ? String(runtime.value.git_rev).slice(0, 10) : 'unknown rev'
  const path = runtime.value?.latest_source_path ? ` (${runtime.value.latest_source_path})` : ''
  return `The API process started before the latest source change${path}. Expected after a code change; data may be stale until the API restarts. · rev ${rev}`
})
</script>

<template>
  <div class="min-h-screen">
    <header class="mx-auto max-w-7xl px-6 py-6">
      <div class="flex items-center justify-between gap-4">
        <NuxtLink to="/" class="text-xl font-black tracking-tight text-ink">Stockey Operator</NuxtLink>
        <div class="flex flex-col items-end gap-2">
          <div
            class="flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs font-black"
            :class="liveTradingEnabled ? 'border-rust/35 bg-rust/10 text-rust' : liveTradingStatusKnown ? 'border-moss/25 bg-moss/10 text-moss' : 'border-ink/15 bg-white/70 text-ink/50'"
            :title="liveTradingNote"
          >
            <span class="h-2 w-2 rounded-full" :class="liveTradingEnabled ? 'bg-rust' : liveTradingStatusKnown ? 'bg-moss' : 'bg-ink/35'"></span>
            <span>{{ liveTradingLabel }}</span>
          </div>
          <nav class="flex flex-wrap items-center justify-end gap-2 text-sm font-semibold">
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/workbench">Workbench</NuxtLink>
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/advisory">Advisory</NuxtLink>
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/recommendations-unified">Recommendations</NuxtLink>
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/watchlist">Watchlist</NuxtLink>
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/positions">Positions</NuxtLink>
            <details class="relative">
              <summary class="cursor-pointer list-none rounded-full px-4 py-2 text-ink/65 hover:bg-white/70">Insight ▾</summary>
              <div class="absolute right-0 z-40 mt-2 flex w-44 flex-col gap-1 rounded-2xl border border-ink/10 bg-paper p-2 shadow-xl">
                <NuxtLink class="rounded-xl px-3 py-2 text-ink/65 hover:bg-white/70" to="/llm-decisions">Decisions</NuxtLink>
                <NuxtLink class="rounded-xl px-3 py-2 text-ink/65 hover:bg-white/70" to="/scorecard">Scorecard</NuxtLink>
              </div>
            </details>
            <details class="relative">
              <summary class="cursor-pointer list-none rounded-full px-4 py-2 text-ink/65 hover:bg-white/70">Manage ▾</summary>
              <div class="absolute right-0 z-40 mt-2 flex w-44 flex-col gap-1 rounded-2xl border border-ink/10 bg-paper p-2 shadow-xl">
                <NuxtLink class="rounded-xl px-3 py-2 text-ink/65 hover:bg-white/70" to="/hypotheses">Playbooks</NuxtLink>
                <NuxtLink class="rounded-xl px-3 py-2 text-ink/65 hover:bg-white/70" to="/prompts">Prompts</NuxtLink>
              </div>
            </details>
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/health-hub">Health</NuxtLink>
            <details class="relative">
              <summary class="cursor-pointer list-none rounded-full px-4 py-2 text-ink/65 hover:bg-white/70">Research ▾</summary>
              <div class="absolute right-0 z-40 mt-2 flex w-56 flex-col gap-1 rounded-2xl border border-ink/10 bg-paper p-2 shadow-xl">
                <NuxtLink class="rounded-xl px-3 py-2 text-ink/65 hover:bg-white/70" to="/screeners">Screeners</NuxtLink>
                <NuxtLink class="rounded-xl px-3 py-2 text-ink/65 hover:bg-white/70" to="/signal-quality">Signal Quality</NuxtLink>
                <NuxtLink class="rounded-xl px-3 py-2 text-ink/65 hover:bg-white/70" to="/regime-overlays">Regime Review</NuxtLink>
                <NuxtLink class="rounded-xl px-3 py-2 text-ink/65 hover:bg-white/70" to="/technical-calibration">Technical Calibration</NuxtLink>
                <NuxtLink class="rounded-xl px-3 py-2 text-ink/65 hover:bg-white/70" to="/action-conflict-rules">Conflict Rules</NuxtLink>
                <NuxtLink class="rounded-xl px-3 py-2 text-ink/65 hover:bg-white/70" to="/identity-issues">Identity Issues</NuxtLink>
                <NuxtLink class="rounded-xl px-3 py-2 text-ink/65 hover:bg-white/70" to="/operations">Operations</NuxtLink>
              </div>
            </details>
          </nav>
        </div>
      </div>
      <div v-if="staleRuntime" class="mt-3 rounded-2xl border border-sun/45 bg-sun/10 px-4 py-3 text-sm text-ember">
        <p class="font-black">{{ runtimeLabel }}</p>
        <p class="mt-0.5 font-semibold text-ember/80">{{ runtimeDetail }}</p>
      </div>
    </header>
    <main class="mx-auto max-w-7xl px-6 pb-16">
      <slot />
    </main>
  </div>
</template>
