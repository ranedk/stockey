// The investor paradigms (LEDGER 83, 2026-10-23). Each card is OUR replica of a method the
// manager has published -- built from ../twitter_handles.md -- never their portfolio and never
// their returns. Their own published figures are shown separately and labelled as theirs.

export interface ParadigmTrack {
  label: string
  // systrader paper track (GET /api/paper/{name}, with its 2016+ replay nested as `reference`)
  // or a stockey forward track (GET /api/paradigms/tracks, matched on `track`).
  source: 'systrader' | 'stockey'
  name: string
}

export interface Paradigm {
  key: string
  person: string
  firm: string
  handle: string
  family: 'momentum' | 'fundamental'
  // A stand-in: the method is judgement or private, so the replica is crude.
  crude?: boolean
  paradigm: string
  rules: string[]
  guessed: string[]
  theirFigures: string
  tracks: ParadigmTrack[]
}

export const PARADIGMS: Paradigm[] = [
  {
    key: 'capitalmind',
    person: 'Deepak Shenoy',
    firm: 'Capitalmind — Adaptive Momentum',
    handle: '@deepakshenoy',
    family: 'momentum',
    paradigm: 'Price is the only signal. Not why a stock is rising, only whether it is, and whether it has stopped. Prefer the stock that rises steadily over one that swings wildly. Follow the rules through drawdowns; the edge is the discipline, not the model.',
    rules: [
      'Universe: the 500 most-traded NSE stocks (no funds).',
      'Score: 3-, 6- and 12-month returns, each divided by its own volatility, averaged.',
      'May hold only stocks with a positive score, above their 200-day average, and not just gone parabolic.',
      'Top 30, 1/30 each; slots nobody qualifies for stay in cash; at most 5 per industry.',
      'Monthly rebalance. Whole book to cash while the market is below its 200-day average.',
    ],
    guessed: [
      'Lookback weights (equal 3/6/12 months), the parabolic thresholds (+50% in a month, or two 9.5% days in two weeks) and the market rule are ours; theirs are private.',
      '"Top 500 by traded value" stands in for their liquidity-filtered top 500.',
    ],
    theirFigures: 'Their own figures: 17.2%/yr over 7 years vs 11.4% for Nifty 50 TRI (to Oct 2026), not SEBI-verified.',
    tracks: [{ label: 'Adaptive Momentum replica', source: 'systrader', name: 'paradigm-capitalmind-momentum' }],
  },
  {
    key: 'weekendinvesting',
    person: 'Alok Jain',
    firm: 'WeekendInvesting — Mi series',
    handle: '@WeekendInvestng',
    family: 'momentum',
    paradigm: '"Bhav Bhagwan Che" — price is God. Price discounts everything before the fundamentals do, so the only inputs are price and liquidity. Zero discretion: the weekly ranking is natural selection, winners are left to run, and a stock is sold only when it falls out of the strong band.',
    rules: [
      'Score: the average of the 6- and 12-month return. Checked weekly.',
      'Rotational books (always invested): Mi 20 = top 20 of ranks 101-500; India Top 10 = top 10 of the 50 largest; NNF 10 = top 10 of ranks 51-100. A holding is sold when it drops out of the top 2N.',
      'Absolute book (Mi 30): up to 33 of the top 500, only stocks with positive momentum above their 200-day average, 1/33 each, empty slots in cash.',
      'Winners are never trimmed: only the stocks that change are traded.',
    ],
    guessed: [
      'His ranking formula, exit band and absolute threshold are private; the 6/12-month average is the approximation his own published notes suggest.',
      'Index universes (Nifty 50, Next 50, MidSmallcap 400) are stood in for by traded-value ranks — index membership has no history here.',
    ],
    theirFigures: 'Their own figures: Mi 20 18.7%/yr since inception (Sep 2026 factsheet); Allcap 22.1%. Not SEBI-verified.',
    tracks: [
      { label: 'Mi 20 replica', source: 'systrader', name: 'paradigm-wi-mi20' },
      { label: 'Mi India Top 10 replica', source: 'systrader', name: 'paradigm-wi-top10' },
      { label: 'Mi NNF 10 replica', source: 'systrader', name: 'paradigm-wi-nnf10' },
      { label: 'Mi 30 (absolute) replica', source: 'systrader', name: 'paradigm-wi-mi30' },
    ],
  },
  {
    key: 'marcellus',
    person: 'Saurabh Mukherjea',
    firm: 'Marcellus — Consistent Compounders / MeritorQ',
    handle: '@saurabhmukherjea',
    family: 'fundamental',
    paradigm: 'Over ten years, share prices follow earnings growth, not the starting P/E. Find companies with clean accounts that have grown double digits and earned above their cost of capital year after year, and hold them with almost no churn.',
    rules: [],
    guessed: [],
    theirFigures: 'Their own account: all three flagship books trailed BSE 500 TRI and Nifty 50 TRI since inception as of Aug 2025.',
    tracks: [{ label: 'Forensic + twin-filter replica', source: 'stockey', name: 'marcellus' }],
  },
  {
    key: 'sageone',
    person: 'Samit Vartak',
    firm: 'SageOne',
    handle: '@SamitVartak',
    family: 'fundamental',
    paradigm: 'Buy businesses whose earnings will double in 3-4 years, at or below fair value, in ranks 101-1,000 before they become large caps. Getting the business wrong costs capital; getting the price a little wrong only costs a few quarters.',
    rules: [],
    guessed: [],
    theirFigures: 'About 80 companies owned over ten years to 2022, ~12 exited at a loss (his numbers).',
    tracks: [{ label: 'Green-flag screen replica', source: 'stockey', name: 'sageone' }],
  },
  {
    key: 'maheshwari',
    person: 'Basant Maheshwari',
    firm: 'Basant Maheshwari Wealth Advisers',
    handle: '@BMTheEquityDesk',
    family: 'fundamental',
    paradigm: 'Find the sector in a secular trend — a pack of its stocks making new highs — and buy its leader, paying the premium P/E. Hold while profits grow; sell when the theme fades, not on the percentage gained.',
    rules: [],
    guessed: [],
    theirFigures: 'PMS 3-year return 16.2% vs 25.2% for BSE 500 TRI (Groww listing).',
    tracks: [{ label: 'Sector-leader replica', source: 'stockey', name: 'maheshwari' }],
  },
  {
    key: 'kedia',
    person: 'Vijay Kedia',
    firm: 'Kedia Securities — SMILE',
    handle: '@VijayKedia1',
    family: 'fundamental',
    crude: true,
    paradigm: 'Small in size, Medium in experience, Large in aspiration, Extra-large market potential. A small company with seasoned, hungry management in a huge market, bought at a fair price and held 10-15 years without deciding the exit at entry.',
    rules: [],
    guessed: [],
    theirFigures: 'Three separate 100x holdings (Aegis, Atul Auto, Cera) over 2004-2017 — his account.',
    tracks: [{ label: 'SMILE stand-in', source: 'stockey', name: 'kedia' }],
  },
  {
    key: 'whiteoak',
    person: 'Prashant Khemka',
    firm: 'WhiteOak — OpcoFinco',
    handle: 'corporate handles',
    family: 'fundamental',
    crude: true,
    paradigm: 'Great businesses at attractive valuations, where great means earning more than the cost of capital on new investment. Price the capital intensity explicitly (Opco/Finco split), and balance the portfolio so the only bet is stock selection.',
    rules: [],
    guessed: [],
    theirFigures: 'No single headline figure; a review notes sector weights stay close to category averages.',
    tracks: [{ label: 'OpcoFinco stand-in', source: 'stockey', name: 'whiteoak' }],
  },
]

// One stockey forward track from GET /api/paradigms/tracks.
export interface StockeyParadigmTrack {
  track: string
  version: number
  start: string
  rebalance: string
  paradigm: string
  approximations: string
  spec: Record<string, unknown>
  as_of: string
  status: 'waiting' | 'live'
  holdings_as_of: string | null
  cash_weight: number | null
  periods: { start: string; end: string; names: number; return_pct: number | null }[]
  return_since_start_pct: number | null
  benchmark: { name: string; return_since_start_pct: number | null; names: number; from?: string }
  holdings: {
    symbol: string
    rank: number | null
    weight: number
    entry_date: string
    return_since_entry_pct: number | null
  }[]
}

// The spec fields that read as rules, in the order a person would read them.
export const STOCKEY_RULE_KEYS = ['universe', 'eligible', 'score', 'construction', 'review'] as const
