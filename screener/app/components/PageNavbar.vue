<script setup lang="ts">
defineProps<{ title: string, icon?: string }>()
</script>

<template>
  <UDashboardNavbar :title="title" :icon="icon">
    <template #leading>
      <UDashboardSidebarCollapse />
    </template>
    <template #right>
      <slot name="right" />
      <UColorModeButton />
    </template>
  </UDashboardNavbar>
</template>
