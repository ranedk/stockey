<script setup lang="ts">
type Dict = Record<string, unknown>

const api = useOperatorApi()
const { data, pending, error: loadError, refresh } = await useAsyncData('prompts', () => api.getPrompts())

const prompts = computed(() => asList(data.value?.prompts))
const editing = ref<string | null>(null)        // prompt_id being edited
const draft = ref('')                            // edited system prompt text
const draftNotes = ref('')
const busy = ref<string | null>(null)
const showHistory = ref<Set<string>>(new Set())

// "add new" form
const newId = ref('')
const newTitle = ref('')
const newBody = ref('')
const showNew = ref(false)

function asList(value: unknown): Dict[] {
  return Array.isArray(value) ? value.filter((item) => typeof item === 'object' && item !== null) as Dict[] : []
}
function display(value: unknown) {
  if (value === null || value === undefined || value === '') return '-'
  return String(value)
}
function activeVersion(p: Dict): Dict {
  const vs = asList(p.versions)
  return vs.find((v) => v.version === p.active_version) || vs[0] || {}
}
function startEdit(p: Dict) {
  editing.value = String(p.prompt_id)
  draft.value = String(activeVersion(p).system_prompt || '')
  draftNotes.value = ''
}
function toggleHistory(id: string) {
  const next = new Set(showHistory.value)
  next.has(id) ? next.delete(id) : next.add(id)
  showHistory.value = next
}

async function saveEdit(p: Dict) {
  const id = String(p.prompt_id)
  busy.value = id
  try {
    await api.createPromptVersion(id, { system_prompt: draft.value, notes: draftNotes.value, activate: true })
    editing.value = null
    await refresh()
  } finally { busy.value = null }
}
async function activate(p: Dict, version: unknown) {
  busy.value = String(p.prompt_id)
  try {
    await api.activatePromptVersion(String(p.prompt_id), { version })
    await refresh()
  } finally { busy.value = null }
}
async function addPrompt() {
  if (!newId.value.trim()) return
  busy.value = '__new__'
  try {
    await api.createPromptVersion(newId.value.trim(), { title: newTitle.value, system_prompt: newBody.value, activate: true })
    newId.value = ''; newTitle.value = ''; newBody.value = ''; showNew.value = false
    await refresh()
  } finally { busy.value = null }
}
</script>

<template>
  <section class="space-y-6">
    <header class="rounded-3xl border border-ink/10 bg-ink px-8 py-7 text-paper">
      <p class="text-xs font-black uppercase tracking-[0.2em] text-paper/50">Review-only · edits create a new version</p>
      <h1 class="mt-2 text-3xl font-black tracking-tight">Prompts</h1>
      <p class="mt-2 max-w-2xl text-paper/70">
        Every LLM prompt the system uses. Editing creates a new version (never an overwrite). A
        <span class="font-semibold text-paper">wired</span> prompt's active version takes effect when LLM
        authority is enabled; others are stored for adoption. Nothing here submits a broker order.
      </p>
    </header>

    <div v-if="loadError" class="rounded-2xl border border-rust/25 bg-rust/10 px-5 py-4 text-sm text-rust">Could not load prompts.</div>
    <p v-if="pending" class="text-sm text-ink/40">loading…</p>

    <div class="flex justify-end">
      <button class="rounded-full border border-ink/15 bg-white/70 px-4 py-2 text-sm font-semibold hover:bg-white" @click="showNew = !showNew">+ Add prompt</button>
    </div>
    <div v-if="showNew" class="rounded-2xl border border-ink/10 bg-white/60 p-5 space-y-3">
      <input v-model="newId" placeholder="prompt_id (e.g. my_custom_prompt)" class="w-full rounded-xl border border-ink/15 bg-white px-3 py-2 text-sm" />
      <input v-model="newTitle" placeholder="title" class="w-full rounded-xl border border-ink/15 bg-white px-3 py-2 text-sm" />
      <textarea v-model="newBody" placeholder="system prompt body" rows="4" class="w-full rounded-xl border border-ink/15 bg-white px-3 py-2 font-mono text-xs"></textarea>
      <button class="rounded-full border border-moss/30 bg-moss/10 px-4 py-2 text-sm font-semibold text-moss hover:bg-moss/20 disabled:opacity-40"
              :disabled="busy === '__new__' || !newId.trim()" @click="addPrompt">Create</button>
    </div>

    <div v-for="(p, idx) in prompts" :key="idx" class="rounded-2xl border border-ink/10 bg-white/60 p-5">
      <div class="flex flex-wrap items-center gap-3">
        <h2 class="text-base font-black tracking-tight text-ink">{{ display(p.title || p.prompt_id) }}</h2>
        <span class="rounded-full border border-ink/10 bg-white/70 px-2.5 py-1 text-xs text-ink/55">{{ display(p.prompt_id) }}</span>
        <span v-if="p.wired" class="rounded-full border border-moss/25 bg-moss/10 px-2.5 py-1 text-xs font-bold text-moss">wired</span>
        <span class="rounded-full border border-ink/10 bg-white/70 px-2.5 py-1 text-xs text-ink/55">active v{{ display(p.active_version) }}</span>
        <span class="rounded-full border border-ink/10 bg-white/70 px-2.5 py-1 text-xs text-ink/45">{{ display(p.authority_scope) }}</span>
        <div class="ml-auto flex gap-2">
          <button class="rounded-full border border-ink/15 bg-white/70 px-3 py-1.5 text-xs font-semibold hover:bg-white" @click="toggleHistory(String(p.prompt_id))">
            history ({{ asList(p.versions).length }})
          </button>
          <button class="rounded-full border border-ink/15 bg-white/70 px-3 py-1.5 text-xs font-semibold hover:bg-white" @click="startEdit(p)">Edit</button>
        </div>
      </div>

      <pre v-if="editing !== p.prompt_id" class="mt-3 max-h-40 overflow-y-auto whitespace-pre-wrap rounded-xl border border-ink/10 bg-paper p-3 font-mono text-xs text-ink/70">{{ activeVersion(p).system_prompt || '(no editable system prompt — code-backed)' }}</pre>

      <div v-else class="mt-3 space-y-2">
        <textarea v-model="draft" rows="8" class="w-full rounded-xl border border-ink/20 bg-white px-3 py-2 font-mono text-xs"></textarea>
        <input v-model="draftNotes" placeholder="change note (optional)" class="w-full rounded-xl border border-ink/15 bg-white px-3 py-2 text-sm" />
        <div class="flex gap-2">
          <button class="rounded-full border border-moss/30 bg-moss/10 px-4 py-1.5 text-xs font-semibold text-moss hover:bg-moss/20 disabled:opacity-40"
                  :disabled="busy === p.prompt_id" @click="saveEdit(p)">Save as new active version</button>
          <button class="rounded-full border border-ink/15 bg-white/70 px-4 py-1.5 text-xs font-semibold hover:bg-white" @click="editing = null">Cancel</button>
        </div>
      </div>

      <div v-if="showHistory.has(String(p.prompt_id))" class="mt-3 space-y-1 border-t border-ink/10 pt-3">
        <div v-for="(v, vi) in asList(p.versions)" :key="vi" class="flex flex-wrap items-center gap-3 text-xs">
          <span class="font-bold text-ink/70">v{{ display(v.version) }}</span>
          <span v-if="v.active" class="rounded-full border border-moss/25 bg-moss/10 px-2 py-0.5 font-semibold text-moss">active</span>
          <span class="text-ink/45">{{ display(v.created_by) }}</span>
          <span class="text-ink/40">{{ String(v.created_at || '').slice(0, 16).replace('T', ' ') }}</span>
          <span v-if="v.notes" class="text-ink/45">{{ display(v.notes) }}</span>
          <button v-if="!v.active" class="ml-auto rounded-full border border-ink/15 bg-white/70 px-2.5 py-0.5 font-semibold hover:bg-white disabled:opacity-40"
                  :disabled="busy === p.prompt_id" @click="activate(p, v.version)">Activate</button>
        </div>
      </div>
    </div>
  </section>
</template>
