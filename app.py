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

FUTURES_WATCHLIST = [
    'BTCUSD', 'ETHUSD', 'SOLUSD', 'XRPUSD', 'BNBUSD',
    'DOGEUSD', 'SUIUSD', 'LINKUSD', 'AVAXUSD', 'NEARUSD',
    'GOLD', 'SILVER', 'CRUDE', 'NATGAS', 'PEPEUSD', 'TONUSD'
]
USDT_INR_RATE = 88.0

MARKET_CACHE = {}
CACHE_TTL = 4

def clean_old_memory():
    now = time.time()
    expired = [k for k, v in MARKET_CACHE.items() if now - v['ts'] > 30]
    for k in expired:
        del MARKET_CACHE[k]
    gc.collect()

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
        "telegram_token": "",
        "telegram_chat_id": "",
        "capital_inr": 10000,
        "max_risk_inr": 500,
        "rr_ratio": 2.0,
        "leverage": 5,
        "kc_length": 20,
        "kc_mult": 1.0,
        "layout": "pro"  # 'classic' or 'pro'
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

def send_telegram_alert(message):
    cfg = load_config()
    token = cfg.get("telegram_token", "").strip()
    chat_id = cfg.get("telegram_chat_id", "").strip()
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=4)
    except Exception:
        pass

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

def analyze_single_coin(sym, mode, risk_usdt, rr_ratio, leverage, kc_len, kc_m):
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
    htf_kcu, _, htf_kcl = calculate_keltner_series(h_close, h_high, h_low, kc_len, kc_m)

    ltf_rsi = calculate_rsi_series(l_close, 14)[-1]
    ltf_kcu, _, ltf_kcl = calculate_keltner_series(l_close, l_high, l_low, kc_len, kc_m)

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
    margin_inr = round(pos_val_inr / max(1.0, leverage))

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

def execute_broker_order(symbol, side, size):
    cfg = load_config()
    api_key = cfg.get("api_key", "").strip()
    api_secret = cfg.get("api_secret", "").strip()
    api_pin = cfg.get("api_pin", "").strip()

    if not api_key or not api_secret:
        return {"status": "simulated", "message": "Demo Mode: API Key missing, simulated trade locked!"}

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
        return {"status": "success", "message": f"Delta Order Executed: {res.text}"}
    except Exception as e:
        return {"status": "error", "message": f"Delta Error: {str(e)}"}

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pro Multi-Window Trading Terminal</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        :root { --bg-dark: #05070d; --panel-bg: #0f172a; --border-col: #1e293b; --accent: #38bdf8; }
        body { background-color: var(--bg-dark); color: #f1f5f9; font-family: 'Inter', system-ui, sans-serif; font-size: 12px; }
        .terminal-card { background-color: var(--panel-bg); border: 1px solid var(--border-col); border-radius: 6px; }
        .table-dark { background-color: var(--panel-bg); --bs-table-bg: var(--panel-bg); }
        .badge-long { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid #10b981; padding: 2px 6px; border-radius: 4px; font-weight: 700; }
        .badge-short { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid #ef4444; padding: 2px 6px; border-radius: 4px; font-weight: 700; }
        .badge-neutral { background: #1f2937; color: #9ca3af; padding: 2px 6px; border-radius: 4px; }
        .coin-selector { cursor: pointer; color: var(--accent); font-weight: 700; }
        .coin-selector:hover { text-decoration: underline; }
        #price-chart { height: 380px; width: 100%; }
        #rsi-chart { height: 110px; width: 100%; border-top: 1px solid var(--border-col); }
        .stat-box { background: #070b14; border: 1px solid #1e293b; border-radius: 5px; padding: 5px 8px; }
        .live-dot { height: 7px; width: 7px; background-color: #10b981; border-radius: 50%; display: inline-block; animation: pulse 1.2s infinite; }
        @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.2; } 100% { opacity: 1; } }
        .mode-btn.active { background-color: #0284c7 !important; color: #fff !important; border-color: #38bdf8 !important; font-weight: bold; }
        .modal-content { background-color: var(--panel-bg); color: #fff; border: 1px solid #334155; }
        .form-control, .form-select { background-color: #070b14; border: 1px solid #334155; color: #fff; font-size: 12px; }
        .mini-table { font-size: 11px; }
        .mini-table th, .mini-table td { padding: 4px 6px; }
    </style>
</head>
<body class="p-1">
    <div class="container-fluid">
        <!-- TOP NAVBAR -->
        <div class="terminal-card p-2 px-3 mb-1 d-flex flex-wrap justify-content-between align-items-center">
            <div class="d-flex align-items-center gap-3">
                <h6 class="fw-bold text-info mb-0"><span class="live-dot me-2"></span>SPIDER PRO TERMINAL</h6>
                <div class="btn-group btn-group-sm" role="group">
                    <button type="button" class="btn btn-outline-info mode-btn active py-0" id="mode-swing" onclick="switchMode('swing')">Swing (1D+1H)</button>
                    <button type="button" class="btn btn-outline-info mode-btn py-0" id="mode-intraday" onclick="switchMode('intraday')">Intraday (1H+15m)</button>
                </div>
            </div>
            <div class="d-flex align-items-center gap-2">
                <span class="badge bg-dark border border-secondary text-warning" id="auto-trade-badge">Auto: OFF</span>
                <button class="btn btn-outline-warning btn-sm py-0 px-2" onclick="toggleAutoTrade()" id="auto-trade-toggle-btn">Auto Trade</button>
                <div class="btn-group btn-group-sm">
                    <button class="btn btn-secondary py-0" onclick="setLayout('pro')">🖥 Pro Layout</button>
                    <button class="btn btn-outline-secondary py-0 text-white" onclick="setLayout('classic')">📱 Classic</button>
                </div>
                <button class="btn btn-primary btn-sm py-0 px-2 fw-bold" data-bs-toggle="modal" data-bs-target="#botModal">🤖 Bot</button>
                <button class="btn btn-info btn-sm py-0 px-2 fw-bold" data-bs-toggle="modal" data-bs-target="#settingsModal">⚙ Setup</button>
                <span id="last-tick" class="text-secondary small">Live</span>
            </div>
        </div>

        <!-- ACTIVE TRADE BANNER -->
        <div id="active-trade-container" class="mb-1 d-none">
            <div class="p-1 px-3 rounded d-flex justify-content-between align-items-center" style="background: rgba(16,185,129,0.15); border: 1px solid #10b981;">
                <div><span class="badge bg-success me-2">LIVE POSITION</span><span class="fw-bold text-white small" id="at-details">...</span></div>
                <div><span class="fw-bold me-3 text-success" id="live-pnl-display">P&L: $0.00</span><button class="btn btn-sm btn-danger fw-bold py-0 px-2" style="font-size:11px;" onclick="closeActiveTrade()">Exit</button></div>
            </div>
        </div>

        <!-- LAYOUT CONTAINER (PRO SPIDER STYLE VS CLASSIC) -->
        <div id="layout-wrapper">
            <!-- PRO MULTI-WINDOW LAYOUT (Spider Style) -->
            <div id="pro-layout" class="row g-1">
                <!-- Left/Middle: Live Chart -->
                <div class="col-lg-6 col-xl-7">
                    <div class="terminal-card p-2 mb-1">
                        <div class="d-flex justify-content-between align-items-center px-1 mb-1">
                            <span class="fw-bold text-warning" id="chart-title">BTCUSD.P (1H)</span>
                            <div class="btn-group btn-group-sm">
                                <button class="btn btn-outline-info py-0 px-1" onclick="switchTF('15m')">15m</button>
                                <button class="btn btn-outline-info active py-0 px-1" onclick="switchTF('1h')">1H</button>
                                <button class="btn btn-outline-info py-0 px-1" onclick="switchTF('4h')">4H</button>
                                <button class="btn btn-outline-info py-0 px-1" onclick="switchTF('1d')">1D</button>
                            </div>
                        </div>
                        <div id="price-chart"></div>
                        <div id="rsi-chart"></div>
                    </div>
                </div>

                <!-- Right Side Windows: Live Scanner & Market Watch -->
                <div class="col-lg-6 col-xl-5">
                    <div class="d-flex flex-column gap-1">
                        <!-- Live Scanner Window -->
                        <div class="terminal-card p-2">
                            <div class="d-flex justify-content-between align-items-center border-bottom border-secondary pb-1 mb-1">
                                <span class="fw-bold text-info" style="font-size:11px;">🔍 LIVE SCANNER (Multi-Window)</span>
                                <span class="badge bg-dark" id="scanner-count">15 Assets</span>
                            </div>
                            <div class="table-responsive" style="max-height: 250px; overflow-y: auto;">
                                <table class="table table-dark table-hover align-middle mb-0 mini-table">
                                    <thead>
                                        <tr class="text-secondary">
                                            <th>Symbol</th>
                                            <th>Price</th>
                                            <th>Signal</th>
                                            <th>Lot</th>
                                            <th>Action</th>
                                        </tr>
                                    </thead>
                                    <tbody id="pro-scanner-tbody">
                                        <tr><td colspan="5" class="text-center text-info">Loading...</td></tr>
                                    </tbody>
                                </table>
                            </div>
                        </div>

                        <!-- Money Manager & Execution Dock -->
                        <div class="terminal-card p-2">
                            <div class="d-flex justify-content-between align-items-center border-bottom border-secondary pb-1 mb-1">
                                <span class="fw-bold text-warning" style="font-size:11px;" id="dock-symbol">BTCUSD.P</span>
                                <span class="badge" style="background:#0284c7;" id="dock-signal-badge">SIGNAL: NEUTRAL</span>
                            </div>
                            <div class="row g-1 mb-1">
                                <div class="col-4"><div class="stat-box"><div class="text-secondary" style="font-size:10px;">Capital</div><div class="fw-bold text-white" id="lbl-capital">₹10k</div></div></div>
                                <div class="col-4"><div class="stat-box"><div class="text-secondary" style="font-size:10px;">Risk/Trade</div><div class="fw-bold text-danger" id="lbl-risk">₹500</div></div></div>
                                <div class="col-4"><div class="stat-box"><div class="text-secondary" style="font-size:10px;">Auto Lot</div><div class="fw-bold text-warning" id="dock-lot">0.00</div></div></div>
                            </div>
                            <div class="row g-1">
                                <div class="col-6"><button class="btn btn-success w-100 fw-bold py-1" style="font-size:11px;" onclick="executeDockOrder('buy')">▲ LONG</button></div>
                                <div class="col-6"><button class="btn btn-danger w-100 fw-bold py-1" style="font-size:11px;" onclick="executeDockOrder('sell')">▼ SHORT</button></div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- FULL SCANNER TABLE (Shared Bottom for both layouts) -->
            <div class="terminal-card p-2 mt-1">
                <div class="d-flex justify-content-between align-items-center px-1 mb-1">
                    <span class="fw-bold text-info" style="font-size:11px;">📋 FULL MARKET WATCH & EXECUTION TABLE</span>
                    <span class="small text-secondary">Click any symbol to load chart</span>
                </div>
                <div class="table-responsive" style="max-height: 220px; overflow-y: auto;">
                    <table class="table table-dark table-hover align-middle mb-0 mini-table">
                        <thead>
                            <tr class="text-secondary">
                                <th>Contract</th>
                                <th>Price</th>
                                <th>HTF RSI</th>
                                <th>LTF RSI</th>
                                <th>KC (Low / Up)</th>
                                <th>Vol</th>
                                <th>Signal</th>
                                <th>Buffer SL</th>
                                <th>Target</th>
                                <th class="text-warning">Lot Size</th>
                                <th>Margin</th>
                                <th>Execute</th>
                            </tr>
                        </thead>
                        <tbody id="scanner-table">
                            <tr><td colspan="12" class="text-center py-2 text-info">Scanning...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <!-- SETTINGS & BOT MODALS -->
    <div class="modal fade" id="botModal" tabindex="-1"><div class="modal-dialog"><div class="modal-content"><div class="modal-header border-secondary"><h6 class="modal-title fw-bold text-info">🤖 Telegram Bot Settings</h6><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div><div class="modal-body"><div class="mb-2"><label class="form-label small text-info">Bot Token</label><input type="text" class="form-control" id="cfg-tg-token"></div><div class="mb-2"><label class="form-label small text-info">Chat ID</label><input type="text" class="form-control" id="cfg-tg-chat"></div></div><div class="modal-footer border-secondary"><button type="button" class="btn btn-primary w-100 fw-bold" onclick="saveBotSettings()">Save Bot</button></div></div></div></div>
    
    <div class="modal fade" id="settingsModal" tabindex="-1"><div class="modal-dialog"><div class="modal-content"><div class="modal-header border-secondary"><h6 class="modal-title fw-bold text-info">⚙ KC & Terminal Setup</h6><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div><div class="modal-body"><div class="row g-2 mb-2"><div class="col-6"><label class="form-label small">KC Length</label><input type="number" class="form-control" id="cfg-kcl" value="20"></div><div class="col-6"><label class="form-label small">KC Multiplier</label><input type="number" step="0.1" class="form-control" id="cfg-kcm" value="1.0"></div><div class="col-6"><label class="form-label small">Capital (₹)</label><input type="number" class="form-control" id="cfg-capital" value="10000"></div><div class="col-6"><label class="form-label small">Risk (₹)</label><input type="number" class="form-control" id="cfg-risk" value="500"></div><div class="col-6"><label class="form-label small">Leverage (Min 5x)</label><input type="number" class="form-control" id="cfg-lev" value="5"></div><div class="col-6"><label class="form-label small">Risk:Reward</label><select class="form-select" id="cfg-rr"><option value="2.0">1 : 2</option><option value="3.0">1 : 3</option></select></div></div><div class="mb-2"><label class="form-label small">Delta API Key</label><input type="text" class="form-control" id="cfg-api-key"></div><div class="mb-2"><label class="form-label small">Delta API Secret</label><input type="password" class="form-control" id="cfg-api-secret"></div></div><div class="modal-footer border-secondary"><button type="button" class="btn btn-info w-100 fw-bold" onclick="saveSettings()">Save Settings</button></div></div></div></div>

    <script>
        let currentSymbol = 'BTCUSD';
        let currentTF = '1h';
        let currentMode = 'swing';
        let hasActiveTrade = false;
        let autoTradeEnabled = false;
        let latestScanMap = {};
        let priceChart, candleSeries, kcUpperSeries, kcMidSeries, kcLowerSeries, rsiChart, rsiSeries;

        function initCharts() {
            priceChart = LightweightCharts.createChart(document.getElementById('price-chart'), {
                layout: { background: { color: '#0f172a' }, textColor: '#9ca3af' },
                grid: { vertLines: { color: '#1e293b' }, horzLines: { color: '#1e293b' } },
                timeScale: { timeVisible: true, borderColor: '#334155' }
            });
            candleSeries = priceChart.addCandlestickSeries({ upColor: '#10b981', downColor: '#ef4444', borderVisible: false });
            kcUpperSeries = priceChart.addLineSeries({ color: '#00f2fe', lineWidth: 2 });
            kcMidSeries = priceChart.addLineSeries({ color: '#f59e0b', lineWidth: 1, lineStyle: 2 });
            kcLowerSeries = priceChart.addLineSeries({ color: '#00f2fe', lineWidth: 2 });

            rsiChart = LightweightCharts.createChart(document.getElementById('rsi-chart'), {
                layout: { background: { color: '#0f172a' }, textColor: '#9ca3af' },
                grid: { vertLines: { color: '#1e293b' }, horzLines: { color: '#1e293b' } },
                timeScale: { visible: false }
            });
            rsiSeries = rsiChart.addLineSeries({ color: '#a855f7', lineWidth: 2 });
            rsiSeries.createPriceLine({ price: 60.5, color: '#10b981', lineWidth: 1, lineStyle: 2 });
            rsiSeries.createPriceLine({ price: 40.5, color: '#ef4444', lineWidth: 1, lineStyle: 2 });
        }

        function setLayout(layoutType) {
            const proLayout = document.getElementById('pro-layout');
            if (layoutType === 'classic') {
                proLayout.style.display = 'none';
                document.getElementById('price-chart').style.height = '420px';
            } else {
                proLayout.style.display = 'flex';
                document.getElementById('price-chart').style.height = '280px';
            }
            priceChart.timeScale().fitContent();
        }

        function switchMode(mode) {
            currentMode = mode;
            document.getElementById('mode-swing').classList.toggle('active', mode === 'swing');
            document.getElementById('mode-intraday').classList.toggle('active', mode === 'intraday');
            switchTF(mode === 'swing' ? '1h' : '15m');
            streamScanner();
        }

        function toggleAutoTrade() {
            autoTradeEnabled = !autoTradeEnabled;
            const btn = document.getElementById('auto-trade-toggle-btn');
            const badge = document.getElementById('auto-trade-badge');
            if (autoTradeEnabled) {
                btn.className = "btn btn-success btn-sm py-0 px-2"; btn.innerText = "Auto: ON";
                badge.className = "badge bg-success text-white"; badge.innerText = "Auto: ON";
            } else {
                btn.className = "btn btn-outline-warning btn-sm py-0 px-2"; btn.innerText = "Auto Trade";
                badge.className = "badge bg-dark border border-secondary text-warning"; badge.innerText = "Auto: OFF";
            }
        }

        async function loadChartData(sym, tf) {
            currentSymbol = sym;
            currentTF = tf;
            document.getElementById('chart-title').innerText = `${sym}.P (${tf.toUpperCase()})`;
            updateDockPanel(sym);
            try {
                const res = await fetch(`/api/chart/${sym}/${tf}`);
                const data = await res.json();
                if (data.candles) {
                    candleSeries.setData(data.candles);
                    kcUpperSeries.setData(data.kc_upper);
                    kcMidSeries.setData(data.kc_mid);
                    kcLowerSeries.setData(data.kc_lower);
                    rsiSeries.setData(data.rsi);
                }
            } catch(e){}
        }

        function switchTF(tf) { loadChartData(currentSymbol, tf); }

        function updateDockPanel(sym) {
            const c = latestScanMap[sym];
            if (!c) return;
            document.getElementById('dock-symbol').innerText = c.symbol;
            document.getElementById('dock-lot').innerText = c.lot_size;
            const sb = document.getElementById('dock-signal-badge');
            sb.innerText = 'SIGNAL: ' + c.signal;
            sb.className = c.signal === 'STRONG LONG' ? 'badge text-bg-success' : (c.signal === 'STRONG SHORT' ? 'badge text-bg-danger' : 'badge bg-secondary');
        }

        function executeDockOrder(side) {
            const c = latestScanMap[currentSymbol];
            if (c) placeOrder(c.clean_sym, side, c.lot_size, c.sl, c.target);
        }

        async function fetchConfig() {
            const res = await fetch('/api/config');
            const d = await res.json();
            document.getElementById('lbl-capital').innerText = "₹" + (d.capital_inr || 10000).toLocaleString();
            document.getElementById('lbl-risk').innerText = "₹" + (d.max_risk_inr || 500).toLocaleString();
            document.getElementById('cfg-capital').value = d.capital_inr || 10000;
            document.getElementById('cfg-risk').value = d.max_risk_inr || 500;
            document.getElementById('cfg-lev').value = Math.max(5, d.leverage || 5);
            document.getElementById('cfg-rr').value = d.rr_ratio || 2.0;
            document.getElementById('cfg-kcl').value = d.kc_length || 20;
            document.getElementById('cfg-kcm').value = d.kc_mult || 1.0;
            document.getElementById('cfg-tg-token').value = d.telegram_token || '';
            document.getElementById('cfg-tg-chat').value = d.telegram_chat_id || '';
            document.getElementById('cfg-api-key').value = d.api_key || '';
            document.getElementById('cfg-api-secret').value = d.api_secret || '';
        }

        async function saveBotSettings() {
            await fetch('/api/config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ telegram_token: document.getElementById('cfg-tg-token').value, telegram_chat_id: document.getElementById('cfg-tg-chat').value }) });
            alert('Bot Settings Saved!'); bootstrap.Modal.getInstance(document.getElementById('botModal')).hide();
        }

        async function saveSettings() {
            const payload = {
                capital_inr: Number(document.getElementById('cfg-capital').value),
                max_risk_inr: Number(document.getElementById('cfg-risk').value),
                leverage: Math.max(5, Number(document.getElementById('cfg-lev').value)),
                rr_ratio: Number(document.getElementById('cfg-rr').value),
                kc_length: Number(document.getElementById('cfg-kcl').value),
                kc_mult: Number(document.getElementById('cfg-kcm').value),
                api_key: document.getElementById('cfg-api-key').value,
                api_secret: document.getElementById('cfg-api-secret').value
            };
            await fetch('/api/config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
            fetchConfig(); streamScanner(); alert('Settings Saved!'); bootstrap.Modal.getInstance(document.getElementById('settingsModal')).hide();
        }

        async function checkTradeState() {
            const res = await fetch('/api/trade_state');
            const d = await res.json();
            const box = document.getElementById('active-trade-container');
            if (d.active_trade) {
                hasActiveTrade = true; box.classList.remove('d-none');
                const t = d.active_trade;
                document.getElementById('at-details').innerText = `${t.symbol} | ${t.side} | Lot: ${t.qty} | SL: $${t.sl}`;
                const currP = latestScanMap[t.clean_sym]?.price || t.entry_price;
                const diff = t.side === 'BUY' ? (currP - t.entry_price) : (t.entry_price - currP);
                const pnlUsd = diff * t.qty;
                document.getElementById('live-pnl-display').innerText = `P&L: $${pnlUsd.toFixed(2)} (₹${Math.round(pnlUsd*88)})`;
            } else { hasActiveTrade = false; box.classList.add('d-none'); }
        }

        async function closeActiveTrade() { await fetch('/api/close_active_trade', { method: 'POST' }); checkTradeState(); }
        async function placeOrder(sym, side, qty, sl, target) {
            if (hasActiveTrade) { alert("1-Trade Lock Active!"); return; }
            const res = await fetch('/api/order', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ symbol: sym, side: side, qty: qty, sl: sl, target: target }) });
            const d = await res.json(); alert(d.message); checkTradeState();
        }

        async function streamScanner() {
            try {
                const res = await fetch(`/api/scan?mode=${currentMode}`);
                const data = await res.json();
                const tbody = document.getElementById('scanner-table');
                const proTbody = document.getElementById('pro-scanner-tbody');
                if (!data || data.length === 0) return;

                let html = '', proHtml = '';
                data.forEach(c => {
                    latestScanMap[c.clean_sym] = c;
                    let bClass = c.signal === 'STRONG LONG' ? 'badge-long' : (c.signal === 'STRONG SHORT' ? 'badge-short' : 'badge-neutral');
                    const lock = hasActiveTrade ? 'disabled' : '';

                    if (autoTradeEnabled && !hasActiveTrade && (c.signal === 'STRONG LONG' || c.signal === 'STRONG SHORT')) {
                        placeOrder(c.clean_sym, c.signal === 'STRONG LONG' ? 'buy' : 'sell', c.lot_size, c.sl, c.target);
                    }

                    proHtml += `
                        <tr>
                            <td class="coin-selector" onclick="loadChartData('${c.clean_sym}', currentTF)">${c.symbol}</td>
                            <td>$${c.price}</td>
                            <td><span class="${bClass}">${c.signal.replace('STRONG ', '')}</span></td>
                            <td class="text-warning">${c.lot_size}</td>
                            <td><button class="btn btn-sm btn-success py-0 px-1" ${lock} onclick="placeOrder('${c.clean_sym}','buy','${c.lot_size}','${c.sl}','${c.target}')">L</button></td>
                        </tr>
                    `;

                    html += `
                        <tr>
                            <td class="coin-selector" onclick="loadChartData('${c.clean_sym}', currentTF)">${c.symbol}</td>
                            <td class="fw-bold">$${c.price}</td>
                            <td>${c.htf_rsi}</td>
                            <td>${c.ltf_rsi}</td>
                            <td class="text-info">${c.kc_l} / ${c.kc_u}</td>
                            <td>${c.vol}</td>
                            <td><span class="${bClass}">${c.signal}</span></td>
                            <td class="text-danger">$${c.sl}</td>
                            <td class="text-success">$${c.target}</td>
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
                proTbody.innerHTML = proHtml;
                document.getElementById('scanner-count').innerText = data.length + " Assets";
                document.getElementById('last-tick').innerText = new Date().toLocaleTimeString();
            } catch(e){}
        }

        initCharts(); fetchConfig(); checkTradeState();
        streamScanner().then(() => loadChartData('BTCUSD', '1h'));
        setInterval(() => { streamScanner(); checkTradeState(); }, 4000);
    </script>
</body>
</html>
"""

@app.route('/')
def home():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/clean_memory', methods=['POST'])
def api_clean_memory():
    clean_old_memory()
    return jsonify({"status": "cleaned"})

@app.route('/api/chart/<symbol>/<interval>')
def api_chart(symbol, interval):
    cfg = load_config()
    raw = fetch_delta_candles(symbol, interval, limit=65)
    if not raw: return jsonify({"error": "No data"})
    times = [int(k['time']) for k in raw]
    closes = [float(k['close']) for k in raw]
    highs = [float(k['high']) for k in raw]
    lows = [float(k['low']) for k in raw]
    kc_u, kc_m, kc_l = calculate_keltner_series(closes, highs, lows, int(cfg.get("kc_length", 20)), float(cfg.get("kc_mult", 1.0)))
    rsis = calculate_rsi_series(closes, 14)
    candles, u_b, m_b, l_b, r_d = [], [], [], [], []
    for i in range(len(times)):
        candles.append({"time": times[i], "open": float(raw[i]['open']), "high": highs[i], "low": lows[i], "close": closes[i]})
        u_b.append({"time": times[i], "value": round(kc_u[i], 4)})
        m_b.append({"time": times[i], "value": round(kc_m[i], 4)})
        l_b.append({"time": times[i], "value": round(kc_l[i], 4)})
        r_d.append({"time": times[i], "value": round(rsis[i], 2)})
    return jsonify({"candles": candles, "kc_upper": u_b, "kc_mid": m_b, "kc_lower": l_b, "rsi": r_d})

@app.route('/api/scan')
def api_scan():
    mode = request.args.get('mode', 'swing').lower()
    cfg = load_config()
    results = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(analyze_single_coin, sym, mode, float(cfg.get("max_risk_inr", 500))/USDT_INR_RATE, float(cfg.get("rr_ratio", 2.0)), max(5.0, float(cfg.get("leverage", 5.0))), int(cfg.get("kc_length", 20)), float(cfg.get("kc_mult", 1.0))) for sym in FUTURES_WATCHLIST]
        for f in futures:
            res = f.result()
            if res:
                results.append(res)
                if res['signal'] in ['STRONG LONG', 'STRONG SHORT']:
                    send_telegram_alert(f"🚨 *Delta Alert*: {res['symbol']} ({res['mode']}) -> *{res['signal']}* @ ${res['price']}")
    return jsonify(results)

@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    if request.method == 'POST':
        old_cfg = load_config()
        new_cfg = request.json
        for k, v in new_cfg.items():
            if v is not None and v != "":
                if k == "api_secret" and "****" in str(v): continue
                old_cfg[k] = v
        save_config(old_cfg)
        return jsonify({"status": "saved"})
    cfg = load_config()
    masked = dict(cfg)
    if masked.get("api_secret"): masked["api_secret"] = "****"
    return jsonify(masked)

@app.route('/api/trade_state', methods=['GET'])
def get_trade_state(): return jsonify(load_trade_state())

@app.route('/api/close_active_trade', methods=['POST'])
def close_trade():
    save_trade_state({"active_trade": None})
    return jsonify({"status": "trade_cleared"})

@app.route('/api/order', methods=['POST'])
def execute_order():
    state = load_trade_state()
    if state.get("active_trade"): return jsonify({"status": "error", "message": "1 Active Trade running!"})
    data = request.json
    symbol, side, qty, sl, target = data.get('symbol'), data.get('side'), data.get('qty', 1), data.get('sl'), data.get('target')
    candles = fetch_delta_candles(symbol, '1h', 5)
    entry_price = float(candles[-1]['close']) if candles else 0.0
    broker_res = execute_broker_order(symbol, side, qty)
    new_trade = {"symbol": symbol + ".P", "clean_sym": symbol, "side": side.upper(), "qty": qty, "entry_price": entry_price, "entry_time": time.strftime("%H:%M:%S"), "sl": sl, "target": target}
    save_trade_state({"active_trade": new_trade})
    send_telegram_alert(f"✅ *Trade Executed*: {new_trade['side']} {new_trade['symbol']} | Qty: {qty} | Entry: ${entry_price}")
    return jsonify({"status": "success", "message": broker_res.get("message", "Order Placed!"), "trade": new_trade})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5050))
    app.run(host='0.0.0.0', port=port, debug=False)
