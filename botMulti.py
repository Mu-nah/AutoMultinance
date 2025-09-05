from flask import Flask
import threading, time, requests, os
import pandas as pd
from dotenv import load_dotenv

app = Flask(__name__)

@app.route("/")
def home():
    return "EMA Crossover Bot Running (Twelve Data)!"

def run_bot():
    load_dotenv()
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    TD_API_KEYS = os.getenv("TD_API_KEYS").split(",")  # multiple keys rotation

    SYMBOLS = ["XAU/USD", "AUD/USD", "GBP/USD", "USD/JPY", "GBP/JPY"]
    EMA_PERIOD = 20  # adjust EMA length

    last_signal = {}
    td_index = 0  # rotate keys

    # --- Telegram notifier ---
    def send_telegram(msg):
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
                timeout=10
            )
        except Exception as e:
            print("Telegram error:", e)

    # --- Twelve Data fetcher ---
    def get_hourly_candles(symbol):
        nonlocal td_index
        for _ in range(len(TD_API_KEYS)):
            key = TD_API_KEYS[td_index]
            td_index = (td_index + 1) % len(TD_API_KEYS)  # rotate to next key
            try:
                url = (f"https://api.twelvedata.com/time_series"
                       f"?symbol={symbol}&interval=1h&outputsize={EMA_PERIOD+3}&apikey={key}")
                r = requests.get(url, timeout=10).json()
                if "values" in r:
                    df = pd.DataFrame(r["values"])
                    df['datetime'] = pd.to_datetime(df['datetime'])
                    df = df.sort_values("datetime")  # oldest → newest
                    df['close'] = df['close'].astype(float)
                    return df
            except Exception as e:
                print(f"Twelve Data key {key} failed: {e}")
        raise Exception("All Twelve Data keys failed")

    # --- Main loop ---
    while True:
        try:
            for symbol in SYMBOLS:
                df = get_hourly_candles(symbol)
                df['ema'] = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean()

                prev_close, prev_ema = df.iloc[-2]['close'], df.iloc[-2]['ema']
                last_close, last_ema = df.iloc[-1]['close'], df.iloc[-1]['ema']

                signal = None
                if prev_close < prev_ema and last_close > last_ema:
                    signal = f"✅ *{symbol}* Bullish EMA{EMA_PERIOD} Cross\nPrice: {last_close}"
                elif prev_close > prev_ema and last_close < last_ema:
                    signal = f"❌ *{symbol}* Bearish EMA{EMA_PERIOD} Cross\nPrice: {last_close}"

                if signal and last_signal.get(symbol) != signal:
                    send_telegram(signal)
                    last_signal[symbol] = signal

        except Exception as e:
            print("Error:", e)

        time.sleep(3600)  # check every 1 hr
# Run in background thread
threading.Thread(target=run_bot, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
