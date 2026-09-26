**The economic story rule**

Every rule needs a story about who's on the other side and why they keep paying. For trend: counterparties aren't irrational — they're disposition-effect sellers exiting winners early, mechanical rebalancers selling strength by mandate, and anchored traders fading moves on stale information. Trend followers get paid for absorbing these non-forecast-driven flows.

**ML and the story rule**

The rule is really a prior that controls multiple-testing bias. It survives ML if every *feature* has a story (LightGBM over storied features = fine). It breaks when raw prices go into a black box — the hypothesis space explodes without the compensating prior, and you lose all diagnostics when the signal decays.

**Pattern-first, story-after**

Post-hoc stories carry near-zero weight on their own (HARKing) — narratives are too easy to invent. Legitimate only as a two-stage pipeline: the story must make **new predictions the pattern didn't**, tested on data that played no role in discovery. Batch these validations to avoid burning the holdout piecemeal.

**TimesFM inputs**

Never raw prices (produces dressed-up persistence) or DMAs/indicators (lossy, falsely smooth). Use vol-standardized log returns, and ideally volatility itself — vol is autocorrelated and mean-reverting, TimesFM's natural territory. Role: vol/regime forecaster, not return predictor; LightGBM remains the signal combiner.

**TimesFM as hypothesis generator**

Use it as a structure detector: (1) forecastability scans across F&O stocks — where skill clusters is the discovery; (2) conditional forecastability by regime/expiry/events; (3) forecast-error spikes as event flags; (4) realized-vs-implied vol gaps → variance-risk-premium hypotheses. Log every scan as a trial batch in the parameter register; a couple of genuine survivors per scan is a good yield.
