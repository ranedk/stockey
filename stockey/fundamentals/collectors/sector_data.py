"""Sector reference data -- feeds fundamental screener step 9 (sector capital-cycle
aggregation, docs/FUNDAMENTAL_SCREENER_PRD.md sec 8 step 9). Resolves
dim_security.sector_code (already populated by the pure-TA-kept
data/sharpelydata/scrip_master.py, live, ~73% coverage on the L1 universe --
confirmed 2026-08-11) into a human-readable name, plus Sharpely's own pre-aggregated
sector-level ratios as a free, independent cross-check.

Endpoint reverse-engineered live 2026-08-11 by rendering sharpely.in/stocks/sectors in
the existing CDP browser session and capturing its network calls (the sector list is
client-side-loaded, not in the static HTML) -- found `/api/v2/core/getAllSectorData`.
The AES-256-CBC decrypt scheme for /api/v2/core responses was already built in this
repo (data/sharpelydata/sharpely_utils.py's decrypt_sharpely_v2, SHARPELY_V2_AES_KEY,
already in .env.example) but had never actually been wired to a live call. This
endpoint's plaintext turned out to additionally be zlib-compressed before encryption
(decrypted bytes started with the zlib header 0x78 0x9c, not valid UTF-8 on their
own) -- confirmed live, not assumed; added decrypt_sharpely_v2_compressed alongside
the existing function rather than changing its contract (the "statements" endpoint
that function was built for is NOT compressed).

Three-level hierarchy in the response, all keyed the same way dim_security.sector_code
already is at the top level: sector (22, e.g. "IN0101"->"Chemicals") -> industry group
(59, e.g. "IN010101"->"Chemicals & Petrochemicals") -> basic industry (197, e.g.
"IN010101001"->"Commodity Chemicals"). dim_security only carries the top (sector)
level, so that's what this module stores and what sector_cycle.py groups by. The finer
levels used to also be captured (for a possible future finer-grained pass) and written
to fundamentals_industry_group_reference/fundamentals_basic_industry_reference --
retired 2026-08-15, zero readers anywhere from the day they were first written,
confirmed live. Re-add if that finer-grained pass actually gets built.
"""

from __future__ import annotations

import json
import re

import pandas as pd

from data.sharpelydata.sharpely_utils import decrypt_sharpely_v2_compressed, get_sharpely_v2_headers
from utils.db import upsert_to_db
from utils.http import get_with_retries

SYNC_SOURCE_NAME = "fundamentals.collectors.sector_data"
SECTOR_TABLE = "fundamentals_sector_reference"
STOCKEY_RUN_STATE: dict[str, object] = {}

SECTOR_DATA_URL = "https://pyapiv2.mintbox.ai/api/v2/core/getAllSectorData"

# Sharpely's own pre-aggregated ratio fields, carried through as-is (not renamed to
# match our own naming conventions) -- a free, independent cross-check alongside our
# own capacity/demand-growth computation (fundamentals/screens/sector_cycle.py), not a
# substitute for it (doesn't include gross-block or revenue growth).
_RATIO_FIELDS = ("Debt to Equity", "Div yield", "PAT margin", "Operating margin", "P/B Ratio", "P/E Ratio", "P/S Ratio", "ROCE", "ROE")


def fetch_sector_reference_data() -> dict:
    headers = get_sharpely_v2_headers()
    response = get_with_retries(SECTOR_DATA_URL, headers=headers)
    outer = decrypt_sharpely_v2_compressed(response.text)
    data = json.loads(outer)
    # The plaintext is itself a JSON-encoded string (double-encoded, same pattern
    # BSE's smart-search response used) -- unwrap once more if so.
    if isinstance(data, str):
        data = json.loads(data)
    return data


def _rows_from_level(entries: list[dict], *, code_key: str, desc_key: str, level: str, as_of_date) -> list[dict]:
    rows = []
    for entry in entries:
        code = entry.get(code_key)
        if not code:
            continue
        description = entry.get(desc_key)
        row = {
            "code": code,
            # Confirmed live 2026-08-11: some descriptions carry a literal embedded
            # newline (e.g. "Construction\nMaterials") -- collapse whitespace so
            # anything downstream doesn't have to special-case it.
            "description": re.sub(r"\s+", " ", description).strip() if description else description,
            "level": level,
            "as_of_date": as_of_date,
            "load_ts": pd.Timestamp.now(tz="UTC"),
        }
        for field in _RATIO_FIELDS:
            row[field.lower().replace(" ", "_").replace("/", "_")] = entry.get(field)
        rows.append(row)
    return rows


def build_reference_rows(data: dict, *, as_of_date=None) -> dict[str, list[dict]]:
    as_of_date = as_of_date or pd.Timestamp.now(tz="UTC").date()
    sector_entries = (data.get("sector") or {}).get("EQ") or []

    return {
        "sector": _rows_from_level(sector_entries, code_key="sector_code", desc_key="sector_desc", level="sector", as_of_date=as_of_date),
    }


def run_sector_reference_refresh() -> dict[str, object]:
    data = fetch_sector_reference_data()
    rows = build_reference_rows(data)

    if rows["sector"]:
        upsert_to_db(pd.DataFrame(rows["sector"]), SECTOR_TABLE, unique_keys=["code", "as_of_date"])

    return {
        "sectors": len(rows["sector"]),
    }


def main() -> int:
    global STOCKEY_RUN_STATE
    # Every sector reader downstream reads this view; first pipeline step, so ensure it here.
    from fundamentals.screens.company_sector import ensure_company_sector_view
    ensure_company_sector_view()
    result = run_sector_reference_refresh()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": result["sectors"],
        "rows_written": result["sectors"],
        **result,
        "fallback_used": False,
        "state_advanced": result["sectors"] > 0,
        "status": "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
