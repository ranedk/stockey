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
const runtimeLabel = computed(() => {
  const rev = runtime.value?.git_rev ? String(runtime.value.git_rev).slice(0, 12) : 'unknown rev'
  const path = runtime.value?.latest_source_path ? ` · ${runtime.value.latest_source_path}` : ''
  return `API restart needed · ${rev}${path}`
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
          <nav class="flex flex-wrap justify-end gap-2 text-sm font-semibold">
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/">Overview</NuxtLink>
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/recommendations">Recommendations</NuxtLink>
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/paper-portfolio">Paper Portfolio</NuxtLink>
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/events">Event Inbox</NuxtLink>
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/manual-review">Manual Review</NuxtLink>
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/operator-journey">Operator Journey</NuxtLink>
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/execution-approvals">Execution Approvals</NuxtLink>
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/action-conflict-rules">Conflict Rules</NuxtLink>
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/decision-trace">Decision Trace</NuxtLink>
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/hypotheses">Playbooks</NuxtLink>
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/screeners">Screeners</NuxtLink>
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/wait-signals">Wait Signals</NuxtLink>
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/signal-quality">Signal Quality</NuxtLink>
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/prompt-registry">Prompt Registry</NuxtLink>
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/identity-issues">Identity Issues</NuxtLink>
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/health">Data Health</NuxtLink>
            <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/operations">Operations</NuxtLink>
          </nav>
        </div>
      </div>
      <div v-if="staleRuntime" class="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-rust/25 bg-rust/10 px-4 py-3 text-sm text-rust">
        <p class="font-black">{{ runtimeLabel }}</p>
        <p class="font-semibold text-rust/80">Running API process is older than source files on disk.</p>
      </div>
    </header>
    <main class="mx-auto max-w-7xl px-6 pb-16">
      <slot />
    </main>
  </div>
</template>
