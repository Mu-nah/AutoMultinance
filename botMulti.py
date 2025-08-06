import os, time, json, hmac, hashlib, threading, requests
import pandas as pd
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from flask import Flask
from collections import deque
import ta
import gspread
from oauth2client.service_account import ServiceAccountCredentials

load_dotenv()

# === CONFIG ===
SYMBOL = "BTCUSDT"
TRADE_QTY = 0.01
ENTRY_BUFFER = 0.8
PIP = 1.0
TP_OFFSET = 100 * PIP
DAILY_TARGET = 1000
DAILY_LOSS_LIMIT = -700

BYBIT_TESTNET_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_TESTNET_API_SECRET = os.getenv("BYBIT_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GSHEET_ID = os.getenv("GSHEET_ID")

BASE_URL_PUBLIC = "https://api.bybit.com"
BASE_URL_TESTNET = "https://api-testnet.bybit.com"
CATEGORY = "linear"

# === STATE ===
in_position = False
entry_price = None
sl_price = None
tp_price = None
trade_direction = None
pending_order = None
pending_order_time = None
daily_trades = deque()
target_hit = False
last_tp_time = None

# === UTILS ===
def now_utc(): return datetime.now(timezone.utc)

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})
    except: pass

def get_gsheet_client():
    creds = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
    scope = ['https://spreadsheets.google.com/feeds','https://www.googleapis.com/auth/drive']
    return gspread.authorize(ServiceAccountCredentials.from_json_keyfile_dict(creds, scope))

def log_trade(data):
    try:
        sheet = get_gsheet_client().open_by_key(GSHEET_ID).sheet1
        sheet.append_row(data)
    except: pass

# === BYBIT REQUESTS ===
def sign_request(timestamp, recv_window, body=""):
    message = BYBIT_TESTNET_API_KEY + str(timestamp) + str(recv_window) + body
    return hmac.new(BYBIT_TESTNET_API_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()

def place_stop_order(order_type, entry):
    global pending_order, sl_price, tp_price, trade_direction, pending_order_time

    side = "Buy" if "buy" in order_type else "Sell"
    direction = "long" if side == "Buy" else "short"
    tp_ref = get_bollinger_band_reference(order_type)
    if tp_ref is None: return

    stop_price = round(entry + ENTRY_BUFFER, 2) if side == "Buy" else round(entry - ENTRY_BUFFER, 2)
    tp = round(tp_ref + TP_OFFSET, 2) if side == "Buy" else round(tp_ref - TP_OFFSET, 2)
    sl = round(get_klines('60').iloc[-1]["open"], 2)

    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    body = {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "side": side,
        "orderType": "TriggerMarket",
        "qty": str(TRADE_QTY),
        "triggerPrice": str(stop_price),
        "triggerDirection": 1 if side == "Buy" else 2,
        "timeInForce": "GTC",
        "positionIdx": 1
    }
    body_json = json.dumps(body, separators=(',', ':'))
    sign = sign_request(timestamp, recv_window, body_json)
    headers = {
        "X-BAPI-API-KEY": BYBIT_TESTNET_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN": sign,
        "Content-Type": "application/json"
    }
    r = requests.post(f"{BASE_URL_TESTNET}/v5/order/create", headers=headers, data=body_json).json()

    if r.get("retCode") == 0:
        pending_order = {"id": r["result"]["orderId"], "side": side, "entry": entry}
        pending_order_time = now_utc()
        sl_price, tp_price, trade_direction = sl, tp, direction
        send_telegram(f"🟩 *{order_type.upper()}* placed\n📍 Entry: `{entry}`\n🎯 TP: `{tp}`\n🛡 SL: `{sl}`")
        log_trade([str(now_utc()), SYMBOL, order_type, entry, sl, tp, "Pending"])
    else:
        send_telegram(f"❌ Failed to place order: {r.get('retMsg')}")

# === KLINE CLEANING ===
def get_klines(interval='5'):
    try:
        params = {
            "category": CATEGORY,
            "symbol": SYMBOL,
            "interval": interval,
            "limit": 100
        }
        r = requests.get(f"{BASE_URL_PUBLIC}/v5/market/kline", params=params).json()
        raw = r.get("result", {}).get("list", [])
        if not raw: return pd.DataFrame()
        df = pd.DataFrame(raw, columns=["start", "open", "high", "low", "close", "volume", "turnover"])
        df = df.astype({col: float for col in ["open", "high", "low", "close"]})
        df["start"] = pd.to_numeric(df["start"])
        df["time"] = pd.to_datetime(df["start"], unit='s', utc=True)
        return df
    except:
        return pd.DataFrame()

def add_indicators(df):
    if df.empty: return df
    df = df.copy()
    df["rsi"] = ta.momentum.rsi(df["close"], 14)
    bb = ta.volatility.BollingerBands(df["close"], 20, 2)
    df["bb_mid"] = bb.bollinger_mavg()
    df["bb_high"] = bb.bollinger_hband()
    df["bb_low"] = bb.bollinger_lband()
    return df

def get_bollinger_band_reference(order_type):
    df = add_indicators(get_klines('5'))
    if df.empty: return None
    c = df.iloc[-1]
    return c["bb_mid"] if "reversal" in order_type else (c["bb_high"] if "buy" in order_type else c["bb_low"])

# === SIGNALS ===
def check_signal():
    global last_tp_time, target_hit

    if target_hit or (last_tp_time and (now_utc() - last_tp_time).seconds < 1800):
        return None

    df_5m = add_indicators(get_klines('5'))
    df_1h = add_indicators(get_klines('60'))
    if df_5m.empty or df_1h.empty:
        return None

    c5, c1 = df_5m.iloc[-1], df_1h.iloc[-1]

    if 47 <= c5["rsi"] <= 53 or 47 <= c1["rsi"] <= 53:
        return None

    def is_touching_bb(c):
        return (
            abs(c["close"] - c["bb_low"]) < 1.0 or
            abs(c["close"] - c["bb_high"]) < 1.0 or
            abs(c["close"] - c["bb_mid"]) < 1.0
        )

    def pip_distance(c, side):
        return abs(c["bb_high"] - c["close"]) if side == "buy" else abs(c["close"] - c["bb_low"])

    # Trend
    if c5["close"] > c5["open"] and c5["close"] > c5["bb_mid"]:
        if c1["close"] > c1["open"] and not is_touching_bb(c1):
            if pip_distance(c5, "buy") >= 1.0 and not is_touching_bb(c5):
                return "trend_buy", c5["close"]

    if c5["close"] < c5["open"] and c5["close"] < c5["bb_mid"]:
        if c1["close"] < c1["open"] and not is_touching_bb(c1):
            if pip_distance(c5, "sell") >= 1.0 and not is_touching_bb(c5):
                return "trend_sell", c5["close"]

    # Reversal
    if c5["close"] < c5["bb_mid"] and c5["close"] > c5["open"]:
        if c1["close"] > c1["open"] and not is_touching_bb(c1):
            if abs(c5["close"] - c5["bb_mid"]) >= 1.0:
                return "reversal_buy", c5["close"]

    if c5["close"] > c5["bb_mid"] and c5["close"] < c5["open"]:
        if c1["close"] < c1["open"] and not is_touching_bb(c1):
            if abs(c5["close"] - c5["bb_mid"]) >= 1.0:
                return "reversal_sell", c5["close"]

    return None

# === MAIN LOOP ===
def bot_loop():
    global in_position, last_tp_time, daily_trades, target_hit, pending_order, entry_price

    while True:
        try:
            if not in_position:
                signal = check_signal()
                if signal:
                    order_type, signal_price = signal
                    place_stop_order(order_type, signal_price)

            if pending_order and pending_order_time:
                if not in_position and (now_utc() - pending_order_time).seconds > 60:
                    in_position = True
                    entry_price = pending_order['entry']
                    send_telegram(f"✅ Trade triggered at `{entry_price}`")
                    pending_order = None
        except Exception as e:
            send_telegram(f"⚠ Error: {e}")
        time.sleep(60)

# === FLASK + DAILY REPORT ===
app = Flask(__name__)

@app.route('/')
def home():
    return "🚀 Bot is running!"

def daily_report():
    global target_hit
    while True:
        now = now_utc()
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        time.sleep((next_midnight - now).total_seconds())
        total_pnl = sum(p for p,_ in daily_trades)
        win_rate = (sum(1 for _,w in daily_trades if w) / len(daily_trades))*100 if daily_trades else 0
        max_win = max((p for p,_ in daily_trades), default=0)
        max_loss = min((p for p,_ in daily_trades), default=0)
        msg = f"""📊 *Yesterday's Summary*
Total Trades: {len(daily_trades)}
Win Rate: {win_rate:.1f}%
Total PnL: {total_pnl}
Biggest Win: {max_win}
Biggest Loss: {max_loss}
{'🎯 Target hit ✅' if target_hit else '🎯 Target not reached ❌'}"""
        send_telegram(msg)
        daily_trades.clear()
        target_hit = False

if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=daily_report, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
