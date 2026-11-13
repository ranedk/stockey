<script setup lang="ts">
// One titled section of an operations page: icon, title, a status badge, a one-line
// explanation in muted text, and an optional timestamp on the right.
defineProps<{ id: string, title: string, icon: string, level?: string | null, levelLabel?: string, note?: string, meta?: string }>()
</script>

<template>
  <UCard :id="id" class="scroll-mt-4" :ui="{ header: 'p-4 sm:px-5', body: 'p-4 sm:p-5' }">
    <template #header>
      <div class="flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
        <div class="flex min-w-0 items-center gap-2">
          <UIcon :name="icon" class="size-5 shrink-0 text-muted" />
          <h3 class="font-semibold text-highlighted">{{ title }}</h3>
          <OpsStatusBadge v-if="level" :level="level" :label="levelLabel" />
        </div>
        <span v-if="meta" class="text-xs text-dimmed">{{ meta }}</span>
      </div>
      <p v-if="note || $slots.note" class="mt-1.5 max-w-3xl text-xs text-muted"><slot name="note">{{ note }}</slot></p>
    </template>
    <slot />
  </UCard>
</template>
