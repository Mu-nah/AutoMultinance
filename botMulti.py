import os, time, json, hmac, hashlib, requests, threading
from datetime import datetime, timedelta, timezone
import pandas as pd
from dotenv import load_dotenv
import ta
from flask import Flask
from collections import deque

load_dotenv()

# ✅ Config
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
BASE_URL = "https://api-testnet.bybit.com"
SYMBOL, TRADE_QUANTITY, SPREAD_THRESHOLD, DAILY_TARGET = "BTCUSDT", 0.001, 17, 1000
RSI_LO, RSI_HI, ENTRY_BUFFER = 47, 53, 0.8
TELEGRAM_TOKEN, CHAT_ID = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")

# ✅ State
in_position, pending_order_time = False, None
entry_price, sl_price, tp_price, trailing_peak, trailing_stop_price, current_trail_percent = None, None, None, None, None, 0.0
trade_direction, daily_trades, target_hit = None, deque(), False

# 📩 Telegram
def send_telegram(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})
    except: pass

# 📊 Get klines
def get_klines(interval='5'):
    ts = str(int(time.time() * 1000))
    params = {
        "category": "linear", "symbol": SYMBOL, "interval": interval, "limit": 100, "start": int(time.time()) - 100*60*int(interval)
    }
    query = '&'.join(f"{k}={v}" for k, v in sorted(params.items()))
    sign = hmac.new(BYBIT_API_SECRET.encode(), (ts + BYBIT_API_KEY + "5000" + query).encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": "5000",
        "X-BAPI-SIGN": sign
    }
    r = requests.get(f"{BASE_URL}/v5/market/kline?" + query, headers=headers).json()
    df = pd.DataFrame(r['result']['list'])
    df.columns = ['start','open','high','low','close','volume','turnover']
    df['time'] = pd.to_datetime(pd.to_numeric(df['start']), unit='s')  # ✅ fix warning
    for c in ['open','high','low','close','volume']: df[c]=df[c].astype(float)
    return df

def add_indicators(df):
    df['rsi']=ta.momentum.rsi(df['close'],14)
    bb=ta.volatility.BollingerBands(df['close'],20,2)
    df['bb_mid'],df['bb_high'],df['bb_low']=bb.bollinger_mavg(),bb.bollinger_hband(),bb.bollinger_lband()
    return df

# 📊 Signal logic
def check_signal():
    if target_hit: return None
    df_5m, df_1h = add_indicators(get_klines('5')), add_indicators(get_klines('60'))
    c5, c1h = df_5m.iloc[-1], df_1h.iloc[-1]
    now=datetime.now(timezone.utc)+timedelta(hours=1)
    if now.minute>=50: return None
    if RSI_LO<=c5['rsi']<=RSI_HI or RSI_LO<=c1h['rsi']<=RSI_HI: return None
    if c1h['close']>=c1h['bb_high'] or c1h['close']<=c1h['bb_low']: return None
    if c5['close']>c5['bb_mid'] and c5['close']>c5['open'] and c1h['close']>c1h['open']: return 'trend_buy'
    if c5['close']<c5['bb_mid'] and c5['close']<c5['open'] and c1h['close']<c1h['open']: return 'trend_sell'
    if c5['close']<c5['bb_mid'] and c5['close']>c5['open'] and c1h['close']>c1h['open']: return 'reversal_buy'
    if c5['close']>c5['bb_mid'] and c5['close']<c5['open'] and c1h['close']<c1h['open']: return 'reversal_sell'
    return None

# 🛠 Place order
def place_order(order_type):
    global sl_price,tp_price,trade_direction,pending_order_time,in_position,entry_price
    side="Buy" if "buy" in order_type else "Sell"
    df=add_indicators(get_klines('5'))
    c5=df.iloc[-1]
    sl_price,tp_price=(c5['open'],round(c5['bb_high']+100,2)) if side=="Buy" else (c5['open'],round(c5['bb_low']-100,2))
    trade_direction='long' if side=="Buy" else 'short'
    ts=str(int(time.time()*1000))
    body={"category":"linear","symbol":SYMBOL,"side":side,"orderType":"Market","qty":"0.001","timeInForce":"GTC"}
    body_json=json.dumps(body,separators=(',',':'))
    sign=hmac.new(BYBIT_API_SECRET.encode(),(ts+BYBIT_API_KEY+"5000"+body_json).encode(),hashlib.sha256).hexdigest()
    headers={"X-BAPI-API-KEY":BYBIT_API_KEY,"X-BAPI-TIMESTAMP":ts,"X-BAPI-RECV-WINDOW":"5000","X-BAPI-SIGN":sign,"Content-Type":"application/json"}
    r=requests.post(f"{BASE_URL}/v5/order/create",headers=headers,data=body_json).json()
    send_telegram(f"🟩 *ORDER PLACED*\n*Type:* `{order_type}`\nSL:`{sl_price}` TP:`{tp_price}`")
    pending_order_time=datetime.utcnow()
    in_position=True
    entry_price=c5['close']

# 🔄 Trailing stop
def manage_trade():
    global trailing_peak,trailing_stop_price,current_trail_percent,in_position
    price=entry_price # for demo; real: get live price
    profit_pct=abs((price-entry_price)/entry_price)
    if profit_pct>=0.03: current_trail_percent=0.015
    elif profit_pct>=0.02: current_trail_percent=0.01
    elif profit_pct>=0.01: current_trail_percent=0.005
    if trade_direction=='long':
        if trailing_peak is None or price>trailing_peak: trailing_peak=price; trailing_stop_price=price*(1-current_trail_percent)
        if current_trail_percent>0 and price<=trailing_stop_price: close_position(price,"Trailing Stop Hit")
        elif price>=tp_price: close_position(price,"Take Profit Hit")
        elif price<=sl_price: close_position(price,"Stop Loss Hit")
    else:
        if trailing_peak is None or price<trailing_peak: trailing_peak=price; trailing_stop_price=price*(1+current_trail_percent)
        if current_trail_percent>0 and price>=trailing_stop_price: close_position(price,"Trailing Stop Hit")
        elif price<=tp_price: close_position(price,"Take Profit Hit")
        elif price>=sl_price: close_position(price,"Stop Loss Hit")

# ❌ Close
def close_position(price,reason):
    global in_position
    in_position=False
    pnl=round((price-entry_price) if trade_direction=='long' else (entry_price-price),2)
    daily_trades.append((pnl,pnl>0))
    send_telegram(f"❌ *Closed:* `{price}` Reason:`{reason}` PnL:`{pnl}`")

# 📊 Daily summary
def daily_report_loop():
    while True:
        now=datetime.utcnow()+timedelta(hours=1)
        next=(now+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
        time.sleep((next-now).total_seconds())
        total_pnl=sum(p for p,_ in daily_trades)
        msg=f"📊 *Summary*\nTrades:{len(daily_trades)}\nPnL:{total_pnl}"
        send_telegram(msg)
        daily_trades.clear()

# 🚀 Bot loop
def bot_loop():
    global in_position
    while True:
        if not in_position:
            s=check_signal()
            if s: place_order(s)
        else: manage_trade()
        time.sleep(120)

# 🌐 Flask
app=Flask(__name__)
@app.route('/')
def home(): return "✅ Bot running!"

if __name__=="__main__":
    threading.Thread(target=bot_loop,daemon=True).start()
    threading.Thread(target=daily_report_loop,daemon=True).start()
    app.run(host="0.0.0.0",port=5000)
