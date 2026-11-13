<script setup lang="ts">
import type { PtStrategy } from '~/types/paperTracks'
// One tracked strategy on the /paper list: where it stands, and against what.
const props = defineProps<{ s: PtStrategy, index: number }>()

const own = computed(() => paperBook(props.s, 'strategy'))
const excess = computed(() => paperExcess(props.s))
const spark = computed(() => (props.s.nav ?? []).map(p => p.values.strategy).filter((v): v is number => isNum(v)))
const owner = computed(() => paperParadigmOwner(props.s.name))
const orders = computed(() => props.s.pending?.orders?.length ?? 0)
</script>

<template>
  <NuxtLink :to="`/paper/${s.name}`" class="block h-full min-w-0">
    <UCard class="h-full min-w-0 overflow-hidden transition hover:shadow-md hover:ring-primary/40" :ui="{ body: 'p-4 sm:p-4 h-full flex flex-col gap-3' }">
      <div class="flex items-start justify-between gap-3">
        <div class="min-w-0">
          <div class="text-[11px] font-medium uppercase tracking-wider text-dimmed">Strategy {{ index + 1 }}</div>
          <div class="truncate font-semibold text-highlighted">{{ paperName(s.name) }}</div>
          <div v-if="owner" class="truncate text-xs text-muted">Copies {{ owner }}</div>
        </div>
        <div v-if="s.days > 0" class="shrink-0 text-right">
          <Change :value="own?.total_return" pill class="text-base" />
          <div class="mt-1 text-[11px] text-dimmed">since {{ dateShort(s.start) }}</div>
        </div>
        <UBadge v-else color="neutral" variant="subtle" icon="i-lucide-hourglass" label="Not started" />
      </div>

      <p class="line-clamp-2 text-xs text-muted" :title="s.spec.signal">{{ s.spec.signal }}</p>
      <p class="-mt-2 truncate text-[11px] text-dimmed" :title="s.spec.universe">Picks from: {{ s.spec.universe }}</p>

      <template v-if="s.days > 0">
        <div class="min-w-0 overflow-hidden"><Sparkline :data="spark" :width="320" :height="36" class="block w-full" preserveAspectRatio="none" /></div>
        <div class="mt-auto grid grid-cols-3 gap-2 border-t border-default pt-3 text-xs">
          <div>
            <div class="text-dimmed">Today</div>
            <Change :value="own?.day_return" :digits="2" />
          </div>
          <div>
            <UTooltip text="Strategy return minus the return of every eligible stock held in equal amounts.">
              <div class="cursor-help text-dimmed underline decoration-dotted underline-offset-2">vs market</div>
            </UTooltip>
            <Change :value="excess" />
          </div>
          <div>
            <div class="text-dimmed">Record</div>
            <div class="num font-medium text-default">{{ s.days }} days</div>
          </div>
        </div>
      </template>
      <div v-else class="mt-auto border-t border-default pt-3 text-xs text-muted">
        Frozen {{ dateShort(s.start) }} · first fill still to come<template v-if="orders"> · {{ orders }} orders on the sheet</template>
      </div>
    </UCard>
  </NuxtLink>
</template>
