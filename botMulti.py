import os, time, requests, pandas as pd
from datetime import datetime
from binance.client import Client
from dotenv import load_dotenv

load_dotenv()

# 🔑 Config
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SYMBOLS = ["BTCUSDT", "ETHUSDT"]

client = Client(API_KEY, API_SECRET)

# 📩 Telegram
def send_telegram(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})
    except Exception as e:
        print("Telegram error:", e)

# 📊 Get latest daily candle
def get_today_candle(symbol):
    klines = client.futures_klines(symbol=symbol, interval="1d", limit=1)
    df = pd.DataFrame(klines, columns=[
        'time','open','high','low','close','volume','close_time','qav',
        'trades','tbb','tbq','ignore'
    ])
    for col in ['open','high','low','close']:
        df[col] = df[col].astype(float)
    df['time'] = pd.to_datetime(df['time'], unit='ms', utc=True)
    return df.iloc[-1]  # today’s candle (still forming)

# 🚀 Main loop
last_direction = {}

while True:
    try:
        for symbol in SYMBOLS:
            today = get_today_candle(symbol)
            
            # bullish if close > open, bearish otherwise
            direction = "bullish" if today['close'] > today['open'] else "bearish"
            today_date = today['time'].date()
            
            key = f"{symbol}_{today_date}"
            
            # 🔄 Alert every time it flips
            if last_direction.get(key) != direction:
                if last_direction.get(key) is not None:  # not the very first reading
                    msg = (f"⚡ *{symbol}* Daily Candle Flip!\n"
                           f"📅 Date: {today_date}\n"
                           f"Now: {direction.upper()} "
                           f"(O:{today['open']} → C:{today['close']})")
                    send_telegram(msg)
                
                # update last known direction
                last_direction[key] = direction

    except Exception as e:
        import traceback
        print("Error:", e)
        traceback.print_exc()

    time.sleep(300)  # check every 5 minutes
