<script setup lang="ts">
// A short, consistent "how this layer is derived" note. The screener's pages
// each show the output of one stage of the fundamental pipeline, and the
// stages are named L1..L5 in the backend and in every document about them;
// without a line of explanation on the page itself, the names mean nothing to
// anyone who has not read fundamentals/screens/.
//
// Deliberately terse: a reader wants to know what produced these rows, not to
// read the PRD.
defineProps<{
  layer: string
  title: string
}>()

const open = ref(false)
</script>

<template>
  <div class="mt-3 rounded-lg border border-slate-200 bg-white">
    <button
      class="flex w-full items-center gap-2 px-3 py-2 text-left"
      @click="open = !open"
    >
      <span class="rounded bg-slate-800 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-white">{{ layer }}</span>
      <span class="text-xs font-medium text-slate-700">{{ title }}</span>
      <span class="ml-auto text-xs text-slate-400">{{ open ? 'hide' : 'how this is built' }}</span>
    </button>
    <div v-if="open" class="border-t border-slate-100 px-3 py-2 text-xs leading-relaxed text-slate-600">
      <slot />
    </div>
  </div>
</template>
