from __future__ import annotations

from typing import List, Optional, Sequence

from .models import CompanyMasterTarget
from utils.company_master import load_company_master_records


def load_company_master_targets(
    ticker: str,
    exchanges: Optional[Sequence[str]] = None,
) -> List[CompanyMasterTarget]:
    df = load_company_master_records(ticker=ticker, exchanges=exchanges)
    if df.empty:
        return []

    allowed_exchanges = {value.upper() for value in exchanges} if exchanges else set()
    targets: List[CompanyMasterTarget] = []
    for row in df.to_dict(orient="records"):
        if row.get("nse_ticker") and (not allowed_exchanges or "NSE" in allowed_exchanges):
            targets.append(
                CompanyMasterTarget(
                    company_master_id=row["company_master_id"],
                    exchange="NSE",
                    ticker=row["nse_ticker"],
                    company_name=row.get("company_name"),
                    nse_ticker=row.get("nse_ticker"),
                    bse_ticker=row.get("bse_ticker"),
                    sharpely_id=row.get("sharpely_id"),
                    dhan_nse_id=row.get("dhan_nse_id"),
                    dhan_bse_id=row.get("dhan_bse_id"),
                )
            )
        if row.get("bse_ticker") and (not allowed_exchanges or "BSE" in allowed_exchanges):
            targets.append(
                CompanyMasterTarget(
                    company_master_id=row["company_master_id"],
                    exchange="BSE",
                    ticker=row["bse_ticker"],
                    company_name=row.get("company_name"),
                    nse_ticker=row.get("nse_ticker"),
                    bse_ticker=row.get("bse_ticker"),
                    sharpely_id=row.get("sharpely_id"),
                    dhan_nse_id=row.get("dhan_nse_id"),
                    dhan_bse_id=row.get("dhan_bse_id"),
                )
            )
    return targets
