<script setup lang="ts">
type Dict = Record<string, unknown>

const props = defineProps<{ symbol: string | null }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const api = useOperatorApi()
const data = ref<Dict | null>(null)
const pending = ref(false)
const loadError = ref(false)

function asDict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Dict : {}
}
function asList(value: unknown): Dict[] {
  return Array.isArray(value) ? value.filter((item) => typeof item === 'object' && item !== null) as Dict[] : []
}
function display(value: unknown) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(2)
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  return String(value)
}

async function load(symbol: string) {
  pending.value = true
  loadError.value = false
  data.value = null
  try {
    data.value = await api.getSymbolWhy(symbol) as Dict
  } catch {
    loadError.value = true
  } finally {
    pending.value = false
  }
}

watch(() => props.symbol, (sym) => { if (sym) load(sym) }, { immediate: true })

const deterministic = computed(() => asDict(data.value?.deterministic))
const grounding = computed(() => asDict(data.value?.grounding))
const hypothesis = computed(() => asDict(data.value?.hypothesis))
const drivenBy = computed(() => String(data.value?.driven_by || ''))
const hasHypothesis = computed(() => Object.keys(hypothesis.value).length > 0)

function drivenByClass(kind: string) {
  if (kind === 'hypothesis') return 'border-moss/30 bg-moss/15 text-moss'
  if (kind === 'event') return 'border-sun/40 bg-sun/15 text-ink'
  return 'border-ink/15 bg-white/70 text-ink/60'
}
const llm = computed(() => asDict(data.value?.llm_decision))
const packet = computed(() => asDict(data.value?.evidence_packet))
const packetError = computed(() => data.value?.evidence_packet_error)

// Verdict dimensions are the packet's known top-level evidence keys; each is {present, status,
// direction, confidence, ...}. Show direction when present, else classification/status.
const DIMENSION_KEYS = [
  'event_provenance', 'fundamental', 'benchmark_excess', 'sector_reliability',
  'exact_class_reliability', 'technical_confirmation', 'market_context', 'risk', 'hypothesis_match'
]
const verdicts = computed(() => {
  const p = packet.value
  return DIMENSION_KEYS
    .filter((key) => p && typeof p[key] === 'object' && p[key] !== null)
    .map((key) => {
      const d = asDict(p[key])
      const direction = d.direction ?? d.classification ?? d.status ?? '-'
      return { name: key, direction, confidence: d.confidence, present: Boolean(d.present) }
    })
})

function dirClass(direction: unknown) {
  const d = String(direction || '').toLowerCase()
  if (d.includes('support')) return 'border-moss/25 bg-moss/10 text-moss'
  if (d.includes('contradict')) return 'border-rust/30 bg-rust/10 text-rust'
  return 'border-ink/15 bg-white/70 text-ink/55'
}
</script>

<template>
  <div class="fixed inset-0 z-50 flex justify-end bg-ink/30 backdrop-blur-sm" @click.self="emit('close')">
    <aside class="h-full w-full max-w-2xl overflow-y-auto bg-paper px-7 py-6 shadow-2xl">
      <div class="flex items-center justify-between">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/40">Why · review-only</p>
          <h2 class="text-2xl font-black tracking-tight text-ink">{{ symbol }}</h2>
        </div>
        <button class="rounded-full border border-ink/15 bg-white/70 px-4 py-2 text-sm font-semibold hover:bg-white"
                @click="emit('close')">Close</button>
      </div>

      <p v-if="pending" class="mt-8 text-sm text-ink/40">Loading the full evidence…</p>
      <div v-else-if="loadError" class="mt-6 rounded-2xl border border-rust/25 bg-rust/10 px-5 py-4 text-sm text-rust">
        Could not load the decision detail for this symbol.
      </div>

      <div v-else-if="data" class="mt-6 space-y-6">
        <!-- What drives this decision (nature of the reason) + hypothesis link -->
        <div class="flex flex-wrap items-center gap-2">
          <span v-if="drivenBy" class="rounded-full border px-3 py-1 text-xs font-black uppercase tracking-wide" :class="drivenByClass(drivenBy)">
            driven by {{ drivenBy }}
          </span>
        </div>
        <section v-if="hasHypothesis" class="rounded-2xl border border-moss/25 bg-moss/5 p-5">
          <h3 class="text-xs font-black uppercase tracking-wide text-moss/80">Hypothesis-driven</h3>
          <div class="mt-2 flex flex-wrap items-center gap-3">
            <NuxtLink :to="`/hypotheses?hypothesis_id=${encodeURIComponent(String(hypothesis.hypothesis_id || ''))}`"
                      class="text-base font-black text-ink underline-offset-2 hover:underline">
              {{ display(hypothesis.title || hypothesis.hypothesis_id) }}
            </NuxtLink>
            <span class="rounded-full border border-ink/15 bg-white/70 px-2.5 py-1 text-xs font-semibold">status: {{ display(hypothesis.status) }}</span>
            <span v-if="hypothesis.match_status" class="rounded-full border border-ink/15 bg-white/70 px-2.5 py-1 text-xs text-ink/55">match: {{ display(hypothesis.match_status) }}</span>
            <span v-if="hypothesis.direction" class="rounded-full border border-ink/15 bg-white/70 px-2.5 py-1 text-xs text-ink/55">{{ display(hypothesis.direction) }}</span>
            <span v-if="hypothesis.match_score !== undefined && hypothesis.match_score !== null" class="text-xs text-ink/45">score {{ display(hypothesis.match_score) }}</span>
          </div>
          <p v-if="hypothesis.description" class="mt-2 text-sm text-ink/65">{{ display(hypothesis.description) }}</p>
        </section>

        <!-- Grounding: mode + supports + guards -->
        <section class="rounded-2xl border border-ink/10 bg-white/60 p-5">
          <h3 class="text-xs font-black uppercase tracking-wide text-ink/45">Grounding</h3>
          <div class="mt-3 flex flex-wrap gap-2 text-sm">
            <span class="rounded-full border border-ink/15 bg-white/70 px-3 py-1 font-semibold">
              mode: {{ display(grounding.decision_mode) }}
            </span>
            <span class="rounded-full border border-ink/15 bg-white/70 px-3 py-1 font-semibold">
              grounded: {{ display(grounding.data_grounded) }}
            </span>
            <span class="rounded-full border border-ink/15 bg-white/70 px-3 py-1 font-semibold">
              thesis supports: {{ Array.isArray(grounding.thesis_supports) ? (grounding.thesis_supports as unknown[]).length : display(grounding.thesis_supports) }}
            </span>
            <span class="rounded-full border border-ink/15 bg-white/70 px-3 py-1 text-xs text-ink/55">
              timing: {{ display(grounding.technical_direction) }}{{ grounding.timing_ok ? ' ✓' : '' }}
            </span>
            <span class="rounded-full border border-ink/15 bg-white/70 px-3 py-1 text-xs text-ink/55">
              regime: {{ display(grounding.regime_state) }}
            </span>
          </div>
          <p v-if="Array.isArray(grounding.grounding_failures) && (grounding.grounding_failures as unknown[]).length" class="mt-3 text-sm text-rust/80">
            Failures: {{ (grounding.grounding_failures as unknown[]).join(', ') }}
          </p>
        </section>

        <!-- Evidence verdicts per dimension -->
        <section class="rounded-2xl border border-ink/10 bg-white/60 p-5">
          <h3 class="text-xs font-black uppercase tracking-wide text-ink/45">Evidence dimensions</h3>
          <div v-if="packetError" class="mt-3 rounded-xl border border-rust/25 bg-rust/10 px-4 py-3 text-sm text-rust">
            {{ packetError }}
          </div>
          <div v-else-if="verdicts.length" class="mt-3 space-y-2">
            <div v-for="v in verdicts" :key="v.name" class="flex items-center justify-between gap-3">
              <span class="text-sm font-semibold text-ink/70">{{ v.name }}</span>
              <span class="flex items-center gap-2">
                <span class="rounded-full border px-2.5 py-1 text-xs font-semibold" :class="dirClass(v.direction)">
                  {{ display(v.direction) }}
                </span>
                <span class="text-xs text-ink/45">conf {{ display(v.confidence) }}</span>
              </span>
            </div>
          </div>
          <p v-else class="mt-3 text-sm text-ink/40">No verdict dimensions available for this date.</p>
        </section>

        <!-- Deterministic recommendation reason -->
        <section class="rounded-2xl border border-ink/10 bg-white/60 p-5">
          <h3 class="text-xs font-black uppercase tracking-wide text-ink/45">Deterministic recommendation</h3>
          <div class="mt-3 flex flex-wrap gap-2 text-sm">
            <span class="rounded-full border border-ink/15 bg-white/70 px-3 py-1 font-semibold">{{ display(deterministic.action_code) }}</span>
            <span class="rounded-full border border-ink/15 bg-white/70 px-3 py-1 text-xs text-ink/55">contract: {{ display(deterministic.reason_contract_status) }}</span>
          </div>
          <p v-if="deterministic.action_reason" class="mt-3 text-sm text-ink/70">{{ display(deterministic.action_reason) }}</p>
          <p v-if="deterministic.action_detail" class="mt-1 text-sm text-ink/55">{{ display(deterministic.action_detail) }}</p>
        </section>

        <!-- LLM decision -->
        <section v-if="Object.keys(llm).length" class="rounded-2xl border border-ink/10 bg-white/60 p-5">
          <h3 class="text-xs font-black uppercase tracking-wide text-ink/45">LLM decision</h3>
          <div class="mt-3 flex flex-wrap gap-2 text-sm">
            <span class="rounded-full border border-ink/15 bg-white/70 px-3 py-1 font-semibold">{{ display(llm.proposed_action) }}</span>
            <span class="rounded-full border border-ink/15 bg-white/70 px-3 py-1 text-xs text-ink/55">mode: {{ display(llm.decision_mode) }}</span>
            <span class="rounded-full border border-ink/15 bg-white/70 px-3 py-1 text-xs text-ink/55">conviction: {{ display(llm.conviction) }}</span>
          </div>
          <p v-if="llm.rationale" class="mt-3 text-sm text-ink/70">{{ display(llm.rationale) }}</p>
        </section>
      </div>
    </aside>
  </div>
</template>
