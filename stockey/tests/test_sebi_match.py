import pandas as pd

from fundamentals.themes import sebi_match as sm


def test_roles():
    assert sm.role_of("Taurian MPS Ltd", sm.tokens("Taurian MPS Ltd.")) == "subject"
    assert sm.role_of("suspected insider trading in the scrip of Wipro Ltd", sm.tokens("Wipro Ltd.")) == "scrip"
    assert sm.role_of("role of Axis Capital Limited in providing guarantee to the NCDs of Sojo Infotel Pvt. Ltd",
                      sm.tokens("Sojo Infotel")) == "other"
    assert sm.role_of("Rajesh Exports Limited", sm.tokens("Rajesh Exports")) == "subject"
    # whole words only: "Tata Power" is not in "Tatanagar Power"
    assert sm.role_of("Tatanagar Power Ltd", sm.tokens("Tata Power")) is None
    # "Corp" is part of the name: Delta Corp is not Delta Leasing and Finance
    assert sm.role_of("Delta Leasing and Finance Ltd", sm.tokens("Delta Corp Ltd.")) is None
    assert sm.role_of("Titan Company Limited", sm.tokens("Titan Company Ltd.")) == "subject"


def test_named_in_the_parties_block():
    parties = "Noticee No. Name of the Noticee PAN 1 A Rathinappan ADLPR8177K 2 Titan Company Limited AAACT"
    assert sm.named_in(parties, sm.tokens("Titan Company Ltd."))
    assert not sm.named_in("1 A Rathinappan 2 G. Manigandan", sm.tokens("Titan Company Ltd."))


def test_match_orders_skips_short_names_and_orders_without_a_matter():
    orders = pd.DataFrame({"url": ["u1", "u2", "u3"], "order_date": ["2026-10-27"] * 3,
                           "kind": ["adjudication", "appeal", "final"],
                           "matter": ["Taurian MPS Ltd", None, "ITC Infotech dealings"]})
    companies = pd.DataFrame({"company_master_id": ["nse:TAURIAN", "nse:ITC"],
                              "company_name": ["Taurian MPS Ltd.", "ITC Ltd."]})
    got = sm.match_orders(orders, companies)
    assert list(got["company_master_id"]) == ["nse:TAURIAN"] and got["role"].iloc[0] == "subject"
