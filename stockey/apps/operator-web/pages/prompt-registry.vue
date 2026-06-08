<script setup lang="ts">
import type { Dict } from '~/types/api'

const api = useOperatorApi()
const ownerArea = ref('')
const authorityScope = ref('')
const { data, refresh, pending, error } = await useAsyncData(
  'prompt-registry',
  () => api.getPromptRegistry({ owner_area: ownerArea.value, authority_scope: authorityScope.value }),
  { watch: [ownerArea, authorityScope] }
)

const contracts = computed(() => data.value?.contracts || [])
const summary = computed(() => data.value?.summary || {})
const ownerOptions = computed(() => Object.keys((summary.value.by_owner_area || {}) as Dict).sort())
const scopeOptions = computed(() => Object.keys((summary.value.by_authority_scope || {}) as Dict).sort())

function listText(value: unknown) {
  if (!Array.isArray(value)) return '-'
  return value.length ? value.join(', ') : '-'
}

function scopeClass(scope: unknown) {
  const text = String(scope || '')
  if (text === 'extraction_only') return 'bg-sky/10 text-ink'
  if (text === 'review_input_only') return 'bg-sun/25 text-ink'
  if (text === 'research_only') return 'bg-moss/10 text-moss'
  return 'bg-ink/10 text-ink'
}
</script>

<template>
  <section class="rounded-[2rem] bg-ink p-8 text-paper shadow-soft">
    <p class="text-sm font-semibold uppercase tracking-[0.35em] text-paper/55">LLM audit</p>
    <div class="mt-4 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 class="max-w-4xl text-5xl font-black tracking-tight">Prompt Registry</h1>
        <p class="mt-5 max-w-3xl text-lg leading-8 text-paper/70">
          One place to review Codex/OpenAI/Gemini prompt contracts, schemas, authority boundaries, fallbacks, and source files.
        </p>
      </div>
      <button class="rounded-full bg-paper px-5 py-3 text-sm font-black text-ink disabled:opacity-50" type="button" :disabled="pending" @click="refresh()">
        {{ pending ? 'Refreshing...' : 'Refresh' }}
      </button>
    </div>
  </section>

  <ApiErrorBanner v-if="error" class="mt-6" :error="error" title="Prompt registry API failed" />

  <section class="mt-6 grid gap-4 md:grid-cols-4">
    <MetricTile label="Contracts" :value="String(summary.contract_count || contracts.length)" note="Registered prompt contracts" />
    <MetricTile label="Broker Allowed" :value="String(summary.broker_execution_allowed_count || 0)" note="Should remain zero" />
    <MetricTile label="Owners" :value="String(ownerOptions.length)" note="Functional areas" />
    <MetricTile label="Scopes" :value="String(scopeOptions.length)" note="Authority classes" />
  </section>

  <section class="mt-6 glass-panel rounded-3xl p-6">
    <div class="grid gap-4 md:grid-cols-2">
      <label class="text-sm font-bold text-ink/70">
        Owner area
        <select v-model="ownerArea" class="mt-2 w-full rounded-2xl border border-black/10 bg-white px-4 py-3 text-ink outline-none focus:border-moss">
          <option value="">All owners</option>
          <option v-for="owner in ownerOptions" :key="owner" :value="owner">{{ owner }}</option>
        </select>
      </label>
      <label class="text-sm font-bold text-ink/70">
        Authority scope
        <select v-model="authorityScope" class="mt-2 w-full rounded-2xl border border-black/10 bg-white px-4 py-3 text-ink outline-none focus:border-moss">
          <option value="">All scopes</option>
          <option v-for="scope in scopeOptions" :key="scope" :value="scope">{{ scope }}</option>
        </select>
      </label>
    </div>
    <div v-if="data?.notes?.length" class="mt-4 grid gap-3 md:grid-cols-3">
      <p v-for="note in data.notes" :key="note" class="rounded-2xl bg-white/75 p-4 text-sm font-semibold text-ink/65">{{ note }}</p>
    </div>
  </section>

  <section class="mt-8 grid gap-4">
    <article v-for="contract in contracts" :key="String(contract.prompt_id)" class="rounded-3xl border border-black/10 bg-white/80 p-5 shadow-soft">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.24em] text-ink/45">{{ contract.owner_area || '-' }}</p>
          <h2 class="mt-1 text-2xl font-black">{{ contract.title || contract.prompt_id }}</h2>
          <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/65">{{ contract.purpose || '-' }}</p>
        </div>
        <div class="flex flex-wrap gap-2">
          <span class="rounded-full px-3 py-1 text-xs font-black" :class="scopeClass(contract.authority_scope)">{{ contract.authority_scope || '-' }}</span>
          <span class="rounded-full bg-ink px-3 py-1 text-xs font-black text-paper">{{ contract.provider || '-' }}</span>
          <span class="rounded-full bg-paper px-3 py-1 text-xs font-black text-ink/65">{{ contract.migration_status || '-' }}</span>
        </div>
      </div>
      <div class="mt-5 grid gap-3 text-sm md:grid-cols-2 lg:grid-cols-3">
        <p class="rounded-2xl bg-paper/80 p-3"><b>ID:</b> {{ contract.prompt_id }}</p>
        <p class="rounded-2xl bg-paper/80 p-3"><b>Version:</b> {{ contract.version }}</p>
        <p class="rounded-2xl bg-paper/80 p-3"><b>Schema:</b> {{ contract.response_schema || '-' }}</p>
        <p class="rounded-2xl bg-paper/80 p-3"><b>Prompt source:</b> {{ contract.prompt_source || '-' }}</p>
        <p class="rounded-2xl bg-paper/80 p-3"><b>System source:</b> {{ contract.system_prompt_source || '-' }}</p>
        <p class="rounded-2xl bg-paper/80 p-3"><b>Model env:</b> {{ listText(contract.model_env_vars) }}</p>
      </div>
      <details class="mt-4 rounded-2xl bg-paper/80 p-4">
        <summary class="cursor-pointer text-sm font-black text-moss">Evidence, outputs, fallback, and notes</summary>
        <div class="mt-3 grid gap-3 text-sm text-ink/70 md:grid-cols-2">
          <p><b>Input evidence:</b> {{ listText(contract.input_evidence) }}</p>
          <p><b>Output tables:</b> {{ listText(contract.output_tables) }}</p>
          <p><b>Fallback:</b> {{ contract.fallback_behavior || '-' }}</p>
          <p><b>Broker execution:</b> {{ contract.broker_execution_allowed ? 'allowed' : 'not allowed' }}</p>
        </div>
        <pre class="mt-3 max-h-64 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ JSON.stringify(contract, null, 2) }}</pre>
      </details>
    </article>
    <p v-if="!contracts.length && !pending" class="rounded-3xl bg-white/80 p-6 text-sm text-ink/55">No prompt contracts match the selected filters.</p>
  </section>
</template>
