<script setup lang="ts">
// Loading / error / empty placeholder for one section. Never invents data.
defineProps<{ pending?: boolean, error?: { message?: string, statusCode?: number } | null, what: string, slow?: string, height?: string }>()
</script>

<template>
  <div v-if="error" class="flex items-start gap-3 rounded-lg border border-error/30 bg-error/5 p-4 text-sm">
    <UIcon name="i-lucide-plug-zap" class="mt-0.5 size-5 shrink-0 text-error" />
    <div class="min-w-0">
      <div class="font-medium text-highlighted">Could not load {{ what }}</div>
      <p class="mt-0.5 break-words text-muted">
        The stockey data API (port 8000) did not answer<template v-if="error.statusCode"> (error {{ error.statusCode }})</template>.
        This says nothing about the data itself; reload the page to try again.
      </p>
    </div>
  </div>
  <div v-else class="space-y-2">
    <USkeleton class="w-full rounded-lg" :style="{ height: height || '6rem' }" />
    <p v-if="slow" class="flex items-center gap-1.5 text-xs text-muted">
      <UIcon name="i-lucide-loader-circle" class="size-3.5 animate-spin" />{{ slow }}
    </p>
  </div>
</template>
