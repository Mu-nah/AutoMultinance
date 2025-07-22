import os
import time
import json
from datetime import datetime, timedelta
import pandas as pd
import threading
from dotenv import load_dotenv
import ta
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from binance.client import Client
from binance.enums import *
from flask import Flask
from collections import deque

load_dotenv()

# ✅ Config
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
TRADE_QUANTITY = 0.001
SPREAD_THRESHOLD = 15  # USD
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GSHEET_ID = os.getenv("GSHEET_ID")
RSI_LO, RSI_HI = 47, 53

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=True)

# Initialize leverage per symbol
for sym in SYMBOLS:
    client.futures_change_leverage(symbol=sym, leverage=10)

# ✅ State: per symbol
state = {
    symbol: {
        "in_position": False,
        "entry_price": None,
        "sl_price": None,
        "tp_price": None,
        "trailing_peak": None,
        "current_trail_percent": 0.0,
        "trade_direction": None  # 'long' or 'short'
    } for symbol in SYMBOLS
}

daily_trades = deque()  # store (pnl, is_win)

# 📩 Telegram
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg})
    except:
        pass

# 📊 Google Sheets
def get_gsheet_client():
    creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)

def log_trade(symbol, data):
    try:
        gc = get_gsheet_client()
        sheet = gc.open_by_key(GSHEET_ID).worksheet(symbol)
        sheet.append_row(data)
    except:
        pass

# 📊 Get data
def get_klines(symbol, interval='5m', limit=100):
    klines = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=[
        'open_time', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_asset_volume', 'number_of_trades',
        'taker_buy_base', 'taker_buy_quote', 'ignore'
    ])
    df['time'] = pd.to_datetime(df['open_time'], unit='ms')
    for col in ['open','high','low','close','volume']:
        df[col] = df[col].astype(float)
    return df

# 📈 Indicators
def add_indicators(df):
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    bb = ta.volatility.BollingerBands(df['close'], window=20, window_dev=2)
    df['bb_mid']  = bb.bollinger_mavg()
    df['bb_high'] = bb.bollinger_hband()
    df['bb_low']  = bb.bollinger_lband()
    return df

# 📊 Signal logic
def check_signal(symbol):
    df_5m = add_indicators(get_klines(symbol, '5m'))
    df_1h = add_indicators(get_klines(symbol, '1h'))
    c5 = df_5m.iloc[-1]
    c1h = df_1h.iloc[-1]

    # Skip last 10 minutes of 1h candle
    if datetime.utcnow().minute >= 50:
        return None

    # RSI neutral filter
    if RSI_LO <= c5['rsi'] <= RSI_HI or RSI_LO <= c1h['rsi'] <= RSI_HI:
        return None
    # Avoid if 1h touches BB
    if c1h['close'] >= c1h['bb_high'] or c1h['close'] <= c1h['bb_low']:
        return None

    # Trend & reversal signals
    if c5['close'] > c5['bb_mid'] and c5['close'] < c5['bb_high'] and c5['close'] > c5['open'] and c1h['close'] > c1h['open']:
        return 'trend_buy'
    if c5['close'] < c5['bb_mid'] and c5['close'] > c5['bb_low'] and c5['close'] < c5['open'] and c1h['close'] < c1h['open']:
        return 'trend_sell'
    if c5['close'] < c5['bb_mid'] and c5['close'] > c5['bb_low'] and c5['close'] > c5['open'] and c1h['close'] > c1h['open']:
        return 'reversal_buy'
    if c5['close'] > c5['bb_mid'] and c5['close'] < c5['bb_high'] and c5['close'] < c5['open'] and c1h['close'] < c1h['open']:
        return 'reversal_sell'

    return None

# 🛠 Place order
def place_order(symbol, order_type):
    s = state[symbol]

    # Spread check
    book = client.futures_order_book(symbol=symbol)
    spread = float(book['asks'][0][0]) - float(book['bids'][0][0])
    if spread > SPREAD_THRESHOLD:
        send_telegram(f"⚠ {symbol} Spread too wide (${spread:.2f}), skipping trade.")
        return

    side = SIDE_BUY if 'buy' in order_type else SIDE_SELL
    s['trade_direction'] = 'long' if 'buy' in order_type else 'short'

    order = client.futures_create_order(symbol=symbol, side=side, type=ORDER_TYPE_MARKET, quantity=TRADE_QUANTITY)
    price = float(order['fills'][0]['price']) if 'fills' in order else float(order.get('avgFillPrice') or client.futures_symbol_ticker(symbol=symbol)['price'])

    df_5m = add_indicators(get_klines(symbol, '5m'))
    df_1h = add_indicators(get_klines(symbol, '1h'))
    c5, c1h = df_5m.iloc[-1], df_1h.iloc[-1]

    s['sl_price'] = c1h['open'] if 'trend' in order_type else c5['open']
    s['tp_price'] = c5['bb_high'] if 'trend_buy' in order_type else c5['bb_low'] if 'trend_sell' in order_type else c5['bb_mid']
    s['entry_price'] = price
    s['trailing_peak'] = price
    s['current_trail_percent'] = 0.0
    s['in_position'] = True

    send_telegram(f"✅ {symbol}: {order_type.upper()} at {price}\nSL: {s['sl_price']}\nTP: {s['tp_price']}")
    log_trade(symbol, [str(datetime.utcnow()), order_type, price, s['sl_price'], s['tp_price'], f"Opened ({s['trade_direction']})"])

# 🔄 Manage trade
def manage_trade(symbol):
    s = state[symbol]
    price = float(client.futures_symbol_ticker(symbol=symbol)['price'])
    profit_pct = abs((price - s['entry_price']) / s['entry_price']) if s['entry_price'] else 0

    if profit_pct >= 0.03:
        s['current_trail_percent'] = 0.015
    elif profit_pct >= 0.02:
        s['current_trail_percent'] = 0.01
    elif profit_pct >= 0.01:
        s['current_trail_percent'] = 0.005

    if s['current_trail_percent'] > 0:
        s['trailing_peak'] = max(s['trailing_peak'], price) if s['trade_direction'] == 'long' else min(s['trailing_peak'], price)
        if s['trade_direction'] == 'long' and price < s['trailing_peak'] * (1 - s['current_trail_percent']):
            return close_position(symbol, price, f"Trailing Stop Hit")
        elif s['trade_direction'] == 'short' and price > s['trailing_peak'] * (1 + s['current_trail_percent']):
            return close_position(symbol, price, f"Trailing Stop Hit")

    if s['trade_direction'] == 'long':
        if price <= s['sl_price']:
            return close_position(symbol, price, "Stop Loss Hit")
        elif price >= s['tp_price']:
            return close_position(symbol, price, "Take Profit Hit")
    else:
        if price >= s['sl_price']:
            return close_position(symbol, price, "Stop Loss Hit")
        elif price <= s['tp_price']:
            return close_position(symbol, price, "Take Profit Hit")

# ❌ Close trade
def close_position(symbol, exit_price, reason):
    s = state[symbol]
    side = SIDE_SELL if s['trade_direction'] == 'long' else SIDE_BUY
    client.futures_create_order(symbol=symbol, side=side, type=ORDER_TYPE_MARKET, quantity=TRADE_QUANTITY)
    pnl = round((exit_price - s['entry_price']) if s['trade_direction'] == 'long' else (s['entry_price'] - exit_price), 2)
    daily_trades.append((pnl, pnl>0))
    send_telegram(f"❌ {symbol}: Closed at {exit_price} ({reason}) | PnL: {pnl}")
    log_trade(symbol, [str(datetime.utcnow()), f"close ({s['trade_direction']})", s['entry_price'], s['sl_price'], s['tp_price'], f"{reason}, PnL: {pnl}"])
    s['in_position'] = False

# 📊 Daily summary
def send_daily_summary():
    if not daily_trades:
        send_telegram("📊 Daily Summary: No trades today.")
        return
    total_pnl = sum(p for p,_ in daily_trades)
    win_rate = (sum(1 for _,w in daily_trades if w) / len(daily_trades))*100
    biggest_win = max((p for p,_ in daily_trades if p>0), default=0)
    biggest_loss = min((p for p,_ in daily_trades if p<0), default=0)
    msg = f"📊 Daily Summary\nTrades: {len(daily_trades)}\nWin rate: {win_rate:.1f}%\nTotal PnL: {total_pnl:.2f}\nBiggest win: {biggest_win}\nBiggest loss: {biggest_loss}"
    send_telegram(msg)
    daily_trades.clear()

# 🚀 Bot loop
def bot_loop():
    while True:
        try:
            for symbol in SYMBOLS:
                if not state[symbol]['in_position']:
                    signal = check_signal(symbol)
                    if signal:
                        place_order(symbol, signal)
                else:
                    manage_trade(symbol)
        except:
            pass
        time.sleep(180)

# 🕒 Daily summary scheduler
def daily_scheduler():
    while True:
        now = datetime.utcnow()
        next_midnight = (now+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
        time.sleep((next_midnight-now).total_seconds())
        send_daily_summary()

# 🌐 Flask
app = Flask(__name__)
@app.route('/')
def home(): return "🚀 Bot running!"

if __name__=="__main__":
    threading.Thread(target=bot_loop,daemon=True).start()
    threading.Thread(target=daily_scheduler,daemon=True).start()
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)))
