import os
import requests
from flask import Flask, jsonify, request, render_template_string

app = Flask(__name__)

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Crypto Scanner | Auto Lot Size & Live Indicators</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
    <style>
        body { background-color: #0b1120; color: #f8fafc; font-family: sans-serif; }
        .card { background-color: #1e293b; border: 1px solid #334155; border-radius: 12px; }
        .table-dark { background-color: #1e293b; --bs-table-bg: #1e293b; }
        .badge-long { background-color: #10b981; color: white; padding: 4px 8px; border-radius: 5px; font-weight: bold; }
        .badge-short { background-color: #ef4444; color: white; padding: 4px 8px; border-radius: 5px; font-weight: bold; }
        .badge-neutral { background-color: #475569; color: #cbd5e1; padding: 4px 8px; border-radius: 5px; }
        .coin-link { cursor: pointer; text-decoration: underline; color: #38bdf8; }
        #chart-container { height: 420px; width: 100%; border-radius: 8px; overflow: hidden; }
        .modal-content { background-color: #1e293b; color: #f8fafc; border: 1px solid #475569; }
        .form-control, .form-select { background-color: #0f172a; border: 1px solid #334155; color: #f8fafc; }
        .active-trade-box { background: rgba(56, 189, 248, 0.1); border: 1px solid #0284c7; border-radius: 8px; }
    </style>
</head>
<body class="p-2 p-md-3">
    <div class="container-fluid">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <div>
                <h5 class="fw-bold text-primary mb-0">Multi-Timeframe Terminal & Risk Controller</h5>
                <div class="small text-secondary mt-1">
                    Capital: <b class="text-white" id="lbl-capital">₹10,000</b> | 
                    Risk: <b class="text-warning" id="lbl-risk">₹500</b> | 
                    Lock: <b class="text-info">1 Trade Only</b>
                </div>
            </div>
            <div>
                <button class="btn btn-outline-info btn-sm me-2" data-bs-toggle="modal" data-bs-target="#settingsModal">⚙ Settings</button>
                <span id="last-tick" class="small text-muted">Loading...</span>
            </div>
        </div>

        <div id="active-trade-container" class="mb-3 d-none">
            <div class="p-3 active-trade-box d-flex justify-content-between align-items-center">
                <div>
                    <span class="badge bg-danger me-2">ACTIVE POSITION LOCKED</span>
                    <span class="fw-bold" id="at-details">Loading...</span>
                </div>
                <button class="btn btn-warning btn-sm fw-bold" onclick="closeActiveTrade()">Reset Trade</button>
            </div>
        </div>

        <div class="card p-2 shadow-lg mb-3">
            <div class="d-flex justify-content-between align-items-center px-2 mb-2">
                <span class="fw-bold small text-info" id="active-coin-title">Chart: BINANCE:BTCUSDT</span>
                <small class="text-muted">Tap coin below to change</small>
            </div>
            <div id="chart-container"></div>
        </div>

        <div class="card p-2 p-md-3 shadow-lg">
            <div class="table-responsive">
                <table class="table table-dark table-hover align-middle mb-0 text-center">
                    <thead>
                        <tr class="text-secondary small">
                            <th>Coin</th>
                            <th>Price</th>
                            <th>1D RSI</th>
                            <th>1H RSI</th>
                            <th>Signal</th>
                            <th>SL</th>
                            <th>Target</th>
                            <th class="text-warning">Lot (₹500)</th>
                            <th>Action</th>
                        </tr>
                    </thead>
                    <tbody id="scanner-table">
                        <tr><td colspan="9" class="text-center py-4">Connecting live market scanner...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <div class="modal fade" id="settingsModal" tabindex="-1">
        <div class="modal-dialog">
            <div class="modal-content">
                <div class="modal-header border-secondary">
                    <h5 class="modal-title">Capital & Broker Settings</h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <div class="row mb-3">
                        <div class="col-6">
                            <label class="form-label">Capital (INR)</label>
                            <input type="number" class="form-control" id="cfg-capital" value="10000">
                        </div>
                        <div class="col-6">
                            <label class="form-label">Max Risk (INR)</label>
                            <input type="number" class="form-control" id="cfg-risk" value="500">
                        </div>
                    </div>
                </div>
                <div class="modal-footer border-secondary">
                    <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                    <button type="button" class="btn btn-primary" onclick="saveSettings()">Save</button>
                </div>
            </div>
        </div>
    </div>

    <script>
        let hasActiveTrade = false;

        function loadTradingViewChart(symbol) {
            document.getElementById('active-coin-title').innerText = "Chart: BINANCE:" + symbol;
            document.getElementById('chart-container').innerHTML = '';
            try {
                new TradingView.widget({
                    "autosize": true,
                    "symbol": "BINANCE:" + symbol,
                    "interval": "60",
                    "timezone": "Asia/Kolkata",
                    "theme": "dark",
                    "style": "1",
                    "locale": "en",
                    "toolbar_bg": "#1e293b",
                    "enable_publishing": false,
                    "hide_side_toolbar": false,
                    "allow_symbol_change": true,
                    "container_id": "chart-container"
                });
            } catch(e) {}
        }

        async function fetchConfig() {
            try {
                const res = await fetch('/api/config');
                const data = await res.json();
                document.getElementById('lbl-capital').innerText = "₹" + (data.capital_inr || 10000);
                document.getElementById('lbl-risk').innerText = "₹" + (data.max_risk_inr || 500);
                document.getElementById('cfg-capital').value = data.capital_inr || 10000;
                document.getElementById('cfg-risk').value = data.max_risk_inr || 500;
            } catch(e){}
        }

        async function saveSettings() {
            const payload = {
                capital_inr: document.getElementById('cfg-capital').value,
                max_risk_inr: document.getElementById('cfg-risk').value
            };
            await fetch('/api/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
            fetchConfig();
            bootstrap.Modal.getInstance(document.getElementById('settingsModal')).hide();
        }

        async function checkTradeState() {
            try {
                const res = await fetch('/api/trade_state');
                const data = await res.json();
                const container = document.getElementById('active-trade-container');
                if (data.active_trade) {
                    hasActiveTrade = true;
                    container.classList.remove('d-none');
                    const t = data.active_trade;
                    document.getElementById('at-details').innerText = `${t.symbol} | ${t.side} | Qty: ${t.qty} | SL: $${t.sl}`;
                } else {
                    hasActiveTrade = false;
                    container.classList.add('d-none');
                }
            } catch(e){}
        }

        async function closeActiveTrade() {
            await fetch('/api/close_active_trade', { method: 'POST' });
            checkTradeState();
        }

        async function placeAutoOrder(symbol, side, qty, sl, target) {
            if (hasActiveTrade) {
                alert("Trade lock active! Reset first.");
                return;
            }
            const res = await fetch('/api/order', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ symbol, side, qty, sl, target })
            });
            const data = await res.json();
            alert(data.message);
            checkTradeState();
        }

        async function streamData() {
            try {
                const res = await fetch('/api/scan');
                const data = await res.json();
                const tbody = document.getElementById('scanner-table');
                let rowsHtml = '';

                data.forEach(coin => {
                    let badge = 'badge-neutral';
                    if (coin.signal === 'STRONG LONG') badge = 'badge-long';
                    if (coin.signal === 'STRONG SHORT') badge = 'badge-short';

                    rowsHtml += `
                        <tr>
                            <td class="fw-bold coin-link" onclick="loadTradingViewChart('${coin.clean_sym}')">${coin.symbol}</td>
                            <td>$${coin.price}</td>
                            <td>${coin.daily_rsi}</td>
                            <td>${coin.h1_rsi}</td>
                            <td><span class="${badge}">${coin.signal}</span></td>
                            <td class="text-warning">${coin.sl !== '-' ? '$' + coin.sl : '-'}</td>
                            <td class="text-info">${coin.target !== '-' ? '$' + coin.target : '-'}</td>
                            <td class="fw-bold text-warning">${coin.lot_size}</td>
                            <td>
                                <button class="btn btn-sm btn-success px-2 py-0" onclick="placeAutoOrder('${coin.clean_sym}', 'buy', '${coin.lot_size}', '${coin.sl}', '${coin.target}')">Buy</button>
                                <button class="btn btn-sm btn-danger px-2 py-0 ms-1" onclick="placeAutoOrder('${coin.clean_sym}', 'sell', '${coin.lot_size}', '${coin.sl}', '${coin.target}')">Sell</button>
                            </td>
                        </tr>
                    `;
                });
                if(rowsHtml) tbody.innerHTML = rowsHtml;
                document.getElementById('last-tick').innerText = new Date().toLocaleTimeString();
            } catch (err) {}
        }

        loadTradingViewChart('BTCUSDT');
        fetchConfig();
        checkTradeState();
        streamData();
        setInterval(streamData, 4000);
    </script>
</body>
</html>
"""

config = {"capital_inr": 10000, "max_risk_inr": 500}
trade_state = {"active_trade": None}
PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]

def get_rsi(symbol, interval):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit=20"
        res = requests.get(url, timeout=3).json()
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
        avg_g = sum(gains[-14:]) / 14
        avg_l = sum(losses[-14:]) / 14
        if avg_l == 0: return 100.0
        return round(100 - (100 / (1 + (avg_g / avg_l))), 2)
    except:
        return 50.0

@app.route("/")
def home():
    return render_template_string(HTML_PAGE)

@app.route("/api/config", methods=["GET", "POST"])
def handle_config():
    global config
    if request.method == "POST":
        config.update(request.json or {})
        return jsonify({"status": "updated"})
    return jsonify(config)

@app.route("/api/trade_state")
def get_state():
    return jsonify(trade_state)

@app.route("/api/close_active_trade", methods=["POST"])
def close_trade():
    global trade_state
    trade_state["active_trade"] = None
    return jsonify({"status": "cleared"})

@app.route("/api/order", methods=["POST"])
def order():
    global trade_state
    trade_state["active_trade"] = request.json
    return jsonify({"message": "Trade position logged & locked!"})

@app.route("/api/scan")
def scan():
    results = []
    risk_usd = float(config.get("max_risk_inr", 500)) / 90.0

    for sym in PAIRS:
        try:
            r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={sym}", timeout=3).json()
            price = float(r["price"])
            d_rsi = get_rsi(sym, "1d")
            h_rsi = get_rsi(sym, "1h")

            signal = "NEUTRAL"
            sl, target, lot = "-", "-", "-"

            if d_rsi > 58 and h_rsi > 52:
                signal = "STRONG LONG"
                sl_val = round(price * 0.985, 2)
                sl = str(sl_val)
                target = str(round(price * 1.03, 2))
                diff = abs(price - sl_val)
                lot = str(round(risk_usd / diff, 4)) if diff > 0 else "-"
            elif d_rsi < 42 and h_rsi < 48:
                signal = "STRONG SHORT"
                sl_val = round(price * 1.015, 2)
                sl = str(sl_val)
                target = str(round(price * 0.97, 2))
                diff = abs(sl_val - price)
                lot = str(round(risk_usd / diff, 4)) if diff > 0 else "-"

            results.append({
                "symbol": sym,
                "clean_sym": sym,
                "price": price,
                "daily_rsi": d_rsi,
                "h1_rsi": h_rsi,
                "signal": signal,
                "sl": sl,
                "target": target,
                "lot_size": lot
            })
        except:
            continue
    return jsonify(results)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
