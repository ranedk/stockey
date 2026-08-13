import type { WatchlistStatus } from '~/types/api'

// Mirrors fundamentals/screens/watchlist_exit.py's own status values. 'active' is
// the default/normal state -- the other three are exit signals (2026-08-13, "we
// will crowd the watchlist with no outcomes" fix), never a deletion, just fewer
// eyes on a company by default until a human looks again.
export const WATCHLIST_STATUS_LABELS: Record<WatchlistStatus, string> = {
  active: 'Active',
  stale: 'Stale',
  invalidated: 'Invalidated',
  price_flagged: 'Price flagged',
}

export const WATCHLIST_STATUS_TONE: Record<WatchlistStatus, 'good' | 'bad' | 'neutral' | 'warn'> = {
  active: 'good',
  stale: 'neutral',
  invalidated: 'bad',
  price_flagged: 'warn',
}
