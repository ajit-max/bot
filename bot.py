import pandas as pd
import datetime as dt
import pandas_ta as ta  # <-- TA-Lib ki jagah ye
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
    return "Bot is Running 24/7!"

def run_web():
    # Render PORT environment variable use karega, ya default 10000
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
PAPER_TRADE = True  # <--- Change to False for Real Trade
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
        # Timeout add kiya taaki hang na ho
        res = requests.get(url, timeout=15)
        if res.status_code == 200:
            data = res.json()
            instrument_list = pd.DataFrame(data)
            instrument_list['expiry'] = pd.to_datetime(instrument_list['expiry'], errors='coerce')
            print("✅ Instrument Master Downloaded")
        else:
            print("❌ Master Download Failed")
    except Exception as e:
        print(f"⚠️ Master Error: {e}")

# ================= CONNECTION =================
def connect():
    try:
        obj = SmartConnect(api_key=API_KEY)
        totp = pyotp.TOTP(TOTP_SECRET).now()
        session = obj.generateSession(CLIENT_ID, PASSWORD, totp)
        if not session["status"]:
            return None
        return obj
    except:
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
    global CAPITAL, instrument_list, initial_capital

    print("🔌 Connecting to Angel One...")
    obj = connect()
    if not obj:
        print("❌ Connection Failed. Retrying in 10s...")
        time.sleep(10)
        return

    get_instrument_master()
    
    trade_active = False
    trade = {}
    daily_pnl = 0
    current_day = dt.datetime.now().date()

    send_telegram("🔥 Bot Started on Render!")
    print("🔥 ENGINE RUNNING")

    while True:
        now = dt.datetime.now()

        # Market Time Check (9:15 to 3:30)
        if not (dt.time(9,15) <= now.time() <= dt.time(15,30)):
            print(f"💤 Market Closed [{now.strftime('%H:%M:%S')}]")
            time.sleep(60) # 1 min wait in closed market
            continue

        try:
            # Candle sync (5 min close only)
            if now.minute % 5 != 0:
                time.sleep(1)
                continue
            
            # Reset daily
            if current_day != now.date():
                daily_pnl = 0
                current_day = now.date()
                get_instrument_master()

            # Daily loss lock
            if daily_pnl <= -(initial_capital * DAILY_MAX_LOSS_PCT):
                print("❌ Daily Loss Limit Hit")
                time.sleep(300)
                continue

            # ===== SPOT DATA =====
            res = obj.getCandleData({
                "exchange": "NSE",
                "symboltoken": INDEX_TOKEN,
                "interval": "FIVE_MINUTE",
                "fromdate": (now - dt.timedelta(days=5)).strftime("%Y-%m-%d 09:15"),
                "todate": now.strftime("%Y-%m-%d %H:%M")
            })

            if not res["status"] or not res["data"]:
                time.sleep(10)
                continue

            df = pd.DataFrame(res["data"], columns=['date','o','h','l','c','v'])
            df[['h','l','c']] = df[['h','l','c']].astype(float)

            if len(df) < 201:
                print("⚠️ Waiting for more candles...")
                time.sleep(10)
                continue

            # --- PANDAS_TA INDICATORS ---
            # TA-Lib ki jagah pandas_ta use kar rahe hain
            df['ema200'] = df.ta.ema(close='c', length=200)
            df['rsi'] = df.ta.rsi(close='c', length=14)

            spot = df['c'].iloc[-2] # Previous closed candle
            ema200 = df['ema200'].iloc[-2]
            rsi = df['rsi'].iloc[-2]

            print(f"🔍 Spot: {spot} | EMA: {round(ema200,2)} | RSI: {round(rsi,2)}")

            # ===== ENTRY =====
            if not trade_active:
                direction = None
                if spot > ema200 and rsi > 60:
                    direction = "CE"
                elif spot < ema200 and rsi < 40:
                    direction = "PE"

                if direction:
                    token, symbol = get_atm_option(spot, direction)
                    if not token: continue

                    ltp_res = obj.ltpData("NFO", symbol, token)
                    if not ltp_res["status"]: continue
                    opt_ltp = float(ltp_res["data"]["ltp"])

                    # ATR Calculation for Option
                    opt_candle = obj.getCandleData({
                        "exchange": "NFO", "symboltoken": token, "interval": "FIVE_MINUTE",
                        "fromdate": (now - dt.timedelta(days=3)).strftime("%Y-%m-%d 09:15"),
                        "todate": now.strftime("%Y-%m-%d %H:%M")
                    })
                    
                    if opt_candle['status'] and opt_candle['data']:
                        opt_df = pd.DataFrame(opt_candle["data"], columns=['date','o','h','l','c','v'])
                        opt_df[['h','l','c']] = opt_df[['h','l','c']].astype(float)
                        # Pandas_TA ATR
                        opt_df['atr'] = opt_df.ta.atr(high='h', low='l', close='c', length=14)
                        opt_atr = opt_df['atr'].iloc[-2]
                    else:
                        opt_atr = 10 # Default fallback

                    sl_points = opt_atr * 1.5
                    sl_price = opt_ltp - sl_points
                    risk_amt = initial_capital * RISK_PER_TRADE
                    risk_per_lot = sl_points * LOT_SIZE
                    lots = int(risk_amt // risk_per_lot)
                    
                    if lots < 1: lots = 1 # Minimum 1 lot
                    qty = lots * LOT_SIZE

                    if place_order(obj, symbol, token, qty, "BUY"):
                        trade = {
                            "symbol": symbol, "token": token, "entry": opt_ltp,
                            "sl": sl_price, "tgt1": opt_ltp + (sl_points * 1.5),
                            "tgt2": opt_ltp + (sl_points * 3), "qty": qty,
                            "remaining_qty": qty, "partial_done": False, "pnl_booked": 0
                        }
                        trade_active = True
                        send_telegram(f"🟢 BUY {symbol}\nEntry: {opt_ltp}\nSL: {sl_price}\nQty: {qty}")

            # ===== EXIT =====
            else:
                ltp_res = obj.ltpData("NFO", trade["symbol"], trade["token"])
                if ltp_res["status"]:
                    curr_ltp = float(ltp_res["data"]["ltp"])
                    
                    # Target 1 Logic
                    if not trade["partial_done"] and curr_ltp >= trade["tgt1"]:
                        exit_qty = trade["qty"] // 2
                        if place_order(obj, trade["symbol"], trade["token"], exit_qty, "SELL"):
                            trade["pnl_booked"] += (curr_ltp - trade["entry"]) * exit_qty
                            trade["remaining_qty"] -= exit_qty
                            trade["partial_done"] = True
                            trade["sl"] = trade["entry"] # SL to Cost
                            send_telegram("💰 Partial Booked | SL to Cost")

                    # Final Exit Logic
                    reason = None
                    if curr_ltp <= trade["sl"]: reason = "STOPLOSS"
                    elif curr_ltp >= trade["tgt2"]: reason = "TARGET HIT"
                    elif now.time() >= dt.time(15,15): reason = "EOD EXIT"

                    if reason:
                        if place_order(obj, trade["symbol"], trade["token"], trade["remaining_qty"], "SELL"):
                            pnl = (curr_ltp - trade["entry"]) * trade["remaining_qty"]
                            total_pnl = trade["pnl_booked"] + pnl
                            daily_pnl += total_pnl
                            send_telegram(f"🔚 {reason}\nExit: {curr_ltp}\nTrade PnL: {total_pnl}")
                            trade_active = False

        except Exception as e:
            print(f"⚠️ Loop Error: {e}")
            time.sleep(5)
        
        # Har 60 sec wait karo loop mein (Candle close logic handle ho raha hai upar)
        time.sleep(60)

if __name__ == "__main__":
    # Flask ko alag thread mein start karo taaki bot ruke nahi
    t = threading.Thread(target=run_web)
    t.start()
    
    # Main Bot Engine
    run_engine()
