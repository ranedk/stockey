<script setup lang="ts">
import type { PtStrategy, PtQualMatrix, PtOptionTrack } from '~/types/paperTracks'
useHead({ title: 'Paper tracks' })

const { data, status, error } = useSystrader<{ strategies: PtStrategy[] }>('/api/paper')
// Optional extras: an older API without these endpoints simply shows nothing.
const { data: quals } = useSystrader<PtQualMatrix | null>('/api/paper/qualifiers', { lazy: true, server: false })
const { data: options } = useSystrader<{ tracks: PtOptionTrack[] } | null>('/api/paper/options', { lazy: true, server: false })

// Numbered in the order shown: our strategies first, then the copies of investors' methods.
const all = computed(() => {
  const list = data.value?.strategies ?? []
  return [...list.filter(s => !paperIsParadigm(s.name)), ...list.filter(s => paperIsParadigm(s.name))].map((s, i) => ({ s, i }))
})
const ours = computed(() => all.value.filter(x => !paperIsParadigm(x.s.name)))
const copies = computed(() => all.value.filter(x => paperIsParadigm(x.s.name)))

const live = computed(() => all.value.filter(x => x.s.days > 0).map(x => x.s))
const ahead = computed(() => live.value.filter(s => (paperExcess(s) ?? 0) > 0).length)
const behind = computed(() => live.value.filter(s => (paperExcess(s) ?? 0) < 0).length)
const longest = computed(() => Math.max(0, ...live.value.map(s => s.days)))
const asOf = computed(() => live.value.map(s => s.as_of).sort().at(-1))
const bars = computed(() => live.value.map(s => ({ label: paperName(s.name), value: paperExcess(s) })))
</script>

<template>
  <UDashboardPanel id="paper">
    <template #header>
      <PageNavbar title="Paper tracks" icon="i-lucide-notebook-pen">
        <template #right>
          <span v-if="asOf" class="hidden text-xs text-muted sm:inline">Close of {{ dateShort(asOf) }}</span>
        </template>
      </PageNavbar>
    </template>

    <template #body>
      <div class="mx-auto w-full max-w-7xl space-y-6">
        <div class="flex flex-wrap items-start gap-3 rounded-xl bg-elevated/50 p-4 ring ring-default">
          <UIcon name="i-lucide-info" class="mt-0.5 size-5 shrink-0 text-primary" />
          <div class="min-w-0 flex-1 text-sm">
            <p class="font-medium text-highlighted">Live records of strategies whose rules are frozen.</p>
            <p class="mt-1 text-muted">
              Everything else in this system is tested on past prices, and the past can be searched until something
              looks good. These records grow one trading day at a time from data nobody could choose, so they are the
              evidence that counts. Nothing here is a recommendation, and no money moves off it.
            </p>
          </div>
        </div>

        <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-plug-zap" title="Could not reach systrader"
          description="The paper-trading API (systrader cmd/api on :8090) did not answer." />

        <template v-else-if="status === 'pending'">
          <div class="grid grid-cols-2 gap-3 lg:grid-cols-4"><USkeleton v-for="i in 4" :key="i" class="h-24 rounded-xl" /></div>
          <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3"><USkeleton v-for="i in 6" :key="i" class="h-52 rounded-xl" /></div>
        </template>

        <UEmpty v-else-if="!all.length" icon="i-lucide-notebook" title="No strategies are being tracked yet" />

        <template v-else>
          <!-- headline numbers -->
          <div class="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <UCard :ui="{ body: 'p-4 sm:p-4' }">
              <div class="text-xs text-muted">Strategies tracked</div>
              <div class="num mt-1 text-2xl font-semibold text-highlighted">{{ all.length }}</div>
              <div class="mt-1 text-[11px] text-dimmed">{{ ours.length }} ours · {{ copies.length }} copies of investors</div>
            </UCard>
            <UCard :ui="{ body: 'p-4 sm:p-4' }">
              <div class="text-xs text-muted">Ahead of the market</div>
              <div class="num mt-1 text-2xl font-semibold text-up">▲ {{ ahead }}</div>
              <div class="mt-1 text-[11px] text-dimmed">beating equal amounts in every eligible stock</div>
            </UCard>
            <UCard :ui="{ body: 'p-4 sm:p-4' }">
              <div class="text-xs text-muted">Behind the market</div>
              <div class="num mt-1 text-2xl font-semibold text-down">▼ {{ behind }}</div>
              <div class="mt-1 text-[11px] text-dimmed">of {{ live.length }} with at least one trading day</div>
            </UCard>
            <UCard :ui="{ body: 'p-4 sm:p-4' }">
              <div class="text-xs text-muted">Longest record</div>
              <div class="num mt-1 text-2xl font-semibold text-highlighted">{{ longest }} <span class="text-sm font-normal text-muted">days</span></div>
              <div class="mt-1 text-[11px] text-dimmed">verdicts wait for 12 months and 12 rebalances</div>
            </UCard>
          </div>

          <!-- at a glance -->
          <div class="grid items-start gap-4 lg:grid-cols-3">
            <UCard class="lg:col-span-2">
              <template #header>
                <h3 class="font-semibold text-highlighted">Ahead of or behind the market, since each start</h3>
                <p class="text-xs text-muted">Strategy return minus the return of holding every eligible stock in equal amounts</p>
              </template>
              <DivergingBars v-if="bars.length" :items="bars" />
              <UEmpty v-else icon="i-lucide-hourglass" title="No trading days yet" class="py-6" />
            </UCard>
            <UCard>
              <template #header><h3 class="font-semibold text-highlighted">How each strategy is measured</h3></template>
              <ul class="space-y-3 text-sm">
                <li v-for="b in PAPER_BOOK_DEFS" :key="b.key" class="flex gap-2.5">
                  <span class="mt-2 h-0.5 w-3.5 shrink-0 rounded" :style="{ background: b.color }" />
                  <div>
                    <div class="font-medium text-highlighted">{{ b.label }}</div>
                    <div class="text-xs text-muted">{{ b.hint }}</div>
                  </div>
                </li>
              </ul>
            </UCard>
          </div>

          <section v-for="g in [{ title: 'Our strategies', sub: 'Rules found and tested in this system, then frozen.', items: ours },
                                { title: 'Copies of investors\' methods', sub: 'Our versions of rules that Indian fund managers publish. Not their portfolios.', items: copies }]"
            :key="g.title" class="space-y-3">
            <template v-if="g.items.length">
              <div>
                <h2 class="text-base font-semibold text-highlighted">{{ g.title }}</h2>
                <p class="text-xs text-muted">{{ g.sub }}<NuxtLink v-if="g.items === copies" to="/paradigms" class="ml-1 text-primary hover:underline">See the methods →</NuxtLink></p>
              </div>
              <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                <PaperStrategyCard v-for="x in g.items" :key="x.s.name" :s="x.s" :index="x.i" />
              </div>
            </template>
          </section>
        </template>

        <PaperOptionTracks v-if="options?.tracks?.length" :tracks="options.tracks" />

        <PaperQualMatrix v-if="quals?.rows?.length" :matrix="quals" />
      </div>
    </template>
  </UDashboardPanel>
</template>
