from datetime import date

from data.nseindia import fo_bhavcopy as fo

OLD = b"""INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,
FUTIDX,NIFTY,28-Oct-2021,0,XX,18300,18400,18200,18350,18350,1000,9175,500000,-100,20-OCT-2021,
OPTIDX,NIFTY,21-Oct-2021,18200,PE,25,35,20,30.3,30.3,1891092,17243179.94,100,5,20-OCT-2021,
OPTSTK,TCS,28-Oct-2021,3500,CE,10,12,9,11,11,10,100,10,1,20-OCT-2021,
"""

UDIFF = b"""TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4
2026-10-16,2026-10-16,FO,NSE,IDO,1,,NIFTY,,2026-10-20,2026-10-19,23150,CE,X,60,80,50,69.4,69.4,70,23146.15,69.4,100,10,7453079,1.125e13,5,F1,65,,,,,
2026-10-16,2026-10-16,FO,NSE,IDF,2,,NIFTY,,2026-10-27,2026-10-27,,,Y,23200,23300,23100,23250,23250,23200,23146.15,23250,1000,5,5000,7.5e9,9,F1,65,,,,,
2026-10-16,2026-10-16,FO,NSE,STO,3,,TCS,,2026-10-27,2026-10-27,3500,CE,Z,1,1,1,1,1,1,3500,1,1,1,1,1,1,F1,175,,,,,
"""


def test_old_format_keeps_index_derivatives_only():
    df = fo.parse_old(OLD)
    assert set(df["instrument"]) == {"FUTIDX", "OPTIDX"}
    fut = df[df["instrument"] == "FUTIDX"].iloc[0]
    assert fut["option_type"] == "XX" and fut["strike"] == 0 and fut["expiry"] == date(2021, 10, 28)
    opt = df[df["instrument"] == "OPTIDX"].iloc[0]
    assert opt["trade_date"] == date(2021, 10, 20) and opt["contracts"] == 1891092
    assert abs(opt["notional_value_rs"] - 17243179.94e5) < 1


def test_udiff_uses_the_actual_expiry_and_marks_futures():
    df = fo.parse_udiff(UDIFF)
    assert list(df["instrument"]) == ["OPTIDX", "FUTIDX"]
    opt, fut = df.iloc[0], df.iloc[1]
    assert opt["expiry"] == date(2026, 10, 19)  # moved for a holiday: the actual one wins
    assert opt["lot_size"] == 65 and opt["underlying_price"] == 23146.15
    assert fut["option_type"] == "XX" and fut["strike"] == 0
    assert set(df.columns) >= set(fo.COLS)
