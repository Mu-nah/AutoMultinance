# 🚀 EMA Cross Bot (Twelve Data, 1H candles)
from flask import Flask
import threading, os, time, requests
from datetime import datetime, timedelta
import pandas as pd
from dotenv import load_dotenv

app = Flask(__name__)

@app.route("/")
def home():
    return "EMA Cross Bot Running!"

def run_bot():
    load_dotenv()
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    TD_API_KEYS = os.getenv("TD_API_KEYS").split(",")  # multiple Twelve Data keys

    SYMBOLS = ["XAU/USD", "AUD/USD", "GBP/USD", "USD/JPY", "EUR/USD", "GBP/JPY"]
    EMA_PERIOD = 9

    # --- Store last cross direction ---
    last_cross = {s: None for s in SYMBOLS}

    # --- Telegram notifier ---
    def send_telegram(msg):
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
                timeout=10
            )
        except:
            pass

    # --- Twelve Data fetch ---
    def fetch_candles(symbol, interval="1h", limit=50):
        for key in TD_API_KEYS:  # try keys in rotation
            try:
                url = (f"https://api.twelvedata.com/time_series"
                       f"?symbol={symbol}&interval={interval}&outputsize={limit}&apikey={key.strip()}")
                r = requests.get(url, timeout=10).json()
                if "values" in r:
                    df = pd.DataFrame(r["values"])
                    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
                    df = df.sort_values("datetime")  # oldest → newest
                    df["close"] = df["close"].astype(float)
                    return df
            except:
                continue
        return None

    # --- Cross check ---
    def check_signal(symbol):
        df = fetch_candles(symbol)
        if df is None or len(df) < EMA_PERIOD + 2:
            return None

        df["ema"] = df["close"].ewm(span=EMA_PERIOD).mean()
        last = df.iloc[-1]   # last closed candle
        prev = df.iloc[-2]   # previous candle

        # Cross detection
        if prev["close"] > prev["ema"] and last["close"] < last["ema"]:
            return {"symbol": symbol, "time": last["datetime"], "close": last["close"],
                    "ema": last["ema"], "direction": "BEARISH"}
        elif prev["close"] < prev["ema"] and last["close"] > last["ema"]:
            return {"symbol": symbol, "time": last["datetime"], "close": last["close"],
                    "ema": last["ema"], "direction": "BULLISH"}
        return None

    # --- Main loop ---
    while True:
        try:
            # Align to the next full hour
            now = datetime.utcnow()
            next_hour = (now + timedelta(hours=1)).replace(minute=0, second=5, microsecond=0)
            wait_time = (next_hour - now).total_seconds()
            time.sleep(wait_time)

            for symbol in SYMBOLS:
                signal = check_signal(symbol)
                if signal and signal["direction"] != last_cross[symbol]:
                    last_cross[symbol] = signal["direction"]  # update memory
                    msg = (f"⚡ *{signal['symbol']}* EMA Cross Alert!\n"
                           f"🕒 {signal['time']}\n"
                           f"Close: {signal['close']:.3f}\n"
                           f"EMA{EMA_PERIOD}: {signal['ema']:.3f}\n"
                           f"Direction: {signal['direction']}")
                    send_telegram(msg)

        except Exception as e:
            time.sleep(60)

# Run bot in background
threading.Thread(target=run_bot, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
