// Human-readable labels for the trigger_type values fundamentals/screens/l3_triggers.py
// writes (mirrors the backend's STRATEGY_LABELS in notifications.py, used to render
// the same names in the digest email). Extracted 2026-08-13 -- was previously copied
// verbatim into strategies/index.vue, strategies/[trigger_type].vue, and
// watchlist/[id].vue, which would have drifted the moment one of them changed.
export const STRATEGY_LABELS: Record<string, string> = {
  rating_downgrade: 'Rating downgrade',
  rating_confirms_deleveraging: 'Rating confirms deleveraging',
  insider_buy: 'Insider buy',
  insider_sell_surprise: 'Insider sell (surprise)',
  capital_raise: 'Capital raise',
  institutional_first_entry: 'First institutional entry',
  llm_flagged: 'LLM-flagged',
  // Added 2026-08-13 alongside the L3 results trigger + auditor_change/RPT gap fix.
  results_decline: 'Results decline',
  results_confirm_turnaround: 'Results confirm turnaround',
  results_delayed: 'Results delayed',
  auditor_change: 'Auditor change',
  related_party_transaction: 'Related-party transaction',
}

// Falls back to the raw trigger_type for anything not listed above so a new
// trigger_type never needs a frontend deploy before it shows up.
export function strategyLabel(triggerType: string): string {
  return STRATEGY_LABELS[triggerType] || triggerType
}
