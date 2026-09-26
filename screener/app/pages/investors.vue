<script setup lang="ts">
import type { InvestorClassification } from '~/types/api'

const api = useApi()
const { data, status, error, refresh } = await useAsyncData('investors', () => api.get<InvestorClassification[]>('/api/investors'))

const tierTone: Record<string, 'good' | 'neutral' | 'warn'> = {
  marquee: 'good',
  recognized: 'neutral',
  unknown: 'warn',
}

const editingKey = ref<string | null>(null)
const editTier = ref('marquee')
const editNotes = ref('')
const submitting = ref(false)

function effectiveTier(inv: InvestorClassification): string {
  return inv.override_tier || inv.llm_tier || 'unknown'
}

function startEdit(inv: InvestorClassification) {
  editingKey.value = inv.investor_key
  editTier.value = effectiveTier(inv)
  editNotes.value = inv.override_notes || ''
}

async function submitOverride(investorKey: string) {
  submitting.value = true
  try {
    await api.post(`/api/investors/${encodeURIComponent(investorKey)}/override`, { tier: editTier.value, notes: editNotes.value || null })
    editingKey.value = null
    await refresh()
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div>
    <h1 class="text-xl font-semibold">Investors</h1>
    <p class="mt-1 text-sm text-slate-600">
      Every named investor from a capital-raise filing (preferential allotment / QIP /
      rights issue / warrants), classified by how widely recognized the name is -- not
      by performance. The LLM's suggestion is a starting point; correct it below if
      it's wrong, the correction always wins.
    </p>

    <div v-if="error" class="mt-6 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
      Could not reach the API ({{ error.message }}).
    </div>

    <div v-else-if="data && data.length === 0" class="mt-6 text-sm text-slate-500">
      No investors classified yet.
    </div>

    <div v-else-if="data" class="mt-4 overflow-x-auto rounded-lg border border-slate-200 bg-white">
      <table class="w-full text-sm">
        <thead class="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase text-slate-500">
          <tr>
            <th class="px-4 py-2">Investor</th>
            <th class="px-4 py-2">Tier</th>
            <th class="px-4 py-2">Reasoning</th>
            <th class="px-4 py-2">First seen</th>
            <th class="px-4 py-2"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="inv in data" :key="inv.investor_key" class="border-b border-slate-100 last:border-0 align-top">
            <td class="px-4 py-3 font-medium">{{ inv.investor_name_display }}</td>
            <td class="px-4 py-3">
              <template v-if="editingKey === inv.investor_key">
                <select v-model="editTier" class="rounded border border-slate-300 p-1 text-xs">
                  <option value="marquee">marquee</option>
                  <option value="recognized">recognized</option>
                  <option value="unknown">unknown</option>
                </select>
              </template>
              <template v-else>
                <BadgePill :label="effectiveTier(inv)" :tone="tierTone[effectiveTier(inv)] || 'neutral'" />
                <span v-if="inv.override_tier" class="ml-1 text-xs text-slate-400">(was {{ inv.llm_tier }})</span>
              </template>
            </td>
            <td class="px-4 py-3 max-w-md text-slate-600">
              <template v-if="editingKey === inv.investor_key">
                <textarea v-model="editNotes" rows="2" placeholder="Override notes (optional)" class="w-full rounded border border-slate-300 p-1 text-xs" />
              </template>
              <template v-else>
                {{ inv.override_notes || inv.llm_reasoning }}
              </template>
            </td>
            <td class="px-4 py-3 text-xs text-slate-400">{{ formatDate(inv.llm_classified_at) }}</td>
            <td class="px-4 py-3 text-right">
              <button v-if="editingKey !== inv.investor_key" class="text-xs text-slate-500 hover:text-slate-900" @click="startEdit(inv)">Edit</button>
              <div v-else class="flex justify-end gap-2">
                <button :disabled="submitting" class="text-xs font-medium text-slate-900" @click="submitOverride(inv.investor_key)">Save</button>
                <button class="text-xs text-slate-400" @click="editingKey = null">Cancel</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <p v-else-if="status === 'pending'" class="mt-6 text-sm text-slate-500">Loading…</p>
  </div>
</template>
