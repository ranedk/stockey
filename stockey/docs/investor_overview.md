# Stockey -- Conceptual Overview (for an investor)

Stockey is an Indian-equity research and decision-support system. This page explains, in plain terms,
*how it decides what is worth buying or selling* and *why you can trust the discipline* -- no code.

## The core idea

A capable AI (an LLM) is allowed to make trade decisions directly -- but only when it has reviewed
the COMPLETE evidence and has strong, data-grounded reasons, the way a disciplined analyst would. It
is never allowed to act on a single news item, a single indicator, or a hunch. Around that judgement
sit deterministic guardrails that the AI cannot talk its way past: they check the decision is
internally consistent, size it so no single trade can be catastrophic, and watch the results for
repeated mistakes.

Two principles run through everything:

- **Beat the market honestly, or just ride it -- but know which.** A decision is labelled either
  ALPHA (a specific reason to expect out-performance) or PARTICIPATION (simply capturing a rising
  market). The system never dresses up market beta as skill.
- **Earn trust from outcomes, not reputation.** No idea -- however famous -- is acted on until it has
  shown, on real Indian data, point-in-time and after costs, that it actually beats the benchmark.

## How a decision is made (the shape of it)

For a given stock on a given day, the system assembles an evidence packet across several
dimensions, then asks two separate questions:

1. **Is there a reason (a thesis)?** This comes from NON-technical evidence: a company event (an order
   win, a results beat), the company's fundamentals (earnings growth, low debt, cash generation), or
   a matched investor playbook. A genuine alpha case needs at least two *independent* reasons pointing
   the same way -- e.g. strong fundamentals AND a supporting catalyst.
2. **Is the timing right?** This is what the price chart is for -- not as a reason to buy, but to judge
   whether the move is already played out or still has room. A stock can have a great story and still
   be a poor entry.

On top of that sit guardrails: a stressed market reduces position size (it does not blanket-block a
genuine opportunity); a broken risk profile or a contradicting signal vetoes the trade; and the size
of any position is capped so a single bad call can never sink the book.

## Where the edge is meant to come from

Three independent sources, each validated the same disciplined way:

- **Investor hypotheses** -- your own experience, written as testable playbooks ("a material order win
  in a constructive market tends to re-rate over weeks"). The system matches them against real
  news/announcements and only trusts a playbook after it has beaten the benchmark on historical
  matches.
- **Event signals** -- structured reading of exchange announcements (order wins, buybacks, rating
  changes, demergers), classified by what the event actually *is*, not by keywords appearing in text.
- **A multi-factor model** -- momentum/trend/value/quality-style factors plus fundamentals, combined
  into a confidence score. Crucially, factors that say the same thing (momentum, trend, relative
  strength are largely one signal) count once; the real lift comes from combining *independent* axes.

## What we have actually validated (and what we have not)

Honesty is part of the design, so the open questions are stated, not hidden:

- **Fundamentals are the strongest single edge** on Indian data (earnings growth is the most
  predictive factor we measured), and **combining independent axes -- technical AND fundamental --
  beats any single factor.** This is measured, not assumed.
- **Famous patterns are not automatically trusted.** When we tested well-known event playbooks,
  post-earnings drift showed real edge while share-buybacks came out *negative* on our sample -- so
  the discipline correctly separates "famous" from "works here."
- **The data is still thin in places.** Several signals could only be validated on a modest universe,
  and the system is explicit about where confidence is provisional.
- **Nothing trades yet.** Every decision today is review-only. Going live requires two independent
  safety switches (both off by default) to be deliberately turned on, and a track record of real
  decisions whose outcomes have been monitored.

## Why you can trust the discipline

- Every decision keeps a full, typed record of the evidence and reasons behind it (auditable).
- It only ever uses information available at the time -- no hindsight, no future data.
- Position size, sector exposure and stops are bounded by hard limits on every trade.
- Live trading is gated behind two default-off master switches plus the broker's own safety checks.
- Outcomes are monitored continuously to catch systematic errors -- it alerts, it never quietly
  carries on.

In short: the AI does the synthesis a good analyst does, the code makes sure it stays consistent,
proportionate, and accountable, and the data decides what is allowed to count as an edge.

See `docs/developer_onboarding.md` for the technical map, and `docs/llm_decision_authority.md`,
`docs/hypothesis_authoring.md`, `docs/price_factor_model.md` for the detail.
