<script setup lang="ts">
// Whether the job scheduler (go-crond) is FIRING, not merely running: it has twice been
// alive while scheduling nothing. Grouped by area, problems first within each.
const props = defineProps<{ data: OpsScheduler }>()
const SECTION_ICON: Record<string, string> = {
  'scheduler': 'i-lucide-alarm-clock', 'job locks': 'i-lucide-lock', 'services': 'i-lucide-server',
}
const groups = computed(() => {
  const m = new Map<string, OpsSchedulerFinding[]>()
  for (const f of [...(props.data.findings ?? [])].sort((a, b) => opsLevel(a.level).rank - opsLevel(b.level).rank)) m.set(f.section, [...(m.get(f.section) ?? []), f])
  return [...m.entries()].map(([section, items]) => ({
    section, items, icon: SECTION_ICON[section] ?? (section.startsWith('OS crontab') ? 'i-lucide-shield-check' : 'i-lucide-list'),
    worst: items[0]!.level,
  })).sort((a, b) => opsLevel(a.worst).rank - opsLevel(b.worst).rank)
})
</script>

<template>
  <div class="grid gap-3 md:grid-cols-2">
    <div v-for="g in groups" :key="g.section" class="rounded-lg border border-default p-3">
      <div class="mb-2 flex items-center gap-2">
        <UIcon :name="g.icon" class="size-4 text-muted" />
        <h4 class="text-sm font-medium text-highlighted">{{ g.section.replace(/^./, c => c.toUpperCase()) }}</h4>
      </div>
      <ul class="space-y-2">
        <li v-for="(f, i) in g.items" :key="i" class="flex items-start gap-2 text-sm">
          <OpsStatusBadge :level="f.level" size="xs" class="mt-0.5 shrink-0" />
          <span class="min-w-0 break-words text-default">{{ f.message }}</span>
        </li>
      </ul>
    </div>
  </div>
</template>
