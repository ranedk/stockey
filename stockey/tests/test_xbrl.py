from datetime import date

from fundamentals.xbrl import mapping, parse

HEAD = ('<?xml version="1.0"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink" '
        'xmlns:in="http://example/in" xmlns:xbrldi="http://xbrl.org/2006/xbrldi">'
        '<link:schemaRef xlink:type="simple" xlink:href="https://x/{tax}_2020-03-31.xsd"/>')


def ctx(cid, start=None, end=None, instant=None, dim=None):
    per = f"<xbrli:instant>{instant}</xbrli:instant>" if instant else \
        f"<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>"
    seg = f'<xbrli:segment><xbrldi:explicitMember dimension="in:{dim}">in:M</xbrldi:explicitMember></xbrli:segment>' if dim else ""
    return f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier>{seg}</xbrli:entity><xbrli:period>{per}</xbrli:period></xbrli:context>'


def fact(tag, cid, v, unit=True):
    u = ' unitRef="INR" decimals="-5"' if unit else ""
    return f'<in:{tag} contextRef="{cid}"{u}>{v}</in:{tag}>'


def doc(body, tax="Ind-AS_entry_point"):
    return (HEAD.format(tax=tax) + body + "</xbrli:xbrl>").encode()


def test_the_year_to_date_context_takes_its_period_from_the_filed_dates_not_the_context():
    xml = doc(ctx("OneD", "2023-07-01", "2023-09-30") + ctx("FourD", "2023-07-01", "2023-09-30") +   # FourD mislabelled
              ctx("OneI", instant="2023-09-30") +
              fact("DateOfStartOfReportingPeriod", "OneD", "2023-07-01", False) +
              fact("DateOfEndOfReportingPeriod", "OneD", "2023-09-30", False) +
              fact("DateOfStartOfReportingPeriod", "FourD", "2023-04-01", False) +
              fact("DateOfEndOfReportingPeriod", "FourD", "2023-09-30", False) +
              fact("RevenueFromOperations", "OneD", "13474600000") + fact("RevenueFromOperations", "FourD", "30296700000") +
              fact("Assets", "OneI", "51544300000"))
    fam, rows = mapping.extract(parse.parse(xml))
    got = {(r.item, r.kind): r.value for r in rows}
    assert fam == "company"
    assert got[("revenue", "q")] == 13474600000 and got[("revenue", "h1")] == 30296700000
    assert got[("total_assets", "i")] == 51544300000


def test_an_undefined_context_is_still_read_by_the_templates_naming():
    xml = doc(fact("DateOfEndOfReportingPeriod", "OneD", "2023-06-30", False) +
              fact("InterestEarned", "OneD", "485870000000") + fact("Advances", "OneI", "1") +
              fact("Deposits", "OneI", "1"), tax="banking_entry_point")
    fam, rows = mapping.extract(parse.parse(xml))
    r = next(r for r in rows if r.item == "revenue")
    assert fam == "bank" and r.kind == "q" and r.start == date(2023, 4, 1) and r.value == 485870000000


def test_segment_lines_never_count_as_the_company_total_and_sums_add_parts():
    xml = doc(ctx("OneI", instant="2024-03-31") + ctx("SegI", instant="2024-03-31", dim="SegmentsAxis") +
              fact("BorrowingsCurrent", "OneI", "100") + fact("BorrowingsNoncurrent", "OneI", "50") +
              fact("Assets", "SegI", "999") + fact("Assets", "OneI", "1000"))
    _, rows = mapping.extract(parse.parse(xml))
    got = {r.item: r.value for r in rows}
    assert got["borrowings"] == 150 and got["total_assets"] == 1000


def test_excise_or_gst_filed_as_an_other_expense_line_is_taken_off_revenue_for_the_period():
    xml = doc(ctx("OneD", "2026-04-01", "2026-06-30") + ctx("FourD", "2026-04-01", "2026-06-30") +
              ctx("OneExpenses1D", "2026-04-01", "2026-06-30", dim="OtherExpensesAxis") +
              ctx("OneExpenses2D", "2026-04-01", "2026-06-30", dim="OtherExpensesAxis") +
              ctx("FourExpenses1D", "2026-04-01", "2026-06-30", dim="OtherExpensesAxis") +   # mislabelled YTD
              fact("DateOfStartOfReportingPeriod", "FourD", "2026-01-01", False) +
              fact("DateOfEndOfReportingPeriod", "FourD", "2026-06-30", False) +
              fact("RevenueFromOperations", "OneD", "1066500000") + fact("RevenueFromOperations", "FourD", "2000000000") +
              fact("DescriptionOfOtherExpenses", "OneExpenses1D", "Excise duty on sales", False) +
              fact("OtherExpenses", "OneExpenses1D", "75900000") +
              fact("DescriptionOfOtherExpenses", "OneExpenses2D", "Selling and marketing", False) +
              fact("OtherExpenses", "OneExpenses2D", "122500000") +
              fact("DescriptionOfOtherExpenses", "FourExpenses1D", "Excise duty on sales", False) +
              fact("OtherExpenses", "FourExpenses1D", "150000000"))
    _, rows = mapping.extract(parse.parse(xml))
    got = {(r.item, r.kind): r.value for r in rows}
    assert got[("excise_duty", "q")] == 75900000 and got[("revenue_net", "q")] == 1066500000 - 75900000
    assert got[("excise_duty", "h1")] == 150000000 and got[("revenue_net", "h1")] == 2000000000 - 150000000


def test_family_follows_the_tags_in_the_shared_2025_taxonomy():
    def fam(*tags):
        return mapping.family(parse.parse(doc("".join(fact(t, "OneD", "1") for t in tags), tax="in-capmkt-ent")))
    assert fam("RevenueFromOperations") == "company"
    assert fam("InterestEarned", "RevenueFromOperations") == "nbfc"
    assert fam("InterestEarned", "InterestExpended") == "bank"
    assert fam("GrossPremiumIncome") == "insurer"


def test_checks_flag_a_filing_that_does_not_add_up():
    R = mapping.Row
    end = date(2024, 3, 31)
    ok = [R("pbt", "q", None, end, 100e7, "", ""), R("tax", "q", None, end, 25e7, "", ""),
          R("pat_continuing", "q", None, end, 75e7, "", "")]
    assert all(c["ok"] for c in mapping.checks("company", ok))
    bad = ok[:2] + [R("pat_continuing", "q", None, end, 7.5e9, "", "")]       # typed 100x too big
    assert not mapping.checks("company", bad)[0]["ok"]


def test_listing_keeps_every_xbrl_filing_from_2020_with_its_basis_and_publish_time():
    from fundamentals.xbrl import store
    res = {"old": [{"xbrl": "https://n/x/A.xml", "toDate": "31-Dec-2024", "consolidated": "Non-Consolidated",
                    "broadCastDate": "06-Feb-2025 10:15:05", "reInd": "N"},
                   {"xbrl": "https://n/x/B.xml", "toDate": "31-Dec-2019", "consolidated": "Consolidated"},   # before 2020
                   {"xbrl": "-", "toDate": "30-Jun-2023", "consolidated": "Consolidated"}],                 # no XBRL
           "int": [{"xbrl": "https://n/x/C.xml", "qe_Date": "30-SEP-2026", "consolidated": "Consolidated",
                    "broadcast_Date": "03-Nov-2026 17:30:27", "type_Sub": "Revised"}]}
    rows = {r["url"][-5]: r for r in store.listing_rows("SOLARINDS", res)}
    assert set(rows) == {"A", "C"}
    assert rows["A"]["basis"] == "standalone" and rows["A"]["public_at"].hour == 10
    assert rows["C"]["basis"] == "consolidated" and rows["C"]["revision"] == "revised"
    assert rows["C"]["period_end"] == date(2026, 9, 30)


def test_comparison_with_screener_in_reports_the_gap_in_percent():
    import pandas as pd
    from fundamentals.xbrl import store
    ours = pd.DataFrame({"symbol": ["A"], "period_end": [date(2024, 3, 31)], "sales_ours": [102.0], "profit_ours": [9.0]})
    scr = pd.DataFrame({"symbol": ["A"], "period_end": [date(2024, 3, 31)], "sales": [100.0], "net_profit": [10.0]})
    m = store.compare(ours, scr)
    assert round(m.loc[0, "sales_gap_pct"], 6) == 2.0 and round(m.loc[0, "profit_gap_pct"], 6) == -10.0
