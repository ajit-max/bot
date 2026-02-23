import pandas as pd
import datetime as dt
import talib
import time
import requests
import pyotp
import warnings
import threading
import os
from flask import Flask
from SmartApi import SmartConnect

warnings.filterwarnings("ignore")

# ================= DUMMY SERVER FOR RENDER =================
app = Flask('')

@app.route('/')
def home():
    return "Bot is Running 24/7 with TA-Lib!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
# ===========================================================

# ================= CONFIG =================
API_KEY = "yRe368gf"
CLIENT_ID = "AABZ146183"
PASSWORD = "6211"
TOTP_SECRET = "ZHFAFO7SKLYN3FNJOBPZYNEGQI"

TELEGRAM_TOKEN = "8291109950:AAE-vcehleqwpl0Bc-2o1dlaUOEQNWw9r-4"
CHAT_ID = "1901759813"

INDEX_TOKEN = "99926000"   # NIFTY Spot
LOT_SIZE = 25
CAPITAL = 50000
RISK_PER_TRADE = 0.02
DAILY_MAX_LOSS_PCT = 0.05
MAX_DRAWDOWN_PCT = 0.10
PAPER_TRADE = True
# ==========================================

instrument_list = None
initial_capital = CAPITAL

# ================= TELEGRAM =================
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=5)
    except:
        pass

# ================= MASTER =================
def get_instrument_master():
    global instrument_list
    url = "https://margincalculator.angelbroking.com/OpenAPI_Standard/v1/instrumentsJSON.json"
    try:
        res = requests.get(url, timeout=20)
        if res.status_code == 200:
            instrument_list = pd.DataFrame(res.json())
            instrument_list['expiry'] = pd.to_datetime(instrument_list['expiry'], errors='coerce')
            print("✅ Instrument Master Downloaded")
    except Exception as e:
        print(f"⚠️ Master Download Error: {e}")

# ================= CONNECTION =================
def connect():
    try:
        obj = SmartConnect(api_key=API_KEY)
        totp = pyotp.TOTP(TOTP_SECRET).now()
        session = obj.generateSession(CLIENT_ID, PASSWORD, totp)
        if not session["status"]:
            raise Exception("Login Failed")
        return obj
    except Exception as e:
        print(f"❌ Connection Error: {e}")
        return None

# ================= OPTION FETCH =================
def get_atm_option(spot_price, opt_type):
    global instrument_list
    if instrument_list is None: return None, None

    strike = round(spot_price / 50) * 50

    df = instrument_list[
        (instrument_list['name'] == 'NIFTY') &
        (instrument_list['instrumenttype'] == 'OPTIDX') &
        (instrument_list['symbol'].str.endswith(opt_type)) &
        (instrument_list['strike'].astype(float) == float(strike * 100))
    ].copy()

    df = df[df['expiry'] >= dt.datetime.now()].sort_values(by='expiry')

    if df.empty:
        return None, None

    return df.iloc[0]['token'], df.iloc[0]['symbol']

# ================= ORDER =================
def place_order(obj, symbol, token, qty, side):
    if PAPER_TRADE:
        print(f"📝 PAPER {side}: {symbol} Qty:{qty}")
        return True

    try:
        params = {
            "variety": "NORMAL",
            "tradingsymbol": symbol,
            "symboltoken": token,
            "transactiontype": side,
            "exchange": "NFO",
            "ordertype": "MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "quantity": str(int(qty))
        }
        obj.placeOrder(params)
        return True
    except Exception as e:
        print("Order Error:", e)
        return False

# ================= ENGINE =================
def run_engine():
    global CAPITAL, initial_capital

    obj = connect()
    if not obj: return
    
    get_instrument_master()

    trade_active = False
    trade = {}
    daily_pnl = 0
    current_day = dt.datetime.now().date()

    send_telegram("🔥 Bot Started on Render with TA-Lib!")
    print("🔥 10/10 ENGINE RUNNING")

    while True:
        now = dt.datetime.now()

        # Candle sync (5 min close only)
        if now.minute % 5 != 0 or now.second > 8:
            time.sleep(2)
            continue

        # Reset daily
        if current_day != now.date():
            daily_pnl = 0
            current_day = now.date()
            get_instrument_master()

        # Daily loss lock
        if daily_pnl <= -(CAPITAL * DAILY_MAX_LOSS_PCT):
            print("❌ Daily Loss Limit Hit")
            time.sleep(300)
            continue

        # Hard capital drawdown stop
        if CAPITAL + daily_pnl <= initial_capital * (1 - MAX_DRAWDOWN_PCT):
            print("🚨 Max Drawdown Hit. Engine Stopped.")
            break

        if not (dt.time(9,20) <= now.time() <= dt.time(15,10)):
            time.sleep(10)
            continue

        try:
            # ===== SPOT DATA =====
            res = obj.getCandleData({
                "exchange": "NSE",
                "symboltoken": INDEX_TOKEN,
                "interval": "FIVE_MINUTE",
                "fromdate": (now - dt.timedelta(days=5)).strftime("%Y-%m-%d 09:15"),
                "todate": now.strftime("%Y-%m-%d %H:%M")
            })

            if not res["status"] or not res["data"]:
                continue

            df = pd.DataFrame(res["data"], columns=['date','o','h','l','c','v'])
            df[['h','l','c']] = df[['h','l','c']].astype(float)

            if len(df) < 210:
                continue

            spot = df['c'].iloc[-2]
            ema200 = talib.EMA(df['c'], 200).iloc[-2]
            rsi = talib.RSI(df['c'], 14).iloc[-2]

            # ===== ENTRY =====
            if not trade_active:
                direction = None
                if spot > ema200 and rsi > 60:
                    direction = "CE"
                elif spot < ema200 and rsi < 40:
                    direction = "PE"

                if direction:
                    token, symbol = get_atm_option(spot, direction)
                    if not token:
                        continue

                    ltp_res = obj.ltpData("NFO", symbol, token)
                    if not ltp_res["status"]:
                        continue

                    opt_ltp = float(ltp_res["data"]["ltp"])

                    # ===== OPTION ATR FOR SL =====
                    opt_candle = obj.getCandleData({
                        "exchange": "NFO",
                        "symboltoken": token,
                        "interval": "FIVE_MINUTE",
                        "fromdate": (now - dt.timedelta(days=3)).strftime("%Y-%m-%d 09:15"),
                        "todate": now.strftime("%Y-%m-%d %H:%M")
                    })

                    opt_df = pd.DataFrame(opt_candle["data"], columns=['date','o','h','l','c','v'])
                    opt_df[['h','l','c']] = opt_df[['h','l','c']].astype(float)

                    opt_atr = talib.ATR(opt_df['h'], opt_df['l'], opt_df['c'], 14).iloc[-2]

                    sl_points = opt_atr * 1.5
                    sl_price = opt_ltp - sl_points

                    risk_amt = CAPITAL * RISK_PER_TRADE
                    risk_per_lot = sl_points * LOT_SIZE

                    lots = int(risk_amt // risk_per_lot)

                    if lots < 1:
                        continue

                    qty = lots * LOT_SIZE

                    if place_order(obj, symbol, token, qty, "BUY"):
                        trade = {
                            "symbol": symbol, "token": token, "entry": opt_ltp,
                            "sl": sl_price, "tgt1": opt_ltp + (sl_points * 1.5),
                            "tgt2": opt_ltp + (sl_points * 3), "qty": qty,
                            "remaining_qty": qty, "partial_done": False, "pnl_booked": 0
                        }
                        trade_active = True
                        send_telegram(f"🟢 BUY {symbol}\nEntry: {round(opt_ltp,2)}\nSL: {round(sl_price,2)}\nQty: {qty}")

            # ===== EXIT =====
            else:
                ltp_res = obj.ltpData("NFO", trade["symbol"], trade["token"])
                if not ltp_res["status"]:
                    continue

                curr_ltp = float(ltp_res["data"]["ltp"])

                # Partial
                if not trade["partial_done"] and curr_ltp >= trade["tgt1"]:
                    exit_qty = trade["qty"] // 2
                    if place_order(obj, trade["symbol"], trade["token"], exit_qty, "SELL"):
                        trade["pnl_booked"] += (curr_ltp - trade["entry"]) * exit_qty
                        trade["remaining_qty"] -= exit_qty
                        trade["partial_done"] = True
                        trade["sl"] = trade["entry"]
                        send_telegram("💰 Partial Booked | SL to Cost")

                reason = None
                if curr_ltp <= trade["sl"]:
                    reason = "STOPLOSS"
                elif curr_ltp >= trade["tgt2"]:
                    reason = "TARGET HIT"
                elif now.time() >= dt.time(15,15):
                    reason = "EOD EXIT"

                if reason:
                    exit_qty = trade["remaining_qty"]
                    if place_order(obj, trade["symbol"], trade["token"], exit_qty, "SELL"):
                        pnl_final = (curr_ltp - trade["entry"]) * exit_qty
                        total_trade_pnl = trade["pnl_booked"] + pnl_final
                        daily_pnl += total_trade_pnl

                        # ===== JOURNAL =====
                        log = {
                            "Date": now, "Symbol": trade["symbol"], "Entry": trade["entry"],
                            "Exit": curr_ltp, "PnL": total_trade_pnl, "DayPnL": daily_pnl
                        }
                        pd.DataFrame([log]).to_csv("trade_log.csv", mode="a", header=not pd.io.common.file_exists("trade_log.csv"), index=False)

                        send_telegram(f"🔚 {reason}\nExit: {round(curr_ltp,2)}\nTrade PnL: ₹{round(total_trade_pnl,2)}\nDay PnL: ₹{round(daily_pnl,2)}")
                        trade_active = False

        except Exception as e:
            print("Loop Error:", e)

        time.sleep(2)

if __name__ == "__main__":
    # Start web server in background
    t = threading.Thread(target=run_web)
    t.daemon = True
    t.start()
    
    # Start trading engine
    run_engine()
