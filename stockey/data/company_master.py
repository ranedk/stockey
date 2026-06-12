import json

from utils.company_master import sync_company_master

SYNC_SOURCE_NAME = "data.company_master"
STOCKEY_RUN_STATE: dict[str, object] = {}


def build_run_state(df) -> dict[str, object]:
    rows = int(len(df))
    state: dict[str, object] = {
        "source": SYNC_SOURCE_NAME,
        "rows": rows,
        "rows_read": rows,
        "rows_written": rows,
        "attempt_count": 1,
        "failed_attempt_count": 0,
        "source_unavailable_count": 0,
        "no_data_count": 1 if rows == 0 else 0,
        "fallback_used": False,
        "state_advanced": bool(rows > 0),
    }
    if rows and hasattr(df, "columns"):
        for column in ["nse_ticker", "bse_ticker", "sharpely_id", "dhan_nse_id", "dhan_bse_id"]:
            if column in df.columns:
                state[f"{column}_count"] = int(df[column].dropna().nunique())
    return state


def main() -> int:
    global STOCKEY_RUN_STATE
    df = sync_company_master()
    STOCKEY_RUN_STATE = build_run_state(df)
    status = "no_data" if int(STOCKEY_RUN_STATE.get("no_data_count") or 0) else "ok"
    print(json.dumps({"status": status, **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
