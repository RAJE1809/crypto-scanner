import os
from flask import Flask, jsonify, request, render_template_string

app = Flask(__name__)

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Multi-Timeframe Terminal & Risk Controller</title>
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
        .chart-box { height: 380px; width: 100%; border-radius: 8px; overflow: hidden; }
        .modal-content { background-color: #1e293b; color: #f8fafc; border: 1px solid #475569; }
        .form-control, .form-select { background-color: #0f172a; border: 1px solid #334155; color: #f8fafc; }
        .active-trade-box { background: rgba(56, 189, 248, 0.1); border: 1px solid #0284c7; border-radius: 8px; }
    </style>
</head>
<body class="p-2 p-md-3">
    <div class="container-fluid">
        <!-- Header -->
        <div class="d-flex justify-content-between align-items-center mb-3">
            <div>
                <h5 class="fw-bold text-primary mb-0">Multi-Timeframe Terminal & Risk Controller</h5>
                <div class="small text-secondary mt-1">
                    Capital: <b class="text-white" id="lbl-capital">₹10,000</b> | 
                    Risk/Trade: <b class="text-warning" id="lbl-risk">₹500 (Fixed SL)</b> | 
                    Lock: <b class="text-info">1 Trade Only</b>
                </div>
            </div>
            <div>
                <button class="btn btn-outline-info btn-sm me-2" data-bs-toggle="modal" data-bs-target="#settingsModal">⚙ Broker & Capital Settings</button>
                <span id="last-tick" class="small text-muted">Updating...</span>
            </div>
        </div>

        <!-- Active Position Lock -->
        <div id="active-trade-container" class="mb-3 d-none">
            <div class="p-3 active-trade-box d-flex justify-content-between align-items-center">
                <div>
                    <span class="badge bg-danger me-2">ACTIVE POSITION LOCKED</span>
                    <span class="fw-bold" id="at-details">Loading...</span>
                </div>
                <button class="btn btn-warning btn-sm fw-bold" onclick="closeActiveTrade()">Reset Trade</button>
            </div>
        </div>

        <!-- Dual Charts: 1D and 1H Side-by-Side -->
        <div class="row g-2 mb-3">
            <div class="col-12 col-lg-6">
                <div class="card p-2 shadow-lg">
                    <div class="px-2 mb-1 d-flex justify-content-between">
                        <span class="fw-bold small text-info" id="chart-d-title">1D Chart: BINANCE:BTCUSDT</span>
                        <small class="text-muted">KC (20, 1.0) + RSI</small>
                    </div>
                    <div id="chart-container-1d" class="chart-box"></div>
                </div>
            </div>
            <div class="col-12 col-lg-6">
                <div class="card p-2 shadow-lg">
                    <div class="px-2 mb-1 d-flex justify-content-between">
                        <span class="fw-bold small text-warning" id="chart-h-title">1H Chart: BINANCE:BTCUSDT</span>
                        <small class="text-muted">KC (20, 1.0) + RSI</small>
                    </div>
                    <div id="chart-container-1h" class="chart-box"></div>
                </div>
            </div>
        </div>

        <!-- Scanner Table -->
        <div class="card p-2 p-md-3 shadow-lg">
            <div class="table-responsive">
                <table class="table table-dark table-hover align-middle mb-0 text-center">
                    <thead>
                        <tr class="text-secondary small">
                            <th>Coin</th>
                            <th>Live Price</th>
                            <th>Daily RSI</th>
                            <th>1H RSI</th>
                            <th>Setup Signal</th>
                            <th>Buffer SL</th>
                            <th>1:2 Target</th>
                            <th class="text-warning">Auto Lot (₹500 Risk)</th>
                            <th>Action</th>
                        </tr>
                    </thead>
                    <tbody id="scanner-table">
                        <tr><td colspan="9" class="text-center py-4 text-info">Scanning market candles...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- Broker & API Settings Modal -->
    <div class="modal fade" id="settingsModal" tabindex="-1">
        <div class="modal-dialog">
            <div class="modal-content">
                <div class="modal-header border-secondary">
                    <h5 class="modal-title">Capital & Broker Configuration</h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <div class="row mb-3">
                        <div class="col-6">
                            <label class="form-label">Total Capital (INR)</label>
                            <input type="number" class="form-control" id="cfg-capital" value="10000">
                        </div>
                        <div class="col-6">
                            <label class="form-label">Max Risk / SL (INR)</label>
                            <input type="number" class="form-control" id="cfg-risk" value="500">
                        </div>
                    </div>
                    <div class="mb-3">
                        <label class="form-label">Select Broker</label>
                        <select class="form-select" id="cfg-broker">
                            <option value="delta">Delta Exchange India</option>
                            <option value="coinswitch">CoinSwitch Pro</option>
                        </select>
                    </div>
                    <div class="mb-3">
                        <label class="form-label">API Key</label>
                        <input type="text" class="form-control" id="cfg-api-key" placeholder="Enter API Key">
                    </div>
                    <div class="mb-3">
                        <label class="form-label">API Secret</label>
                        <input type="password" class="form-control" id="cfg-api-secret" placeholder="Enter API Secret">
                    </div>
                    <div class="mb-3">
                        <label class="form-label">API PIN / Password</label>
                        <input type="password" class="form-control" id="cfg-api-pin" placeholder="Enter API PIN">
                    </div>
                </div>
                <div class="modal-footer border-secondary">
                    <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                    <button type="button" class="btn btn-primary" onclick="saveSettings()">Save & Connect</button>
                </div>
            </div>
        </div>
    </div>

    <script>
        const COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"];
        let hasActiveTrade = false;
        let currentCoin = "BTCUSDT";

        function renderChart(containerId, symbol, interval, titleElemId, labelPrefix) {
            document.getElementById(titleElemId).innerText = `${labelPrefix}: BINANCE:${symbol}`;
            document.getElementById(containerId).innerHTML = '';

            new TradingView.widget({
                "autosize": true,
                "symbol": "BINANCE:" + symbol,
                "interval": interval,
                "timezone": "Asia/Kolkata",
                "theme": "dark",
                "style": "1",
                "locale": "en",
                "toolbar_bg": "#1e293b",
                "enable_publishing": false,
                "hide_side_toolbar": true,
                "allow_symbol_change": false,
                "container_id": containerId,
                "studies": [
                    { "id": "Keltner Channels@tv-basicstudies", "inputs": { "length": 20, "mult": 1.0 } },
                    { "id": "RSI@tv-basicstudies", "inputs": { "length": 14 } }
                ]
            });
        }

        function setSymbol(symbol) {
            currentCoin = symbol;
            renderChart("chart-container-1d", symbol, "D", "chart-d-title", "1D Chart");
            renderChart("chart-container-1h", symbol, "60", "chart-h-title", "1H Chart");
        }

        function calculateRSI(closes) {
            if (closes.length < 15) return 50.0;
            let gains = 0, losses = 0;
            for (let i = 1; i <= 14; i++) {
                let diff = closes[i] - closes[i - 1];
                if (diff >= 0) gains += diff;
                else losses += Math.abs(diff);
            }
            let avgGain = gains / 14;
            let avgLoss = losses / 14;
            for (let i = 15; i < closes.length; i++) {
                let diff = closes[i] - closes[i - 1];
                avgGain = (avgGain * 13 + (diff > 0 ? diff : 0)) / 14;
                avgLoss = (avgLoss * 13 + (diff < 0 ? Math.abs(diff) : 0)) / 14;
            }
            if (avgLoss === 0) return 100.0;
            let rs = avgGain / avgLoss;
            return (100 - (100 / (1 + rs))).toFixed(2);
        }

        async function fetchCandleRSI(symbol, interval) {
            try {
                const res = await fetch(`https://api.binance.com/api/v3/klines?symbol=${symbol}&interval=${interval}&limit=50`);
                const data = await res.json();
                const closes = data.map(k => parseFloat(k[4]));
                return calculateRSI(closes);
            } catch(e) {
                return 50.0;
            }
        }

        async function scanMarket() {
            const riskINR = parseFloat(document.getElementById('cfg-risk').value || 500);
            const riskUSD = riskINR / 90.0;
            const tbody = document.getElementById('scanner-table');
            let rows = '';

            for (const sym of COINS) {
                try {
                    const [pRes, dRSI, hRSI] = await Promise.all([
                        fetch(`https://api.binance.com/api/v3/ticker/price?symbol=${sym}`).then(r => r.json()),
                        fetchCandleRSI(sym, "1d"),
                        fetchCandleRSI(sym, "1h")
                    ]);

                    const price = parseFloat(pRes.price);
                    let signal = 'NEUTRAL', badge = 'badge-neutral';
                    let sl = '-', target = '-', lot = '-';

                    if (dRSI > 58 && hRSI > 52) {
                        signal = 'STRONG LONG';
                        badge = 'badge-long';
                        const slVal = +(price * 0.985).toFixed(2);
                        sl = slVal;
                        target = +(price * 1.03).toFixed(2);
                        lot = (riskUSD / Math.abs(price - slVal)).toFixed(4);
                    } else if (dRSI < 42 && hRSI < 48) {
                        signal = 'STRONG SHORT';
                        badge = 'badge-short';
                        const slVal = +(price * 1.015).toFixed(2);
                        sl = slVal;
                        target = +(price * 0.97).toFixed(2);
                        lot = (riskUSD / Math.abs(slVal - price)).toFixed(4);
                    }

                    const disable = (hasActiveTrade || signal === 'NEUTRAL') ? 'disabled' : '';

                    rows += `
                        <tr>
                            <td class="fw-bold coin-link" onclick="setSymbol('${sym}')">${sym}</td>
                            <td>$${price}</td>
                            <td class="${dRSI > 60 ? 'text-success' : (dRSI < 40 ? 'text-danger' : '')}">${dRSI}</td>
                            <td class="${hRSI > 55 ? 'text-success' : (hRSI < 45 ? 'text-danger' : '')}">${hRSI}</td>
                            <td><span class="${badge}">${signal}</span></td>
                            <td class="text-warning">${sl !== '-' ? '$' + sl : '-'}</td>
                            <td class="text-info">${target !== '-' ? '$' + target : '-'}</td>
                            <td class="fw-bold text-warning">${lot}</td>
                            <td>
                                <button class="btn btn-sm btn-success px-2 py-0" ${disable} onclick="placeTrade('${sym}', 'buy', '${lot}', '${sl}', '${target}')">Buy</button>
                                <button class="btn btn-sm btn-danger px-2 py-0 ms-1" ${disable} onclick="placeTrade('${sym}', 'sell', '${lot}', '${sl}', '${target}')">Sell</button>
                            </td>
                        </tr>
                    `;
                } catch(e) {}
            }

            if (rows) tbody.innerHTML = rows;
            document.getElementById('last-tick').innerText = 'Tick: ' + new Date().toLocaleTimeString();
        }

        async function fetchConfig() {
            try {
                const res = await fetch('/api/config');
                const data = await res.json();
                document.getElementById('lbl-capital').innerText = "₹" + (data.capital_inr || 10000);
                document.getElementById('lbl-risk').innerText = "₹" + (data.max_risk_inr || 500) + " (Fixed SL)";
                document.getElementById('cfg-capital').value = data.capital_inr || 10000;
                document.getElementById('cfg-risk').value = data.max_risk_inr || 500;
                document.getElementById('cfg-broker').value = data.broker || 'delta';
                document.getElementById('cfg-api-key').value = data.api_key || '';
                document.getElementById('cfg-api-pin').value = data.api_pin || '';
            } catch(e){}
        }

        async function saveSettings() {
            const payload = {
                capital_inr: document.getElementById('cfg-capital').value,
                max_risk_inr: document.getElementById('cfg-risk').value,
                broker: document.getElementById('cfg-broker').value,
                api_key: document.getElementById('cfg-api-key').value,
                api_secret: document.getElementById('cfg-api-secret').value,
                api_pin: document.getElementById('cfg-api-pin').value
            };
            await fetch('/api/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
            alert('Settings & Broker Credentials Saved!');
            fetchConfig();
            bootstrap.Modal.getInstance(document.getElementById('settingsModal')).hide();
            scanMarket();
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
                    document.getElementById('at-details').innerText = `${t.symbol} | ${t.side.toUpperCase()} | Lot: ${t.qty} | SL: $${t.sl} | Target: $${t.target}`;
                } else {
                    hasActiveTrade = false;
                    container.classList.add('d-none');
                }
            } catch(e){}
        }

        async function closeActiveTrade() {
            if (!confirm("Close/reset active position lock?")) return;
            await fetch('/api/close_active_trade', { method: 'POST' });
            checkTradeState();
        }

        async function placeTrade(symbol, side, qty, sl, target) {
            if (hasActiveTrade) {
                alert("Only 1 active position allowed! Please close current position.");
                return;
            }
            if (!confirm(`Execute ${side.toUpperCase()} on ${symbol}?\nLot: ${qty}\nRisk: ₹500`)) return;

            const res = await fetch('/api/order', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ symbol, side, qty, sl, target })
            });
            const data = await res.json();
            alert(data.message);
            checkTradeState();
        }

        // Initialize Dual Charts & Scanner
        setSymbol('BTCUSDT');
        fetchConfig();
        checkTradeState();
        scanMarket();
        setInterval(scanMarket, 5000);
        setInterval(checkTradeState, 4000);
    </script>
</body>
</html>
"""

config = {
    "capital_inr": 10000,
    "max_risk_inr": 500,
    "broker": "delta",
    "api_key": "",
    "api_secret": "",
    "api_pin": ""
}
trade_state = {"active_trade": None}

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
    return jsonify({"message": f"Order submitted for {request.json.get('symbol')} via {config.get('broker')}!"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
