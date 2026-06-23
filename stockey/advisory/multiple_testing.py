"""Multiple-testing / false-discovery controls for research promotion candidates.

When many candidate groups (source family x horizon, split buckets, event classes, ...) are
screened in one run, some clear threshold gates by chance. These helpers add a Benjamini-Hochberg
false-discovery-rate (FDR) control on top of the existing deterministic gates so a handful of
lucky candidates cannot become promotion reviews on noise alone.

All functions are pure and dependency-free (no numpy/scipy). They are research-only utilities:
they never change config, portfolio, or broker behavior.
"""

from __future__ import annotations

import math


def binomial_right_tail_p_value(successes: int, trials: int, p: float = 0.5) -> float | None:
    """One-sided p-value P(X >= successes) for X ~ Binomial(trials, p).

    Used to test a candidate's null hypothesis "beating the benchmark after costs is a coin
    flip" (p=0.5) from its excess-hit count. Returns None when trials <= 0 (cannot test).
    """
    if trials is None or int(trials) <= 0:
        return None
    n = int(trials)
    k = max(0, min(int(successes), n))
    p = float(p)
    if p <= 0.0:
        return 1.0 if k <= 0 else 0.0
    if p >= 1.0:
        return 1.0 if k <= n else 0.0
    if k <= 0:
        return 1.0
    if n <= 1000:
        total = 0.0
        for i in range(k, n + 1):
            total += math.comb(n, i) * (p ** i) * ((1.0 - p) ** (n - i))
        return float(min(1.0, max(0.0, total)))
    # Normal approximation with continuity correction for large n.
    mean = n * p
    sd = math.sqrt(n * p * (1.0 - p))
    if sd <= 0.0:
        return 1.0 if k <= mean else 0.0
    z = (k - 0.5 - mean) / sd
    return float(min(1.0, max(0.0, 0.5 * math.erfc(z / math.sqrt(2.0)))))


def benjamini_hochberg(p_values: list[float | None], alpha: float = 0.10) -> list[bool]:
    """Benjamini-Hochberg FDR control. Returns per-item rejected flags (order-preserving).

    rejected[i] is True when p_values[i] survives FDR control at `alpha` (i.e. is a discovery).
    None p-values (untestable candidates) are treated as non-significant.
    """
    n = len(p_values)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: (1.0 if p_values[i] is None else float(p_values[i])))
    max_rank = 0
    for rank, idx in enumerate(order, start=1):
        p = p_values[idx]
        if p is None:
            continue
        if float(p) <= (rank / n) * float(alpha):
            max_rank = rank
    rejected = [False] * n
    if max_rank > 0:
        for rank, idx in enumerate(order, start=1):
            if rank <= max_rank and p_values[idx] is not None:
                rejected[idx] = True
    return rejected


def benjamini_hochberg_qvalues(p_values: list[float | None]) -> list[float | None]:
    """BH-adjusted q-values (order-preserving). None stays None."""
    n = len(p_values)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: (1.0 if p_values[i] is None else float(p_values[i])))
    q: list[float | None] = [None] * n
    running_min = 1.0
    for rank in range(n, 0, -1):
        idx = order[rank - 1]
        p = p_values[idx]
        if p is None:
            continue
        running_min = min(running_min, float(p) * n / rank)
        q[idx] = float(min(1.0, max(0.0, running_min)))
    return q
