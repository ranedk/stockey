from datetime import date

import pytest

from data import download_runner
from data.nseindia import surveillance_indicator as si

HEADER = (
    "ScripCode,Symbol,Nse Exclusive,Status,Series,GSM,"
    "Long_Term_Additional_Surveillance_Measure (Long Term ASM),Unsolicited_SMS,"
    "Insolvency_Resolution_Process(IRP),"
    "Short_Term_Additional_Surveillance_Measure (Short Term ASM),Default,ICA,Filler4,Filler5,"
    "Pledge,Add-on_PB,Total Pledge,Social Media Platforms,ESM,Loss making,"
    "The Overall encumbered share in the scrip is more than 50 Percent.,Under BZ/SZ Series\n"
)


def test_parse_csv_types_flags_and_keeps_raw_row():
    content = (HEADER
               + "500001, ABC ,N,A,EQ,100,2,100,100,100,100,100,,,100,100,100,100,1,0,100,100\n"
               + "500002,XYZ,N,A,BE,0,100,100,1,100,100,100,,,1,100,100,100,100,100,0,100\n").encode()

    df = si.parse_csv(content, date(2026, 9, 24))

    assert df["symbol"].tolist() == ["ABC", "XYZ"]
    abc = df.iloc[0]
    assert (abc["gsm"], abc["lt_asm"], abc["st_asm"], abc["esm"], abc["loss_making"]) == (100, 2, 100, 1, 0)
    xyz = df.iloc[1]
    assert (xyz["gsm"], xyz["irp"], xyz["pledge"], xyz["encumbered_over_50"]) == (0, 1, 1, 0)
    assert '"Filler4"' not in abc["raw"] and '"GSM": "100"' in abc["raw"]


def test_parse_csv_fails_loudly_when_nse_renames_a_column():
    content = (HEADER.replace("GSM,", "GSM Stage,", 1) + "1,ABC,N,A,EQ,100,100,100,100,100,100,100,,,100,100,100,100,100,100,100,100\n").encode()
    with pytest.raises(ValueError, match="GSM"):
        si.parse_csv(content, date(2026, 9, 24))


def test_collector_is_scheduled_after_bhavcopy_parser():
    modules = [step["module"] for step in download_runner.PARSER_STEPS]
    assert modules.index("data.nseindia.surveillance_indicator") > modules.index("data.nseindia.bhavcopy_parser")
