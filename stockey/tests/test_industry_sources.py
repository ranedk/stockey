import io

import pandas as pd
import pytest

from fundamentals.collectors import industry_classification as ic
from fundamentals.collectors import rbi_nbfc_registry as rbi


def test_parse_bse_header_maps_four_levels():
    payload = {"Sector": "Financial Services", "IndustryNew": "Financial Services", "IGroup": "Finance",
               "ISubGroup": "Housing Finance Company", "Industry": "Housing Finance Company"}
    assert ic.parse_bse_header(payload) == {
        "macro_sector": "Financial Services", "sector": "Financial Services",
        "industry": "Finance", "basic_industry": "Housing Finance Company",
    }


def test_parse_bse_header_without_classification_is_none():
    assert ic.parse_bse_header({"Sector": "-", "IndustryNew": "", "IGroup": None, "ISubGroup": "-"}) is None


def test_parse_nse_symbol_data_reads_nested_labels():
    payload = {"equityResponse": [{"secInfo": {"sector": "Financial Services", "industryInfo": "Capital Markets",
                                               "basicIndustry": "Depositories Clearing Houses and Other Intermediaries"}}]}
    labels = ic.parse_nse_symbol_data(payload)
    assert labels["basic_industry"] == "Depositories Clearing Houses and Other Intermediaries"
    assert labels["industry"] == "Capital Markets" and labels["macro_sector"] is None


def test_load_targets_orders_never_labelled_then_priority_then_oldest(monkeypatch):
    rows = pd.DataFrame([
        {"isin": "INE1", "symbol": "OLD", "company_master_id": "a", "bse_scrip_code": "1", "fetch_date": "2026-01-01"},
        {"isin": "INE2", "symbol": "FRESH", "company_master_id": "b", "bse_scrip_code": "2", "fetch_date": "2026-09-20"},
        {"isin": "INE3", "symbol": "NEW", "company_master_id": "c", "bse_scrip_code": "3", "fetch_date": None},
        {"isin": "INE4", "symbol": "NEWPRIO", "company_master_id": "d", "bse_scrip_code": None, "fetch_date": None},
    ])
    monkeypatch.setattr(ic, "sql_to_df", lambda *a, **k: rows.copy())
    targets = ic.load_targets(today=pd.Timestamp("2026-09-25", tz="Asia/Kolkata"), priority_symbols={"NEWPRIO"})
    assert targets["symbol"].tolist() == ["NEWPRIO", "NEW", "OLD"]  # FRESH is not due


def _rbi_xlsx(title="List of NBFCs registered with the RBI (as on June 30, 2026)"):
    rows = [
        [title, None, None, None, None, None, None, None, None],
        ["Sl. No.", "NBFC Name", "Regional Office", "Whether have CoR for holding/ Accepting Public Deposits",
         "Classification", "Corporate Identification Number", "Layer", "Address", "Email ID"],
        [1, "Jio Financial Services Limited", "Mumbai", "No", "CIC", "L65990MH1999PLC120918", "Middle", "x", "y"],
        [2, "Some Shell Private Limited", "Kolkata", "No", "ICC*", "U65999WB1990PTC000001", "Base", "x", "y"],
        [3, "Deposit Co Limited", "Chennai", "Yes", "ICC", "L65191TN1990PLC000002", "Middle", "x", "y"],
    ]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf) as xw:
        pd.DataFrame(rows).to_excel(xw, sheet_name="List of NBFCs", header=False, index=False)
    return buf.getvalue()


def test_parse_rbi_registry():
    df = rbi.parse_xlsx(_rbi_xlsx()).set_index("nbfc_name")
    assert str(df["as_of_date"].iloc[0]) == "2026-06-30"
    assert df.at["Some Shell Private Limited", "classification"] == "ICC"  # footnote star dropped
    assert df["is_listed"].to_dict() == {"Jio Financial Services Limited": True,
                                         "Some Shell Private Limited": False, "Deposit Co Limited": True}
    assert bool(df.at["Deposit Co Limited", "deposit_taking"]) is True


def test_parse_rbi_registry_needs_the_as_on_date():
    with pytest.raises(ValueError, match="as on"):
        rbi.parse_xlsx(_rbi_xlsx(title="List of NBFCs"))
