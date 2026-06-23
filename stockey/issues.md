# ISSUES.md

## Current Development Priorities

1. Improve BUY underparticipation using evidence, not intuition.
   - Use `advisory.recommendation_diagnostics` and
     `advisory.technical_threshold_calibration`.
   - Current evidence has repeatedly shown no-BUY causes are mostly technical:
     missing entry trigger, weak participation, total technical score below BUY
     threshold, breakout volume, pivot clearance, close quality, and stale
     candidate evidence.
   - Do not loosen global regime policy to force BUYs.

2. Continue replacing single-regime thinking with layered context.
   - Keep broad regime as annotation/context.
   - Use source-family, exact event class, sector split, macro/breadth, and
     benchmark-excess evidence.

3. Make context-to-entry reliable.
   - Fresh context rows should become durable watchlist pressure.
   - Durable watch rows should get point-in-time technical prechecks.
   - Technical/risk/lifecycle confirmation should be required before portfolio
     or broker authority.

4. Keep source degradation visible.
   - NSE/Dhan/Screener/announcement/macro failures should persist fallback
     telemetry and sync-state health.
   - Do not hide source unavailability as empty data.

5. Keep research evidence operational.
   - Technical threshold calibration, signal-quality split reports,
     event-policy evaluator, causal-memory evaluator, and adversarial-review
     evaluator should remain easy to run and inspect.

6. Performance matters, but correctness comes first.
   - Avoid long full-advisory runs when a bounded repair is sufficient.
   - Do not parallelize browser-bound NSE/Dhan/Screener flows aggressively;
     these sources are rate/session sensitive.
