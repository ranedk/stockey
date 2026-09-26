<script setup lang="ts">
// The watchlist badge used to render `confluence {count}/{evaluable}`, which reads as
// "1 of 4, the other 3 unknown". It is not: evaluable_count is exactly
// confluence_count + contradicting_count, so "1/4" means 1 supportive and 3 ACTIVELY
// CONTRADICTING. Those are opposite messages.
//
// confluence_score.py already computes contradicting_count for precisely this reason --
// its docstring says it is tracked separately "so a human can tell 'quietly unconfirmed'
// apart from 'actively contradicted' at a glance" -- and the badge was dropping it. The
// live distribution shows the collision: 1/1 (nothing against) and 1/2 (one against)
// looked nearly identical, and 0/2 (two axes against) rendered in the same neutral tone
// as a company nobody had scored.
const props = defineProps<{
  supportive: number | null
  contradicting: number | null
  evaluable: number | null
}>()

const scored = computed(() => props.supportive !== null && props.evaluable !== null)

// Tone follows the BALANCE, not merely "is anything supportive".
const tone = computed<'good' | 'bad' | 'warn' | 'neutral'>(() => {
  if (!scored.value) return 'neutral'
  const yes = props.supportive ?? 0
  const no = props.contradicting ?? 0
  if (yes > no) return 'good'
  if (no > yes) return 'bad'
  return 'warn'          // evenly split is genuinely ambiguous, not "fine"
})

const label = computed(() => {
  if (!scored.value) return 'not scored'
  const yes = props.supportive ?? 0
  const no = props.contradicting ?? 0
  // Both numbers, always -- the count of "no" is the part that was invisible.
  return no > 0 ? `${yes} for · ${no} against` : `${yes} for · none against`
})

const title = computed(() => {
  if (!scored.value) return 'confluence_score.py has not scored this company yet'
  const unknown = 5 - (props.evaluable ?? 0)
  return `${props.supportive} of ${props.evaluable} evaluable axes support this`
    + `, ${props.contradicting} contradict`
    + (unknown > 0 ? `, ${unknown} could not be evaluated` : '')
})
</script>

<template>
  <span :title="title">
    <BadgePill :label="label" :tone="tone" />
  </span>
</template>
