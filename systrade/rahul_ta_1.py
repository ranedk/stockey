from kiteconnect import KiteConnect
import pandas as pd
from datetime import datetime, timedelta

# ---------------------------------
# Zerodha Login
# ---------------------------------

API_KEY = ""
ACCESS_TOKEN = ""

kite = KiteConnect(api_key=API_KEY)
kite.set_access_token(ACCESS_TOKEN)

# ---------------------------------
# Get NSE Equity Stocks
# ---------------------------------

# ---------------------------------
# Read NSE Equity List
# ---------------------------------

equity_file = r"D:\Users\Documents\EQUITY_L.csv"

equity_df = pd.read_csv(equity_file)

equity_symbols = equity_df["SYMBOL"].tolist()

print("Total NSE Equities in CSV :", len(equity_symbols))


# ---------------------------------
# Download Zerodha Instruments
# ---------------------------------

print("Downloading Zerodha instruments...")

instruments = kite.instruments("NSE")


# ---------------------------------
# Create Symbol → Token Dictionary
# ---------------------------------

token_lookup = {}

for item in instruments:

    token_lookup[item["tradingsymbol"]] = item["instrument_token"]


# ---------------------------------
# Create Final Stock List
# ---------------------------------

stocks = []

missing = []

for symbol in equity_symbols:

    if symbol in token_lookup:

        stocks.append(
            (
                symbol,
                token_lookup[symbol]
            )
        )

    else:

        missing.append(symbol)


print("Stocks to Scan :", len(stocks))
print("Missing Symbols :", len(missing))


# ---------------------------------
# Scan Starts
# ---------------------------------

results = []

count = 1

for symbol, token in stocks:

    print(f"{count}/{len(stocks)}  {symbol}")

    count += 1

    try:

        # -------------------------
        # Download History
        # -------------------------

        to_date = datetime.today()

        from_date = to_date - timedelta(days=800)

        data = kite.historical_data(
            token,
            from_date.strftime("%Y-%m-%d"),
            to_date.strftime("%Y-%m-%d"),
            "day"
        )

        
        if len(data) < 500:
            continue

        df = pd.DataFrame(data)

        df = df.tail(500)

        df.reset_index(drop=True, inplace=True)

        # ---------------------------------
        # MACD Calculation
        # ---------------------------------

        df["EMA6"] = df["close"].ewm(span=6, adjust=False).mean()

        df["EMA13"] = df["close"].ewm(span=13, adjust=False).mean()

        df["MACD"] = df["EMA6"] - df["EMA13"]

        df["Signal_Line"] = df["MACD"].ewm(span=5, adjust=False).mean()


        # ---------------------------------
        # MACD First Derivative
        # ---------------------------------

        df["MACD_First_Derivative"] = (df["MACD"] - df["MACD"].shift(1))


        # ---------------------------------     
        # MACD Second Derivative
        # ---------------------------------

        df["MACD_Second_Derivative"] = (df["MACD"] - 2 * df["MACD"].shift(1) + df["MACD"].shift(2))

        # ---------------------------------
        # Signal Line First Derivative
        # ---------------------------------

        df["Signal_First_Derivative"] = (df["Signal_Line"] - df["Signal_Line"].shift(1))

        # ---------------------------------
        # Signal Line Second Derivative
        # ---------------------------------

        df["Signal_Second_Derivative"] = (df["Signal_Line"] - 2 * df["Signal_Line"].shift(1) + df["Signal_Line"].shift(2))

        # ---------------------------------
        # Bollinger Bands (20,2)
        # ---------------------------------

        df["SMA20"] = df["close"].rolling(20).mean()

        df["STD20"] = df["close"].rolling(20).std()

        df["BB_Upper"] = df["SMA20"] + (2 * df["STD20"])

        df["BB_Lower"] = df["SMA20"] - (2 * df["STD20"])

        # ---------------------------------
        # Bollinger Band Slopes
        # ---------------------------------

        df["BB_Upper_Slope"] = df["BB_Upper"] - df["BB_Upper"].shift(1)

        df["BB_Lower_Slope"] = df["BB_Lower"] - df["BB_Lower"].shift(1)

        df["BB_Slope_Diff"] = (df["BB_Lower_Slope"] - df["BB_Upper_Slope"])


        # ---------------------------------
        # Bollinger Band Second Derivatives
        # ---------------------------------

        df["BB_Upper_Second_Derivative"] = (
        df["BB_Upper_Slope"] - df["BB_Upper_Slope"].shift(1)
        )

        df["BB_Lower_Second_Derivative"] = (
        df["BB_Lower_Slope"] - df["BB_Lower_Slope"].shift(1)
        )
        
        # ---------------------------------
        # 10 EMA frst and second derivative
        # ---------------------------------

        df["EMA10"] = df["close"].ewm(span=10, adjust=False).mean()

        df["EMA10_Slope"] = df["EMA10"] - df["EMA10"].shift(1)

        df["EMA10_Second_Derivative"] = (df["EMA10"] - 2 * df["EMA10"].shift(1) + df["EMA10"].shift(2))


        # ---------------------------------
        # 20 EMA first and second derivative
        # ---------------------------------

        df["EMA20"] = df["close"].ewm(span=20, adjust=False).mean()

        df["EMA20_Slope"] = (df["EMA20"] - df["EMA20"].shift(1))

        df["EMA20_Second_Derivative"] = (df["EMA20"]- 2 * df["EMA20"].shift(1)+ df["EMA20"].shift(2))
        # -------------------------
        # COI LOGIC
        # -------------------------

        # ---------------------------------
        # COI Logic - Scan Backwards
        # ---------------------------------

        coi_list = []

        candidate_index = None
        candidate_low = None
        

        for i in range(len(df)-2, -1, -1):

                low = df.loc[i, "low"]

            

                if candidate_index is None:

                    if low < df.loc[i+1, "low"]:

                        candidate_index = i
                        candidate_low = low

                else:

                    if low < candidate_low:

                        candidate_index = i
                        candidate_low = low

                    else:

                        coi_list.append(candidate_index)

                        
                        candidate_index = None
                        candidate_low = None

           # If scan ends while a candidate is still active,
            # mark that candle also
        if candidate_index is not None:

            coi_list.append(candidate_index)

        # ---------------------------------
        # COI Validation
        # ---------------------------------

        valid_coi = []

        for i in coi_list:

            if i == 0 or i == len(df)-1:
                continue

            c_minus_1 = df.loc[i-1]
            c0 = df.loc[i]
            c_plus_1 = df.loc[i+1]
            
# ---------------
#c+1 slope
# ----------------

            macd_c_plus_1 = c_plus_1["MACD"]

            signal_c_plus_1 = c_plus_1["Signal_Line"]

            macd_above_signal_c_plus_1 = macd_c_plus_1 > signal_c_plus_1

            signal_first_derivative_c_plus_1 = (c_plus_1["Signal_First_Derivative"])

            signal_second_derivative_c_plus_1 = (c_plus_1["Signal_Second_Derivative"])

            ema10_slope_c_plus_1 = c_plus_1["EMA10_Slope"]

            ema10_second_derivative_c_plus_1 = (c_plus_1["EMA10_Second_Derivative"])

            bb_slope_diff_c_plus_1 = c_plus_1["BB_Slope_Diff"]

            ema20_slope_c_plus_1 = c_plus_1["EMA20_Slope"]

            ema20_second_derivative_c_plus_1 = (c_plus_1["EMA20_Second_Derivative"])

            upper_band_slope_c_plus_1 = c_plus_1["BB_Upper_Slope"]

            lower_band_slope_c_plus_1 = c_plus_1["BB_Lower_Slope"]

            upper_band_second_derivative_c_plus_1 = (c_plus_1["BB_Upper_Second_Derivative"])

            lower_band_second_derivative_c_plus_1 = (c_plus_1["BB_Lower_Second_Derivative"])

            if (
                c_minus_1["close"] < c_minus_1["open"]
                and c0["high"] < c_minus_1["high"]
                and c0["low"] < c_minus_1["low"]
                and c_plus_1["high"] > c0["high"]
                and c_plus_1["low"] > c0["low"]
                and c_plus_1["close"] > c0["high"]
            ):

                # ---------------------------------
                # Check Bollinger Band Importance
                # ---------------------------------

                important = False

                for candle in [c_minus_1, c0, c_plus_1]:

                    bb = candle["BB_Lower"]

                    body_low = min(candle["open"], candle["close"])
                    body_high = max(candle["open"], candle["close"])

                    # Candle touches Lower Bollinger Band
                    if candle["low"] <= bb <= candle["high"]:
                        important = True
                        break

                    # Candle body crosses Lower Bollinger Band
                    if body_low <= bb <= body_high:
                        important = True
                        break

                # ---------------------------------
                # Very Important
                # ---------------------------------

                very_important = False
        
                if c_plus_1["BB_Slope_Diff"] > 0:
                    very_important = True

                # ---------------------------------
                # Upper Band Rising
                # ---------------------------------

                upper_band_rising = False

                if important and c_plus_1["BB_Upper_Slope"] > 0:
                    upper_band_rising = True

                macd_first_derivative = c_plus_1["MACD_First_Derivative"]

                macd_second_derivative = c_plus_1["MACD_Second_Derivative"]

                valid_coi.append((i, important, very_important, upper_band_rising,  macd_first_derivative, macd_second_derivative, signal_first_derivative_c_plus_1,
                signal_second_derivative_c_plus_1,ema10_slope_c_plus_1, ema10_second_derivative_c_plus_1, bb_slope_diff_c_plus_1, ema20_slope_c_plus_1, ema20_second_derivative_c_plus_1,
                upper_band_slope_c_plus_1, lower_band_slope_c_plus_1, upper_band_second_derivative_c_plus_1, lower_band_second_derivative_c_plus_1,
                macd_c_plus_1,signal_c_plus_1,macd_above_signal_c_plus_1))

        # ---------------------------------
        # Save Result
        # ---------------------------------

        if len(valid_coi) > 0:

            for latest, important, very_important, upper_band_rising,  macd_first_derivative, macd_second_derivative, signal_first_derivative_c_plus_1, signal_second_derivative_c_plus_1, ema10_slope_c_plus_1, ema10_second_derivative_c_plus_1, bb_slope_diff_c_plus_1, ema20_slope_c_plus_1, ema20_second_derivative_c_plus_1, upper_band_slope_c_plus_1, lower_band_slope_c_plus_1, upper_band_second_derivative_c_plus_1, lower_band_second_derivative_c_plus_1, macd_c_plus_1, signal_c_plus_1, macd_above_signal_c_plus_1 in valid_coi:

                # -------------------------------------------------
                # MOVE AFTER C+1 UNTIL CLOSE BELOW C0 LOW
                # -------------------------------------------------

                c_plus_1_index = latest + 1

                # Safety check: make sure C+1 exists
                if c_plus_1_index < len(df):

                    c_plus_1 = df.loc[c_plus_1_index]

                    entry_close = c_plus_1["close"]
                    c0_low = df.loc[latest, "low"]

    # -------------------------------------------------
    # Start with C+1 itself
    # C+1 = Day 0
    # -------------------------------------------------

                    highest_high_before_break = c_plus_1["high"]
                    highest_high_index = c_plus_1_index

                    close_below_c0_index = None

    # -------------------------------------------------
    # Scan candles AFTER C+1
    # -------------------------------------------------

                    for j in range(c_plus_1_index + 1, len(df)):

                        current_high = df.loc[j, "high"]
                        current_close = df.loc[j, "close"]

        # -------------------------------------------------
        # First check whether candle CLOSED below C0 low
        # If yes, setup is considered broken.
        # This candle is NOT included in highest-high calculation.
        # -------------------------------------------------

                        if current_close < c0_low:

                            close_below_c0_index = j
                            break

        # -------------------------------------------------
        # Track highest high while setup remains valid
        # -------------------------------------------------

                        if current_high > highest_high_before_break:

                            highest_high_before_break = current_high
                            highest_high_index = j

    # -------------------------------------------------
    # Maximum percentage move from C+1 close
    # -------------------------------------------------

                    max_move_before_break = ((highest_high_before_break - entry_close)/ entry_close) * 100

    # -------------------------------------------------
    # Trading days taken to reach highest high
    #
    # C+1 itself = Day 0
    # Next candle = Day 1
    # -------------------------------------------------

                    days_to_high = (highest_high_index - c_plus_1_index)

    # -------------------------------------------------
    # Date when highest high was reached
    # -------------------------------------------------

                    highest_high_date = df.loc[highest_high_index,"date"]

    # -------------------------------------------------
    # Check whether setup eventually broke
    # -------------------------------------------------

                    if close_below_c0_index is not None:

                        c0_status = "BROKEN"

                        days_until_c0_break = (close_below_c0_index - c_plus_1_index)

                        days_active = days_until_c0_break

                        c0_break_date = df.loc[close_below_c0_index, "date"]

                    else:

                        c0_status = "ACTIVE"

                        days_until_c0_break = None

        # Number of trading days from C+1
        # up to latest available candle
                        days_active = ((len(df) - 1)- c_plus_1_index)

                        c0_break_date = None

            
                else:

    # -------------------------------------------------
    # C+1 does not exist
    # -------------------------------------------------

                    entry_close = None
                    c0_low = None

                    highest_high_before_break = None
                    highest_high_index = None
                    highest_high_date = None

                    max_move_before_break = None
                    days_to_high = None

                    close_below_c0_index = None
                    c0_status = None

                    days_until_c0_break = None
                    days_active = None
                    c0_break_date = None
    
                results.append({

                    "Stock": symbol,

                    "COI Date": df.loc[latest, "date"],

                    "COI Low": df.loc[latest, "low"],

                    "Current Close": df.loc[latest, "close"],

                    "Important": "YES" if important else "NO",

                    "Very Important": "YES" if very_important else "NO",

                    "Upper Band Rising": "YES" if upper_band_rising else "NO", 

                    "MACD 1st Derivative C+1": macd_first_derivative,

                    "MACD 2nd Derivative C+1": macd_second_derivative,

                    "Signal Line 1st Derivative C+1": signal_first_derivative_c_plus_1,

                    "Signal Line 2nd Derivative C+1": signal_second_derivative_c_plus_1,

                    "10 EMA Slope C+1": ema10_slope_c_plus_1,

                    "10 EMA 2nd Derivative C+1": ema10_second_derivative_c_plus_1,

                    "BB Slope Difference C+1": bb_slope_diff_c_plus_1,

                    "20 EMA Slope C+1": ema20_slope_c_plus_1,

                    "20 EMA 2nd Derivative C+1": ema20_second_derivative_c_plus_1,

                    "Upper Band Slope C+1": upper_band_slope_c_plus_1,

                    "Lower Band Slope C+1": lower_band_slope_c_plus_1,

                    "Upper Band 2nd Derivative C+1": upper_band_second_derivative_c_plus_1,

                    "Lower Band 2nd Derivative C+1": lower_band_second_derivative_c_plus_1,

                    "MACD C+1": macd_c_plus_1,

                    "Signal Line C+1": signal_c_plus_1,

                    "MACD Above Signal C+1": "YES" if macd_above_signal_c_plus_1 else "NO",
                   
                    "Entry Close C+1": entry_close,

                    "C0 Low": c0_low,

                    "Highest High Before C0 Break": highest_high_before_break,

                    "Max Move Before C0 Break %": max_move_before_break,

                    "Days To Highest High": days_to_high,
    
                    "Highest High Date": highest_high_date,

                    "C0 Status": c0_status,

                    "Days Until C0 Break": days_until_c0_break,

                    "Days Active": days_active,

                    "C0 Break Date": c0_break_date


            })

    except Exception as e:

        print(f"{symbol} skipped : {e}")

# ---------------------------------
# Scanner Finished
# ---------------------------------

print("Total results:", len(results))

result_df = pd.DataFrame(results)

print("\nColumns:")
print(result_df.columns)

print("\nFirst few rows:")
print(result_df.head())

result_df["COI Date"] = pd.to_datetime(result_df["COI Date"]).dt.tz_localize(None)

result_df.to_excel("COI_Detailed_Result.xlsx", index=False)

print("\nScanner Finished")
print("Total Matches :", len(result_df))
