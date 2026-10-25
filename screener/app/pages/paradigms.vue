<script setup lang="ts">
// Investor paradigms (LEDGER 83): our replicas of methods Indian managers publish.
const momentum = PARADIGMS.filter(p => p.family === 'momentum')
const fundamental = PARADIGMS.filter(p => p.family === 'fundamental')
</script>

<template>
  <div>
    <h1 class="text-xl font-semibold">Paradigms</h1>
    <p class="mt-1 text-sm text-slate-600">
      Portfolios built from the stock-picking methods seven Indian managers talk about in public.
      These are <span class="font-medium">our rules</span> written from what they publish -- not their
      portfolios and not their returns. Where a step is private we picked a value and say so.
      Their own figures are shown separately and are theirs, not checked by us. A record only:
      nothing here trades.
    </p>

    <template v-for="group in [{ title: 'Price only (momentum)', items: momentum }, { title: 'Business quality (fundamental)', items: fundamental }]" :key="group.title">
      <h2 class="mt-8 text-sm font-semibold uppercase tracking-wide text-slate-500">{{ group.title }}</h2>
      <section v-for="p in group.items" :key="p.key" class="mt-4 rounded-xl border border-slate-200 bg-slate-50 p-4">
        <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h3 class="text-lg font-semibold text-slate-900">{{ p.person }}</h3>
          <span class="text-sm text-slate-600">{{ p.firm }}</span>
          <span class="text-xs text-slate-400">{{ p.handle }}</span>
          <BadgePill v-if="p.crude" tone="warn" label="crude stand-in" />
        </div>
        <p class="mt-2 text-sm text-slate-800">{{ p.paradigm }}</p>
        <ul v-if="p.rules.length" class="mt-3 list-disc space-y-0.5 pl-5 text-sm text-slate-700">
          <li v-for="r in p.rules" :key="r">{{ r }}</li>
        </ul>
        <div v-if="p.guessed.length" class="mt-3 rounded bg-amber-50 px-3 py-2 text-xs text-amber-900">
          <span class="font-medium">Our guesses:</span>
          <span v-for="g in p.guessed" :key="g"> {{ g }}</span>
        </div>
        <p class="mt-2 text-xs italic text-slate-500">{{ p.theirFigures }}</p>
        <div class="mt-3 space-y-3">
          <template v-for="tr in p.tracks" :key="tr.name">
            <ParadigmSystraderTrack v-if="tr.source === 'systrader'" :name="tr.name" :label="tr.label" />
            <ParadigmStockeyTrack v-else :name="tr.name" :label="tr.label" />
          </template>
        </div>
      </section>
    </template>
  </div>
</template>
