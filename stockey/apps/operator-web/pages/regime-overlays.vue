<script setup lang="ts">
import type { Dict, RegimeOverlayDecisionResult } from '~/types/api'

const api = useOperatorApi()
const { data, refresh, pending, error } = await useAsyncData('regime-overlays', () => api.getRegimeOverlays(50))

const activeProposalKey = ref('')
const decisionReason = ref('')
const decisionError = ref('')
const decisionResult = ref<RegimeOverlayDecisionResult | null>(null)

const proposals = computed(() => data.value?.proposals || [])
const decisions = computed(() => data.value?.decisions || [])
const summary = computed(() => data.value?.summary || {})
const boundary = computed(() => data.value?.operator_boundary || {})

const decisionOptions = [
  {
    value: 'approve_for_testing',
    label: 'Approve for testing',
    tone: 'moss',
    effect: 'Keeps this overlay available for research/testing only. It does not change action policy, portfolio state, or broker execution.'
  },
  {
    value: 'promote_to_review_rule',
    label: 'Promote to review rule',
    tone: 'sun',
    effect: 'Marks this as a reviewed rule candidate. A separate config/rule implementation is still required before action consolidation can use it.'
  },
  {
    value: 'needs_more_evidence',
    label: 'Needs more evidence',
    tone: 'ink',
    effect: 'Keeps it open for more macro, breadth, news, or outcome evidence. No production behavior changes.'
  },
  {
    value: 'reject',
    label: 'Reject',
    tone: 'rust',
    effect: 'Closes this proposal as not useful right now. It remains audit evidence only.'
  }
]

function display(value: unknown, fallback = '-') {
  if (value === null || value === undefined || value === '') return fallback
  return String(value)
}

function dateText(value: unknown) {
  if (!value) return '-'
  const date = new Date(String(value))
  if (Number.isNaN(date.getTime())) return String(value)
  return date.toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' })
}

function pct(value: unknown) {
  const num = Number(value)
  if (Number.isNaN(num)) return '-'
  return `${Math.round(num * 100)}%`
}

function listValue(value: unknown): unknown[] {
  if (Array.isArray(value)) return value
  if (!value) return []
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value)
      return Array.isArray(parsed) ? parsed : []
    } catch {
      return [value]
    }
  }
  return []
}

function objectValue(value: unknown): Dict {
  if (value && typeof value === 'object' && !Array.isArray(value)) return value as Dict
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value)
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed as Dict : {}
    } catch {
      return {}
    }
  }
  return {}
}

function proposalKey(row: Dict) {
  return `${String(row.asof_date || '')}:${String(row.proposal_id || '')}`
}

function statusClass(value: unknown) {
  const text = String(value || '').toLowerCase()
  if (text.includes('reject')) return 'border-rust/25 bg-rust/10 text-rust'
  if (text.includes('promoted')) return 'border-sun/35 bg-sun/20 text-ink'
  if (text.includes('approved')) return 'border-moss/25 bg-moss/10 text-moss'
  if (text.includes('evidence')) return 'border-ink/15 bg-white/80 text-ink/65'
  return 'border-blue-300 bg-blue-50 text-blue-900'
}

function familyClass(value: unknown) {
  const text = String(value || '').toLowerCase()
  if (text.includes('risk_off') || text.includes('shock')) return 'bg-rust/10 text-rust'
  if (text.includes('risk_on')) return 'bg-moss/10 text-moss'
  if (text.includes('transition')) return 'bg-sun/25 text-ink'
  return 'bg-white/80 text-ink/65'
}

function optionClass(option: Dict) {
  const tone = String(option.tone || '')
  if (tone === 'moss') return 'border-moss/25 bg-moss/10 text-moss hover:bg-moss/15'
  if (tone === 'rust') return 'border-rust/25 bg-rust/10 text-rust hover:bg-rust/15'
  if (tone === 'sun') return 'border-sun/40 bg-sun/25 text-ink hover:bg-sun/35'
  return 'border-black/10 bg-white/80 text-ink hover:bg-white'
}

async function decide(row: Dict, decision: string) {
  activeProposalKey.value = proposalKey(row)
  decisionError.value = ''
  decisionResult.value = null
  try {
    decisionResult.value = await api.decideRegimeOverlay({
      asof_date: row.asof_date,
      proposal_id: row.proposal_id,
      decision,
      decision_reason: decisionReason.value
    })
    decisionReason.value = ''
    await refresh()
  } catch (err) {
    decisionError.value = err instanceof Error ? err.message : String(err)
  } finally {
    activeProposalKey.value = ''
  }
}
</script>

<template>
  <section class="space-y-8">
    <div class="overflow-hidden rounded-[2rem] border border-black/10 bg-gradient-to-br from-slate-950 via-ink to-moss p-8 text-paper shadow-soft">
      <div class="flex flex-col gap-5 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.28em] text-paper/55">Regime Review</p>
          <h1 class="mt-3 text-4xl font-black tracking-tight">Dynamic regime overlays</h1>
          <p class="mt-3 max-w-3xl text-sm font-semibold leading-7 text-paper/75">
            Review LLM/deterministic regime proposals before they are allowed anywhere near action policy. These rows are rule candidates, not trading instructions.
          </p>
        </div>
        <button class="rounded-full bg-paper px-5 py-3 text-sm font-black text-ink shadow-soft hover:bg-sun" :disabled="pending" @click="refresh()">
          Refresh
        </button>
      </div>
      <div class="mt-6 grid gap-3 md:grid-cols-4">
        <MetricTile label="Proposals" :value="display(summary.proposal_count, '0')" note="Returned rows" />
        <MetricTile label="Decisions" :value="display(summary.decision_count, '0')" note="Recent audit rows" />
        <MetricTile label="Review only" :value="summary.review_only ? 'Yes' : 'No'" note="No direct policy change" />
        <MetricTile label="Policy changed" :value="summary.production_policy_changed ? 'Yes' : 'No'" note="Must remain No" />
      </div>
    </div>

    <ApiErrorBanner v-if="error" :error="error" />

    <div class="rounded-[2rem] border border-black/10 bg-white/80 p-5 shadow-soft">
      <p class="text-xs font-black uppercase tracking-[0.22em] text-ink/45">Boundary</p>
      <p class="mt-2 text-sm font-semibold leading-6 text-ink/70">{{ display(boundary.operator_note, 'Accept/reject decisions are review state only.') }}</p>
      <div class="mt-4 flex flex-wrap gap-2 text-xs font-black">
        <span class="rounded-full bg-moss/10 px-3 py-1.5 text-moss">authority: {{ display(boundary.authority_scope, 'review_input_only') }}</span>
        <span class="rounded-full bg-rust/10 px-3 py-1.5 text-rust">action policy changed: {{ boundary.changes_action_policy ? 'yes' : 'no' }}</span>
        <span class="rounded-full bg-rust/10 px-3 py-1.5 text-rust">broker order: {{ boundary.submits_broker_order ? 'yes' : 'no' }}</span>
      </div>
    </div>

    <div v-if="decisionResult" class="rounded-[2rem] border border-moss/25 bg-moss/10 p-5 text-sm text-moss">
      <p class="font-black">Decision recorded: {{ decisionResult.decision }}</p>
      <p class="mt-1 font-semibold">{{ decisionResult.note }}</p>
      <p class="mt-1 text-moss/75">{{ display(decisionResult.decision_effect?.next_step) }}</p>
    </div>
    <div v-if="decisionError" class="rounded-[2rem] border border-rust/25 bg-rust/10 p-5 text-sm font-semibold text-rust">{{ decisionError }}</div>

    <div class="grid gap-6 xl:grid-cols-[1fr_22rem]">
      <div class="space-y-5">
        <div v-if="!proposals.length" class="rounded-[2rem] border border-dashed border-black/15 bg-white/60 p-8 text-center">
          <h2 class="text-xl font-black">No regime overlay proposals yet</h2>
          <p class="mt-2 text-sm text-ink/60">Run <code>python -m advisory.regime_overlay --no-llm</code> or enable <code>--llm</code> to create proposals.</p>
        </div>

        <article v-for="row in proposals" :key="proposalKey(row)" class="rounded-[2rem] border border-black/10 bg-white/85 p-5 shadow-soft">
          <div class="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
            <div>
              <div class="flex flex-wrap gap-2">
                <span class="rounded-full border px-3 py-1 text-xs font-black" :class="statusClass(row.production_status)">{{ display(row.production_status, 'proposed') }}</span>
                <span class="rounded-full px-3 py-1 text-xs font-black" :class="familyClass(row.regime_family)">{{ display(row.regime_family) }}</span>
                <span class="rounded-full bg-white px-3 py-1 text-xs font-black text-ink/65">confidence {{ pct(row.confidence) }}</span>
                <span class="rounded-full bg-white px-3 py-1 text-xs font-black text-ink/65">base {{ display(row.base_regime, 'UNKNOWN') }}</span>
              </div>
              <h2 class="mt-3 text-2xl font-black tracking-tight text-ink">{{ display(row.proposed_regime) }}</h2>
              <p class="mt-2 text-sm font-semibold leading-6 text-ink/70">{{ display(row.summary) }}</p>
            </div>
            <div class="rounded-2xl bg-paper p-4 text-xs font-semibold text-ink/65 lg:w-64">
              <p><b>Asof:</b> {{ dateText(row.asof_date) }}</p>
              <p><b>Expires:</b> {{ dateText(row.expires_on) }}</p>
              <p><b>Bias:</b> {{ display(row.recommended_bias) }}</p>
              <p><b>Fallback:</b> {{ row.fallback_used ? 'yes' : 'no' }}</p>
            </div>
          </div>

          <div class="mt-5 grid gap-4 lg:grid-cols-2">
            <div class="rounded-2xl bg-paper p-4">
              <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">Rationale</p>
              <p class="mt-2 text-sm leading-6 text-ink/75">{{ display(row.rationale) }}</p>
            </div>
            <div class="rounded-2xl bg-paper p-4">
              <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">Positioning Policy Candidate</p>
              <div class="mt-2 space-y-1 text-sm text-ink/75">
                <p v-for="[key, value] in Object.entries(objectValue(row.positioning_policy))" :key="key"><b>{{ key }}:</b> {{ display(value) }}</p>
              </div>
            </div>
          </div>

          <details class="mt-4 rounded-2xl border border-black/10 bg-white/70 p-4">
            <summary class="cursor-pointer text-sm font-black text-ink">Evidence, proposed rules, and operator questions</summary>
            <div class="mt-4 grid gap-4 lg:grid-cols-3">
              <div>
                <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">Evidence</p>
                <ul class="mt-2 space-y-2 text-sm text-ink/75">
                  <li v-for="item in listValue(row.evidence)" :key="String(item)" class="rounded-xl bg-paper px-3 py-2">{{ display(item) }}</li>
                </ul>
              </div>
              <div>
                <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">Rule Suggestions</p>
                <div class="mt-2 space-y-2">
                  <div v-for="rule in listValue(row.rule_suggestions)" :key="String(objectValue(rule).rule_name || rule)" class="rounded-xl bg-paper px-3 py-2 text-sm text-ink/75">
                    <p class="font-black text-ink">{{ display(objectValue(rule).rule_name, 'Rule') }}</p>
                    <p class="mt-1"><b>Condition:</b> {{ display(objectValue(rule).condition) }}</p>
                    <p class="mt-1"><b>Effect:</b> {{ display(objectValue(rule).expected_effect) }}</p>
                  </div>
                </div>
              </div>
              <div>
                <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">Questions</p>
                <ul class="mt-2 space-y-2 text-sm text-ink/75">
                  <li v-for="item in listValue(row.operator_questions)" :key="String(item)" class="rounded-xl bg-paper px-3 py-2">{{ display(item) }}</li>
                </ul>
              </div>
            </div>
          </details>

          <div class="mt-5 rounded-2xl border border-black/10 bg-paper p-4">
            <label class="text-xs font-black uppercase tracking-[0.22em] text-ink/45" :for="`reason-${proposalKey(row)}`">Decision reason</label>
            <textarea :id="`reason-${proposalKey(row)}`" v-model="decisionReason" class="mt-2 min-h-20 w-full rounded-2xl border border-black/10 bg-white px-4 py-3 text-sm text-ink outline-none focus:border-moss" placeholder="Why are you accepting, rejecting, or asking for more evidence?"></textarea>
            <div class="mt-4 grid gap-3 md:grid-cols-2">
              <button
                v-for="option in decisionOptions"
                :key="option.value"
                class="rounded-2xl border px-4 py-3 text-left text-sm font-black transition disabled:opacity-50"
                :class="optionClass(option)"
                :disabled="activeProposalKey === proposalKey(row)"
                :title="option.effect"
                @click="decide(row, option.value)"
              >
                <span>{{ option.label }}</span>
                <span class="mt-1 block text-xs font-semibold opacity-75">{{ option.effect }}</span>
              </button>
            </div>
          </div>
        </article>
      </div>

      <aside class="space-y-4">
        <div class="rounded-[2rem] border border-black/10 bg-white/80 p-5 shadow-soft">
          <p class="text-xs font-black uppercase tracking-[0.22em] text-ink/45">Recent Decisions</p>
          <div v-if="!decisions.length" class="mt-3 rounded-2xl bg-paper p-4 text-sm text-ink/55">No regime overlay decisions recorded yet.</div>
          <div v-for="row in decisions" :key="String(row.decision_id)" class="mt-3 rounded-2xl bg-paper p-4 text-sm text-ink/75">
            <div class="flex flex-wrap gap-2">
              <span class="rounded-full border px-3 py-1 text-xs font-black" :class="statusClass(row.decision)">{{ display(row.decision) }}</span>
              <span class="rounded-full bg-white px-3 py-1 text-xs font-black text-ink/60">{{ dateText(row.decided_at) }}</span>
            </div>
            <p class="mt-2 font-black text-ink">{{ display(row.proposed_regime) }}</p>
            <p class="mt-1 text-xs text-ink/60">{{ display(row.decision_reason, 'No reason recorded.') }}</p>
          </div>
        </div>
      </aside>
    </div>
  </section>
</template>
