<script setup lang="ts">
useHead({ title: 'Screener' })
const route = useRoute()
const router = useRouter()

const { data: catalog } = useApi<any>('catalog')
const { data: classes } = useApi<any>('meta/classes')
const fields = computed<Record<string, any>>(() => Object.fromEntries((catalog.value?.fields || []).map((f: any) => [f.key, f])))

// ---- state, mirrored in the URL so a screen can be bookmarked and shared
const parseConds = (): Cond[] => {
  const f = route.query.f
  const list = (Array.isArray(f) ? f : f ? [f] : []) as string[]
  const out = list.map((s) => { const [field, op, ...rest] = s.split(':'); return { field: field!, op: op!, value: rest.join(':') } })
  if (route.query.sector) out.push({ field: 'sector', op: 'in', value: String(route.query.sector) })
  if (route.query.industry) out.push({ field: 'industry', op: 'in', value: String(route.query.industry) })
  return out
}
const conds = ref<Cond[]>(parseConds())
const sort = ref(String(route.query.sort || 'mcap'))
const order = ref(String(route.query.order || 'desc'))
const colSet = ref(String(route.query.cols || 'overview'))
const page = ref(Number(route.query.page || 1))
const q = ref(String(route.query.q || ''))
const preset = ref<string | null>(null)
const PAGE = 50

watch([conds, sort, order, colSet, page, q], () => {
  router.replace({ query: {
    f: conds.value.map(c => `${c.field}:${c.op}:${c.value}`), sort: sort.value, order: order.value,
    cols: colSet.value === 'overview' ? undefined : colSet.value, page: page.value > 1 ? page.value : undefined, q: q.value || undefined,
  } })
}, { deep: true })
watch([conds, q], () => { page.value = 1 }, { deep: true })

const query = computed(() => ({
  f: conds.value.map(c => `${c.field}:${c.op}:${c.value}`), sort: sort.value, order: order.value,
  limit: PAGE, offset: (page.value - 1) * PAGE, q: q.value || undefined,
}))
const { data, status } = useFetch<any>('/_api/companies', { query, watch: [query] })

// ---- quick filters
const sizeBands = ['Large', 'Mid', 'Small', 'Micro']
const condFor = (field: string) => conds.value.find(c => c.field === field)
const setIn = (field: string, values: string[]) => {
  conds.value = conds.value.filter(c => c.field !== field)
  if (values.length) conds.value.push({ field, op: 'in', value: values.join(',') })
  preset.value = null
}
const sizeSel = computed({ get: () => condFor('size_band')?.value.split(',') ?? [], set: v => setIn('size_band', v) })
const sectorSel = computed({ get: () => condFor('sector')?.value.split(',') ?? [], set: v => setIn('sector', v) })
const tagSel = computed({
  get: () => conds.value.filter(c => c.field === 'tags').map(c => c.value),
  set: (v: string[]) => { conds.value = [...conds.value.filter(c => c.field !== 'tags'), ...v.map(t => ({ field: 'tags', op: 'has', value: t }))]; preset.value = null },
})
const sectorItems = computed(() => (classes.value?.sectors || []).map((s: any) => ({ label: s.name, value: s.name, suffix: String(s.n) })))
const tagItems = computed(() => Object.keys(catalog.value?.tags || {}))

// ---- custom conditions (numeric fields from the catalogue)
const numericFields = computed(() => (catalog.value?.fields || []).filter((f: any) => ['inr', 'pct', 'x', 'num'].includes(f.unit) && f.key !== 'mcap_rank'))
const fieldGroups = computed(() => {
  const g: Record<string, any[]> = {}
  for (const f of numericFields.value) (g[f.group] ||= []).push({ label: f.label, value: f.key })
  return Object.entries(g).flatMap(([group, items], i) => [...(i ? [{ type: 'separator' }] : []), { type: 'label', label: group }, ...items])
})
const draft = reactive({ field: 'roce', op: 'gte', value: 15 as number | null, open: false })
const draftUnit = computed(() => fields.value[draft.field]?.unit)
const draftHint = computed(() => ({ pct: '%', x: 'x', inr: '₹ Cr', num: '' } as Record<string, string>)[draftUnit.value] ?? '')
function addCond() {
  if (draft.value == null) return
  const u = draftUnit.value
  const v = u === 'pct' ? draft.value / 100 : u === 'inr' ? draft.value * 1e7 : draft.value
  conds.value.push({ field: draft.field, op: draft.op, value: String(+v.toFixed(6)) })
  draft.open = false
  preset.value = null
}
const removeCond = (i: number) => { conds.value.splice(i, 1); preset.value = null }
const chipConds = computed(() => conds.value.map((c, i) => ({ ...c, i })).filter(c => !['size_band', 'sector', 'tags'].includes(c.field)))
function applyPreset(p: typeof PRESETS[number]) {
  if (preset.value === p.id) { preset.value = null; conds.value = []; sort.value = 'mcap'; return }
  preset.value = p.id
  conds.value = p.conds.map(c => ({ ...c }))
  sort.value = p.sort || 'mcap'
  order.value = p.sort === 'pe' ? 'asc' : 'desc'
}
const clearAll = () => { conds.value = []; q.value = ''; preset.value = null }

// ---- table
const cols = computed(() => COLUMN_SETS[colSet.value]!.cols)
function toggleSort(k: string) {
  if (sort.value === k) order.value = order.value === 'desc' ? 'asc' : 'desc'
  else { sort.value = k; order.value = 'desc' }
}
const fmtCell = (k: string, v: any) => byUnit(fields.value[k]?.unit || '', v)
const signed = new Set(['sales_yoy', 'profit_yoy', 'q_sales_yoy', 'q_profit_yoy', 'sales_cagr3', 'ret_1d', 'ret_1w', 'ret_1m', 'ret_3m', 'ret_6m', 'ret_1y', 'from_high_52w', 'dma200_gap', 'dma50_gap'])
const pages = computed(() => Math.max(1, Math.ceil((data.value?.total || 0) / PAGE)))

function exportCsv() {
  const rows = data.value?.rows || []
  const keys = ['symbol', 'name', 'industry', 'price', ...cols.value.filter(k => k !== 'stage'), 'stage']
  const csv = [keys.join(','), ...rows.map((r: any) => keys.map(k => JSON.stringify(r[k] ?? '')).join(','))].join('\n')
  const a = document.createElement('a')
  a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }))
  a.download = 'screen.csv'
  a.click()
}
</script>

<template>
  <UDashboardPanel id="screener">
    <template #header>
      <PageNavbar title="Screener" icon="i-lucide-filter">
        <template #right>
          <UButton icon="i-lucide-download" color="neutral" variant="outline" size="sm" label="Export" class="hidden sm:flex" @click="exportCsv" />
        </template>
      </PageNavbar>
      <UDashboardToolbar class="overflow-x-auto">
        <div class="flex gap-1.5 py-0.5">
          <UButton v-for="p in PRESETS" :key="p.id" :icon="p.icon" :label="p.label" size="xs" class="shrink-0 rounded-full"
            :color="preset === p.id ? 'primary' : 'neutral'" :variant="preset === p.id ? 'solid' : 'outline'" @click="applyPreset(p)" />
        </div>
      </UDashboardToolbar>
    </template>

    <template #body>
      <div class="mx-auto w-full max-w-7xl space-y-4">
        <!-- filters -->
        <UCard :ui="{ body: 'p-3 sm:p-4 space-y-3' }">
          <div class="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            <UInput v-model="q" icon="i-lucide-search" placeholder="Name or symbol" size="sm" />
            <USelectMenu v-model="sizeSel" :items="sizeBands" multiple placeholder="Any size" icon="i-lucide-layers" size="sm" />
            <USelectMenu v-model="sectorSel" :items="sectorItems" value-key="value" multiple placeholder="Any sector" icon="i-lucide-blocks" size="sm" />
            <USelectMenu v-model="tagSel" :items="tagItems" multiple placeholder="Any tag" icon="i-lucide-tags" size="sm" />
          </div>
          <div class="flex flex-wrap items-center gap-1.5">
            <UBadge v-for="c in chipConds" :key="c.i" color="primary" variant="subtle" size="md" class="gap-1 pr-1">
              <span class="font-medium">{{ fields[c.field]?.label || c.field }}</span>
              <span class="opacity-70">{{ OP_LABEL[c.op] }}</span>
              <span class="num">{{ c.op === 'in' ? c.value.replaceAll(',', ', ') : condValue(fields[c.field]?.unit, c.value) }}</span>
              <UButton icon="i-lucide-x" size="xs" variant="link" color="primary" class="p-0" aria-label="Remove" @click="removeCond(c.i)" />
            </UBadge>
            <UPopover v-model:open="draft.open">
              <UButton icon="i-lucide-plus" label="Add condition" size="xs" color="neutral" variant="ghost" />
              <template #content>
                <div class="w-72 space-y-3 p-3">
                  <USelectMenu v-model="draft.field" :items="fieldGroups" value-key="value" class="w-full" size="sm" />
                  <div class="flex gap-2">
                    <USelect v-model="draft.op" :items="[{ label: '≥ at least', value: 'gte' }, { label: '≤ at most', value: 'lte' }, { label: '> above', value: 'gt' }, { label: '< below', value: 'lt' }]" size="sm" class="w-32" />
                    <UInputNumber v-model="draft.value" size="sm" class="flex-1" :step="draftUnit === 'x' ? 0.5 : 1">
                    </UInputNumber>
                  </div>
                  <div class="flex items-center justify-between">
                    <span class="text-xs text-muted">{{ fields[draft.field]?.desc || (draftHint ? `Value in ${draftHint}` : '') }}</span>
                    <UButton label="Add" size="xs" @click="addCond" />
                  </div>
                </div>
              </template>
            </UPopover>
            <UButton v-if="conds.length || q" label="Clear all" size="xs" color="neutral" variant="link" @click="clearAll" />
          </div>
        </UCard>

        <!-- results -->
        <UCard :ui="{ body: 'p-0 sm:p-0', header: 'py-2 sm:py-2' }">
          <template #header>
            <div class="flex flex-wrap items-center justify-between gap-2">
              <div class="flex items-baseline gap-2">
                <span class="num text-lg font-semibold text-highlighted">{{ (data?.total ?? 0).toLocaleString('en-IN') }}</span>
                <span class="text-sm text-muted">companies</span>
                <UIcon v-if="status === 'pending'" name="i-lucide-loader-circle" class="size-4 animate-spin text-muted" />
              </div>
              <UTabs v-model="colSet" :items="Object.entries(COLUMN_SETS).map(([k, v]) => ({ label: v.label, value: k }))" size="xs" :content="false" />
            </div>
          </template>
          <div class="overflow-x-auto">
            <table class="w-full min-w-[900px] text-sm">
              <thead>
                <tr class="border-b border-default text-xs text-muted">
                  <th class="sticky left-0 z-10 bg-default px-4 py-2.5 text-left font-medium">Company</th>
                  <th class="px-2 py-2.5 text-left font-medium">60 days</th>
                  <th v-for="k in ['price', 'ret_1d', ...cols]" :key="k" class="px-3 py-2.5 text-right font-medium whitespace-nowrap">
                    <button class="inline-flex items-center gap-1 hover:text-highlighted" :class="sort === k ? 'text-highlighted' : ''" @click="toggleSort(k)">
                      <UTooltip :text="fields[k]?.desc" :disabled="!fields[k]?.desc"><span>{{ k === 'price' ? 'Price' : fields[k]?.label || k }}</span></UTooltip>
                      <UIcon :name="sort === k ? (order === 'desc' ? 'i-lucide-arrow-down' : 'i-lucide-arrow-up') : 'i-lucide-arrow-down-up'" class="size-3" :class="sort === k ? '' : 'opacity-30'" />
                    </button>
                  </th>
                  <th class="px-4 py-2.5 text-left font-medium">Tags</th>
                </tr>
              </thead>
              <tbody :class="status === 'pending' ? 'opacity-60' : ''">
                <tr v-for="r in data?.rows" :key="r.symbol" class="group border-b border-default/60 last:border-0 hover:bg-elevated/40">
                  <td class="sticky left-0 z-10 max-w-64 bg-default px-4 py-2 group-hover:bg-elevated">
                    <NuxtLink :to="`/company/${encodeURIComponent(r.symbol)}`" class="block">
                      <div class="truncate font-medium text-highlighted group-hover:text-primary">{{ r.name }}</div>
                      <div class="truncate text-xs text-muted"><span class="font-mono">{{ r.symbol }}</span> · {{ r.industry || '—' }}</div>
                    </NuxtLink>
                  </td>
                  <td class="px-2 py-2"><Sparkline :data="r.spark" :width="80" :height="26" /></td>
                  <td class="num px-3 py-2 text-right">{{ inr(r.price) }}</td>
                  <td class="px-3 py-2 text-right"><Change :value="r.ret_1d" /></td>
                  <td v-for="k in cols" :key="k" class="num px-3 py-2 text-right whitespace-nowrap">
                    <StageBadge v-if="k === 'stage'" :stage="r.stage" size="xs" short />
                    <Change v-else-if="signed.has(k)" :value="r[k]" />
                    <span v-else :class="sort === k ? 'font-medium text-highlighted' : ''">{{ fmtCell(k, r[k]) }}</span>
                  </td>
                  <td class="px-4 py-2">
                    <div class="flex gap-1 whitespace-nowrap">
                      <TagBadge v-for="t in (r.tags || []).slice(0, 2)" :key="t" :tag="t" :rule="catalog?.tags?.[t]" size="xs" />
                      <UTooltip v-if="(r.tags || []).length > 2" :text="r.tags.slice(2).join(' · ')">
                        <UBadge :label="`+${r.tags.length - 2}`" size="xs" color="neutral" variant="outline" />
                      </UTooltip>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
            <UEmpty v-if="data && !data.rows.length" icon="i-lucide-filter-x" title="No company matches every condition"
              description="Remove a condition or loosen a value." class="py-12" />
          </div>
          <template #footer>
            <div class="flex items-center justify-between gap-2">
              <span class="text-xs text-muted">Financials: last 12 months from exchange filings · prices: last close</span>
              <UPagination v-model:page="page" :total="data?.total || 0" :items-per-page="PAGE" size="xs" />
            </div>
          </template>
        </UCard>
      </div>
    </template>
  </UDashboardPanel>
</template>
