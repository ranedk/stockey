"""The operator's hand-check of the theme map (docs/THEMATIC_WATCHLIST_PRD.md: 50 tags,
>= 90% agree before the watchlist goes live).

Draws a fixed-seed sample of KEPT tags (quantified or unquantified; disputed and invalid ones
are already out), stratified by where the share came from -- the profile's own text, an annual
report, or none -- and capped per theme so the big themes do not fill it. Writes JSON with
everything the operator needs to judge a tag: what the company does, the evidence quote, and
the share with the text it was read from.
"""
from __future__ import annotations

import argparse
import json

import pandas as pd

from fundamentals.themes import catalog
from utils.db import sql_to_df

STRATA = {"profile_share": 10, "report_share": 15, "no_share": 25}
PER_THEME = 3
SEED = 26


def load_tags(version: int) -> pd.DataFrame:
    return sql_to_df("""
        WITH latest AS (
            SELECT DISTINCT ON (company_master_id) company_master_id, fetched_on, ticker, screener_slug,
                   about, peers_path
              FROM fundamentals_company_profile WHERE status = 'ok'
             ORDER BY company_master_id, fetched_on DESC)
        SELECT t.company_master_id, l.ticker, coalesce(l.screener_slug, l.ticker) AS slug, c.company_name,
               l.about, l.peers_path, t.theme_id, t.role, t.evidence_quote, t.status AS tag_status,
               t.exposure_pct AS profile_pct, t.exposure_quote AS profile_quote, t.check_reason,
               a.status AS ar_status, a.exposure_pct AS report_pct, a.segment_name, a.segment_quote,
               a.theme_quote, a.total_quote, a.fiscal_year, a.report_url, a.report_label
          FROM fundamentals_theme_tag t
          JOIN latest l ON l.company_master_id = t.company_master_id AND l.fetched_on = t.profile_fetched_on
          JOIN company_master c ON c.company_master_id = t.company_master_id
          LEFT JOIN fundamentals_theme_exposure_ar a
            ON a.company_master_id = t.company_master_id AND a.theme_version = t.theme_version
           AND a.theme_id = t.theme_id
         WHERE t.theme_version = %(v)s AND t.status IN ('quantified', 'unquantified')""", params={"v": version})


def stratum(r) -> str:
    if r.tag_status == "quantified":
        return "profile_share"
    if r.ar_status == "quantified":
        return "report_share"
    return "no_share"


def draw(tags: pd.DataFrame) -> pd.DataFrame:
    tags = tags.assign(stratum=[stratum(r) for r in tags.itertuples()])
    tags = tags.sample(frac=1.0, random_state=SEED)
    picked, per_theme = [], {}
    for name, n in STRATA.items():
        got = 0
        for i, r in tags[tags["stratum"] == name].iterrows():
            if got == n:
                break
            if per_theme.get(r.theme_id, 0) >= PER_THEME:
                continue
            picked.append(i)
            per_theme[r.theme_id] = per_theme.get(r.theme_id, 0) + 1
            got += 1
    return tags.loc[picked]


def to_items(sample: pd.DataFrame, version: int) -> list[dict]:
    themes = {t.id: t for t in catalog.load(version)}
    out = []
    for k, r in enumerate(sample.itertuples(), 1):
        th = themes[r.theme_id]
        if r.stratum == "profile_share":
            share = {"pct": r.profile_pct, "source": "screener.in key points", "quote": r.profile_quote}
        elif r.stratum == "report_share":
            share = {"pct": r.report_pct, "source": f"annual report ({r.report_label}), {r.fiscal_year}",
                     "segment": r.segment_name, "quote": f"{r.theme_quote}  /  total: {r.total_quote}",
                     "url": r.report_url}
        else:
            share = {"pct": None, "source": "not separately reported" if r.ar_status == "not_separate" else "no figure"}
        out.append({
            "id": f"{r.company_master_id}:{r.theme_id}", "n": k, "stratum": r.stratum,
            "company": r.company_name, "ticker": r.ticker,
            "screener_url": f"https://www.screener.in/company/{r.slug}/",
            "industry": r.peers_path, "about": (r.about or "")[:600],
            "theme_id": th.id, "theme": th.name, "definition": th.definition, "exclude": list(th.exclude),
            "min_exposure": th.min_exposure, "role": r.role, "evidence": r.evidence_quote,
            "share": share, "check_reason": r.check_reason,
        })
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Draw the 50-tag hand-check sample.")
    ap.add_argument("--version", type=int, default=1)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    sample = draw(load_tags(a.version))
    items = to_items(sample, a.version)
    with open(a.out, "w") as fh:
        json.dump({"version": a.version, "seed": SEED, "items": items}, fh, indent=1, default=str)
    print(json.dumps(sample["stratum"].value_counts().to_dict()), sample["theme_id"].nunique(), "themes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
