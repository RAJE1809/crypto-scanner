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

FUTURES_WATCHLIST = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'GOLD', 'CRUDE']
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
    journal.insert(0, trade_record)
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
        return {"balance_usd": 113.63, "balance_inr": 10000}
    try:
        timestamp = int(time.time())
        endpoint = "/v2/wallet/balances"
        signature = hmac.new(api_secret.encode(), f"GET{timestamp}{endpoint}".encode(), hashlib.sha256).hexdigest()
        headers = {"api-key": api_key, "signature": signature, "timestamp": str(timestamp)}
        res = requests.get("https://api.india.delta.exchange" + endpoint, headers=headers, timeout=4)
        if res.status_code == 200:
            wallets = res.json().get("result", [])
            for w in wallets:
                if w.get("asset_symbol") in ["USDT", "USD"]:
                    bal = float(w.get("balance", 0))
                    return {"balance_usd": bal, "balance_inr": round(bal * USDT_INR_RATE, 2)}
    except Exception:
        pass
    return {"balance_usd": 0.0, "balance_inr": 0.0}

def fetch_binance_spot_candles(symbol, resolution, limit=35):
    spot_sym = symbol if 'USDT' in symbol else symbol.replace('USD', 'USDT')
    if 'GOLD' in symbol or 'CRUDE' in symbol:
        spot_sym = symbol
    url = f"https://api.binance.com/api/v3/klines?symbol={spot_sym}&interval={resolution}&limit={limit}"
    try:
        res = requests.get(url, timeout=3.0)
        if res.status_code == 200:
            data = res.json()
            candles = []
            for k in data:
                candles.append({'time': int(k[0]/1000), 'open': float(k[1]), 'high': float(k[2]), 'low': float(k[3]), 'close': float(k[4]), 'volume': float(k[5])})
            return candles
    except Exception:
        pass
    return []

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
    return []

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

    htf_raw = fetch_binance_spot_candles(sym, htf_tf, 30)
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
    clean_name = sym.replace('USDT', '').replace('USD', '')
    return {
        'symbol': clean_name + 'USDT', 'clean_sym': sym, 'mode': mode.upper(),
        'price': curr_price, 'htf_rsi': round(htf_rsi, 2), 'ltf_rsi': round(ltf_rsi, 2),
        'kc_u': round(ltf_kcu[-1], 4), 'kc_l': round(ltf_kcl[-1], 4), 'vol': recent_vol,
        'signal': signal, 'sl': sl_price, 'target': target_price, 'lot_size': calc_qty, 'margin_inr': round(pos_val_inr / max(1.0, leverage))
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
    <title>Pro Trading Terminal with Binance Charts</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
    <style>
        body { background-color: #070b14; color: #f1f5f9; font-family: 'Inter', sans-serif; font-size: 13px; }
        .card-custom { background-color: #111827; border: 1px solid #1f2937; border-radius: 8px; }
        .table-dark { background-color: #111827; --bs-table-bg: #111827; }
        .badge-long { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid #10b981; font-weight: 700; }
        .badge-short { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid #ef4444; font-weight: 700; }
        .coin-link { cursor: pointer; color: #38bdf8; font-weight: bold; text-decoration: underline; }
        .tv-chart-box { height: 380px; width: 100%; border-radius: 6px; overflow: hidden; }
        .modal-content { background-color: #111827; color: #fff; border: 1px solid #374151; }
        .form-control, .form-select { background-color: #070b14; border: 1px solid #374151; color: #fff; }
    </style>
</head>
<body class="p-2">
    <div class="container-fluid">
        <div class="card-custom p-2 px-3 mb-2 d-flex flex-wrap justify-content-between align-items-center">
            <div class="d-flex align-items-center gap-3">
                <h5 class="fw-bold text-info mb-0">⚡ BINANCE DUAL-CHART TERMINAL</h5>
                <div class="btn-group btn-group-sm">
                    <button class="btn btn-outline-info active" id="mode-swing" onclick="switchMode('swing')">Swing (1D+1H)</button>
                    <button class="btn btn-outline-info" id="mode-intraday" onclick="switchMode('intraday')">Intraday (1H+15m)</button>
                </div>
            </div>
            <div class="d-flex align-items-center gap-3">
                <span class="badge bg-dark border border-secondary p-2">Wallet: <b class="text-warning" id="live-wallet-balance">Loading...</b></span>
                <span class="badge bg-success" id="auto-trade-badge">Auto: OFF</span>
                <button class="btn btn-outline-warning btn-sm py-1" onclick="toggleAutoTrade()" id="auto-trade-toggle-btn">Auto Trade</button>
                <button class="btn btn-primary btn-sm py-1 fw-bold" data-bs-toggle="modal" data-bs-target="#botModal">🤖 Bot</button>
                <button class="btn btn-info btn-sm py-1 fw-bold" data-bs-toggle="modal" data-bs-target="#settingsModal">⚙ Setup</button>
                <button class="btn btn-secondary btn-sm py-1 fw-bold" data-bs-toggle="modal" data-bs-target="#journalModal">📖 Journal</button>
            </div>
        </div>

        <div id="active-trade-container" class="mb-2 d-none">
            <div class="p-2 px-3 rounded d-flex justify-content-between align-items-center" style="background: rgba(16,185,129,0.15); border: 1px solid #10b981;">
                <div><span class="badge bg-success me-2">LIVE POSITION</span><span class="fw-bold text-white" id="at-details">...</span></div>
                <div><span class="fw-bold fs-6 me-3 text-success" id="live-pnl-display">P&L: $0.00</span><button class="btn btn-sm btn-danger fw-bold py-0" onclick="closeActiveTrade()">Exit Position</button></div>
            </div>
        </div>

        <!-- TRADINGVIEW BINANCE DUAL CHARTS -->
        <div class="row g-2 mb-2">
            <div class="col-md-6">
                <div class="card-custom p-2">
                    <div class="d-flex justify-content-between align-items-center px-1 mb-1">
                        <span class="fw-bold text-warning" id="chart1-title">HTF Chart (Binance)</span>
                        <span class="badge bg-secondary" id="active-symbol-1">BINANCE:BTCUSDT</span>
                    </div>
                    <div id="tv-chart-1" class="tv-chart-box"></div>
                </div>
            </div>
            <div class="col-md-6">
                <div class="card-custom p-2">
                    <div class="d-flex justify-content-between align-items-center px-1 mb-1">
                        <span class="fw-bold text-info" id="chart2-title">LTF Chart (Binance)</span>
                        <span class="badge bg-secondary" id="active-symbol-2">BINANCE:BTCUSDT</span>
                    </div>
                    <div id="tv-chart-2" class="tv-chart-box"></div>
                </div>
            </div>
        </div>

        <div class="card-custom p-2">
            <div class="d-flex justify-content-between align-items-center px-2 mb-2">
                <span class="fw-bold text-info">📋 LIVE FUTURES SCANNER (Click symbol to load Binance Charts)</span>
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
                        <tr><td colspan="12" class="text-center py-3 text-info">Loading Scanner...</td></tr>
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
                    <h6 class="modal-title fw-bold text-info">📖 Trading Journal & History</h6>
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

    <div class="modal fade" id="botModal" tabindex="-1"><div class="modal-dialog"><div class="modal-content"><div class="modal-header border-secondary"><h6 class="modal-title fw-bold text-info">🤖 Telegram Bot</h6><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div><div class="modal-body"><div class="mb-2"><label class="form-label small">Bot Token</label><input type="text" class="form-control" id="cfg-tg-token"></div><div class="mb-2"><label class="form-label small">Chat ID</label><input type="text" class="form-control" id="cfg-tg-chat" value="7687426054"></div></div><div class="modal-footer border-secondary"><button type="button" class="btn btn-primary w-100 fw-bold" onclick="saveBotSettings()">Save</button></div></div></div></div>
    <div class="modal fade" id="settingsModal" tabindex="-1"><div class="modal-dialog"><div class="modal-content"><div class="modal-header border-secondary"><h6 class="modal-title fw-bold text-info">⚙ Setup</h6><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div><div class="modal-body"><div class="row g-2 mb-2"><div class="col-6"><label class="form-label small">KC Length</label><input type="number" class="form-control" id="cfg-kcl" value="20"></div><div class="col-6"><label class="form-label small">KC Mult</label><input type="number" step="0.1" class="form-control" id="cfg-kcm" value="1.0"></div><div class="col-6"><label class="form-label small">Capital (₹)</label><input type="number" class="form-control" id="cfg-capital" value="10000"></div><div class="col-6"><label class="form-label small">Risk (₹)</label><input type="number" class="form-control" id="cfg-risk" value="500"></div><div class="col-6"><label class="form-label small">Leverage</label><input type="number" class="form-control" id="cfg-lev" value="5"></div><div class="col-6"><label class="form-label small">R:R</label><select class="form-select" id="cfg-rr"><option value="2.0">1 : 2</option></select></div></div><div class="mb-2"><label class="form-label small">Delta API Key</label><input type="text" class="form-control" id="cfg-api-key"></div><div class="mb-2"><label class="form-label small">Delta API Secret</label><input type="password" class="form-control" id="cfg-api-secret"></div></div><div class="modal-footer border-secondary"><button type="button" class="btn btn-info w-100 fw-bold" onclick="saveSettings()">Save</button></div></div></div></div>

    <script>
        let currentSymbol = 'BTCUSDT';
        let currentMode = 'swing';
        let hasActiveTrade = false;
        let autoTradeEnabled = false;
        let latestScanMap = {};
        let tvWidget1 = null, tvWidget2 = null;

        function loadTradingViewDualCharts(sym) {
            currentSymbol = sym;
            let binanceSym = "BINANCE:" + sym;
            if (sym === 'GOLD') binanceSym = "OANDA:XAUUSD";
            else if (sym === 'CRUDE') binanceSym = "TVC:USOIL";

            document.getElementById('active-symbol-1').innerText = binanceSym;
            document.getElementById('active-symbol-2').innerText = binanceSym;

            const htfInterval = currentMode === 'swing' ? 'D' : '60';
            const ltfInterval = currentMode === 'swing' ? '60' : '15';

            document.getElementById('chart1-title').innerText = `HTF Chart (${currentMode === 'swing' ? 'Daily' : '1H'})`;
            document.getElementById('chart2-title').innerText = `LTF Chart (${currentMode === 'swing' ? '1H' : '15m'})`;

            document.getElementById('tv-chart-1').innerHTML = '';
            document.getElementById('tv-chart-2').innerHTML = '';

            tvWidget1 = new TradingView.widget({
                "autosize": true,
                "symbol": binanceSym,
                "interval": htfInterval,
                "timezone": "Asia/Kolkata",
                "theme": "dark",
                "style": "1",
                "locale": "en",
                "toolbar_bg": "#111827",
                "enable_publishing": false,
                "hide_side_toolbar": false,
                "container_id": "tv-chart-1"
            });

            tvWidget2 = new TradingView.widget({
                "autosize": true,
                "symbol": binanceSym,
                "interval": ltfInterval,
                "timezone": "Asia/Kolkata",
                "theme": "dark",
                "style": "1",
                "locale": "en",
                "toolbar_bg": "#111827",
                "enable_publishing": false,
                "hide_side_toolbar": false,
                "container_id": "tv-chart-2"
            });
        }

        function switchMode(mode) {
            currentMode = mode;
            document.getElementById('mode-swing').classList.toggle('active', mode === 'swing');
            document.getElementById('mode-intraday').classList.toggle('active', mode === 'intraday');
            loadTradingViewDualCharts(currentSymbol);
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
                btn.className = "btn btn-outline-warning btn-sm py-1"; btn.innerText = "
