from flask import Flask
import threading

app = Flask(__name__)

@app.route("/")
def home():
    return "Daily candle bot running!"

def run_bot():
    import time, requests, pandas as pd
    from binance.client import Client
    import os
    from datetime import datetime
    from dotenv import load_dotenv

    load_dotenv()
    API_KEY = os.getenv("BINANCE_API_KEY")
    API_SECRET = os.getenv("BINANCE_API_SECRET")
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    SYMBOLS = ["BTCUSDT", "ETHUSDT"]

    client = Client(API_KEY, API_SECRET)

    last_direction = {}

    def send_telegram(msg):
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})
        except Exception as e:
            print("Telegram error:", e)

    def get_today_candle(symbol):
        klines = client.futures_klines(symbol=symbol, interval="1d", limit=1)
        df = pd.DataFrame(klines, columns=['time','open','high','low','close','volume','close_time','qav','trades','tbb','tbq','ignore'])
        for col in ['open','high','low','close']:
            df[col] = df[col].astype(float)
        df['time'] = pd.to_datetime(df['time'], unit='ms', utc=True)
        return df.iloc[-1]

    while True:
        try:
            for symbol in SYMBOLS:
                today = get_today_candle(symbol)
                direction = "bullish" if today['close'] > today['open'] else "bearish"
                today_date = today['time'].date()
                key = f"{symbol}_{today_date}"
                if last_direction.get(key) != direction:
                    if last_direction.get(key) is not None:
                        msg = f"⚡ *{symbol}* Daily Candle Flip!\n📅 Date: {today_date}\nNow: {direction.upper()} (O:{today['open']} → C:{today['close']})"
                        send_telegram(msg)
                    last_direction[key] = direction
        except Exception as e:
            print("Error:", e)
        time.sleep(300)

# Run the bot in a separate thread
threading.Thread(target=run_bot, daemon=True).start()

# Run Flask
if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
