import type { PaperStrategy, PaperSummary } from '~/types/systrader'

export const PAPER_BOOKS = [
  { key: 'strategy', label: 'Strategy', colour: '#0f766e' },
  { key: 'equal-weight', label: 'Equal-weight universe (beta control)', colour: '#94a3b8' },
  { key: 'random-ranking', label: 'Random ranking, same size (selection control)', colour: '#f59e0b' },
  { key: 'account', label: 'Rs 1 crore account (whole shares, Dhan charges)', colour: '#6366f1' },
]

export const PAPER_BOOK_LABEL: Record<string, string> = Object.fromEntries(
  PAPER_BOOKS.map(b => [b.key, b.label]),
)

export function strategyBook(s: PaperStrategy | null | undefined): PaperSummary | undefined {
  return s?.summaries?.find(x => x.book === 'strategy')
}

export function benchmarkBook(s: PaperStrategy | null | undefined): PaperSummary | undefined {
  return s?.summaries?.find(x => x.book === 'equal-weight')
}

/**
 * How far ahead of (or behind) the equal-weight book the strategy is. This is
 * the only number that means anything on its own: a strategy up 12% in a market
 * up 14% has lost, and the headline should say so.
 */
export function excessReturn(s: PaperStrategy | null | undefined): number | null {
  const a = strategyBook(s)
  const b = benchmarkBook(s)
  if (!a || !b) return null
  return a.total_return - b.total_return
}

/**
 * "Trend Quintile" from "trend-quintile".
 *
 * Named paperStrategy* rather than the obvious strategyLabel: utils/strategyLabels.ts
 * already exports a strategyLabel() for L3 trigger types, and Nuxt's auto-import
 * resolves a duplicate name to whichever it found first — silently, with no error and
 * no warning. The first version of this file collided and rendered raw slugs.
 */
export function paperStrategyName(name: string): string {
  return name
    .split('-')
    .map(w => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ')
}

/** "Strategy 1 · Trend Quintile" — the numbering the operator asked for, kept
 *  alongside the real name so the URL stays self-describing. */
export function paperStrategyLabel(name: string, index: number): string {
  return `Strategy ${index + 1} · ${paperStrategyName(name)}`
}
