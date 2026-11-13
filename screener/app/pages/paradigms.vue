<script setup lang="ts">
// Investor paradigms (systrader LEDGER 83): our replicas of methods Indian managers publish.
useHead({ title: 'Paradigms' })
const groups = [
  { key: 'momentum', title: 'Price only', sub: 'Buy what is rising, sell what stops. Each has a backtest from 2016 and a live record.', icon: 'i-lucide-trending-up',
    items: PARADIGM_LIST.filter(p => p.family === 'momentum') },
  { key: 'fundamental', title: 'Business quality', sub: 'Pick businesses by their numbers and hold them. Live records only.', icon: 'i-lucide-building-2',
    items: PARADIGM_LIST.filter(p => p.family === 'fundamental') },
]
const initials = (n: string) => n.split(' ').map(w => w[0]).join('').slice(0, 2)
</script>

<template>
  <UDashboardPanel id="paradigms">
    <template #header>
      <PageNavbar title="Paradigms" icon="i-lucide-git-compare" />
    </template>

    <template #body>
      <div class="mx-auto w-full max-w-7xl space-y-6">
        <div class="flex flex-wrap items-start gap-3 rounded-xl bg-elevated/50 p-4 ring ring-default">
          <UIcon name="i-lucide-info" class="mt-0.5 size-5 shrink-0 text-primary" />
          <div class="min-w-0 flex-1 text-sm">
            <p class="font-medium text-highlighted">Portfolios built from the stock-picking methods seven Indian fund managers describe in public.</p>
            <p class="mt-1 text-muted">
              These are <b class="font-medium text-default">our rules</b>, written from what they publish. They are not their portfolios
              and not their returns. Where a step is private we picked a value and say so. Their own figures are shown separately and are
              theirs, not checked by us. A record only: nothing here trades.
            </p>
          </div>
        </div>

        <!-- quick jump -->
        <div class="flex flex-wrap gap-2">
          <a v-for="p in PARADIGM_LIST" :key="p.key" :href="`#${p.key}`"
            class="inline-flex items-center gap-2 rounded-full py-1 pl-1 pr-3 text-xs ring ring-default transition hover:bg-elevated">
            <span class="grid size-5 place-items-center rounded-full text-[10px] font-semibold"
              :class="p.family === 'momentum' ? 'bg-primary/15 text-primary' : 'bg-warning/15 text-warning'">{{ initials(p.person) }}</span>
            {{ p.person }}
          </a>
        </div>

        <section v-for="g in groups" :key="g.key" class="space-y-4">
          <div class="flex items-center gap-2">
            <UIcon :name="g.icon" class="size-5 text-muted" />
            <div>
              <h2 class="text-base font-semibold text-highlighted">{{ g.title }}</h2>
              <p class="text-xs text-muted">{{ g.sub }}</p>
            </div>
          </div>

          <UCard v-for="p in g.items" :id="p.key" :key="p.key" class="scroll-mt-4">
            <template #header>
              <div class="flex flex-wrap items-start gap-3">
                <span class="grid size-10 shrink-0 place-items-center rounded-full text-sm font-semibold"
                  :class="p.family === 'momentum' ? 'bg-primary/15 text-primary' : 'bg-warning/15 text-warning'">{{ initials(p.person) }}</span>
                <div class="min-w-0 flex-1">
                  <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <h3 class="text-lg font-semibold text-highlighted">{{ p.person }}</h3>
                    <UBadge :color="p.family === 'momentum' ? 'primary' : 'warning'" variant="subtle" size="sm"
                      :label="p.family === 'momentum' ? 'Price only' : 'Business quality'" />
                    <UTooltip v-if="p.crude" text="The method relies on judgement or private steps, so our version is only a rough stand-in.">
                      <UBadge color="error" variant="outline" size="sm" icon="i-lucide-triangle-alert" label="Rough stand-in" />
                    </UTooltip>
                  </div>
                  <div class="text-sm text-muted">{{ p.firm }} <span class="text-dimmed">· {{ p.handle }}</span></div>
                </div>
              </div>
            </template>

            <div class="grid gap-5 lg:grid-cols-5">
              <div class="space-y-3 lg:col-span-2">
                <blockquote class="border-l-2 border-primary/50 pl-3 text-sm text-default">{{ p.paradigm }}</blockquote>
                <div v-if="p.rules.length">
                  <div class="mb-1.5 text-xs font-medium text-muted">Our rules</div>
                  <ul class="space-y-1.5 text-sm text-default">
                    <li v-for="r in p.rules" :key="r" class="flex gap-2">
                      <UIcon name="i-lucide-check" class="mt-0.5 size-4 shrink-0 text-primary" /><span>{{ r }}</span>
                    </li>
                  </ul>
                </div>
                <div v-if="p.guessed.length" class="rounded-lg bg-warning/10 px-3 py-2 text-xs text-default ring ring-warning/25">
                  <span class="font-semibold text-warning">Our guesses: </span>
                  {{ p.guessed.join(' ') }}
                </div>
                <div class="flex gap-2 rounded-lg bg-elevated/60 px-3 py-2 text-xs text-muted">
                  <UIcon name="i-lucide-quote" class="mt-0.5 size-3.5 shrink-0" />
                  <div><span class="font-medium text-default">Their claim, not checked by us.</span> {{ p.theirFigures.replace(/^Their own figures:\s*/, '') }}</div>
                </div>
              </div>
              <div class="space-y-3 lg:col-span-3">
                <template v-for="tr in p.tracks" :key="tr.name">
                  <ParadigmMomentumTrack v-if="tr.source === 'systrader'" :name="tr.name" :label="tr.label" />
                  <ParadigmFundamentalTrack v-else :name="tr.name" :label="tr.label" />
                </template>
              </div>
            </div>
          </UCard>
        </section>
      </div>
    </template>
  </UDashboardPanel>
</template>
