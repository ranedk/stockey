"""Portfolio buckets -- the operator's sleeves, each with its own capital and its own
internal allocation rule (operator instruction 2026-09-23: "separate portfolios for all
the 4 buckets with 1 Crore as the allocated investment").

A bucket is NOT a strategy. It is a capital envelope with a holding horizon, and the
strategy that fills it lives elsewhere: `longterm` is filled by the mechanical ruleset +
adjudicator in this repo, `momentum` by systrader's frozen paper tracks, `swing` by an
engine that does not exist yet, `options` by nothing at all. Declaring all four here
anyway is deliberate -- a bucket with no engine should be visible as an empty envelope
rather than absent, because "we decided not to run it yet" and "we forgot" look identical
in a portfolio that simply omits it.

INTERNAL ALLOCATION is the only thing this module decides: how one bucket's crore splits
across its own positions. It does not decide how much each bucket gets (that is the
operator's 30/50/10/10, applied later on top) and it never sizes a position by conviction.

Each bucket's position count is chosen against the liquidity of what it actually buys,
not as a diversification preference:
  - longterm buys the fundamentals universe, where 16 of 21 names held in September traded
    under Rs 1 crore a day. At 10% of ADV (l5_sizing's cap) a Rs 33 lakh/day name supports
    Rs 3.3 lakh, so a larger per-position target would simply be ADV-capped down and the
    bucket would silently under-deploy. 25 x Rs 4 lakh sits just under that ceiling for
    most of the book.
  - swing turns over in days to weeks, so it pays costs far more often: it is restricted
    to names liquid enough that the round trip does not eat the move (LEDGER row 35: a
    Rs 20,000 position costs ~30 bps a round trip, a Rs 1 lakh one ~25 bps, and the Rs
    14.75 DP charge is fixed per sale whatever the size).
  - momentum's numbers are not guessed here: LEDGER row 36 measured those books faithful
    to their paper record at Rs 1 crore, and breaking down below it.
"""
from __future__ import annotations

from environs import Env

env = Env()
env.read_env()

# Operator-set 2026-09-23: one crore per bucket, for now equal across all four.
DEFAULT_BUCKET_CAPITAL_RS = env.float("PORTFOLIO_BUCKET_CAPITAL_RS", 10_000_000.0)

BUCKETS: dict[str, dict] = {
    "longterm": {
        "capital_rs": DEFAULT_BUCKET_CAPITAL_RS,
        "target_positions": env.int("PORTFOLIO_LONGTERM_POSITIONS", 25),
        "hold_horizon": "quarters -- the thesis is fundamental and its target date is 6-12 months out",
        "engine": "fundamentals ruleset + adjudicator (this repo)",
        "min_adv_rs": env.float("PORTFOLIO_LONGTERM_MIN_ADV_RS", 0.0),
    },
    "swing": {
        "capital_rs": DEFAULT_BUCKET_CAPITAL_RS,
        "target_positions": env.int("PORTFOLIO_SWING_POSITIONS", 10),
        "hold_horizon": "days to weeks",
        "engine": "not built -- needs its own spec, pre-registration and forward record",
        # A swing book pays the round trip many times a year, so it may only hold names
        # where that round trip is small: Rs 5 crore a day at 10% of ADV supports the
        # Rs 10 lakh position this bucket implies.
        "min_adv_rs": env.float("PORTFOLIO_SWING_MIN_ADV_RS", 50_000_000.0),
    },
    "momentum": {
        "capital_rs": DEFAULT_BUCKET_CAPITAL_RS,
        "target_positions": None,  # set by the frozen spec's own quantile, not here
        "hold_horizon": "monthly rebalance",
        "engine": "systrader paper tracks (LEDGER rows 31-36)",
        "min_adv_rs": 100_000_000.0,  # the tracks' own Rs 10 crore floor
    },
    "options": {
        "capital_rs": DEFAULT_BUCKET_CAPITAL_RS,
        "target_positions": None,
        "hold_horizon": "undecided",
        "engine": "not built -- no option chain, IV or margin data is collected today",
        "min_adv_rs": None,
    },
}

DEFAULT_BUCKET = "longterm"


def bucket_config(bucket: str) -> dict:
    if bucket not in BUCKETS:
        raise ValueError(f"unknown bucket {bucket!r}; known: {', '.join(sorted(BUCKETS))}")
    return BUCKETS[bucket]


def capital_per_position_rs(bucket: str) -> float:
    """One position's share of its bucket's capital.

    A bucket whose position count is set by its own strategy (momentum's quantile) has no
    per-position share to compute here -- asking for one is a caller bug, not a default.
    """
    cfg = bucket_config(bucket)
    count = cfg["target_positions"]
    if not count:
        raise ValueError(f"bucket {bucket!r} sizes positions in its own engine, not here")
    return float(cfg["capital_rs"]) / int(count)
