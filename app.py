import time
import json
import os
import gc
import hmac
import hashlib
import requests
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "broker_config.json")
STATE_FILE = os.path.join(BASE_DIR, "trade_state.json")

WATCHLIST = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'LINKUSDT', 'AVAXUSDT', 'SUIUSDT']
USDT_INR_RATE = 88.0

# --- MEMORY CACHE WITH AUTO-CLEANUP (ANTI-HANG) ---
MARKET_CACHE = {}
CACHE_TTL = 2  # Seconds

def clean_old_memory():
    """જૂનો કેશ ડેટા અને મેમરી આપોઆપ સાફ કરે છે જેથી સિસ્ટમ હેંગ ન થાય"""
    now = time.time()
    expired_keys = [k for k, v in MARKET_CACHE.items() if now - v['ts'] > 30]
    for k in expired_keys:
        del MARKET_CACHE[k]
    gc.collect()

# --- CONFIGURATION & STATE ---
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "broker": os.environ.get("DEFAULT_BROKER", "delta"),
        "api_key": os.environ.get("API_KEY", ""),
        "api_secret": os.environ.get("API_SECRET", ""),
        "api_pin": os.environ.get("API_PIN", ""),
        "capital_inr": 10000,
        "max_risk_inr": 500,
        "rr_ratio": 2.0,
        "leverage": 3
    }

def save_config(data):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(data, f, indent=4)

def load_trade_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {"active_trade": None}

def save_trade_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=4)

# --- MARKET DATA & TECHNICAL INDICATORS ---
def fetch_klines_raw(symbol, interval, limit=90):
    cache_key = f"{symbol}_{interval}"
    now = time.time()
    if cache_key in MARKET_CACHE and (now - MARKET_CACHE[cache_key]['ts'] < CACHE_TTL):
        return MARKET_CACHE[cache_key]['data']

    urls = [
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}",
        f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    ]
    for url in urls:
        try:
            res = requests.get(url, timeout=4)
            if res.status_code == 200:
                data = res.json()[-limit:]  # Keep only latest bars
                MARKET_CACHE[cache_key] = {'ts': now, 'data': data}
                return data
        except Exception:
            continue
    return None

def calculate_rsi_series(closes, period=14):
    rsis = [50.0] * len(closes)
    if len(closes) <= period:
        return rsis
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
        if i >= period:
            avg_gain = sum(gains[-period:]) / period
            avg_loss = sum(losses[-period:]) / period
            if avg_loss == 0:
                rsis[i] = 100.0
            else:
                rs = avg_gain / avg_loss
                rsis[i] = 100 - (100 / (1 + rs))
    return rsis

def calculate_ema_series(series, period):
    multiplier = 2 / (period + 1)
    ema = [series[0]]
    for price in series[1:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema

def calculate_keltner_series(closes, highs, lows, length=20, mult=1.0):
    ema = calculate_ema_series(closes, length)
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr)
    
    upper, lower = [], []
    for i in range(len(closes)):
        start_idx = max(0, i - length + 1)
        atr = sum(trs[start_idx:i+1]) / (i - start_idx + 1)
        upper.append(ema[i] + (mult * atr))
        lower.append(ema[i] - (mult * atr))
    return upper, ema, lower

# --- BROKER EXECUTION ---
def execute_broker_order(symbol, side, size):
    cfg = load_config()
    broker = cfg.get("broker", "delta")
    api_key = cfg.get("api_key", "").strip()
    api_secret = cfg.get("api_secret", "").strip()
    api_pin = cfg.get("api_pin", "").strip()

    if not api_key or not api_secret:
        return {"status": "simulated", "message": "Demo Mode: API Key સેટ નથી, પરંતુ ટર્મિનલમાં 1-Trade Lock એક્ટિવ થઈ ગયો છે."}

    if broker == "delta":
        try:
            timestamp = int(time.time())
            endpoint = "/v2/orders"
            body = {
                "product_symbol": symbol.replace('/', ''),
                "size": max(1, int(float(size))),
                "side": side.lower(),
                "order_type": "market_order"
            }
            payload = json.dumps(body)
            sig_data = f"POST{timestamp}{endpoint}{payload}"
            signature = hmac.new(api_secret.encode(), sig_data.encode(), hashlib.sha256).hexdigest()
            headers = {
                "api-key": api_key,
                "signature": signature,
                "timestamp": str(timestamp),
                "Content-Type": "application/json"
            }
            if api_pin:
                headers["x-pin"] = api_pin
            res = requests.post("https://api.india.delta.exchange" + endpoint, data=payload, headers=headers, timeout=6)
            return {"status": "success", "broker": "Delta India", "message": f"Delta Order Sent: {res.text}"}
        except Exception as e:
            return {"status": "error", "message": f"Delta Error: {str(e)}"}

    elif broker == "coinswitch":
        try:
            timestamp = int(time.time() * 1000)
            endpoint = "/trade/api/v2/order"
            body = {
                "symbol": f"{symbol.replace('USDT', '')}/USDT",
                "side": side.lower(),
                "type": "market",
                "quantity": float(size)
            }
            payload = json.dumps(body)
            sig_data = f"{timestamp}POST{endpoint}{payload}"
            signature = hmac.new(api_secret.encode(), sig_data.encode(), hashlib.sha256).hexdigest()
            headers = {
                "X-AUTH-APIKEY": api_key,
                "X-AUTH-SIGNATURE": signature,
                "X-AUTH-TIMESTAMP": str(timestamp),
                "Content-Type": "application/json"
            }
            res = requests.post("https://coinswitch.co" + endpoint, data=payload, headers=headers, timeout=6)
            return {"status": "success", "broker": "CoinSwitch Pro", "message": f"CoinSwitch Order Sent: {res.text}"}
        except Exception as e:
            return {"status": "error", "message": f"CoinSwitch Error: {str(e)}"}

# --- PRO TRADING TERMINAL UI ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pro Crypto Swing Terminal V2</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        :root { --bg-dark: #070b14; --panel-bg: #111827; --border-col: #1f2937; --accent: #38bdf8; }
        body { background-color: var(--bg-dark); color: #f3f4f6; font-family: 'Inter', system-ui, sans-serif; font-size: 13px; overflow-x: hidden; }
        .terminal-card { background-color: var(--panel-bg); border: 1px solid var(--border-col); border-radius: 8px; }
        .table-dark { background-color: var(--panel-bg); --bs-table-bg: var(--panel-bg); }
        .badge-long { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid #10b981; padding: 4px 8px; border-radius: 4px; font-weight: 700; }
        .badge-short { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid #ef4444; padding: 4px 8px; border-radius: 4px; font-weight: 700; }
        .badge-neutral { background: #1f2937; color: #9ca3af; padding: 4px 8px; border-radius: 4px; }
        .coin-selector { cursor: pointer; color: var(--accent); font-weight: 700; }
        .coin-selector:hover { text-decoration: underline; }
        #price-chart { height: 350px; width: 100%; }
        #rsi-chart { height: 120px; width: 100%; border-top: 1px solid var(--border-col); }
        .stat-box { background: #0b1120; border: 1px solid #1e293b; border-radius: 6px; padding: 8px 10px; }
        .live-dot { height: 8px; width: 8px; background-color: #10b981; border-radius: 50%; display: inline-block; animation: pulse 1.2s infinite; }
        @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.2; } 100% { opacity: 1; } }
        .flash-up { animation: f-green 0.5s; }
        .flash-down { animation: f-red 0.5s; }
        @keyframes f-green { 0% { background-color: rgba(16, 185, 129, 0.25); } 100% { background-color: transparent; } }
        @keyframes f-red { 0% { background-color: rgba(239, 68, 68, 0.25); } 100% { background-color: transparent; } }
        .modal-content { background-color: var(--panel-bg); color: #fff; border: 1px solid #374151; }
        .form-control, .form-select { background-color: #070b14; border: 1px solid #374151; color: #fff; font-size: 13px; }
        .form-control:focus, .form-select:focus { background-color: #070b14; color: #fff; border-color: var(--accent); box-shadow: none; }
    </style>
</head>
<body class="p-2">
    <div class="container-fluid">
        <!-- TOP HEADER BAR -->
        <div class="terminal-card p-2 px-3 mb-2 d-flex flex-wrap justify-content-between align-items-center">
            <div class="d-flex align-items-center gap-3">
                <h5 class="fw-bold text-info mb-0"><span class="live-dot me-2"></span>PRO SWING TERMINAL <small class="text-secondary fs-6">v2.0</small></h5>
                <span class="badge bg-dark border border-secondary">Broker: <b class="text-warning" id="lbl-broker">Delta India</b></span>
                <span class="badge bg-dark border border-secondary">Memory: <b class="text-success" id="mem-status">Auto-Clean ON</b></span>
            </div>
            <div class="d-flex align-items-center gap-2 mt-2 mt-md-0">
                <button class="btn btn-outline-warning btn-sm py-1" onclick="clearMemoryNow()">🧹 Clean RAM</button>
                <button class="btn btn-info btn-sm py-1 fw-bold" data-bs-toggle="modal" data-bs-target="#settingsModal">⚙ Money & API Setup</button>
                <span id="last-tick" class="text-secondary small ms-2">Syncing...</span>
            </div>
        </div>

        <!-- ACTIVE TRADE LOCK BANNER -->
        <div id="active-trade-container" class="mb-2 d-none">
            <div class="p-2 px-3 rounded d-flex justify-content-between align-items-center" style="background: rgba(16,185,129,0.15); border: 1px solid #10b981;">
                <div>
                    <span class="badge bg-danger me-2">1-TRADE LOCK ACTIVE</span>
                    <span class="fw-bold text-white" id="at-details"></span>
                </div>
                <button class="btn btn-sm btn-warning fw-bold py-0" onclick="closeActiveTrade()">Exit / Unlock Terminal</button>
            </div>
        </div>

        <!-- MAIN WORKSPACE: LEFT CHART + RIGHT EXECUTION DOCK -->
        <div class="row g-2 mb-2">
            <!-- Left Column: Visual Chart with Auto Price Levels -->
            <div class="col-lg-8">
                <div class="terminal-card p-2 h-100">
                    <div class="d-flex flex-wrap justify-content-between align-items-center px-2 mb-2">
                        <div class="d-flex align-items-center gap-2">
                            <span class="fs-6 fw-bold text-warning" id="chart-title">BTCUSDT (1H)</span>
                            <span class="badge" style="background:#0284c7;">KC Upper/Lower (20, 1.0)</span>
                            <span class="badge" style="background:#d97706;">EMA 20 Mid</span>
                            <span class="badge" style="background:#7c3aed;">RSI (14) [60.5 / 40.5]</span>
                        </div>
                        <div class="d-flex gap-2">
                            <div class="form-check form-switch me-2">
                                <input class="form-check-input" type="checkbox" id="toggle-levels" checked onchange="loadChartData(currentSymbol, currentTF)">
                                <label class="form-check-label small text-secondary" for="toggle-levels">Draw Entry/SL/TP Lines</label>
                            </div>
                            <div class="btn-group btn-group-sm">
                                <button class="btn btn-outline-info active py-0" id="btn-1h" onclick="switchTF('1h')">1H</button>
                                <button class="btn btn-outline-info py-0" id="btn-4h" onclick="switchTF('4h')">4H</button>
                                <button class="btn btn-outline-info py-0" id="btn-1d" onclick="switchTF('1d')">Daily</button>
                            </div>
                        </div>
                    </div>
                    <div id="price-chart"></div>
                    <div id="rsi-chart"></div>
                </div>
            </div>

            <!-- Right Column: Systematic Money Management & Order Dock -->
            <div class="col-lg-4">
                <div class="terminal-card p-3 h-100 d-flex flex-column justify-content-between">
                    <div>
                        <div class="d-flex justify-content-between align-items-center border-bottom border-secondary pb-2 mb-2">
                            <h6 class="fw-bold text-info mb-0">📊 SYSTEMATIC MONEY MANAGER</h6>
                            <span class="badge bg-secondary" id="dock-symbol">BTCUSDT</span>
                        </div>

                        <!-- Capital & Risk Grid -->
                        <div class="row g-2 mb-2">
                            <div class="col-6">
                                <div class="stat-box">
                                    <div class="text-secondary small">Total Capital</div>
                                    <div class="fw-bold fs-6 text-white" id="lbl-capital">₹10,000</div>
                                </div>
                            </div>
                            <div class="col-6">
                                <div class="stat-box">
                                    <div class="text-secondary small">Max Risk / Trade (5%)</div>
                                    <div class="fw-bold fs-6 text-danger" id="lbl-risk">₹500</div>
                                </div>
                            </div>
                            <div class="col-6">
                                <div class="stat-box">
                                    <div class="text-secondary small">Risk : Reward Ratio</div>
                                    <div class="fw-bold fs-6 text-info" id="lbl-rr">1 : 2.0</div>
                                </div>
                            </div>
                            <div class="col-6">
                                <div class="stat-box">
                                    <div class="text-secondary small">Expected Profit</div>
                                    <div class="fw-bold fs-6 text-success" id="lbl-profit">+₹1,000</div>
                                </div>
                            </div>
                        </div>

                        <!-- Selected Coin Live Trade Plan -->
                        <div class="p-2 rounded mb-2" style="background:#0b1120; border:1px solid #1e293b;">
                            <div class="d-flex justify-content-between mb-1">
                                <span class="text-secondary">Live Entry Price:</span>
                                <span class="fw-bold text-info" id="dock-price">$0.00</span>
                            </div>
                            <div class="d-flex justify-content-between mb-1">
                                <span class="text-secondary">KC Buffer Stop Loss:</span>
                                <span class="fw-bold text-danger" id="dock-sl">$0.00</span>
                            </div>
                            <div class="d-flex justify-content-between mb-1">
                                <span class="text-secondary">Auto Target (R:R):</span>
                                <span class="fw-bold text-success" id="dock-tp">$0.00</span>
                            </div>
                            <hr class="my-1 border-secondary">
                            <div class="d-flex justify-content-between mb-1">
                                <span class="text-warning fw-bold">Auto Calculated Lot:</span>
                                <span class="fw-bold fs-6 text-warning" id="dock-lot">0.000</span>
                            </div>
                            <div class="d-flex justify-content-between">
                                <span class="text-secondary small">Est. Margin (3x Lev):</span>
                                <span class="text-white small fw-bold" id="dock-margin">₹0</span>
                            </div>
                        </div>
                    </div>

                    <!-- Direct Terminal Execution Buttons -->
                    <div>
                        <div class="mb-2 text-center">
                            <span id="dock-signal-badge" class="badge-neutral d-block py-2">SIGNAL: NEUTRAL</span>
                        </div>
                        <div class="row g-2">
                            <div class="col-6">
                                <button class="btn btn-success w-100 fw-bold py-2" id="dock-buy-btn" onclick="executeDockOrder('buy')">
                                    ▲ LONG / BUY
                                </button>
                            </div>
                            <div class="col-6">
                                <button class="btn btn-danger w-100 fw-bold py-2" id="dock-sell-btn" onclick="executeDockOrder('sell')">
                                    ▼ SHORT / SELL
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- BOTTOM MULTI-COIN SCANNER TABLE -->
        <div class="terminal-card p-2">
            <div class="table-responsive">
                <table class="table table-dark table-hover align-middle mb-0">
                    <thead>
                        <tr class="text-secondary small">
                            <th>Symbol (Click to Load)</th>
                            <th>Live Price</th>
                            <th>Daily RSI (>60.5 / <40.5)</th>
                            <th>1H RSI</th>
                            <th>1H KC Lower / Upper</th>
                            <th>Strategy Signal</th>
                            <th>Auto SL (KC Band)</th>
                            <th>Auto Target</th>
                            <th class="text-warning">Auto Lot Size</th>
                            <th>Est. Margin (3x)</th>
                            <th>Quick Trade</th>
                        </tr>
                    </thead>
                    <tbody id="scanner-table">
                        <tr><td colspan="11" class="text-center py-3">Scanning multi-timeframe feeds...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- MONEY MANAGEMENT & BROKER SETTINGS MODAL -->
    <div class="modal fade" id="settingsModal" tabindex="-1">
        <div class="modal-dialog">
            <div class="modal-content">
                <div class="modal-header border-secondary">
                    <h6 class="modal-title fw-bold text-info">⚙ Systematic Money Management & API Setup</h6>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <div class="row g-2 mb-3">
                        <div class="col-6">
                            <label class="form-label small text-secondary">Total Capital (₹)</label>
                            <input type="number" class="form-control" id="cfg-capital" value="10000">
                        </div>
                        <div class="col-6">
                            <label class="form-label small text-secondary">Fixed Risk / SL per Trade (₹)</label>
                            <input type="number" class="form-control" id="cfg-risk" value="500">
                        </div>
                        <div class="col-6">
                            <label class="form-label small text-secondary">Risk:Reward Ratio (1:X)</label>
                            <select class="form-select" id="cfg-rr">
                                <option value="2.0">1 : 2 (Standard Swing)</option>
                                <option value="2.5">1 : 2.5</option>
                                <option value="3.0">1 : 3 (High Reward)</option>
                            </select>
                        </div>
                        <div class="col-6">
                            <label class="form-label small text-secondary">Futures Leverage (Max 3x-5x)</label>
                            <select class="form-select" id="cfg-lev">
                                <option value="2">2x (Safe)</option>
                                <option value="3" selected>3x (Recommended)</option>
                                <option value="5">5x (Moderate)</option>
                            </select>
                        </div>
                    </div>
                    <hr class="border-secondary">
                    <div class="mb-2">
                        <label class="form-label small text-secondary">Select Execution Broker</label>
                        <select class="form-select" id="cfg-broker">
                            <option value="delta">Delta Exchange India</option>
                            <option value="coinswitch">CoinSwitch Pro</option>
                        </select>
                    </div>
                    <div class="mb-2">
                        <label class="form-label small text-secondary">API Key</label>
                        <input type="text" class="form-control" id="cfg-api-key">
                    </div>
                    <div class="mb-2">
                        <label class="form-label small text-secondary">API Secret</label>
                        <input type="password" class="form-control" id="cfg-api-secret">
                    </div>
                    <div class="mb-2">
                        <label class="form-label small text-secondary">API PIN</label>
                        <input type="password" class="form-control" id="cfg-api-pin">
                    </div>
                </div>
                <div class="modal-footer border-secondary">
                    <button type="button" class="btn btn-info w-100 fw-bold" onclick="saveSettings()">Save & Recalculate Terminal</button>
                </div>
            </div>
        </div>
    </div>

    <script>
        let currentSymbol = 'BTCUSDT';
        let currentTF = '1h';
        let hasActiveTrade = false;
        let prevPrices = {};
        let latestScanMap = {};
        let priceChart, candleSeries, kcUpperSeries, kcMidSeries, kcLowerSeries;
        let rsiChart, rsiSeries, rsiUpperLine, rsiLowerLine;
        let priceLines = [];

        function initCharts() {
            const pContainer = document.getElementById('price-chart');
            const rContainer = document.getElementById('rsi-chart');

            priceChart = LightweightCharts.createChart(pContainer, {
                layout: { background: { color: '#070b14' }, textColor: '#9ca3af' },
                grid: { vertLines: { color: '#111827' }, horzLines: { color: '#111827' } },
                crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
                timeScale: { timeVisible: true, borderColor: '#1f2937' },
                rightPriceScale: { borderColor: '#1f2937' }
            });

            candleSeries = priceChart.addCandlestickSeries({
                upColor: '#10b981', downColor: '#ef4444', borderVisible: false,
                wickUpColor: '#10b981', wickDownColor: '#ef4444'
            });

            // High-visibility Keltner Channel (20, 1.0)
            kcUpperSeries = priceChart.addLineSeries({ color: '#00f2fe', lineWidth: 2, title: 'KC Upper (20,1)' });
            kcMidSeries = priceChart.addLineSeries({ color: '#f59e0b', lineWidth: 1, lineStyle: 2, title: 'EMA 20' });
            kcLowerSeries = priceChart.addLineSeries({ color: '#00f2fe', lineWidth: 2, title: 'KC Lower (20,1)' });

            // RSI Chart with Clear 60.50 & 40.50 Threshold Bands
            rsiChart = LightweightCharts.createChart(rContainer, {
                layout: { background: { color: '#070b14' }, textColor: '#9ca3af' },
                grid: { vertLines: { color: '#111827' }, horzLines: { color: '#111827' } },
                timeScale: { visible: false },
                rightPriceScale: { borderColor: '#1f2937' }
            });
            rsiSeries = rsiChart.addLineSeries({ color: '#a855f7', lineWidth: 2, title: 'RSI 14' });

            rsiSeries.createPriceLine({ price: 60.5, color: '#10b981', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'Long >60.5' });
            rsiSeries.createPriceLine({ price: 40.5, color: '#ef4444', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'Short <40.5' });

            priceChart.timeScale().subscribeVisibleLogicalRangeChange(range => {
                if (range) rsiChart.timeScale().setVisibleLogicalRange(range);
            });
        }

        function drawPriceLevelsOnChart(coinData) {
            // Clear old horizontal lines to prevent memory leak
            priceLines.forEach(l => candleSeries.removePriceLine(l));
            priceLines = [];

            if (!document.getElementById('toggle-levels').checked || !coinData) return;

            const entryLine = candleSeries.createPriceLine({
                price: coinData.price, color: '#38bdf8', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: 'ENTRY'
            });
            const slLine = candleSeries.createPriceLine({
                price: coinData.sl, color: '#ef4444', lineWidth: 2, lineStyle: 2, axisLabelVisible: true, title: 'STOP LOSS (₹500)'
            });
            const tpLine = candleSeries.createPriceLine({
                price: coinData.target, color: '#10b981', lineWidth: 2, lineStyle: 2, axisLabelVisible: true, title: 'TARGET'
            });
            priceLines.push(entryLine, slLine, tpLine);
        }

        async function loadChartData(sym, tf) {
            currentSymbol = sym;
            currentTF = tf;
            document.getElementById('chart-title').innerText = `${sym} (${tf.toUpperCase()})`;
            updateDockPanel(sym);

            try {
                const res = await fetch(`/api/chart/${sym}/${tf}`);
                const data = await res.json();
                if (data.candles) {
                    // Keep max 90 candles in browser RAM
                    candleSeries.setData(data.candles.slice(-90));
                    kcUpperSeries.setData(data.kc_upper.slice(-90));
                    kcMidSeries.setData(data.kc_mid.slice(-90));
                    kcLowerSeries.setData(data.kc_lower.slice(-90));
                    rsiSeries.setData(data.rsi.slice(-90));

                    if (latestScanMap[sym]) {
                        drawPriceLevelsOnChart(latestScanMap[sym]);
                    }
                }
            } catch (e) {}
        }

        function switchTF(tf) {
            ['1h', '4h', '1d'].forEach(t => {
                document.getElementById('btn-' + t).classList.toggle('active', t === tf);
            });
            loadChartData(currentSymbol, tf);
        }

        function updateDockPanel(sym) {
            const c = latestScanMap[sym];
            if (!c) return;
            document.getElementById('dock-symbol').innerText = sym;
            document.getElementById('dock-price').innerText = '$' + c.price;
            document.getElementById('dock-sl').innerText = '$' + c.sl;
            document.getElementById('dock-tp').innerText = '$' + c.target;
            document.getElementById('dock-lot').innerText = c.lot_size;
            document.getElementById('dock-margin').innerText = '₹' + c.margin_inr;

            const sigBadge = document.getElementById('dock-signal-badge');
            sigBadge.innerText = 'SIGNAL: ' + c.signal;
            sigBadge.className = c.signal === 'STRONG LONG' ? 'badge-long d-block py-2' : (c.signal === 'STRONG SHORT' ? 'badge-short d-block py-2' : 'badge-neutral d-block py-2');

            document.getElementById('dock-buy-btn').disabled = hasActiveTrade;
            document.getElementById('dock-sell-btn').disabled = hasActiveTrade;
        }

        function executeDockOrder(side) {
            const c = latestScanMap[currentSymbol];
            if (!c) return;
            placeOrder(c.clean_sym, side, c.lot_size, c.sl, c.target);
        }

        async function fetchConfig() {
            const res = await fetch('/api/config');
            const data = await res.json();
            const cap = data.capital_inr || 10000;
            const risk = data.max_risk_inr || 500;
            const rr = data.rr_ratio || 2.0;

            document.getElementById('lbl-capital').innerText = "₹" + cap.toLocaleString();
            document.getElementById('lbl-risk').innerText = "₹" + risk.toLocaleString();
            document.getElementById('lbl-rr').innerText = "1 : " + rr;
            document.getElementById('lbl-profit').innerText = "+₹" + (risk * rr).toLocaleString();
            document.getElementById('lbl-broker').innerText = data.broker === 'coinswitch' ? 'CoinSwitch Pro' : 'Delta India';

            document.getElementById('cfg-capital').value = cap;
            document.getElementById('cfg-risk').value = risk;
            document.getElementById('cfg-rr').value = rr;
            document.getElementById('cfg-lev').value = data.leverage || 3;
            document.getElementById('cfg-broker').value = data.broker || 'delta';
            document.getElementById('cfg-api-key').value = data.api_key || '';
            document.getElementById('cfg-api-secret').value = data.api_secret || '';
            document.getElementById('cfg-api-pin').value = data.api_pin || '';
        }

        async function saveSettings() {
            const payload = {
                capital_inr: Number(document.getElementById('cfg-capital').value),
                max_risk_inr: Number(document.getElementById('cfg-risk').value),
                rr_ratio: Number(document.getElementById('cfg-rr').value),
                leverage: Number(document.getElementById('cfg-lev').value),
                broker: document.getElementById('cfg-broker').value,
                api_key: document.getElementById('cfg-api-key').value,
                api_secret: document.getElementById('cfg-api-secret').value,
                api_pin: document.getElementById('cfg-api-pin').value
            };
            await fetch('/api/config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
            fetchConfig();
            streamScanner();
            bootstrap.Modal.getInstance(document.getElementById('settingsModal')).hide();
        }

        async function clearMemoryNow() {
            await fetch('/api/clean_memory', { method: 'POST' });
            prevPrices = {};
            document.getElementById('mem-status').innerText = "RAM Purged!";
            setTimeout(() => document.getElementById('mem-status').innerText = "Auto-Clean ON", 2000);
        }

        async function checkTradeState() {
            const res = await fetch('/api/trade_state');
            const data = await res.json();
            const box = document.getElementById('active-trade-container');
            if (data.active_trade) {
                hasActiveTrade = true;
                box.classList.remove('d-none');
                const t = data.active_trade;
                document.getElementById('at-details').innerText = `${t.symbol} | ${t.side} | Lot: ${t.qty} | SL: $${t.sl} | Target: $${t.target}`;
            } else {
                hasActiveTrade = false;
                box.classList.add('d-none');
            }
        }

        async function closeActiveTrade() {
            if (!confirm("Unlock active position lock?")) return;
            await fetch('/api/close_active_trade', { method: 'POST' });
            checkTradeState();
            streamScanner();
        }

        async function placeOrder(sym, side, qty, sl, target) {
            if (hasActiveTrade) { alert("1-Trade Lock is Active! Exit current trade first."); return; }
            if (!confirm(`Confirm ${side.toUpperCase()} Order for ${sym}\\nAuto Lot: ${qty}\\nStop Loss: $${sl}\\nTarget: $${target}`)) return;
            const res = await fetch('/api/order', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ symbol: sym, side: side, qty: qty, sl: sl, target: target })
            });
            const d = await res.json();
            alert(d.message);
            checkTradeState();
            streamScanner();
        }

        async function streamScanner() {
            try {
                const res = await fetch('/api/scan');
                const data = await res.json();
                const tbody = document.getElementById('scanner-table');
                let html = '';

                data.forEach(c => {
                    latestScanMap[c.clean_sym] = c;
                    let bClass = 'badge-neutral';
                    if (c.signal === 'STRONG LONG') bClass = 'badge-long';
                    if (c.signal === 'STRONG SHORT') bClass = 'badge-short';
                    const lock = hasActiveTrade ? 'disabled' : '';

                    let fClass = '';
                    if (prevPrices[c.clean_sym]) {
                        if (c.price > prevPrices[c.clean_sym]) fClass = 'flash-up';
                        else if (c.price < prevPrices[c.clean_sym]) fClass = 'flash-down';
                    }
                    prevPrices[c.clean_sym] = c.price;

                    html += `
                        <tr class="${fClass}">
                            <td class="coin-selector" onclick="loadChartData('${c.clean_sym}', currentTF)">${c.symbol}</td>
                            <td class="fw-bold">$${c.price}</td>
                            <td class="${c.daily_rsi > 60.5 ? 'text-success fw-bold' : (c.daily_rsi < 40.5 ? 'text-danger fw-bold' : '')}">${c.daily_rsi}</td>
                            <td class="${c.h1_rsi > 60.5 ? 'text-success fw-bold' : (c.h1_rsi < 40.5 ? 'text-danger fw-bold' : '')}">${c.h1_rsi}</td>
                            <td class="text-info">${c.h1_kc_l} / ${c.h1_kc_u}</td>
                            <td><span class="${bClass}">${c.signal}</span></td>
                            <td class="text-danger fw-semibold">$${c.sl}</td>
                            <td class="text-success fw-semibold">$${c.target}</td>
                            <td class="fw-bold text-warning">${c.lot_size}</td>
                            <td class="text-secondary">₹${c.margin_inr}</td>
                            <td>
                                <button class="btn btn-sm btn-success py-0 px-2" ${lock} onclick="placeOrder('${c.clean_sym}','buy','${c.lot_size}','${c.sl}','${c.target}')">L</button>
                                <button class="btn btn-sm btn-danger py-0 px-2 ms-1" ${lock} onclick="placeOrder('${c.clean_sym}','sell','${c.lot_size}','${c.sl}','${c.target}')">S</button>
                            </td>
                        </tr>
                    `;
                });
                tbody.innerHTML = html;
                updateDockPanel(currentSymbol);
                if (latestScanMap[currentSymbol]) drawPriceLevelsOnChart(latestScanMap[currentSymbol]);
                document.getElementById('last-tick').innerText = "Live: " + new Date().toLocaleTimeString();
            } catch (e) {}
        }

        initCharts();
        fetchConfig();
        checkTradeState();
        streamScanner().then(() => loadChartData('BTCUSDT', '1h'));

        // Auto tick refresh every 3s & periodic memory cleanup every 60s
        setInterval(() => {
            streamScanner();
            loadChartData(currentSymbol, currentTF);
        }, 3000);
        setInterval(clearMemoryNow, 60000);
    </script>
</body>
</html>
"""

# --- FLASK API ENDPOINTS ---
@app.route('/')
def home():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/clean_memory', methods=['POST'])
def api_clean_memory():
    clean_old_memory()
    return jsonify({"status": "cleaned"})

@app.route('/api/chart/<symbol>/<interval>')
def api_chart(symbol, interval):
    clean_old_memory()
    raw = fetch_klines_raw(symbol, interval, limit=90)
    if not raw:
        return jsonify({"error": "Failed to load chart data"})
    
    times = [int(k[0] / 1000) for k in raw]
    opens = [float(k[1]) for k in raw]
    highs = [float(k[2]) for k in raw]
    lows = [float(k[3]) for k in raw]
    closes = [float(k[4]) for k in raw]
    
    kc_u, kc_m, kc_l = calculate_keltner_series(closes, highs, lows, 20, 1.0)
    rsis = calculate_rsi_series(closes, 14)
    
    candles, upper_band, mid_band, lower_band, rsi_data = [], [], [], [], []
    for i in range(len(times)):
        candles.append({"time": times[i], "open": opens[i], "high": highs[i], "low": lows[i], "close": closes[i]})
        upper_band.append({"time": times[i], "value": round(kc_u[i], 4)})
        mid_band.append({"time": times[i], "value": round(kc_m[i], 4)})
        lower_band.append({"time": times[i], "value": round(kc_l[i], 4)})
        rsi_data.append({"time": times[i], "value": round(rsis[i], 2)})
        
    return jsonify({
        "candles": candles,
        "kc_upper": upper_band,
        "kc_mid": mid_band,
        "kc_lower": lower_band,
        "rsi": rsi_data
    })

@app.route('/api/scan')
def api_scan():
    clean_old_memory()
    cfg = load_config()
    max_risk_inr = float(cfg.get("max_risk_inr", 500))
    rr_ratio = float(cfg.get("rr_ratio", 2.0))
    leverage = float(cfg.get("leverage", 3.0))
    risk_usdt = max_risk_inr / USDT_INR_RATE

    results = []
    for sym in WATCHLIST:
        d_raw = fetch_klines_raw(sym, '1d', 50)
        h_raw = fetch_klines_raw(sym, '1h', 50)
        if not d_raw or not h_raw:
            continue

        d_close = [float(k[4]) for k in d_raw]
        d_high = [float(k[2]) for k in d_raw]
        d_low = [float(k[3]) for k in d_raw]

        h_close = [float(k[4]) for k in h_raw]
        h_high = [float(k[2]) for k in h_raw]
        h_low = [float(k[3]) for k in h_raw]

        d_rsi = calculate_rsi_series(d_close, 14)[-1]
        d_kc_u, _, d_kc_l = calculate_keltner_series(d_close, d_high, d_low, 20, 1.0)
        
        h_rsi = calculate_rsi_series(h_close, 14)[-1]
        h_kc_u, _, h_kc_l = calculate_keltner_series(h_close, h_high, h_low, 20, 1.0)
        
        curr_price = h_close[-1]

        daily_long = (d_rsi > 60.50) and (d_close[-1] > d_kc_u[-1])
        h1_long = (h_rsi > 60.50) and (curr_price > h_kc_u[-1])
        daily_short = (d_rsi < 40.50) and (d_close[-1] < d_kc_l[-1])
        h1_short = (h_rsi < 40.50) and (curr_price < h_kc_l[-1])

        signal = "NEUTRAL"
        # Default Long Buffer SL & Target Calculation
        sl_price = round(h_kc_l[-1] * 0.998, 4)
        per_unit_risk = abs(curr_price - sl_price)
        if per_unit_risk == 0:
            per_unit_risk = curr_price * 0.01
        calc_qty = round(risk_usdt / per_unit_risk, 3)
        target_price = round(curr_price + (per_unit_risk * rr_ratio), 4)

        if daily_long and h1_long:
            signal = "STRONG LONG"
        elif daily_short and h1_short or (d_rsi < 45 and h_rsi < 45):
            if daily_short and h1_short:
                signal = "STRONG SHORT"
            sl_price = round(h_kc_u[-1] * 1.002, 4)
            per_unit_risk = abs(sl_price - curr_price)
            if per_unit_risk == 0:
                per_unit_risk = curr_price * 0.01
            calc_qty = round(risk_usdt / per_unit_risk, 3)
            target_price = round(curr_price - (per_unit_risk * rr_ratio), 4)

        position_value_inr = calc_qty * curr_price * USDT_INR_RATE
        margin_required_inr = round(position_value_inr / leverage)

        results.append({
            'symbol': sym.replace('USDT', '/USDT'),
            'clean_sym': sym,
            'price': curr_price,
            'daily_rsi': round(d_rsi, 2),
            'h1_rsi': round(h_rsi, 2),
            'h1_kc_u': round(h_kc_u[-1], 3),
            'h1_kc_l': round(h_kc_l[-1], 3),
            'signal': signal,
            'sl': sl_price,
            'target': target_price,
            'lot_size': calc_qty,
            'margin_inr': margin_required_inr
        })
    return jsonify(results)

@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    if request.method == 'POST':
        old_cfg = load_config()
        new_cfg = request.json
        if "****" in new_cfg.get("api_secret", ""):
            new_cfg["api_secret"] = old_cfg.get("api_secret", "")
        save_config(new_cfg)
        return jsonify({"status": "saved"})
    cfg = load_config()
    masked = dict(cfg)
    if masked.get("api_secret"):
        sec = masked["api_secret"]
        masked["api_secret"] = sec[:3] + "********" if len(sec) > 4 else "****"
    return jsonify(masked)

@app.route('/api/trade_state', methods=['GET'])
def get_trade_state():
    return jsonify(load_trade_state())

@app.route('/api/close_active_trade', methods=['POST'])
def close_trade():
    save_trade_state({"active_trade": None})
    return jsonify({"status": "trade_cleared"})

@app.route('/api/order', methods=['POST'])
def execute_order():
    state = load_trade_state()
    if state.get("active_trade"):
        return jsonify({"status": "error", "message": "1 Active Trade પહેલેથી ચાલુ છે! પહેલા તેને Exit કરો."})

    data = request.json
    symbol = data.get('symbol')
    side = data.get('side')
    qty = data.get('qty', 1)
    sl = data.get('sl')
    target = data.get('target')

    broker_res = execute_broker_order(symbol, side, qty)
    new_trade = {
        "symbol": symbol,
        "side": side.upper(),
        "qty": qty,
        "entry_time": time.strftime("%H:%M:%S"),
        "sl": sl,
        "target": target
    }
    save_trade_state({"active_trade": new_trade})
    return jsonify({"status": "success", "message": broker_res.get("message", "Order Executed!"), "trade": new_trade})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5050))
    app.run(host='0.0.0.0', port=port, debug=False)
