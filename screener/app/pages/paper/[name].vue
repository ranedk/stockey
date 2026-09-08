<script setup lang="ts">
import type { PaperStrategy, PaperOrder } from '~/types/systrader'

const BOOKS = PAPER_BOOKS
const BOOK_LABEL = PAPER_BOOK_LABEL

const route = useRoute()
const name = computed(() => String(route.params.name))

const api = useSystraderApi()
const { data, status, error } = await useAsyncData(
  () => `paper-${name.value}`,
  () => api.get<PaperStrategy>(`/api/paper/${name.value}`),
  { watch: [name] },
)

const forward = computed<PaperStrategy | undefined>(() => data.value ?? undefined)
const reference = computed<PaperStrategy | null | undefined>(() => data.value?.reference)

// Holdings, worst first: the losers are what a human needs to look at, and
// burying them under a hundred winners is how a book goes wrong quietly.
const holdingsByReturn = computed(() => {
  const rows = [...(forward.value?.holdings ?? [])]
  return rows.sort((a, b) => (a.return ?? 0) - (b.return ?? 0))
})
const showAllHoldings = ref(false)
const shownHoldings = computed(() =>
  showAllHoldings.value ? holdingsByReturn.value : holdingsByReturn.value.slice(0, 12))

const showAllOrders = ref(false)
const orders = computed<PaperOrder[]>(() => forward.value?.pending?.orders ?? [])
const shownOrders = computed(() => showAllOrders.value ? orders.value : orders.value.slice(0, 15))

function pct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return '—'
  return `${(v * 100).toFixed(digits)}%`
}

function signedPct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined) return '—'
  const s = v > 0 ? '+' : ''
  return `${s}${(v * 100).toFixed(digits)}%`
}

const sideTone: Record<string, 'good' | 'bad' | 'warn'> = { BUY: 'good', SELL: 'warn', EXIT: 'bad' }
</script>

<template>
  <div>
    <NuxtLink to="/paper" class="text-xs text-slate-500 hover:text-slate-900">← All strategies</NuxtLink>
    <h1 class="mt-1 text-xl font-semibold">{{ paperStrategyName(name) }}</h1>
    <p class="mt-1 max-w-3xl text-sm text-slate-600">
      A forward record for one frozen strategy. Everything else in this system is scored against
      history, and history can be mined; this page accumulates evidence from data nobody chose.
      Nothing here is a recommendation, and no capital moves off it.
    </p>

    <div v-if="status === 'pending'" class="mt-6 text-sm text-slate-500">Loading…</div>
    <div v-else-if="error" class="mt-6 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
      Could not reach systrader's API. Is <code>cmd/api</code> running on :8090?
    </div>

    <template v-else-if="forward">
      <!-- Headline: where the book stands right now -->
      <section class="mt-5 rounded-lg border border-slate-200 bg-white p-4">
        <div v-if="forward.days > 0" class="flex flex-wrap items-end gap-x-10 gap-y-4">
          <div>
            <div class="text-xs text-slate-500">Since {{ formatDate(forward.start) }}</div>
            <ReturnValue :value="strategyBook(forward)?.total_return" size="lg" />
          </div>
          <div>
            <div class="text-xs text-slate-500">Today</div>
            <ReturnValue :value="strategyBook(forward)?.day_return" size="lg" />
          </div>
          <div>
            <div class="text-xs text-slate-500">Against equal-weight</div>
            <ReturnValue :value="excessReturn(forward)" size="lg" />
          </div>
          <div>
            <div class="text-xs text-slate-500">Positions up / down</div>
            <div class="text-2xl font-semibold tabular-nums">
              <span class="text-emerald-600">{{ forward.winners }}</span>
              <span class="text-slate-300"> / </span>
              <span class="text-rose-600">{{ forward.losers }}</span>
            </div>
          </div>
          <div>
            <div class="text-xs text-slate-500">Max drawdown</div>
            <ReturnValue :value="strategyBook(forward)?.max_drawdown" size="lg" />
          </div>
        </div>
        <div v-else class="flex flex-wrap items-center gap-x-8 gap-y-2 text-sm">
          <div>
            <span class="text-slate-500">Frozen</span>
            <span class="ml-2 font-medium">{{ formatDate(forward.start) }}</span>
          </div>
          <div>
            <span class="text-slate-500">Status</span>
            <span class="ml-2 font-medium text-slate-700">awaiting the first fill</span>
          </div>
          <div v-if="forward.pending?.orders?.length">
            <span class="text-slate-500">On the sheet</span>
            <span class="ml-2 font-medium">{{ forward.pending.orders.length }} orders</span>
          </div>
        </div>
      </section>

      <!-- What is being tracked -->
      <section class="mt-6 rounded-lg border border-slate-200 bg-white p-4">
        <h2 class="text-sm font-semibold text-slate-900">What this strategy is</h2>
        <dl class="mt-3 grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
          <div><dt class="text-xs text-slate-500">Universe</dt><dd>{{ forward.spec.universe }}</dd></div>
          <div><dt class="text-xs text-slate-500">Signal</dt><dd>{{ forward.spec.signal }}</dd></div>
          <div><dt class="text-xs text-slate-500">Selection</dt><dd>{{ forward.spec.selection }}</dd></div>
          <div><dt class="text-xs text-slate-500">Weighting</dt><dd>{{ forward.spec.weighting }}</dd></div>
          <div><dt class="text-xs text-slate-500">Rebalance</dt><dd>{{ forward.spec.rebalance }}</dd></div>
          <div><dt class="text-xs text-slate-500">Execution</dt><dd>{{ forward.spec.execution }}</dd></div>
          <div><dt class="text-xs text-slate-500">Costs assumed</dt><dd>{{ forward.spec.costs }}</dd></div>
          <div><dt class="text-xs text-slate-500">Frozen spec</dt><dd><code class="text-xs">{{ forward.spec.doc }}</code></dd></div>
        </dl>
        <p class="mt-3 border-t border-slate-100 pt-3 text-xs leading-relaxed text-slate-500">
          <span class="font-medium text-slate-700">Why these rules.</span>
          Trend followers are paid by sellers who exit winners early, by mandated rebalancers who
          sell strength, and by traders anchored to stale prices. Cross-sectionally that shows up as
          recent relative winners continuing to outperform — the documented momentum premium, which
          this almost certainly re-finds rather than discovers. Its known failure mode is a momentum
          crash: a sharp rebound off a market bottom punishes a book holding the prior winners.
        </p>
      </section>

      <!-- The order sheet -->
      <section class="mt-6">
        <div class="flex flex-wrap items-baseline justify-between gap-2">
          <h2 class="text-sm font-semibold text-slate-900">Order sheet for the next session</h2>
          <div class="text-xs text-slate-500">
            priced off the close of {{ formatDate(forward.pending?.based_on) }}
            · <BadgePill v-if="forward.pending?.due" label="rebalance due" tone="good" />
            <span v-else>hold — next rebalance in {{ forward.pending?.days_to_due }} trading days</span>
          </div>
        </div>
        <p class="mt-1 text-xs text-slate-500">
          What the frozen rules would place at the next open. On a hold day the list is shown anyway,
          so the book can be watched without being touched.
        </p>

        <div v-if="orders.length === 0" class="mt-3 rounded-lg border border-slate-200 bg-white p-4 text-sm text-slate-500">
          No orders — the book already matches its target.
        </div>
        <div v-else class="mt-3 overflow-hidden rounded-lg border border-slate-200 bg-white">
          <table class="w-full text-sm">
            <thead class="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th class="px-3 py-2 text-left">Side</th>
                <th class="px-3 py-2 text-left">Symbol</th>
                <th class="px-3 py-2 text-right">Weight now</th>
                <th class="px-3 py-2 text-right">Target</th>
                <th class="px-3 py-2 text-right">Reference price</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100">
              <tr v-for="o in shownOrders" :key="o.symbol">
                <td class="px-3 py-1.5"><BadgePill :label="o.side" :tone="sideTone[o.side]" /></td>
                <td class="px-3 py-1.5 font-medium">{{ o.symbol }}</td>
                <td class="px-3 py-1.5 text-right tabular-nums text-slate-500">{{ pct(o.from_weight) }}</td>
                <td class="px-3 py-1.5 text-right tabular-nums">{{ pct(o.to_weight) }}</td>
                <td class="px-3 py-1.5 text-right tabular-nums">{{ formatPrice(o.fill_price) }}</td>
              </tr>
            </tbody>
          </table>
          <button v-if="orders.length > 15" class="w-full border-t border-slate-100 px-3 py-2 text-xs text-slate-600 hover:bg-slate-50"
                  @click="showAllOrders = !showAllOrders">
            {{ showAllOrders ? 'Show fewer' : `Show all ${orders.length} orders` }}
          </button>
        </div>
      </section>

      <!-- Forward performance -->
      <section class="mt-8">
        <h2 class="text-sm font-semibold text-slate-900">Forward record</h2>
        <p class="mt-1 text-xs text-slate-500">
          Started {{ formatDate(forward.start) }}. This is the only evidence that counts:
          every day adds one observation that could not have been mined.
        </p>
        <div v-if="forward.days === 0" class="mt-3 rounded-lg border border-dashed border-slate-300 bg-white p-6 text-center">
          <p class="text-sm font-medium text-slate-700">No trading days yet.</p>
          <p class="mt-1 text-xs text-slate-500">
            The record begins with the first fill after the strategy was frozen. Judgement is
            pre-committed to wait for 12 months and 12 rebalances — not for the first good week.
          </p>
        </div>
        <template v-else>
          <div class="mt-3 rounded-lg border border-slate-200 bg-white p-4">
            <NavChart :points="forward.nav ?? []" :books="BOOKS" />
          </div>
          <table class="mt-3 w-full overflow-hidden rounded-lg border border-slate-200 bg-white text-sm">
            <thead class="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th class="px-3 py-2 text-left">Book</th>
                <th class="px-3 py-2 text-right">Return</th>
                <th class="px-3 py-2 text-right">Max drawdown</th>
                <th class="px-3 py-2 text-right">Holdings</th>
                <th class="px-3 py-2 text-right">Cost drag /yr</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100">
              <tr v-for="s in forward.summaries ?? []" :key="s.book">
                <td class="px-3 py-1.5">{{ BOOK_LABEL[s.book] ?? s.book }}</td>
                <td class="px-3 py-1.5 text-right tabular-nums">{{ signedPct(s.total_return) }}</td>
                <td class="px-3 py-1.5 text-right tabular-nums">{{ signedPct(s.max_drawdown) }}</td>
                <td class="px-3 py-1.5 text-right tabular-nums">{{ s.holdings }}</td>
                <td class="px-3 py-1.5 text-right tabular-nums">{{ pct(s.ann_cost) }}</td>
              </tr>
            </tbody>
          </table>
        </template>
      </section>

      <!-- Current book -->
      <section v-if="(forward.holdings ?? []).length" class="mt-8">
        <div class="flex flex-wrap items-baseline justify-between gap-2">
          <h2 class="text-sm font-semibold text-slate-900">
            Current book
            <span class="text-xs font-normal text-slate-500">as of {{ formatDate(forward.holdings_as_of) }}</span>
          </h2>
          <div class="text-xs text-slate-500">
            {{ forward.winners }} up, {{ forward.losers }} down · worst first
          </div>
        </div>
        <div class="mt-2 overflow-hidden rounded-lg border border-slate-200 bg-white">
          <table class="w-full text-sm">
            <thead class="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th class="px-3 py-2 text-left">Symbol</th>
                <th class="px-3 py-2 text-right">Since entry</th>
                <th class="px-3 py-2 text-right">Weight</th>
                <th class="px-3 py-2 text-left">Entered</th>
                <th class="px-3 py-2 text-right">Entry</th>
                <th class="px-3 py-2 text-right">Last</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100">
              <tr v-for="h in shownHoldings" :key="h.symbol">
                <td class="px-3 py-1.5 font-medium">{{ h.symbol }}</td>
                <td class="px-3 py-1.5 text-right"><ReturnValue :value="h.return" /></td>
                <td class="px-3 py-1.5 text-right tabular-nums text-slate-500">{{ pct(h.weight, 2) }}</td>
                <td class="px-3 py-1.5 text-slate-500">{{ formatDate(h.entry_date) }}</td>
                <td class="px-3 py-1.5 text-right tabular-nums text-slate-500">{{ formatPrice(h.entry_price) }}</td>
                <td class="px-3 py-1.5 text-right tabular-nums">{{ formatPrice(h.last_price) }}</td>
              </tr>
            </tbody>
          </table>
          <button v-if="holdingsByReturn.length > 12"
                  class="w-full border-t border-slate-100 px-3 py-2 text-xs text-slate-600 hover:bg-slate-50"
                  @click="showAllHoldings = !showAllHoldings">
            {{ showAllHoldings ? 'Show fewer' : `Show all ${holdingsByReturn.length} positions` }}
          </button>
        </div>
      </section>

      <!-- In-sample reference, clearly separated -->
      <section v-if="reference" class="mt-10 rounded-lg border border-amber-200 bg-amber-50/40 p-4">
        <h2 class="text-sm font-semibold text-slate-900">
          Backtested reference <BadgePill label="in-sample — not evidence" tone="warn" />
        </h2>
        <p class="mt-1 max-w-3xl text-xs leading-relaxed text-slate-600">
          The same frozen rules run over {{ formatDate(reference.start) }}–{{ formatDate(reference.as_of) }}.
          The hypothesis was found by searching this very data, so these numbers are what searching
          produces and cannot support the strategy — they are here to show what was expected, so the
          forward record above can be compared against it.
        </p>
        <div class="mt-3 rounded-lg border border-slate-200 bg-white p-4">
          <NavChart :points="reference.nav ?? []" :books="BOOKS" />
        </div>
        <table class="mt-3 w-full overflow-hidden rounded-lg border border-slate-200 bg-white text-sm">
          <thead class="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th class="px-3 py-2 text-left">Book</th>
              <th class="px-3 py-2 text-right">Return</th>
              <th class="px-3 py-2 text-right">Max drawdown</th>
              <th class="px-3 py-2 text-right">Holdings</th>
              <th class="px-3 py-2 text-right">Cost drag /yr</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100">
            <tr v-for="s in reference.summaries ?? []" :key="s.book">
              <td class="px-3 py-1.5">{{ BOOK_LABEL[s.book] ?? s.book }}</td>
              <td class="px-3 py-1.5 text-right tabular-nums">{{ signedPct(s.total_return) }}</td>
              <td class="px-3 py-1.5 text-right tabular-nums">{{ signedPct(s.max_drawdown) }}</td>
              <td class="px-3 py-1.5 text-right tabular-nums">{{ s.holdings }}</td>
              <td class="px-3 py-1.5 text-right tabular-nums">{{ pct(s.ann_cost) }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <!-- How it got here -->
      <section class="mt-10 border-t border-slate-200 pt-6">
        <h2 class="text-sm font-semibold text-slate-900">How this strategy got here</h2>
        <ol class="mt-3 max-w-3xl space-y-2 text-xs leading-relaxed text-slate-600">
          <li>
            <span class="font-medium text-slate-800">1. Exploration.</span>
            A slice explorer cut every rule's edge by liquidity, size, volatility, price, sector,
            trend state, market breadth and year — each slice measured against itself on the same
            day. Cross-sectional trend, never previously tested on this universe, came out monotone
            and not explained by taking more risk. 62 buckets were examined, so no bucket was a
            result. (Ledger row 17.)
          </li>
          <li>
            <span class="font-medium text-slate-800">2. Cost screen.</span>
            The portfolio was then run with real turnover and costs against two controls. It held:
            it beat an equal-weight book and a turnover-matched random ranking, survived doubled
            costs, held in both halves of the sample, and sat on a flat nine-point parameter
            plateau. Declared in advance to be capable of killing the idea but not of supporting
            it, because it ran on the same data the idea was mined from. (Ledger row 18.)
          </li>
          <li>
            <span class="font-medium text-slate-800">3. What did NOT survive.</span>
            The most striking exploration finding — that the signal inverts when market breadth
            collapses — moves an annual number by almost nothing, because washouts are 3% of days.
            It is deliberately not in the strategy.
          </li>
          <li>
            <span class="font-medium text-slate-800">4. Forward record.</span>
            The configuration was frozen in writing before any forward data existed, with its kill
            criteria: a negative monthly difference against the equal-weight book over 18 months,
            or realised costs above 100 bps, stops it. Changing any parameter restarts the clock.
          </li>
        </ol>
      </section>
    </template>
  </div>
</template>
