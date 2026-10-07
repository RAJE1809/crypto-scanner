import os
import requests
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

# In-memory trade state & settings
config = {
    "capital_inr": 10000,
    "max_risk_inr": 500,
    "broker": "delta",
    "api_key": "",
    "api_secret": "",
    "api_pin": ""
}
trade_state = {"active_trade": None}

PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]

def get_rsi(symbol, interval):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit=25"
        res = requests.get(url, timeout=5).json()
        closes = [float(k[4]) for k in res]
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            if diff >= 0:
                gains.append(diff)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(diff))
        avg_gain = sum(gains[-14:]) / 14
        avg_loss = sum(losses[-14:]) / 14
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 2)
    except:
        return 50.0

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/config", methods=["GET", "POST"])
def handle_config():
    global config
    if request.method == "POST":
        data = request.json or {}
        config.update(data)
        return jsonify({"status": "updated", "config": config})
    return jsonify(config)

@app.route("/api/trade_state")
def get_trade_state():
    return jsonify(trade_state)

@app.route("/api/close_active_trade", methods=["POST"])
def close_trade():
    global trade_state
    trade_state["active_trade"] = None
    return jsonify({"status": "cleared"})

@app.route("/api/order", methods=["POST"])
def place_order():
    global trade_state
    if trade_state["active_trade"]:
        return jsonify({"message": "Position locked! Close existing trade first."}), 400
    
    data = request.json or {}
    trade_state["active_trade"] = {
        "symbol": data.get("symbol"),
        "side": data.get("side"),
        "qty": data.get("qty"),
        "sl": data.get("sl"),
        "target": data.get("target")
    }
    return jsonify({"message": f"Order executed successfully for {data.get('symbol')}!"})

@app.route("/api/scan")
def scan_coins():
    results = []
    usd_inr = 90.0
    risk_usd = float(config.get("max_risk_inr", 500)) / usd_inr

    for sym in PAIRS:
        try:
            p_res = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={sym}", timeout=4).json()
            price = float(p_res["price"])
            daily_rsi = get_rsi(sym, "1d")
            h1_rsi = get_rsi(sym, "1h")

            signal = "NEUTRAL"
            sl, target, lot_size = "-", "-", "-"

            if daily_rsi > 60 and h1_rsi > 55:
                signal = "STRONG LONG"
                sl_val = round(price * 0.985, 2)
                sl = str(sl_val)
                target = str(round(price * 1.03, 2))
                diff = abs(price - sl_val)
                lot_size = str(round(risk_usd / diff, 4)) if diff > 0 else "-"
            elif daily_rsi < 40 and h1_rsi < 45:
                signal = "STRONG SHORT"
                sl_val = round(price * 1.015, 2)
                sl = str(sl_val)
                target = str(round(price * 0.97, 2))
                diff = abs(sl_val - price)
                lot_size = str(round(risk_usd / diff, 4)) if diff > 0 else "-"

            results.append({
                "symbol": sym,
                "clean_sym": sym,
                "price": price,
                "daily_rsi": daily_rsi,
                "h1_rsi": h1_rsi,
                "signal": signal,
                "sl": sl,
                "target": target,
                "lot_size": lot_size
            })
        except:
            continue

    return jsonify(results)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
