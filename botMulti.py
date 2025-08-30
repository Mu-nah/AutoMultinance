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
    from dotenv import load_dotenv

    load_dotenv()
    API_KEY = os.getenv("BINANCE_API_KEY")
    API_SECRET = os.getenv("BINANCE_API_SECRET")
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    TD_API_KEYS = os.getenv("TD_API_KEYS").split(",")  # multiple Twelve Data keys

    BINANCE_SYMBOLS = ["BTCUSDT", "ETHUSDT"]
    TWELVEDATA_SYMBOLS = ["XAU/USD"]
    SYMBOLS = BINANCE_SYMBOLS + TWELVEDATA_SYMBOLS

    client = Client(API_KEY, API_SECRET)
    last_direction = {}

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

    # --- Binance forming daily candle ---
    def get_today_candle_binance(symbol):
        klines = client.futures_klines(symbol=symbol, interval="1d", limit=2)
        df = pd.DataFrame(klines, columns=[
            'time','open','high','low','close','volume',
            'close_time','qav','trades','tbb','tbq','ignore'
        ])
        for col in ['open','high','low','close']:
            df[col] = df[col].astype(float)
        df['time'] = pd.to_datetime(df['time'], unit='ms', utc=True)
        return df.iloc[-1]  # last row = forming candle

    # --- Twelve Data forming daily candle ---
    def get_today_candle_twelvedata(symbol):
        for key in TD_API_KEYS:  # try keys one by one
            try:
                url = (f"https://api.twelvedata.com/time_series"
                       f"?symbol={symbol}&interval=1day&outputsize=2&apikey={key}")
                r = requests.get(url, timeout=10).json()
                if "values" in r:
                    candle = r["values"][0]  # most recent = forming
                    return {
                        "time": pd.to_datetime(candle["datetime"], utc=True),
                        "open": float(candle["open"]),
                        "high": float(candle["high"]),
                        "low": float(candle["low"]),
                        "close": float(candle["close"])
                    }
            except Exception as e:
                print(f"Twelve Data key {key} failed:", e)
        raise Exception("All Twelve Data keys failed for XAU/USD")

    # --- main loop ---
    while True:
        try:
            for symbol in SYMBOLS:
                if symbol in BINANCE_SYMBOLS:
                    today = get_today_candle_binance(symbol)
                elif symbol in TWELVEDATA_SYMBOLS:
                    today = get_today_candle_twelvedata(symbol)
                else:
                    continue

                direction = "bullish" if today['close'] > today['open'] else "bearish"
                today_date = today['time'].date()
                open_price, close_price = today['open'], today['close']

                key = f"{symbol}_{today_date}"
                if last_direction.get(key) != direction:
                    if last_direction.get(key) is not None:
                        msg = (f"⚡ *{symbol}* Daily Candle Flip!\n"
                               f"📅 {today_date}\n"
                               f"Now: {direction.upper()} "
                               f"(O:{open_price} → C:{close_price})")
                        send_telegram(msg)
                last_direction[key] = direction  # always update

        except Exception as e:
            print("Error:", e)

        time.sleep(300)  # check every 5 minutes

# Run the bot in a background thread
threading.Thread(target=run_bot, daemon=True).start()

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
