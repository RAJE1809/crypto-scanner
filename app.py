import time
import json
import os
import gc
import hmac
import hashlib
import requests
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "broker_config.json")
STATE_FILE = os.path.join(BASE_DIR, "trade_state.json")

# Delta Exchange Top High-Volume Futures + Commodities (Gold, Silver, Oil)
FUTURES_WATCHLIST = [
    'BTCUSD', 'ETHUSD', 'SOLUSD', 'XRPUSD', 'BNBUSD',
    'DOGEUSD', 'SUIUSD', 'LINKUSD', 'AVAXUSD', 'NEARUSD',
    'GOLD', 'SILVER', 'CRUDE', 'PEPEUSD', 'TONUSD'
]
USDT_INR_RATE = 88.0

# --- MEMORY CACHE & AUTO CLEANUP ---
MARKET_CACHE = {}
CACHE_TTL = 4  # Seconds

def clean_old_memory():
    now = time.time()
    expired = [k for k, v in MARKET_CACHE.items() if now - v['ts'] > 30]
    for k in expired:
        del MARKET_CACHE[k]
    gc.collect()

# --- CONFIG & STATE ---
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "broker": "delta",
        "api_key": "",
        "api_secret": "",
        "api_pin": "",
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

# --- DELTA EXCHANGE FUTURES DATA FETCHER ---
def fetch_delta_candles(symbol, resolution, limit=65):
    cache_key = f"{symbol}_{resolution}"
    now = int(time.time())
    if cache_key in MARKET_CACHE and (now - MARKET_CACHE[cache_key]['ts'] < CACHE_TTL):
        return MARKET_CACHE[cache_key]['data']

    sec_map = {'15m': 900, '1h': 3600, '4h': 14400, '1d': 86400}
    step = sec_map.get(resolution, 3600)
    start_time = now - (step * (limit + 8))

    sym_variants = [symbol, symbol.replace('USD', 'USDT')]
    endpoints = [
        "https://api.india.delta.exchange/v2/history/candles",
        "https://api.delta.exchange/v2/history/candles"
    ]
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}

    for base_url in endpoints:
        for sym in sym_variants:
            try:
                params = {"resolution": resolution, "symbol": sym, "start": start_time, "end": now}
                res = requests.get(base_url, params=params, headers=headers, timeout=3.5)
                if res.status_code == 200:
                    candles = res.json().get("result", [])
                    if candles and len(candles) > 15:
                        candles = sorted(candles, key=lambda x: int(x['time']))
                        unique, seen = [], set()
                        for c in candles:
                            t = int(c['time'])
                            if t not in seen:
                                seen.add(t)
                                unique.append(c)
                        final_data = unique[-limit:]
                        MARKET_CACHE[cache_key] = {'ts': now, 'data': final_data}
                        return final_data
            except Exception:
                continue
    return None

# --- INDICATORS: RSI (14) & KELTNER CHANNEL (20, 1.0) ---
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
            rsis[i] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + (avg_gain / avg_loss)))
    return rsis

def calculate_ema_series(series, period):
    mult = 2 / (period + 1)
    ema = [series[0]]
    for price in series[1:]:
        ema.append((price - ema[-1]) * mult + ema[-1])
    return ema

def calculate_keltner_series(closes, highs, lows, length=20, mult=1.0):
    ema = calculate_ema_series(closes, length)
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
    upper, lower = [], []
    for i in range(len(closes)):
        start_idx = max(0, i - length + 1)
        atr = sum(trs[start_idx:i+1]) / (i - start_idx + 1)
        upper.append(ema[i] + (mult * atr))
        lower.append(ema[i] - (mult * atr))
    return upper, ema, lower

# --- PARALLEL COIN ANALYZER (SWING vs INTRADAY) ---
def analyze_single_coin(sym, mode, risk_usdt, rr_ratio, leverage):
    htf_tf = '1d' if mode == 'swing' else '1h'
    ltf_tf = '1h' if mode == 'swing' else '15m'

    htf_raw = fetch_delta_candles(sym, htf_tf, 45)
    ltf_raw = fetch_delta_candles(sym, ltf_tf, 45)
    if not htf_raw or not ltf_raw:
        return None

    h_close = [float(k['close']) for k in htf_raw]
    h_high = [float(k['high']) for k in htf_raw]
    h_low = [float(k['low']) for k in htf_raw]

    l_close = [float(k['close']) for k in ltf_raw]
    l_high = [float(k['high']) for k in ltf_raw]
    l_low = [float(k['low']) for k in ltf_raw]
    l_vol = [float(k.get('volume', 0)) for k in ltf_raw]

    htf_rsi = calculate_rsi_series(h_close, 14)[-1]
    htf_kcu, _, htf_kcl = calculate_keltner_series(h_close, h_high, h_low, 20, 1.0)

    ltf_rsi = calculate_rsi_series(l_close, 14)[-1]
    ltf_kcu, _, ltf_kcl = calculate_keltner_series(l_close, l_high, l_low, 20, 1.0)

    curr_price = l_close[-1]
    recent_vol = round(sum(l_vol[-5:]), 1)

    htf_long = (htf_rsi > 60.50) and (h_close[-1] > htf_kcu[-1])
    ltf_long = (ltf_rsi > 60.50) and (curr_price > ltf_kcu[-1])

    htf_short = (htf_rsi < 40.50) and (h_close[-1] < htf_kcl[-1])
    ltf_short = (ltf_rsi < 40.50) and (curr_price < ltf_kcl[-1])

    signal = "NEUTRAL"
    sl_buffer = 0.998 if mode == 'swing' else 0.999
    sl_price = round(ltf_kcl[-1] * sl_buffer, 4)
    per_unit_risk = abs(curr_price - sl_price)
    if per_unit_risk == 0:
        per_unit_risk = curr_price * 0.005
    calc_qty = round(risk_usdt / per_unit_risk, 3)
    target_price = round(curr_price + (per_unit_risk * rr_ratio), 4)

    if htf_long and ltf_long:
        signal = "STRONG LONG"
    elif htf_short and ltf_short or (htf_rsi < 45 and ltf_rsi < 45):
        if htf_short and ltf_short:
            signal = "STRONG SHORT"
        short_buf = 1.002 if mode == 'swing' else 1.001
        sl_price = round(ltf_kcu[-1] * short_buf, 4)
        per_unit_risk = abs(sl_price - curr_price)
        if per_unit_risk == 0:
            per_unit_risk = curr_price * 0.005
        calc_qty = round(risk_usdt / per_unit_risk, 3)
        target_price = round(curr_price - (per_unit_risk * rr_ratio), 4)

    pos_val_inr = calc_qty * curr_price * USDT_INR_RATE
    margin_inr = round(pos_val_inr / leverage)

    return {
        'symbol': sym + '.P',
        'clean_sym': sym,
        'mode': mode.upper(),
        'price': curr_price,
        'htf_rsi': round(htf_rsi, 2),
        'ltf_rsi': round(ltf_rsi, 2),
        'kc_u': round(ltf_kcu[-1], 4),
        'kc_l': round(ltf_kcl[-1], 4),
        'vol': recent_vol,
        'signal': signal,
        'sl': sl_price,
        'target': target_price,
        'lot_size': calc_qty,
        'margin_inr': margin_inr
    }

# --- DELTA FUTURES ORDER EXECUTION ---
def execute_broker_order(symbol, side, size):
    cfg = load_config()
    api_key = cfg.get("api_key", "").strip()
    api_secret = cfg.get("api_secret", "").strip()
    api_pin = cfg.get("api_pin", "").strip()

    if not api_key or not api_secret:
        return {"status": "simulated", "message": "Demo Mode: API Key સેટ નથી, પરંતુ ટર્મિનલમાં 1-Trade Lock એક્ટિવ થઈ ગયો છે."}

    try:
        timestamp = int(time.time())
        endpoint = "/v2/orders"
        body = {
            "product_symbol": symbol.replace('.P', ''),
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
        return {"status": "success", "message": f"Delta Futures Order Sent: {res.text}"}
    except Exception as e:
        return {"status": "error", "message": f"Delta Error: {str(e)}"}

# --- FRONTEND UI TEMPLATE ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Delta Futures Terminal | Intraday & Swing</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        :root { --bg-dark: #060911; --panel-bg: #111827; --border-col: #1f2937; --accent: #38bdf8; }
        body { background-color: var(--bg-dark); color: #f3f4f6; font-family: 'Inter', system-ui, sans-serif; font-size: 13px; }
        .terminal-card { background-color: var(--panel-bg); border: 1px solid var(--border-col); border-radius: 8px; }
        .table-dark { background-color: var(--panel-bg); --bs-table-bg: var(--panel-bg); }
        .badge-long { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid #10b981; padding: 4px 8px; border-radius: 4px; font-weight: 700; }
        .badge-short { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid #ef4444; padding: 4px 8px; border-radius: 4px; font-weight: 700; }
        .badge-neutral { background: #1f2937; color: #9ca3af; padding: 4px 8px; border-radius: 4px; }
        .coin-selector { cursor: pointer; color: var(--accent); font-weight: 700; }
        .coin-selector:hover { text-decoration: underline; }
        #price-chart { height: 340px; width: 100%; }
        #rsi-chart { height: 115px; width: 100%; border-top: 1px solid var(--border-col); }
        .stat-box { background: #0b1120; border: 1px solid #1e293b; border-radius: 6px; padding: 7px 10px; }
        .live-dot { height: 8px; width: 8px; background-color: #10b981; border-radius: 50%; display: inline-block; animation: pulse 1.2s infinite; }
        @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.2; } 100% { opacity: 1; } }
        .mode-btn.active { background-color: #0284c7 !important; color: #fff !important; border-color: #38bdf8 !important; font-weight: bold; }
        .modal-content { background-color: var(--panel-bg); color: #fff; border: 1px solid #374151; }
        .form-control, .form-select { background-color: #070b14; border: 1px solid #374151; color: #fff; font-size: 13px; }
    </style>
</head>
<body class="p-2">
    <div class="container-fluid">
        <!-- HEADER & MODE SWITCHER -->
        <div class="terminal-card p-2 px-3 mb-2 d-flex flex-wrap justify-content-between align-items-center">
            <div class="d-flex align-items-center gap-3">
                <h5 class="fw-bold text-info mb-0"><span class="live-dot me-2"></span>DELTA FUTURES TERMINAL <small class="text-warning">CRYPTO & COMMODITIES</small></h5>
                <div class="btn-group btn-group-sm" role="group">
                    <button type="button" class="btn btn-outline-info mode-btn active" id="mode-swing" onclick="switchMode('swing')">
                        📈 SWING SCANNER (1D + 1H)
                    </button>
                    <button type="button" class="btn btn-outline-info mode-btn" id="mode-intraday" onclick="switchMode('intraday')">
                        ⚡ INTRADAY SCANNER (1H + 15m)
                    </button>
                </div>
            </div>
            <div class="d-flex align-items-center gap-2 mt-2 mt-md-0">
                <span class="badge bg-dark border border-secondary">Crypto, Gold, Silver, Oil</span>
                <button class="btn btn-outline-warning btn-sm py-1" onclick="clearMemoryNow()">🧹 Clean RAM</button>
                <button class="btn btn-info btn-sm py-1 fw-bold" data-bs-toggle="modal" data-bs-target="#settingsModal">⚙ Money & Delta API</button>
                <span id="last-tick" class="text-secondary small ms-1">Live</span>
            </div>
        </div>

        <!-- 1-TRADE LOCK BANNER -->
        <div id="active-trade-container" class="mb-2 d-none">
            <div class="p-2 px-3 rounded d-flex justify-content-between align-items-center" style="background: rgba(16,185,129,0.15); border: 1px solid #10b981;">
                <div>
                    <span class="badge bg-danger me-2">1-TRADE LOCK ACTIVE</span>
                    <span class="fw-bold text-white" id="at-details"></span>
                </div>
                <button class="btn btn-sm btn-warning fw-bold py-0" onclick="closeActiveTrade()">Exit / Unlock Terminal</button>
            </div>
        </div>

        <!-- CHART & SYSTEMATIC MONEY MANAGER -->
        <div class="row g-2 mb-2">
            <div class="col-lg-8">
                <div class="terminal-card p-2 h-100">
                    <div class="d-flex flex-wrap justify-content-between align-items-center px-2 mb-2">
                        <div class="d-flex align-items-center gap-2">
                            <span class="fs-6 fw-bold text-warning" id="chart-title">BTCUSD.P (1H)</span>
                            <span class="badge" style="background:#0284c7;">KC Bands (20, 1.0)</span>
                            <span class="badge" style="background:#d97706;">EMA 20</span>
                            <span class="badge" style="background:#7c3aed;">RSI 14 [60.5 / 40.5]</span>
                        </div>
                        <div class="d-flex gap-2">
                            <div class="form-check form-switch me-2">
                                <input class="form-check-input" type="checkbox" id="toggle-levels" checked onchange="loadChartData(currentSymbol, currentTF)">
                                <label class="form-check-label small text-secondary" for="toggle-levels">Auto Draw Levels</label>
                            </div>
                            <div class="btn-group btn-group-sm">
                                <button class="btn btn-outline-info py-0" id="btn-15m" onclick="switchTF('15m')">15m</button>
                                <button class="btn btn-outline-info active py-0" id="btn-1h" onclick="switchTF('1h')">1H</button>
                                <button class="btn btn-outline-info py-0" id="btn-4h" onclick="switchTF('4h')">4H</button>
                                <button class="btn btn-outline-info py-0" id="btn-1d" onclick="switchTF('1d')">1D</button>
                            </div>
                        </div>
                    </div>
                    <div id="price-chart"></div>
                    <div id="rsi-chart"></div>
                </div>
            </div>

            <div class="col-lg-4">
                <div class="terminal-card p-3 h-100 d-flex flex-column justify-content-between">
                    <div>
                        <div class="d-flex justify-content-between align-items-center border-bottom border-secondary pb-2 mb-2">
                            <h6 class="fw-bold text-info mb-0">📊 FUTURES MONEY MANAGER</h6>
                            <span class="badge bg-warning text-dark fw-bold" id="dock-symbol">BTCUSD.P</span>
                        </div>
                        <div class="row g-2 mb-2">
                            <div class="col-6"><div class="stat-box"><div class="text-secondary small">Capital</div><div class="fw-bold fs-6" id="lbl-capital">₹10,000</div></div></div>
                            <div class="col-6"><div class="stat-box"><div class="text-secondary small">Fixed SL Risk</div><div class="fw-bold fs-6 text-danger" id="lbl-risk">₹500</div></div></div>
                            <div class="col-6"><div class="stat-box"><div class="text-secondary small">Risk : Reward</div><div class="fw-bold fs-6 text-info" id="lbl-rr">1 : 2.0</div></div></div>
                            <div class="col-6"><div class="stat-box"><div class="text-secondary small">Target Profit</div><div class="fw-bold fs-6 text-success" id="lbl-profit">+₹1,000</div></div></div>
                        </div>
                        <div class="p-2 rounded mb-2" style="background:#0b1120; border:1px solid #1e293b;">
                            <div class="d-flex justify-content-between mb-1"><span class="text-secondary">Mode:</span><span class="fw-bold text-warning" id="dock-mode">SWING (1D+1H)</span></div>
                            <div class="d-flex justify-content-between mb-1"><span class="text-secondary">Futures Price:</span><span class="fw-bold text-info" id="dock-price">$0.00</span></div>
                            <div class="d-flex justify-content-between mb-1"><span class="text-secondary">KC Buffer SL:</span><span class="fw-bold text-danger" id="dock-sl">$0.00</span></div>
                            <div class="d-flex justify-content-between mb-1"><span class="text-secondary">Target (1:2):</span><span class="fw-bold text-success" id="dock-tp">$0.00</span></div>
                            <hr class="my-1 border-secondary">
                            <div class="d-flex justify-content-between mb-1"><span class="text-warning fw-bold">Auto Lot Size:</span><span class="fw-bold fs-6 text-warning" id="dock-lot">0.000</span></div>
                            <div class="d-flex justify-content-between"><span class="text-secondary small">Req. Margin (Lev):</span><span class="text-white small fw-bold" id="dock-margin">₹0</span></div>
                        </div>
                    </div>
                    <div>
                        <div class="mb-2 text-center"><span id="dock-signal-badge" class="badge-neutral d-block py-2">SIGNAL: NEUTRAL</span></div>
                        <div class="row g-2">
                            <div class="col-6"><button class="btn btn-success w-100 fw-bold py-2" id="dock-buy-btn" onclick="executeDockOrder('buy')">▲ LONG FUTURES</button></div>
                            <div class="col-6"><button class="btn btn-danger w-100 fw-bold py-2" id="dock-sell-btn" onclick="executeDockOrder('sell')">▼ SHORT FUTURES</button></div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- FUTURES SCANNER TABLE -->
        <div class="terminal-card p-2">
            <div class="d-flex justify-content-between align-items-center px-2 mb-2">
                <span class="fw-bold text-info" id="scanner-header-title">📈 SWING FUTURES SCANNER (HTF: Daily | Entry LTF: 1-Hour)</span>
                <span class="small text-secondary">Click any Perpetual Contract to load its live chart & levels</span>
            </div>
            <div class="table-responsive">
                <table class="table table-dark table-hover align-middle mb-0">
                    <thead>
                        <tr class="text-secondary small">
                            <th>Perpetual Contract</th>
                            <th>Futures Price</th>
                            <th id="th-htf-rsi">Daily RSI (>60.5 / <40.5)</th>
                            <th id="th-ltf-rsi">1H RSI</th>
                            <th>KC Bands (Lower / Upper)</th>
                            <th>Recent Vol</th>
                            <th>Setup Signal</th>
                            <th>Buffer SL</th>
                            <th>Target (1:2)</th>
                            <th class="text-warning">Auto Lot (₹500 Risk)</th>
                            <th>Margin</th>
                            <th>Execute</th>
                        </tr>
                    </thead>
                    <tbody id="scanner-table">
                        <tr><td colspan="12" class="text-center py-4 text-info">Scanning Delta Exchange Futures & Commodities...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- SETTINGS MODAL -->
    <div class="modal fade" id="settingsModal" tabindex="-1">
        <div class="modal-dialog">
            <div class="modal-content">
                <div class="modal-header border-secondary">
                    <h6 class="modal-title fw-bold text-info">⚙ Delta Futures Money Management & API</h6>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <div class="row g-2 mb-3">
                        <div class="col-6"><label class="form-label small">Total Capital (₹)</label><input type="number" class="form-control" id="cfg-capital" value="10000"></div>
                        <div class="col-6"><label class="form-label small">Fixed SL Risk (₹)</label><input type="number" class="form-control" id="cfg-risk" value="500"></div>
                        <div class="col-6">
                            <label class="form-label small">Risk:Reward Ratio</label>
                            <select class="form-select" id="cfg-rr"><option value="2.0">1 : 2</option><option value="2.5">1 : 2.5</option><option value="3.0">1 : 3</option></select>
                        </div>
                        <div class="col-6">
                            <label class="form-label small">Futures Leverage</label>
                            <select class="form-select" id="cfg-lev"><option value="2">2x</option><option value="3" selected>3x</option><option value="5">5x</option><option value="10">10x (Intraday)</option></select>
                        </div>
                    </div>
                    <hr class="border-secondary">
                    <div class="mb-2"><label class="form-label small">Delta Exchange API Key</label><input type="text" class="form-control" id="cfg-api-key"></div>
                    <div class="mb-2"><label class="form-label small">Delta Exchange API Secret</label><input type="password" class="form-control" id="cfg-api-secret"></div>
                    <div class="mb-2"><label class="form-label small">API PIN (Optional)</label><input type="password" class="form-control" id="cfg-api-pin"></div>
                </div>
                <div class="modal-footer border-secondary">
                    <button type="button" class="btn btn-info w-100 fw-bold" onclick="saveSettings()">Save & Update Scanner</button>
                </div>
            </div>
        </div>
    </div>

    <script>
        let currentSymbol = 'BTCUSD';
        let currentTF = '1h';
        let currentMode = 'swing';
        let hasActiveTrade = false;
        let latestScanMap = {};
        let priceChart, candleSeries, kcUpperSeries, kcMidSeries, kcLowerSeries;
        let rsiChart, rsiSeries;
        let priceLines = [];

        function initCharts() {
            priceChart = LightweightCharts.createChart(document.getElementById('price-chart'), {
                layout: { background: { color: '#060911' }, textColor: '#9ca3af' },
                grid: { vertLines: { color: '#111827' }, horzLines: { color: '#111827' } },
                timeScale: { timeVisible: true, borderColor: '#1f2937' },
                rightPriceScale: { borderColor: '#1f2937' }
            });
            candleSeries = priceChart.addCandlestickSeries({ upColor: '#10b981', downColor: '#ef4444', borderVisible: false, wickUpColor: '#10b981', wickDownColor: '#ef4444' });
            kcUpperSeries = priceChart.addLineSeries({ color: '#00f2fe', lineWidth: 2, title: 'KC Upper' });
            kcMidSeries = priceChart.addLineSeries({ color: '#f59e0b', lineWidth: 1, lineStyle: 2, title: 'EMA 20' });
            kcLowerSeries = priceChart.addLineSeries({ color: '#00f2fe', lineWidth: 2, title: 'KC Lower' });

            rsiChart = LightweightCharts.createChart(document.getElementById('rsi-chart'), {
                layout: { background: { color: '#060911' }, textColor: '#9ca3af' },
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

        function switchMode(mode) {
            currentMode = mode;
            document.getElementById('mode-swing').classList.toggle('active', mode === 'swing');
            document.getElementById('mode-intraday').classList.toggle('active', mode === 'intraday');

            if (mode === 'swing') {
                document.getElementById('scanner-header-title').innerText = '📈 SWING FUTURES SCANNER (HTF: Daily | Entry LTF: 1-Hour)';
                document.getElementById('th-htf-rsi').innerText = 'Daily RSI (>60.5/<40.5)';
                document.getElementById('th-ltf-rsi').innerText = '1H RSI';
                document.getElementById('dock-mode').innerText = 'SWING (1D + 1H)';
                switchTF('1h');
            } else {
                document.getElementById('scanner-header-title').innerText = '⚡ INTRADAY FUTURES SCANNER (HTF: 1-Hour | Entry LTF: 15-Min)';
                document.getElementById('th-htf-rsi').innerText = '1H RSI (>60.5/<40.5)';
                document.getElementById('th-ltf-rsi').innerText = '15m RSI';
                document.getElementById('dock-mode').innerText = 'INTRADAY (1H + 15m)';
                switchTF('15m');
            }
            streamScanner();
        }

        function drawPriceLevelsOnChart(coinData) {
            priceLines.forEach(l => candleSeries.removePriceLine(l));
            priceLines = [];
            if (!document.getElementById('toggle-levels').checked || !coinData) return;

            priceLines.push(
                candleSeries.createPriceLine({ price: coinData.price, color: '#38bdf8', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: 'ENTRY' }),
                candleSeries.createPriceLine({ price: coinData.sl, color: '#ef4444', lineWidth: 2, lineStyle: 2, axisLabelVisible: true, title: 'SL (₹500)' }),
                candleSeries.createPriceLine({ price: coinData.target, color: '#10b981', lineWidth: 2, lineStyle: 2, axisLabelVisible: true, title: 'TARGET' })
            );
        }

        async function loadChartData(sym, tf) {
            currentSymbol = sym;
            currentTF = tf;
            document.getElementById('chart-title').innerText = `${sym}.P (${tf.toUpperCase()})`;
            updateDockPanel(sym);
            try {
                const res = await fetch(`/api/chart/${sym}/${tf}`);
                const data = await res.json();
                if (data.candles && data.candles.length > 0) {
                    candleSeries.setData(data.candles);
                    kcUpperSeries.setData(data.kc_upper);
                    kcMidSeries.setData(data.kc_mid);
                    kcLowerSeries.setData(data.kc_lower);
                    rsiSeries.setData(data.rsi);
                    if (latestScanMap[sym]) drawPriceLevelsOnChart(latestScanMap[sym]);
                }
            } catch (e) {}
        }

        function switchTF(tf) {
            ['15m', '1h', '4h', '1d'].forEach(t => document.getElementById('btn-' + t).classList.toggle('active', t === tf));
            loadChartData(currentSymbol, tf);
        }

        function updateDockPanel(sym) {
            const c = latestScanMap[sym];
            if (!c) return;
            document.getElementById('dock-symbol').innerText = c.symbol;
            document.getElementById('dock-price').innerText = '$' + c.price;
            document.getElementById('dock-sl').innerText = '$' + c.sl;
            document.getElementById('dock-tp').innerText = '$' + c.target;
            document.getElementById('dock-lot').innerText = c.lot_size;
            document.getElementById('dock-margin').innerText = '₹' + c.margin_inr;
            const sb = document.getElementById('dock-signal-badge');
            sb.innerText = 'SIGNAL: ' + c.signal;
            sb.className = c.signal === 'STRONG LONG' ? 'badge-long d-block py-2' : (c.signal === 'STRONG SHORT' ? 'badge-short d-block py-2' : 'badge-neutral d-block py-2');
        }

        function executeDockOrder(side) {
            const c = latestScanMap[currentSymbol];
            if (c) placeOrder(c.clean_sym, side, c.lot_size, c.sl, c.target);
        }

        async function fetchConfig() {
            const res = await fetch('/api/config');
            const d = await res.json();
            const cap = d.capital_inr || 10000, risk = d.max_risk_inr || 500, rr = d.rr_ratio || 2.0;
            document.getElementById('lbl-capital').innerText = "₹" + cap.toLocaleString();
            document.getElementById('lbl-risk').innerText = "₹" + risk.toLocaleString();
            document.getElementById('lbl-rr').innerText = "1 : " + rr;
            document.getElementById('lbl-profit').innerText = "+₹" + (risk * rr).toLocaleString();
            document.getElementById('cfg-capital').value = cap;
            document.getElementById('cfg-risk').value = risk;
            document.getElementById('cfg-rr').value = rr;
            document.getElementById('cfg-lev').value = d.leverage || 3;
            document.getElementById('cfg-api-key').value = d.api_key || '';
            document.getElementById('cfg-api-secret').value = d.api_secret || '';
            document.getElementById('cfg-api-pin').value = d.api_pin || '';
        }

        async function saveSettings() {
            const payload = {
                capital_inr: Number(document.getElementById('cfg-capital').value),
                max_risk_inr: Number(document.getElementById('cfg-risk').value),
                rr_ratio: Number(document.getElementById('cfg-rr').value),
                leverage: Number(document.getElementById('cfg-lev').value),
                broker: "delta",
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
        }

        async function checkTradeState() {
            const res = await fetch('/api/trade_state');
            const d = await res.json();
            const box = document.getElementById('active-trade-container');
            if (d.active_trade) {
                hasActiveTrade = true;
                box.classList.remove('d-none');
                const t = d.active_trade;
                document.getElementById('at-details').innerText = `${t.symbol}.P | ${t.side} | Lot: ${t.qty} | SL: $${t.sl} | Target: $${t.target}`;
            } else {
                hasActiveTrade = false;
                box.classList.add('d-none');
            }
        }

        async function closeActiveTrade() {
            await fetch('/api/close_active_trade', { method: 'POST' });
            checkTradeState();
            streamScanner();
        }

        async function placeOrder(sym, side, qty, sl, target) {
            if (hasActiveTrade) { alert("1-Trade Lock Active!"); return; }
            if (!confirm(`Execute ${side.toUpperCase()} on Delta Futures for ${sym}.P?\nLot: ${qty} | SL: $${sl} | Target: $${target}`)) return;
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
                const res = await fetch(`/api/scan?mode=${currentMode}`);
                const data = await res.json();
                const tbody = document.getElementById('scanner-table');
                if (!data || data.length === 0) return;

                let html = '';
                data.forEach(c => {
                    latestScanMap[c.clean_sym] = c;
                    let bClass = c.signal === 'STRONG LONG' ? 'badge-long' : (c.signal === 'STRONG SHORT' ? 'badge-short' : 'badge-neutral');
                    const lock = hasActiveTrade ? 'disabled' : '';

                    html += `
                        <tr>
                            <td class="coin-selector" onclick="loadChartData('${c.clean_sym}', currentTF)">${c.symbol}</td>
                            <td class="fw-bold">$${c.price}</td>
                            <td class="${c.htf_rsi > 60.5 ? 'text-success fw-bold' : (c.htf_rsi < 40.5 ? 'text-danger fw-bold' : '')}">${c.htf_rsi}</td>
                            <td class="${c.ltf_rsi > 60.5 ? 'text-success fw-bold' : (c.ltf_rsi < 40.5 ? 'text-danger fw-bold' : '')}">${c.ltf_rsi}</td>
                            <td class="text-info">${c.kc_l} / ${c.kc_u}</td>
                            <td class="text-secondary">${c.vol}</td>
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
                document.getElementById('last-tick').innerText = "Updated: " + new Date().toLocaleTimeString();
            } catch (e) {}
        }

        initCharts();
        fetchConfig();
        checkTradeState();
        streamScanner().then(() => loadChartData('BTCUSD', '1h'));
        setInterval(() => { streamScanner(); loadChartData(currentSymbol, currentTF); }, 4000);
        setInterval(clearMemoryNow, 60000);
    </script>
</body>
</html>
"""

# --- FLASK ENDPOINTS ---
@app.route('/')
def home():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/clean_memory', methods=['POST'])
def api_clean_memory():
    clean_old_memory()
    return jsonify({"status": "cleaned"})

@app.route('/api/chart/<symbol>/<interval>')
def api_chart(symbol, interval):
    raw = fetch_delta_candles(symbol, interval, limit=65)
    if not raw:
        return jsonify({"error": "No chart data"})
    times = [int(k['time']) for k in raw]
    opens = [float(k['open']) for k in raw]
    highs = [float(k['high']) for k in raw]
    lows = [float(k['low']) for k in raw]
    closes = [float(k['close']) for k in raw]

    kc_u, kc_m, kc_l = calculate_keltner_series(closes, highs, lows, 20, 1.0)
    rsis = calculate_rsi_series(closes, 14)

    candles, u_band, m_band, l_band, r_data = [], [], [], [], []
    for i in range(len(times)):
        candles.append({"time": times[i], "open": opens[i], "high": highs[i], "low": lows[i], "close": closes[i]})
        u_band.append({"time": times[i], "value": round(kc_u[i], 4)})
        m_band.append({"time": times[i], "value": round(kc_m[i], 4)})
        l_band.append({"time": times[i], "value": round(kc_l[i], 4)})
        r_data.append({"time": times[i], "value": round(rsis[i], 2)})

    return jsonify({"candles": candles, "kc_upper": u_band, "kc_mid": m_band, "kc_lower": l_band, "rsi": r_data})

@app.route('/api/scan')
def api_scan():
    mode = request.args.get('mode', 'swing').lower()
    cfg = load_config()
    max_risk_inr = float(cfg.get("max_risk_inr", 500))
    rr_ratio = float(cfg.get("rr_ratio", 2.0))
    leverage = float(cfg.get("leverage", 3.0))
    risk_usdt = max_risk_inr / USDT_INR_RATE

    results = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(analyze_single_coin, sym, mode, risk_usdt, rr_ratio, leverage) for sym in FUTURES_WATCHLIST]
        for f in futures:
            res = f.result()
            if res:
                results.append(res)
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
