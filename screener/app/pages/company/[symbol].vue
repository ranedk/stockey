<script setup lang="ts">
const route = useRoute()
const symbol = computed(() => String(route.params.symbol).toUpperCase())
const { data, error } = useApi<any>(() => `companies/${encodeURIComponent(symbol.value)}`)
const c = computed(() => data.value?.company)
const m = computed(() => data.value?.industry_median)
useHead({ title: () => c.value?.name || symbol.value })

// price
const range = ref('1y')
const chartMode = ref<'line' | 'candle'>('line')
const { data: px, status: pxStatus } = useApi<any>(() => `companies/${encodeURIComponent(symbol.value)}/prices?range=${range.value}`)
const rangeItems = ['6m', '1y', '3y', '5y', 'max'].map(v => ({ label: v.toUpperCase(), value: v }))
const rangeReturn = computed(() => {
  const bars = px.value?.bars || []
  const from = bars.find((b: any) => b.d >= px.value?.visible_from)
  return from && bars.length ? bars.at(-1).c / from.c - 1 : null
})

// financials
const finPeriod = ref<'q' | 'fy'>('q')
const basis = ref<string | undefined>()
const { data: fin } = useApi<any>(() => `companies/${encodeURIComponent(symbol.value)}/financials?period=${finPeriod.value}${basis.value ? `&basis=${basis.value}` : ''}`)
const { data: finFy } = useApi<any>(() => `companies/${encodeURIComponent(symbol.value)}/financials?period=fy${basis.value ? `&basis=${basis.value}` : ''}`)
const rows = computed<any[]>(() => (fin.value?.rows || []).slice(finPeriod.value === 'q' ? -12 : -7))
const fyRows = computed<any[]>(() => (finFy.value?.rows || []).slice(-7))
const isFin = computed(() => ['bank', 'nbfc', 'insurer'].includes(c.value?.family))
const series = (key: string) => rows.value.map(r => ({ label: periodLabel(r.period_end, finPeriod.value), value: r[key] ?? null }))

const crShort = (v: number) => croreShort(v)
const pctFmt = (v: number) => pct(v, 1)

// statement table
const statement = ref<'pl' | 'bs' | 'cf'>('pl')
const LINES: Record<string, { key: string, label: string, fmt?: 'pct' | 'eps', strong?: boolean }[]> = {
  pl: [
    { key: 'sales', label: 'Sales', strong: true }, { key: 'expenses', label: 'Expenses' },
    { key: 'ebitda', label: 'Operating profit (EBITDA)', strong: true }, { key: 'opm', label: 'Operating margin', fmt: 'pct' },
    { key: 'other_income', label: 'Other income' }, { key: 'finance_costs', label: 'Interest' },
    { key: 'depreciation', label: 'Depreciation' }, { key: 'pbt', label: 'Profit before tax', strong: true },
    { key: 'tax_rate', label: 'Tax rate', fmt: 'pct' }, { key: 'profit', label: 'Net profit', strong: true },
    { key: 'npm', label: 'Net margin', fmt: 'pct' }, { key: 'eps', label: 'EPS (₹)', fmt: 'eps' },
    { key: 'sales_yoy', label: 'Sales growth YoY', fmt: 'pct' }, { key: 'profit_yoy', label: 'Profit growth YoY', fmt: 'pct' },
  ],
  pl_bank: [
    { key: 'revenue', label: 'Interest earned', strong: true }, { key: 'interest_expended', label: 'Interest paid' },
    { key: 'other_income', label: 'Other income' }, { key: 'operating_expenses', label: 'Operating expenses' },
    { key: 'operating_profit', label: 'Operating profit', strong: true }, { key: 'provisions', label: 'Provisions' },
    { key: 'pbt', label: 'Profit before tax' }, { key: 'profit', label: 'Net profit', strong: true },
    { key: 'eps', label: 'EPS (₹)', fmt: 'eps' }, { key: 'gross_npa_pct', label: 'Gross NPA', fmt: 'pct' },
    { key: 'net_npa_pct', label: 'Net NPA', fmt: 'pct' }, { key: 'profit_yoy', label: 'Profit growth YoY', fmt: 'pct' },
  ],
  bs: [
    { key: 'total_equity', label: 'Equity', strong: true }, { key: 'minority', label: 'Minority interest' },
    { key: 'borrowings', label: 'Borrowings', strong: true }, { key: 'payables', label: 'Trade payables' },
    { key: 'total_assets', label: 'Total assets', strong: true }, { key: 'ppe', label: 'Fixed assets' },
    { key: 'cwip', label: 'Capital work in progress' }, { key: 'investments', label: 'Investments' },
    { key: 'inventories', label: 'Inventories' }, { key: 'receivables', label: 'Trade receivables' },
    { key: 'cash', label: 'Cash and equivalents' }, { key: 'debt_equity', label: 'Debt / equity', fmt: 'eps' },
    { key: 'roe', label: 'ROE', fmt: 'pct' }, { key: 'roce', label: 'ROCE', fmt: 'pct' },
  ],
  cf: [
    { key: 'cfo', label: 'Cash from operations', strong: true }, { key: 'capex', label: 'Capital spending' },
    { key: 'fcf', label: 'Free cash flow', strong: true }, { key: 'cfi', label: 'Cash from investing' },
    { key: 'cff', label: 'Cash from financing' }, { key: 'dividends_paid', label: 'Dividends paid' },
    { key: 'cash_conversion', label: 'Cash conversion (CFO / profit)', fmt: 'eps' },
  ],
}
const stmtRows = computed(() => (statement.value === 'pl' ? rows.value : fyRows.value))
const stmtPeriod = computed(() => (statement.value === 'pl' ? finPeriod.value : 'fy'))
const lines = computed(() => {
  const key = statement.value === 'pl' && c.value?.family === 'bank' ? 'pl_bank' : statement.value
  return LINES[key]!.filter(l => stmtRows.value.some(r => isNum(r[l.key])))
})
const cell = (v: any, f?: string) => (f === 'pct' ? pct(v) : f === 'eps' ? (isNum(v) ? v.toFixed(2) : '—') : crore(v, { unit: false }))

// peers
const peerCols = [
  { key: 'name', label: 'Company' }, { key: 'mcap', label: 'Market cap' }, { key: 'pe', label: 'P/E' },
  { key: 'roce', label: 'ROCE' }, { key: 'sales_yoy', label: 'Sales growth' }, { key: 'opm', label: 'Op. margin' },
  { key: 'ret_1y', label: '1Y' }, { key: 'stage', label: 'Trend' },
]

const verdicts = computed(() => c.value ? [
  { title: 'Growth', icon: 'i-lucide-sprout', v: growthVerdict(c.value),
    metrics: [{ label: 'Sales growth (12M)', value: c.value.sales_yoy, median: m.value?.sales_yoy, unit: 'pct', hib: true },
      { label: 'Profit growth (12M)', value: c.value.profit_yoy, median: m.value?.profit_yoy, unit: 'pct', hib: true }] },
  { title: 'Profitability', icon: 'i-lucide-gauge', v: profitVerdict(c.value),
    metrics: isFin.value
      ? [{ label: 'ROE', value: c.value.roe, median: m.value?.roe, unit: 'pct', hib: true }, { label: 'Net margin', value: c.value.npm, median: m.value?.npm, unit: 'pct', hib: true }]
      : [{ label: 'ROCE', value: c.value.roce, median: m.value?.roce, unit: 'pct', hib: true }, { label: 'Operating margin', value: c.value.opm, median: m.value?.opm, unit: 'pct', hib: true }] },
  { title: 'Balance sheet', icon: 'i-lucide-scale', v: balanceVerdict(c.value),
    metrics: isFin.value ? [] : [{ label: 'Debt / equity', value: c.value.debt_equity, median: m.value?.debt_equity, unit: 'x', hib: false }] },
  { title: 'Valuation', icon: 'i-lucide-tag', v: valueVerdict(c.value, m.value),
    metrics: [{ label: 'P/E', value: c.value.pe, median: m.value?.pe, unit: 'x', hib: null }, { label: 'P/B', value: c.value.pb, median: m.value?.pb, unit: 'x', hib: null }] },
  { title: 'Price trend', icon: 'i-lucide-activity', v: trendVerdict(c.value),
    metrics: [{ label: '1-year return', value: c.value.ret_1y, median: m.value?.ret_1y, unit: 'pct', hib: true }] },
] : [])

const stats = computed(() => c.value ? [
  { label: 'Market cap', value: crore(c.value.mcap), hint: c.value.size_band ? `${c.value.size_band} cap · ${SIZE_DESC[c.value.size_band]}` : '' },
  { label: 'P/E', value: mult(c.value.pe), hint: m.value?.pe ? `Industry ${mult(m.value.pe)}` : '' },
  { label: isFin.value ? 'ROE' : 'ROCE', value: pct(isFin.value ? c.value.roe : c.value.roce), hint: 'Last 12 months' },
  { label: 'Sales (12M)', value: crore(c.value.sales), hint: c.value.sales_yoy != null ? `${pct(c.value.sales_yoy, 1, true)} YoY` : '' },
  { label: 'Net profit (12M)', value: crore(c.value.profit), hint: c.value.profit_yoy != null ? `${pct(c.value.profit_yoy, 1, true)} YoY` : '' },
  { label: 'Dividend yield', value: pct(c.value.dividend_yield, 2), hint: 'Last year' },
  { label: 'Avg traded value', value: crore(c.value.traded_value_20d), hint: 'Per day, 20 days' },
  { label: 'Book value', value: mult(c.value.pb), hint: 'Price / book' },
] : [])
</script>

<template>
  <UDashboardPanel id="company">
    <template #header>
      <PageNavbar :title="c?.name || symbol" icon="i-lucide-building-2">
        <template #right>
          <UButton icon="i-lucide-bell-plus" color="neutral" variant="outline" label="Alert" size="sm" disabled class="hidden sm:flex" />
          <UButton icon="i-lucide-plus" label="Watchlist" size="sm" disabled class="hidden sm:flex" />
        </template>
      </PageNavbar>
    </template>

    <template #body>
      <UEmpty v-if="error" icon="i-lucide-search-x" :title="`No company called ${symbol}`" description="Try the search box (Ctrl K)." />
      <div v-else-if="c" class="mx-auto w-full max-w-7xl space-y-6">
        <!-- header -->
        <div class="grid gap-6 lg:grid-cols-[1fr_auto]">
          <div class="min-w-0 space-y-3">
            <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
              <h1 class="text-2xl font-semibold tracking-tight text-highlighted sm:text-3xl">{{ c.name }}</h1>
              <UBadge :label="c.symbol" color="neutral" variant="outline" class="font-mono" />
              <UBadge v-if="c.series === 'SM' || c.series === 'ST'" label="SME" color="neutral" variant="soft" />
            </div>
            <UBreadcrumb v-if="c.sector" :items="[{ label: c.macro_sector }, { label: c.sector, to: { path: '/screener', query: { sector: c.sector } } }, { label: c.industry, to: { path: '/screener', query: { industry: c.industry } } }, { label: c.basic_industry }].filter(i => i.label)"
              :ui="{ link: 'text-xs', separatorIcon: 'size-3' }" />
            <div class="flex flex-wrap items-center gap-1.5">
              <UTooltip v-if="c.size_band" :text="SIZE_DESC[c.size_band]">
                <UBadge :label="`${c.size_band} cap`" color="primary" variant="subtle" icon="i-lucide-layers" />
              </UTooltip>
              <StageBadge :stage="c.stage" />
              <TagBadge v-for="t in c.tags" :key="t" :tag="t" :rule="data?.tag_rules?.[t]" />
              <UTooltip v-if="data?.industry_rank" :text="`By market cap among ${data.industry_rank.of} companies in ${c.industry}`">
                <UBadge :label="`#${data.industry_rank.rank} in industry`" color="neutral" variant="subtle" icon="i-lucide-trophy" />
              </UTooltip>
            </div>
          </div>
          <div class="w-full space-y-3 lg:w-72 lg:text-right">
            <div>
              <div class="num text-3xl font-semibold tracking-tight text-highlighted">{{ inr(c.price) }}</div>
              <div class="mt-1 flex items-center gap-2 lg:justify-end">
                <Change :value="c.ret_1d" :digits="2" pill />
                <span class="text-xs text-muted">today · close {{ dateShort(c.price_date) }}</span>
              </div>
            </div>
            <RangeBar :low="c.low_52w" :high="c.high_52w" :value="c.price" />
          </div>
        </div>

        <!-- key stats -->
        <div class="grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-default bg-[var(--ui-border)] sm:grid-cols-4 xl:grid-cols-8">
          <div v-for="s in stats" :key="s.label" class="bg-default p-3">
            <div class="text-xs text-muted">{{ s.label }}</div>
            <div class="num mt-1 font-semibold text-highlighted">{{ s.value }}</div>
            <div class="mt-0.5 truncate text-[11px] text-dimmed">{{ s.hint }}</div>
          </div>
        </div>

        <!-- chart -->
        <UCard :ui="{ body: 'p-2 sm:p-4' }">
          <template #header>
            <div class="flex flex-wrap items-center justify-between gap-3">
              <div class="flex items-center gap-3">
                <h3 class="font-semibold text-highlighted">Price</h3>
                <span v-if="rangeReturn != null" class="text-sm"><Change :value="rangeReturn" /> <span class="text-xs text-muted">over {{ range.toUpperCase() }}</span></span>
              </div>
              <div class="flex items-center gap-2">
                <div class="hidden md:flex items-center gap-3 text-xs text-muted mr-2">
                  <span class="inline-flex items-center gap-1.5"><span class="h-0.5 w-3 rounded bg-[var(--series-2)]" />50-day avg</span>
                  <span class="inline-flex items-center gap-1.5"><span class="h-0.5 w-3 rounded bg-[var(--series-7)]" />200-day avg</span>
                </div>
                <UFieldGroup size="xs">
                  <UButton :color="chartMode === 'line' ? 'primary' : 'neutral'" :variant="chartMode === 'line' ? 'soft' : 'ghost'" icon="i-lucide-chart-spline" aria-label="Line" @click="chartMode = 'line'" />
                  <UButton :color="chartMode === 'candle' ? 'primary' : 'neutral'" :variant="chartMode === 'candle' ? 'soft' : 'ghost'" icon="i-lucide-chart-candlestick" aria-label="Candles" @click="chartMode = 'candle'" />
                </UFieldGroup>
                <UTabs v-model="range" :items="rangeItems" size="xs" :content="false" />
              </div>
            </div>
          </template>
          <PriceChart v-if="px?.bars?.length" :bars="px.bars" :visible-from="px.visible_from" :mode="chartMode" />
          <USkeleton v-else-if="pxStatus === 'pending'" class="h-[400px]" />
          <UEmpty v-else icon="i-lucide-chart-no-axes-combined" title="No price history" />
        </UCard>

        <!-- scorecard -->
        <div>
          <div class="mb-3 flex flex-col gap-0.5 sm:flex-row sm:items-baseline sm:justify-between">
            <h2 class="text-lg font-semibold text-highlighted">At a glance</h2>
            <span v-if="m" class="text-xs text-muted">Compared with the median of {{ m.n }} companies in {{ c.industry }}</span>
          </div>
          <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
            <UCard v-for="v in verdicts" :key="v.title" :ui="{ body: 'p-4 sm:p-4 space-y-3' }">
              <div class="flex items-center justify-between gap-2">
                <div class="flex items-center gap-2 text-sm font-medium text-highlighted">
                  <UIcon :name="v.icon" class="size-4 text-muted" />{{ v.title }}
                </div>
              </div>
              <UTooltip :text="v.v.rule">
                <UBadge :label="v.v.label" :color="v.v.color" variant="subtle" size="lg" class="font-semibold" />
              </UTooltip>
              <VsIndustry v-for="x in v.metrics" :key="x.label" :label="x.label" :value="x.value" :median="x.median" :unit="x.unit as any" :higher-is-better="x.hib" />
            </UCard>
          </div>
        </div>

        <!-- financial charts -->
        <div v-if="fin?.rows?.length">
          <div class="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 class="text-lg font-semibold text-highlighted">Financials</h2>
              <p class="text-xs text-muted">From the company's exchange filings (XBRL), as filed · ₹ crore</p>
            </div>
            <div class="flex items-center gap-2">
              <USelect v-if="(fin?.bases?.length || 0) > 1" :model-value="fin.basis" :items="fin.bases.map((b: string) => ({ label: b === 'consolidated' ? 'Consolidated' : 'Standalone', value: b }))" size="xs" class="w-36" @update:model-value="(v: any) => basis = v" />
              <UTabs v-model="finPeriod" :items="[{ label: 'Quarterly', value: 'q' }, { label: 'Annual', value: 'fy' }]" size="xs" :content="false" />
            </div>
          </div>
          <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            <UCard :ui="{ body: 'p-4 sm:p-4' }">
              <div class="mb-1 flex items-baseline justify-between"><span class="text-sm font-medium">{{ c.family === 'bank' ? 'Interest earned' : 'Sales' }}</span><Change :value="rows.at(-1)?.sales_yoy" class="text-xs" /></div>
              <FinBars :rows="series('sales')" :format="crShort" />
            </UCard>
            <UCard :ui="{ body: 'p-4 sm:p-4' }">
              <div class="mb-1 flex items-baseline justify-between"><span class="text-sm font-medium">Net profit</span><Change :value="rows.at(-1)?.profit_yoy" class="text-xs" /></div>
              <FinBars :rows="series('profit')" :format="crShort" color="var(--series-3)" />
            </UCard>
            <UCard :ui="{ body: 'p-4 sm:p-4' }">
              <div class="mb-1 text-sm font-medium">{{ isFin ? 'Net margin' : 'Operating margin' }}</div>
              <FinBars :rows="series(isFin ? 'npm' : 'opm')" :format="pctFmt" kind="line" color="var(--series-7)" />
            </UCard>
            <UCard v-if="fyRows.some(r => isNum(r.cfo))" :ui="{ body: 'p-4 sm:p-4' }">
              <div class="mb-1 text-sm font-medium">Cash flow <span class="text-xs font-normal text-muted">· yearly</span></div>
              <GroupedBars :labels="fyRows.map(r => periodLabel(r.period_end, 'fy'))" :format="crShort"
                :series="[{ name: 'Net profit', values: fyRows.map(r => r.profit ?? null) }, { name: 'Operating cash flow', values: fyRows.map(r => r.cfo ?? null) }, { name: 'Free cash flow', values: fyRows.map(r => r.fcf ?? null) }]" />
            </UCard>
            <UCard v-if="!isFin && fyRows.some(r => isNum(r.borrowings))" :ui="{ body: 'p-4 sm:p-4' }">
              <div class="mb-1 text-sm font-medium">Debt and cash <span class="text-xs font-normal text-muted">· year end</span></div>
              <GroupedBars :labels="fyRows.map(r => periodLabel(r.period_end, 'fy'))" :format="crShort"
                :series="[{ name: 'Borrowings', values: fyRows.map(r => r.borrowings ?? null) }, { name: 'Cash', values: fyRows.map(r => r.cash ?? null) }]" />
            </UCard>
            <UCard v-if="fyRows.some(r => isNum(r.roe))" :ui="{ body: 'p-4 sm:p-4' }">
              <div class="mb-1 text-sm font-medium">{{ isFin ? 'Return on equity' : 'Return on capital (ROCE)' }} <span class="text-xs font-normal text-muted">· yearly</span></div>
              <FinBars :rows="fyRows.map(r => ({ label: periodLabel(r.period_end, 'fy'), value: (isFin ? r.roe : r.roce) ?? null }))" :format="pctFmt" kind="line" color="var(--series-1)" />
            </UCard>
          </div>

          <!-- statements -->
          <UCard class="mt-3" :ui="{ body: 'p-0 sm:p-0' }">
            <template #header>
              <div class="flex flex-wrap items-center justify-between gap-2">
                <UTabs v-model="statement" :items="[{ label: 'Profit & loss', value: 'pl' }, { label: 'Balance sheet', value: 'bs' }, { label: 'Cash flow', value: 'cf' }]" size="sm" variant="link" :content="false" />
                <span class="text-xs text-muted">{{ statement === 'pl' ? (finPeriod === 'q' ? 'Quarterly' : 'Annual') : 'Annual' }} · {{ fin.basis }} · ₹ crore</span>
              </div>
            </template>
            <div class="overflow-x-auto">
              <table class="w-full min-w-max text-sm">
                <thead>
                  <tr class="border-b border-default text-xs text-muted">
                    <th class="sticky left-0 z-10 bg-default px-4 py-2 text-left font-medium">&nbsp;</th>
                    <th v-for="r in stmtRows" :key="r.period_end" class="px-3 py-2 text-right font-medium whitespace-nowrap">{{ periodLabel(r.period_end, stmtPeriod) }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="l in lines" :key="l.key" class="border-b border-default/60 last:border-0 hover:bg-elevated/40" :class="l.fmt ? 'text-muted' : ''">
                    <td class="sticky left-0 z-10 bg-default px-4 py-2 whitespace-nowrap" :class="l.strong ? 'font-medium text-highlighted' : ''">{{ l.label }}</td>
                    <td v-for="r in stmtRows" :key="r.period_end" class="num px-3 py-2 text-right whitespace-nowrap" :class="[l.strong ? 'font-medium text-highlighted' : '', isNum(r[l.key]) && r[l.key] < 0 ? 'text-down' : '']">{{ cell(r[l.key], l.fmt) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <template #footer>
              <div class="flex items-center gap-1.5 text-xs text-muted">
                <UIcon name="i-lucide-info" class="size-3.5" />
                Latest figures were made public {{ dateShort(rows.at(-1)?.public_at) }}. Figures are shown as filed; later restatements are not applied.
              </div>
            </template>
          </UCard>
        </div>
        <UAlert v-else icon="i-lucide-file-question" color="neutral" variant="subtle" title="No financial statements filed in XBRL"
          description="This company has no machine-readable results on the exchange (common for SME and recently listed companies)." />

        <!-- peers -->
        <UCard v-if="data?.peers?.length > 1" :ui="{ body: 'p-0 sm:p-0' }">
          <template #header>
            <div class="flex items-baseline justify-between gap-2">
              <h3 class="font-semibold text-highlighted">Peers <span class="text-sm font-normal text-muted">· {{ c.basic_industry }}</span></h3>
              <UButton :to="{ path: '/screener', query: { industry: c.industry } }" size="xs" variant="link" trailing-icon="i-lucide-arrow-right" label="Screen the industry" />
            </div>
          </template>
          <div class="overflow-x-auto">
            <table class="w-full min-w-max text-sm">
              <thead><tr class="border-b border-default text-xs text-muted">
                <th v-for="col in peerCols" :key="col.key" class="px-4 py-2 font-medium" :class="col.key === 'name' ? 'text-left' : 'text-right'">{{ col.label }}</th>
              </tr></thead>
              <tbody>
                <tr v-for="p in data.peers" :key="p.symbol" class="border-b border-default/60 last:border-0 hover:bg-elevated/40" :class="p.symbol === c.symbol ? 'bg-primary/5' : ''">
                  <td class="px-4 py-2"><NuxtLink :to="`/company/${encodeURIComponent(p.symbol)}`" class="font-medium text-highlighted hover:text-primary">{{ p.name }}</NuxtLink></td>
                  <td class="num px-4 py-2 text-right">{{ crore(p.mcap) }}</td>
                  <td class="num px-4 py-2 text-right">{{ mult(p.pe) }}</td>
                  <td class="num px-4 py-2 text-right">{{ pct(p.roce) }}</td>
                  <td class="num px-4 py-2 text-right"><Change :value="p.sales_yoy" /></td>
                  <td class="num px-4 py-2 text-right">{{ pct(p.opm) }}</td>
                  <td class="num px-4 py-2 text-right"><Change :value="p.ret_1y" /></td>
                  <td class="px-4 py-2 text-right"><StageBadge :stage="p.stage" size="xs" short /></td>
                </tr>
              </tbody>
            </table>
          </div>
        </UCard>

        <!-- about -->
        <UCard v-if="data?.profile?.about">
          <template #header><h3 class="font-semibold text-highlighted">About</h3></template>
          <p class="max-w-3xl text-sm leading-relaxed text-toned">{{ data.profile.about }}</p>
        </UCard>
      </div>
      <div v-else class="mx-auto w-full max-w-7xl space-y-4">
        <USkeleton class="h-24" /><USkeleton class="h-16" /><USkeleton class="h-[400px]" />
      </div>
    </template>
  </UDashboardPanel>
</template>
