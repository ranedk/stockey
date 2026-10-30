from datetime import date

from fundamentals.collectors import sebi_orders as so

PAGE = """<div>1 to 25 of 31,028 records</div><table><tr><th>Date</th><th>Title</th></tr>
<tr><td>Oct 27, 2026</td><td><a href='https://www.sebi.gov.in/enforcement/orders/oct-2026/x_105452.html'
 title='Adjudication Order in the matter of Taurian MPS Ltd.'>Adjudication Order in the matter of Taurian MPS Ltd.</a></td></tr>
<tr><td>Oct 22, 2026</td><td><a href='https://www.sebi.gov.in/enforcement/orders/oct-2026/y_105400.html'
 title="Adjudication order in the matter of Rishabh Tanwar<br><a href='c.pdf'>[Corrigendum]</a>">Adjudication order</a></td></tr>
<tr><td>Oct 16, 2026</td><td><a href='https://www.sebi.gov.in/enforcement/orders/oct-2026/z_105300.html'
 title='Appeal No. 7107 of 2026 filed by Deepali Singhal'>Appeal</a></td></tr>
</table>"""


def test_a_listing_page_gives_date_title_link_kind_and_matter():
    rows, total = so.parse_page(PAGE)
    assert total == 31028
    assert [r["order_date"] for r in rows] == [date(2026, 10, 27), date(2026, 10, 22), date(2026, 10, 16)]
    assert rows[0]["matter"] == "Taurian MPS Ltd" and rows[0]["kind"] == "adjudication"
    assert rows[1]["title"] == "Adjudication order in the matter of Rishabh Tanwar"
    assert rows[2]["kind"] == "appeal" and rows[2]["matter"] is None


def test_order_kinds_read_off_the_title():
    assert so.order_kind("Settlement Order in the matter of X Ltd") == "settlement"
    assert so.order_kind("Interim Order cum Show Cause Notice in the matter of X Ltd") == "interim"
    assert so.order_kind("Confirmatory order in the matter of Rajesh Exports Limited") == "confirmatory"
    assert so.order_kind("Exemption Order in the matter of Allcargo Logistics Limited") == "exemption"
    assert so.order_kind("Final Order in the matter of X Ltd") == "final"
    assert so.order_kind("Recovery of dues from Y") == "other"
