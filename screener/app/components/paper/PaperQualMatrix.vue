<script setup lang="ts">
import type { PtQualMatrix } from '~/types/paperTracks'
// Every stock in the top fifth of any tracked strategy (or of any variant inside a blend) on its
// latest decision day, and which ones. The more columns a stock fills, the more of them agree.
const props = defineProps<{ matrix: PtQualMatrix }>()

const filter = ref('')
const minCount = ref(1)
const limit = ref(50)

const columns = computed(() => props.matrix.columns ?? [])
const allRows = computed(() => props.matrix.rows ?? [])
const rows = computed(() => {
  const f = filter.value.trim().toUpperCase()
  return allRows.value
    .filter(r => r.count >= minCount.value && (!f || r.symbol.includes(f)))
    .sort((a, b) => b.count - a.count || a.symbol.localeCompare(b.symbol))
})
watch([filter, minCount], () => { limit.value = 50 })

// one header cell per strategy spanning its variants
const groups = computed(() => {
  const out: { strategy: string, span: number }[] = []
  for (const c of columns.value) {
    const last = out[out.length - 1]
    if (last && last.strategy === c.strategy) last.span++
    else out.push({ strategy: c.strategy, span: 1 })
  }
  return out
})
const minItems = computed(() => columns.value.map((_, i) => ({ label: `${i + 1}+ of ${columns.value.length}`, value: i + 1 })))
const strong = computed(() => allRows.value.filter(r => r.count >= Math.ceil(columns.value.length / 2)).length)
</script>

<template>
  <UCard :ui="{ body: 'p-0 sm:p-0' }">
    <template #header>
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div class="max-w-3xl">
          <h3 class="font-semibold text-highlighted">Which strategies each stock qualifies for</h3>
          <p class="mt-1 text-xs text-muted">
            Every stock in the top fifth of any tracked strategy (or of any part of a blend) on the latest decision
            day<template v-if="matrix.as_of">, {{ dateShort(matrix.as_of) }}</template>. Qualifying is not the same as
            holding: each book trades only on its own schedule. The more ticks a stock has, the more strategies agree
            on it. Combination tracks are not shown separately; their columns are their parents'.
          </p>
        </div>
        <div class="flex gap-4 text-right">
          <div>
            <div class="num text-xl font-semibold text-highlighted">{{ num(allRows.length) }}</div>
            <div class="text-[11px] text-muted">stocks qualify</div>
          </div>
          <div>
            <div class="num text-xl font-semibold text-up">{{ num(strong) }}</div>
            <div class="text-[11px] text-muted">in half or more</div>
          </div>
        </div>
      </div>
      <div class="mt-3 flex flex-wrap items-center gap-2">
        <UInput v-model="filter" icon="i-lucide-search" placeholder="Find a symbol" size="sm" class="w-44" />
        <USelect v-model="minCount" :items="minItems" size="sm" class="w-36" />
        <span class="text-xs text-muted">showing {{ num(Math.min(limit, rows.length)) }} of {{ num(rows.length) }}</span>
      </div>
    </template>

    <div class="max-h-[560px] overflow-auto">
      <table class="w-full min-w-max border-separate border-spacing-0 text-xs">
        <thead class="sticky top-0 z-20 bg-default">
          <tr>
            <th class="sticky left-0 z-30 border-b border-default bg-default px-3 py-1.5" />
            <th v-for="g in groups" :key="g.strategy" :colspan="g.span"
              class="max-w-40 border-b border-l border-default px-2 py-1.5 text-center font-semibold text-default">
              <NuxtLink :to="`/paper/${g.strategy}`" class="hover:text-primary">{{ paperName(g.strategy) }}</NuxtLink>
            </th>
            <th class="border-b border-default px-3 py-1.5" />
          </tr>
          <tr class="text-muted">
            <th class="sticky left-0 z-30 border-b border-default bg-default px-3 py-1.5 text-left font-medium">Stock</th>
            <th v-for="(c, i) in columns" :key="c.strategy + '/' + c.variant"
              class="border-b border-default px-2 py-1.5 text-center font-normal"
              :class="i === 0 || columns[i - 1]!.strategy !== c.strategy ? 'border-l' : ''">
              {{ c.label.includes(' · ') ? c.variant : '' }}
            </th>
            <th class="border-b border-default px-3 py-1.5 text-right font-medium">Ticks</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows.slice(0, limit)" :key="r.symbol" class="group">
            <td class="sticky left-0 z-10 border-b border-default bg-default px-3 py-1 font-medium text-highlighted group-hover:bg-elevated">
              <NuxtLink :to="`/company/${encodeURIComponent(r.symbol)}`" class="hover:text-primary">{{ r.symbol }}</NuxtLink>
            </td>
            <td v-for="(m, i) in r.marks" :key="i" class="border-b border-default px-2 py-1 text-center group-hover:bg-elevated/60"
              :class="i === 0 || columns[i - 1]!.strategy !== columns[i]!.strategy ? 'border-l' : ''">
              <UIcon v-if="m" name="i-lucide-check" class="size-3.5 text-up" />
              <span v-else class="text-dimmed">·</span>
            </td>
            <td class="num border-b border-default px-3 py-1 text-right group-hover:bg-elevated/60">
              <span class="font-medium text-highlighted">{{ r.count }}</span><span class="text-dimmed">/{{ columns.length }}</span>
            </td>
          </tr>
        </tbody>
      </table>
      <UEmpty v-if="!rows.length" icon="i-lucide-search-x" title="No stock matches" class="py-8" />
    </div>
    <div v-if="rows.length > limit" class="border-t border-default p-2 text-center">
      <UButton variant="ghost" color="neutral" size="sm" :label="`Show ${Math.min(100, rows.length - limit)} more`" @click="limit += 100" />
    </div>
  </UCard>
</template>
