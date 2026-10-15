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
JOURNAL_FILE = os.path.join(BASE_DIR, "trade_journal.json")

FUTURES_WATCHLIST = ['BTCUSD', 'ETHUSD', 'SOLUSD', 'XRPUSD', 'GOLD', 'CRUDE']
USDT_INR_RATE = 88.0

MARKET_CACHE = {}
SCAN_CACHE = {"swing": {"ts": 0, "data": []}, "intraday": {"ts": 0, "data": []}}
CACHE_TTL = 15

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
        "telegram_token": "",
        "telegram_chat_id": "",
        "capital_inr": 10000,
        "max_risk_inr": 500,
        "rr_ratio": 2.0,
        "leverage": 5,
        "kc_length": 20,
        "kc_mult": 1.0
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

def load_journal():
    if os.path.exists(JOURNAL_FILE):
        try:
            with open(JOURNAL_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_to_journal(trade_record):
    journal = load_journal()
    journal.insert(0, trade_record) # Newest first
    with open(JOURNAL_FILE, 'w') as f:
        json.dump(journal, f, indent=4)

def send_telegram_alert(message):
    cfg = load_config()
    token = cfg.get("telegram_token", "").strip()
    chat_id = cfg.get("telegram_chat_id", "").strip()
    if not token or not chat_id: return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}, timeout=2)
    except Exception:
        pass

def fetch_delta_wallet_balance():
    cfg = load_config()
    api_key, api_secret = cfg.get("api_key", "").strip(), cfg.get("api_secret", "").strip()
    if not api_key or not api_secret:
        return {"balance_usd": 113.63, "balance_inr": 10000} # Simulated balance in demo mode
    try:
        timestamp = int(time.time())
        endpoint = "/v2/wallet/balances"
        signature = hmac.new(api_secret.encode(), f"GET{timestamp}{endpoint}".encode(), hashlib.sha256).hexdigest()
        headers = {"api-key": api_key, "signature": signature, "timestamp": str(timestamp)}
        res = requests.get("https://api.india.delta.exchange" + endpoint, headers=headers, timeout=4)
        if res.status_code == 200:
            wallets = res.json().get("result", [])
            for w in wallets:
                if w.get("asset_symbol") == "USDT" or w.get("asset_symbol") == "USD":
                    bal = float(w.get("balance", 0))
                    return {"balance_usd": bal, "balance_inr": round(bal * USDT_INR_RATE, 2)}
    except Exception:
        pass
    return {"balance_usd": 0.0, "balance_inr": 0.0}

def fetch_delta_candles(symbol, resolution, limit=35):
    cache_key = f"{symbol}_{resolution}"
    now = int(time.time())
    if cache_key in MARKET_CACHE and (now - MARKET_CACHE[cache_key]['ts'] < CACHE_TTL):
        return MARKET_CACHE[cache_key]['data']

    sec_map = {'15m': 900, '1h': 3600, '4h': 14400, '1d': 86400}
    step = sec_map.get(resolution, 3600)
    start_time = now - (step * (limit + 4))

    sym_variants = [symbol, symbol.replace('USD', 'USDT')]
    endpoint = "https://api.india.delta.exchange/v2/history/candles"
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}

    for sym in sym_variants:
        try:
            params = {"resolution": resolution, "symbol": sym, "start": start_time, "end": now}
            res = requests.get(endpoint, params=params, headers=headers, timeout=2.0)
            if res.status_code == 200:
                candles = res.json().get("result", [])
                if candles and len(candles) > 5:
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
    if len(closes) <= period: return rsis
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

    htf_raw = fetch_delta_candles(sym, htf_tf, 30)
    ltf_raw = fetch_delta_candles(sym, ltf_tf, 30)
    if not htf_raw or not ltf_raw: return None

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
    if per_unit_risk == 0: per_unit_risk = curr_price * 0.005
    calc_qty = round(risk_usdt / per_unit_risk, 3)
    target_price = round(curr_price + (per_unit_risk * rr_ratio), 4)

    if htf_long and ltf_long:
        signal = "STRONG LONG"
    elif htf_short and ltf_short:
        signal = "STRONG SHORT"
        short_buf = 1.002 if mode == 'swing' else 1.001
        sl_price = round(ltf_kcu[-1] * short_buf, 4)
        per_unit_risk = abs(sl_price - curr_price)
        if per_unit_risk == 0: per_unit_risk = curr_price * 0.005
        calc_qty = round(risk_usdt / per_unit_risk, 3)
        target_price = round(curr_price - (per_unit_risk * rr_ratio), 4)

    pos_val_inr = calc_qty * curr_price * USDT_INR_RATE
    margin_inr = round(pos_val_inr / max(1.0, leverage))

    return {
        'symbol': sym + '.P', 'clean_sym': sym, 'mode': mode.upper(),
        'price': curr_price, 'htf_rsi': round(htf_rsi, 2), 'ltf_rsi': round(ltf_rsi, 2),
        'kc_u': round(ltf_kcu[-1], 4), 'kc_l': round(ltf_kcl[-1], 4), 'vol': recent_vol,
        'signal': signal, 'sl': sl_price, 'target': target_price, 'lot_size': calc_qty, 'margin_inr': margin_inr
    }

def execute_broker_order(symbol, side, size):
    cfg = load_config()
    api_key, api_secret = cfg.get("api_key", "").strip(), cfg.get("api_secret", "").strip()
    if not api_key or not api_secret: return {"status": "simulated", "message": "Demo Mode: Simulated Trade Placed!"}
    try:
        timestamp = int(time.time())
        endpoint = "/v2/orders"
        body = {"product_symbol": symbol.replace('.P', ''), "size": max(1, int(float(size))), "side": side.lower(), "order_type": "market_order"}
        payload = json.dumps(body)
        signature = hmac.new(api_secret.encode(), f"POST{timestamp}{endpoint}{payload}".encode(), hashlib.sha256).hexdigest()
        headers = {"api-key": api_key, "signature": signature, "timestamp": str(timestamp), "Content-Type": "application/json"}
        res = requests.post("https://api.india.delta.exchange" + endpoint, data=payload, headers=headers, timeout=4)
        return {"status": "success", "message": f"Delta Order Executed: {res.text}"}
    except Exception as e:
        return {"status": "error", "message": f"Delta Error: {str(e)}"}

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pro Trading Terminal with Wallet & Journal</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        body { background-color: #070b14; color: #f1f5f9; font-family: 'Inter', sans-serif; font-size: 13px; }
        .card-custom { background-color: #111827; border: 1px solid #1f2937; border-radius: 8px; }
        .table-dark { background-color: #111827; --bs-table-bg: #111827; }
        .badge-long { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid #10b981; font-weight: 700; }
        .badge-short { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid #ef4444; font-weight: 700; }
        .coin-link { cursor: pointer; color: #38bdf8; font-weight: bold; text-decoration: underline; }
        .chart-box { height: 260px; width: 100%; }
        .rsi-box { height: 90px; width: 100%; border-top: 1px solid #1f2937; }
        .modal-content { background-color: #111827; color: #fff; border: 1px solid #374151; }
        .form-control, .form-select { background-color: #070b14; border: 1px solid #374151; color: #fff; }
    </style>
</head>
<body class="p-2">
    <div class="container-fluid">
        <!-- TOP BAR WITH LIVE WALLET -->
        <div class="card-custom p-2 px-3 mb-2 d-flex flex-wrap justify-content-between align-items-center">
            <div class="d-flex align-items-center gap-3">
                <h5 class="fw-bold text-info mb-0">⚡ DELTA PRO TERMINAL</h5>
                <div class="btn-group btn-group-sm">
                    <button class="btn btn-outline-info active" id="mode-swing" onclick="switchMode('swing')">Swing (1D+1H)</button>
                    <button class="btn btn-outline-info" id="mode-intraday" onclick="switchMode('intraday')">Intraday (1H+15m)</button>
                </div>
            </div>
            <div class="d-flex align-items-center gap-3">
                <span class="badge bg-dark border border-secondary p-2">Live Wallet: <b class="text-warning" id="live-wallet-balance">Loading...</b></span>
                <span class="badge bg-success" id="auto-trade-badge">Auto: OFF</span>
                <button class="btn btn-outline-warning btn-sm py-1" onclick="toggleAutoTrade()" id="auto-trade-toggle-btn">Auto Trade</button>
                <button class="btn btn-primary btn-sm py-1 fw-bold" data-bs-toggle="modal" data-bs-target="#botModal">🤖 Bot</button>
                <button class="btn btn-info btn-sm py-1 fw-bold" data-bs-toggle="modal" data-bs-target="#settingsModal">⚙ Setup</button>
                <button class="btn btn-secondary btn-sm py-1 fw-bold" data-bs-toggle="modal" data-bs-target="#journalModal">📖 Journal</button>
            </div>
        </div>

        <!-- ACTIVE POSITION -->
        <div id="active-trade-container" class="mb-2 d-none">
            <div class="p-2 px-3 rounded d-flex justify-content-between align-items-center" style="background: rgba(16,185,129,0.15); border: 1px solid #10b981;">
                <div><span class="badge bg-success me-2">LIVE POSITION</span><span class="fw-bold text-white" id="at-details">...</span></div>
                <div><span class="fw-bold fs-6 me-3 text-success" id="live-pnl-display">P&L: $0.00</span><button class="btn btn-sm btn-danger fw-bold py-0" onclick="closeActiveTrade()">Exit & Save to Journal</button></div>
            </div>
        </div>

        <!-- DUAL CHARTS -->
        <div class="row g-2 mb-2">
            <div class="col-md-6">
                <div class="card-custom p-2">
                    <div class="d-flex justify-content-between align-items-center px-1 mb-1">
                        <span class="fw-bold text-warning" id="chart1-title">Chart 1: HTF Trend</span>
                        <span class="badge bg-secondary" id="active-symbol-1">BTCUSD.P</span>
                    </div>
                    <div id="chart-1" class="chart-box"></div>
                    <div id="rsi-1" class="rsi-box"></div>
                </div>
            </div>
            <div class="col-md-6">
                <div class="card-custom p-2">
                    <div class="d-flex justify-content-between align-items-center px-1 mb-1">
                        <span class="fw-bold text-info" id="chart2-title">Chart 2: LTF Entry</span>
                        <span class="badge bg-secondary" id="active-symbol-2">BTCUSD.P</span>
                    </div>
                    <div id="chart-2" class="chart-box"></div>
                    <div id="rsi-2" class="rsi-box"></div>
                </div>
            </div>
        </div>

        <!-- SCANNER TABLE -->
        <div class="card-custom p-2">
            <div class="d-flex justify-content-between align-items-center px-2 mb-2">
                <span class="fw-bold text-info">📋 LIVE FUTURES & COMMODITIES SCANNER</span>
                <span class="small text-secondary">Capital: <b class="text-white" id="lbl-capital">₹10,000</b> | Risk: <b class="text-danger" id="lbl-risk">₹500</b></span>
            </div>
            <div class="table-responsive">
                <table class="table table-dark table-hover align-middle mb-0">
                    <thead>
                        <tr class="text-secondary small">
                            <th>Contract</th>
                            <th>Price</th>
                            <th id="th-htf">HTF RSI</th>
                            <th id="th-ltf">LTF RSI</th>
                            <th>KC Bands (Low / Up)</th>
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
                        <tr><td colspan="12" class="text-center py-3 text-info">Loading...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- JOURNAL MODAL -->
    <div class="modal fade" id="journalModal" tabindex="-1">
        <div class="modal-dialog modal-lg">
            <div class="modal-content">
                <div class="modal-header border-secondary">
                    <h6 class="modal-title fw-bold text-info">📖 Automated Trading Journal & History</h6>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <div class="table-responsive" style="max-height: 400px; overflow-y: auto;">
                        <table class="table table-dark table-striped table-hover small">
                            <thead>
                                <tr class="text-secondary">
                                    <th>Time</th>
                                    <th>Symbol</th>
                                    <th>Side</th>
                                    <th>Qty</th>
                                    <th>Entry</th>
                                    <th>Exit</th>
                                    <th>Realized P&L</th>
                                </tr>
                            </thead>
                            <tbody id="journal-tbody">
                                <tr><td colspan="7" class="text-center text-secondary">No closed trades recorded yet.</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- OTHER MODALS -->
    <div class="modal fade" id="botModal" tabindex="-1"><div class="modal-dialog"><div class="modal-content"><div class="modal-header border-secondary"><h6 class="modal-title fw-bold text-info">🤖 Telegram Bot</h6><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div><div class="modal-body"><div class="mb-2"><label class="form-label small">Bot Token</label><input type="text" class="form-control" id="cfg-tg-token"></div><div class="mb-2"><label class="form-label small">Chat ID</label><input type="text" class="form-control" id="cfg-tg-chat"></div></div><div class="modal-footer border-secondary"><button type="button" class="btn btn-primary w-100 fw-bold" onclick="saveBotSettings()">Save</button></div></div></div></div>
    <div class="modal fade" id="settingsModal" tabindex="-1"><div class="modal-dialog"><div class="modal-content"><div class="modal-header border-secondary"><h6 class="modal-title fw-bold text-info">⚙ Setup</h6><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div><div class="modal-body"><div class="row g-2 mb-2"><div class="col-6"><label class="form-label small">KC Length</label><input type="number" class="form-control" id="cfg-kcl" value="20"></div><div class="col-6"><label class="form-label small">KC Mult</label><input type="number" step="0.1" class="form-control" id="cfg-kcm" value="1.0"></div><div class="col-6"><label class="form-label small">Capital (₹)</label><input type="number" class="form-control" id="cfg-capital" value="10000"></div><div class="col-6"><label class="form-label small">Risk (₹)</label><input type="number" class="form-control" id="cfg-risk" value="500"></div><div class="col-6"><label class="form-label small">Leverage</label><input type="number" class="form-control" id="cfg-lev" value="5"></div><div class="col-6"><label class="form-label small">R:R</label><select class="form-select" id="cfg-rr"><option value="2.0">1 : 2</option></select></div></div><div class="mb-2"><label class="form-label small">Delta API Key</label><input type="text" class="form-control" id="cfg-api-key"></div><div class="mb-2"><label class="form-label small">Delta API Secret</label><input type="password" class="form-control" id="cfg-api-secret"></div></div><div class="modal-footer border-secondary"><button type="button" class="btn btn-info w-100 fw-bold" onclick="saveSettings()">Save</button></div></div></div></div>

    <script>
        let currentSymbol = 'BTCUSD';
        let currentMode = 'swing';
        let hasActiveTrade = false;
        let autoTradeEnabled = false;
        let latestScanMap = {};
        let c1, s1, u1, m1, l1, r1, rs1;
        let c2, s2, u2, m2, l2, r2, rs2;

        function initCharts() {
            c1 = LightweightCharts.createChart(document.getElementById('chart-1'), { layout: { background: { color: '#111827' }, textColor: '#9ca3af' }, grid: { vertLines: { color: '#1f2937' }, horzLines: { color: '#1f2937' } } });
            s1 = c1.addCandlestickSeries({ upColor: '#10b981', downColor: '#ef4444' });
            u1 = c1.addLineSeries({ color: '#00f2fe', lineWidth: 2 }); m1 = c1.addLineSeries({ color: '#f59e0b', lineWidth: 1, lineStyle: 2 }); l1 = c1.addLineSeries({ color: '#00f2fe', lineWidth: 2 });

            r1 = LightweightCharts.createChart(document.getElementById('rsi-1'), { layout: { background: { color: '#111827' }, textColor: '#9ca3af' }, grid: { vertLines: { color: '#1f2937' }, horzLines: { color: '#1f2937' } }, timeScale: { visible: false } });
            rs1 = r1.addLineSeries({ color: '#a855f7', lineWidth: 2 });
            rs1.createPriceLine({ price: 60.5, color: '#10b981', lineWidth: 1, lineStyle: 2 });
            rs1.createPriceLine({ price: 40.5, color: '#ef4444', lineWidth: 1, lineStyle: 2 });

            c2 = LightweightCharts.createChart(document.getElementById('chart-2'), { layout: { background: { color: '#111827' }, textColor: '#9ca3af' }, grid: { vertLines: { color: '#1f2937' }, horzLines: { color: '#1f2937' } } });
            s2 = c2.addCandlestickSeries({ upColor: '#10b981', downColor: '#ef4444' });
            u2 = c2.addLineSeries({ color: '#00f2fe', lineWidth: 2 }); m2 = c2.addLineSeries({ color: '#f59e0b', lineWidth: 1, lineStyle: 2 }); l2 = c2.addLineSeries({ color: '#00f2fe', lineWidth: 2 });

            r2 = LightweightCharts.createChart(document.getElementById('rsi-2'), { layout: { background: { color: '#111827' }, textColor: '#9ca3af' }, grid: { vertLines: { color: '#1f2937' }, horzLines: { color: '#1f2937' } }, timeScale: { visible: false } });
            rs2 = r2.addLineSeries({ color: '#a855f7', lineWidth: 2 });
            rs2.createPriceLine({ price: 60.5, color: '#10b981', lineWidth: 1, lineStyle: 2 });
            rs2.createPriceLine({ price: 40.5, color: '#ef4444', lineWidth: 1, lineStyle: 2 });
        }

        async function loadDualCharts(sym) {
            currentSymbol = sym;
            document.getElementById('active-symbol-1').innerText = sym + '.P';
            document.getElementById('active-symbol-2').innerText = sym + '.P';
            const htf = currentMode === 'swing' ? '1d' : '1h';
            const ltf = currentMode === 'swing' ? '1h' : '15m';

            try {
                const res1 = await fetch(`/api/chart/${sym}/${htf}`);
                const d1 = await res1.json();
                if (d1.candles) { s1.setData(d1.candles); u1.setData(d1.kc_upper); m1.setData(d1.kc_mid); l1.setData(d1.kc_lower); rs1.setData(d1.rsi); }

                const res2 = await fetch(`/api/chart/${sym}/${ltf}`);
                const d2 = await res2.json();
                if (d2.candles) { s2.setData(d2.candles); u2.setData(d2.kc_upper); m2.setData(d2.kc_mid); l2.setData(d2.kc_lower); rs2.setData(d2.rsi); }
            } catch(e){}
        }

        function switchMode(mode) {
            currentMode = mode;
            document.getElementById('mode-swing').classList.toggle('active', mode === 'swing');
            document.getElementById('mode-intraday').classList.toggle('active', mode === 'intraday');
            loadDualCharts(currentSymbol);
            streamScanner();
        }

        function toggleAutoTrade() {
            autoTradeEnabled = !autoTradeEnabled;
            const btn = document.getElementById('auto-trade-toggle-btn');
            const badge = document.getElementById('auto-trade-badge');
            if (autoTradeEnabled) {
                btn.className = "btn btn-success btn-sm py-1"; btn.innerText = "Auto: ON";
                badge.className = "badge bg-success"; badge.innerText = "Auto: ON";
            } else {
                btn.className = "btn btn-outline-warning btn-sm py-1"; btn.innerText = "Auto Trade";
                badge.className = "badge bg-success"; badge.innerText = "Auto: OFF";
            }
        }

        async function fetchWallet() {
            try {
                const res = await fetch('/api/wallet');
                const d = await res.json();
                document.getElementById('live-wallet-balance').innerText = `$${d.balance_usd.toFixed(2)} (₹${d.balance_inr.toLocaleString()})`;
            } catch(e){}
        }

        async function fetchJournal() {
            try {
                const res = await fetch('/api/journal');
                const list = await res.json();
                const tbody = document.getElementById('journal-tbody');
                if (list.length === 0) { tbody.innerHTML = `<tr><td colspan="7" class="text-center text-secondary">No closed trades recorded yet.</td></tr>`; return; }
                let html = '';
                list.forEach(j => {
                    const col = j.pnl >= 0 ? 'text-success' : 'text-danger';
                    html += `<tr><td>${j.exit_time}</td><td><b>${j.symbol}</b></td><td>${j.side}</td><td>${j.qty}</td><td>$${j.entry}</td><td>$${j.exit}</td><td class="fw-bold ${col}">$${j.pnl.toFixed(2)} (₹${Math.round(j.pnl*88)})</td></tr>`;
                });
                tbody.innerHTML = html;
            } catch(e){}
        }

        async function fetchConfig() {
            const res = await fetch('/api/config');
            const d = await res.json();
            document.getElementById('lbl-capital').innerText = "₹" + (d.capital_inr || 10000).toLocaleString();
            document.getElementById('lbl-risk').innerText = "₹" + (d.max_risk_inr || 500).toLocaleString();
            document.getElementById('cfg-capital').value = d.capital_inr || 10000;
            document.getElementById('cfg-risk').value = d.max_risk_inr || 500;
            document.getElementById('cfg-lev').value = Math.max(5, d.leverage || 5);
            document.getElementById('cfg-kcl').value = d.kc_length || 20;
            document.getElementById('cfg-kcm').value = d.kc_mult || 1.0;
            document.getElementById('cfg-tg-token').value = d.telegram_token || '';
            document.getElementById('cfg-tg-chat').value = d.telegram_chat_id || '7687426054';
            document.getElementById('cfg-api-key').value = d.api_key || '';
            document.getElementById('cfg-api-secret').value = d.api_secret || '';
        }

        async function saveBotSettings() {
            await fetch('/api/config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ telegram_token: document.getElementById('cfg-tg-token').value, telegram_chat_id: document.getElementById('cfg-tg-chat').value }) });
            alert('Bot Saved!'); bootstrap.Modal.getInstance(document.getElementById('botModal')).hide();
        }

        async function saveSettings() {
            const payload = {
                capital_inr: Number(document.getElementById('cfg-capital').value),
                max_risk_inr: Number(document.getElementById('cfg-risk').value),
                leverage: Math.max(5, Number(document.getElementById('cfg-lev').value)),
                kc_length: Number(document.getElementById('cfg-kcl').value),
                kc_mult: Number(document.getElementById('cfg-kcm').value),
                api_key: document.getElementById('cfg-api-key').value,
                api_secret: document.getElementById('cfg-api-secret').value
            };
            await fetch('/api/config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
            fetchConfig(); streamScanner(); alert('Saved!'); bootstrap.Modal.getInstance(document.getElementById('settingsModal')).hide();
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

        async function closeActiveTrade() {
            await fetch('/api/close_active_trade', { method: 'POST' });
            checkTradeState();
            fetchJournal();
            alert('Trade exited and saved to Trading Journal!');
        }

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
                if (!data || data.length === 0) return;

                let html = '';
                data.forEach(c => {
                    latestScanMap[c.clean_sym] = c;
                    let bClass = c.signal === 'STRONG LONG' ? 'badge-long' : (c.signal === 'STRONG SHORT' ? 'badge-short' : 'badge-neutral');
                    const lock = hasActiveTrade ? 'disabled' : '';

                    if (autoTradeEnabled && !hasActiveTrade && (c.signal === 'STRONG LONG' || c.signal === 'STRONG SHORT')) {
                        placeOrder(c.clean_sym, c.signal === 'STRONG LONG' ? 'buy' : 'sell', c.lot_size, c.sl, c.target);
                    }

                    html += `
                        <tr>
                            <td class="coin-link" onclick="loadDualCharts('${c.clean_sym}')">${c.symbol}</td>
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
                document.getElementById('last-tick').innerText = new Date().toLocaleTimeString();
            } catch(e){}
        }

        initCharts(); fetchConfig(); checkTradeState(); fetchWallet(); fetchJournal();
        streamScanner().then(() => loadDualCharts('BTCUSD'));
        setInterval(() => { streamScanner(); checkTradeState(); fetchWallet(); }, 3000);
    </script>
</body>
</html>
"""

@app.route('/')
def home():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/wallet')
def api_wallet():
    return jsonify(fetch_delta_wallet_balance())

@app.route('/api/journal')
def api_journal():
    return jsonify(load_journal())

@app.route('/api/chart/<symbol>/<interval>')
def api_chart(symbol, interval):
    cfg = load_config()
    raw = fetch_delta_candles(symbol, interval, limit=35)
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
    now = time.time()
    if SCAN_CACHE[mode]["data"] and (now - SCAN_CACHE[mode]["ts"] < 10):
        return jsonify(SCAN_CACHE[mode]["data"])

    cfg = load_config()
    results = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(analyze_single_coin, sym, mode, float(cfg.get("max_risk_inr", 500))/USDT_INR_RATE, float(cfg.get("rr_ratio", 2.0)), max(5.0, float(cfg.get("leverage", 5.0))), int(cfg.get("kc_length", 20)), float(cfg.get("kc_mult", 1.0))) for sym in FUTURES_WATCHLIST]
        for f in futures:
            res = f.result()
            if res:
                results.append(res)
                if res['signal'] in ['STRONG LONG', 'STRONG SHORT']:
                    send_telegram_alert(f"🚨 *Delta Alert*: {res['symbol']} ({res['mode']}) -> *{res['signal']}* @ ${res['price']}")
    
    SCAN_CACHE[mode] = {"ts": now, "data": results}
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
    state = load_trade_state()
    if state.get("active_trade"):
        t = state["active_trade"]
        candles = fetch_delta_candles(t['clean_sym'], '1h', 5)
        exit_price = float(candles[-1]['close']) if candles else t['entry_price']
        diff = exit_price - t['entry_price'] if t['side'] == 'BUY' else t['entry_price'] - exit_price
        pnl_usd = diff * t['qty']
        
        journal_entry = {
            "symbol": t['symbol'],
            "side": t['side'],
            "qty": t['qty'],
            "entry": t['entry_price'],
            "exit": exit_price,
            "pnl": pnl_usd,
            "exit_time": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        save_to_journal(journal_entry)
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
    port = int(os.environ.com("PORT", 5050) if "PORT" in os.environ else 5050)
    app.run(host='0.0.0.0', port=port, debug=False)
