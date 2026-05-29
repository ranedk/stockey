<script setup lang="ts">
const api = useOperatorApi()
const { data } = await useAsyncData('summary-health', () => api.getSummary())
</script>

<template>
  <section>
    <p class="text-sm font-semibold uppercase tracking-[0.3em] text-ink/45">Data Health</p>
    <h1 class="mt-3 text-4xl font-black">Runtime and ingestion state</h1>
  </section>

  <section class="mt-8 grid gap-6 lg:grid-cols-3">
    <div class="glass-panel rounded-3xl p-5">
      <h2 class="font-black">Cron</h2>
      <pre class="mt-4 max-h-96 overflow-auto text-xs">{{ JSON.stringify(data?.cron_status || [], null, 2) }}</pre>
    </div>
    <div class="glass-panel rounded-3xl p-5">
      <h2 class="font-black">Sync State</h2>
      <pre class="mt-4 max-h-96 overflow-auto text-xs">{{ JSON.stringify(data?.sync_state || [], null, 2) }}</pre>
    </div>
    <div class="glass-panel rounded-3xl p-5">
      <h2 class="font-black">Runtime</h2>
      <pre class="mt-4 max-h-96 overflow-auto text-xs">{{ JSON.stringify(data?.runtime_processes || [], null, 2) }}</pre>
    </div>
  </section>
</template>

