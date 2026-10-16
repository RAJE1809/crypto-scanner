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

FUTURES_WATCHLIST = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'DOGEUSDT', 'SUIUSDT', 'LINKUSDT', 'AVAXUSDT', 'PAXGUSDT']
USDT_INR_RATE = 88.0

MARKET_CACHE = {}
SCAN_CACHE = {"swing": {"ts": 0, "data": []}, "intraday": {"ts": 0, "data": []}}
ALERT_COOLDOWN = {}

def clean_old_memory():
    now = time.time()
    expired = [k for k, v in MARKET_CACHE.items() if now - v['ts'] > 30]
    for k in expired:
        del MARKET_CACHE[k]
    gc.collect()

def load_config():
    default_cfg = {
        "broker": "delta",
        "api_key": "",
        "api_secret": "",
        "telegram_token": "8847204764:AAEopmSPVSusOSy8tbDBINE6FGOv1hWn7iE",
        "telegram_chat_id": "7687426054",
        "capital_inr": 10000,
        "max_risk_inr": 500,
        "rr_ratio": 2.0,
        "leverage": 5,
        "kc_length": 20,
        "kc_mult": 1.0,
        "min_volume_spike": 0,   # Custom Filter: Volume Spike %
        "min_rsi_filter": 0,     # Custom Filter: Min RSI
        "auto_trade": False
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                saved = json.load(f)
                default_cfg.update(saved)
        except Exception:
            pass
    return default_cfg

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
        json.dump(journal[:50], f, indent=4)

def send_telegram_alert(message):
    cfg = load_config()
    token = cfg.get("telegram_token", "").strip()
    chat_id = cfg.get("telegram_chat_id", "").strip()
    if not token or not chat_id: return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}, timeout=2)
    except Exception:
        pass

def fetch_multi_exchange_candles(symbol, resolution, limit=35):
    """Multi-Exchange Data Feed: Binance Public API + Delta Exchange Fallback"""
    cache_key = f"{symbol}_{resolution}"
    now = int(time.time())
    if cache_key in MARKET_CACHE and (now - MARKET_CACHE[cache_key]['ts'] < 8):
        return MARKET_CACHE[cache_key]['data']

    # 1. Try Binance Feed First (Fastest & Deepest Liquidity Data)
    try:
        url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={resolution}&limit={limit}"
        res = requests.get(url, timeout=2.0)
        if res.status_code == 200:
            raw = res.json()
            candles = [{'time': int(k[0]/1000), 'open': float(k[1]), 'high': float(k[2]), 'low': float(k[3]), 'close': float(k[4]), 'volume': float(k[5])} for k in raw]
            MARKET_CACHE[cache_key] = {'ts': now, 'data': candles}
            return candles
    except Exception:
        pass

    # 2. Fallback to Delta Exchange API
    sec_map = {'15m': 900, '1h': 3600, '4h': 14400, '1d': 86400}
    step = sec_map.get(resolution, 3600)
    start_time = now - (step * (limit + 4))
    delta_sym = symbol.replace('USDT', 'USD') if 'PAXG' not in symbol else 'GOLD'

    try:
        params = {"resolution": resolution, "symbol": delta_sym, "start": start_time, "end": now}
        res = requests.get("https://api.india.delta.exchange/v2/history/candles", params=params, timeout=2.5)
        if res.status_code == 200:
            raw_res = res.json().get("result", [])
            if raw_res:
                candles = [{'time': int(c['time']), 'open': float(c['open']), 'high': float(c['high']), 'low': float(c['low']), 'close': float(c['close']), 'volume': float(c.get('volume', 0))} for c in raw_res]
                MARKET_CACHE[cache_key] = {'ts': now, 'data': candles}
                return candles
    except Exception:
        pass
    return []

def calculate_rsi(closes, period=14):
    if len(closes) <= period:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    return 100.0 if avg_loss == 0 else round(100 - (100 / (1 + (avg_gain / avg_loss))), 2)

def calculate_keltner(closes, highs, lows, length=20, mult=1.0):
    k_mult = 2 / (length + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = (price - ema) * k_mult + ema
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
    atr = sum(trs[-length:]) / min(len(trs), length)
    return round(ema + (mult * atr), 4), round(ema - (mult * atr), 4)

def analyze_coin(sym, mode, cfg):
    htf_tf = '1d' if mode == 'swing' else '1h'
    ltf_tf = '1h' if mode == 'swing' else '15m'

    htf_raw = fetch_multi_exchange_candles(sym, htf_tf, 30)
    ltf_raw = fetch_multi_exchange_candles(sym, ltf_tf, 30)
    if not htf_raw or not ltf_raw:
        return None

    h_close = [float(k['close']) for k in htf_raw]
    h_high = [float(k['high']) for k in htf_raw]
    h_low = [float(k['low']) for k in htf_raw]
    l_close = [float(k['close']) for k in ltf_raw]
    l_high = [float(k['high']) for k in ltf_raw]
    l_low = [float(k['low']) for k in ltf_raw]
    l_vol = [float(k.get('volume', 0)) for k in ltf_raw]

    kc_len = int(cfg.get("kc_length", 20))
    kc_m = float(cfg.get("kc_mult", 1.0))
    risk_usdt = float(cfg.get("max_risk_inr", 500)) / USDT_INR_RATE
    rr_ratio = float(cfg.get("rr_ratio", 2.0))
    leverage = max(5.0, float(cfg.get("leverage", 5.0)))

    htf_rsi = calculate_rsi(h_close, 14)
    htf_kcu, htf_kcl = calculate_keltner(h_close, h_high, h_low, kc_len, kc_m)
    ltf_rsi = calculate_rsi(l_close, 14)
    ltf_kcu, ltf_kcl = calculate_keltner(l_close, l_high, l_low, kc_len, kc_m)

    curr_price = l_close[-1]
    recent_vol = sum(l_vol[-5:])
    avg_vol_24h = (sum(l_vol) / len(l_vol)) * 5 if l_vol else 1
    vol_spike_pct = round(((recent_vol - avg_vol_24h) / avg_vol_24h) * 100, 1) if avg_vol_24h > 0 else 0

    htf_long = (htf_rsi > 60.50) and (h_close[-1] > htf_kcu)
    ltf_long = (ltf_rsi > 60.50) and (curr_price > ltf_kcu)
    htf_short = (htf_rsi < 40.50) and (h_close[-1] < htf_kcl)
    ltf_short = (ltf_rsi < 40.50) and (curr_price < ltf_kcl)

    signal = "NEUTRAL"
    sl_price = round(ltf_kcl * 0.998, 4)
    per_unit_risk = abs(curr_price - sl_price) or (curr_price * 0.005)
    calc_qty = round(risk_usdt / per_unit_risk, 3)
    target_price = round(curr_price + (per_unit_risk * rr_ratio), 4)

    if htf_long and ltf_long:
        signal = "STRONG LONG"
    elif htf_short and ltf_short:
        signal = "STRONG SHORT"
        sl_price = round(ltf_kcu * 1.002, 4)
        per_unit_risk = abs(sl_price - curr_price) or (curr_price * 0.005)
        calc_qty = round(risk_usdt / per_unit_risk, 3)
        target_price = round(curr_price - (per_unit_risk * rr_ratio), 4)

    # Apply Custom Filters from settings
    min_vol = float(cfg.get("min_volume_spike", 0))
    min_rsi = float(cfg.get("min_rsi_filter", 0))
    if vol_spike_pct < min_vol or ltf_rsi < min_rsi:
        if signal != "NEUTRAL":
            signal = "FILTERED"

    return {
        'symbol': sym,
        'price': curr_price,
        'htf_rsi': htf_rsi,
        'ltf_rsi': ltf_rsi,
        'kc_u': ltf_kcu,
        'kc_l': ltf_kcl,
        'vol_spike': vol_spike_pct,
        'signal': signal,
        'sl': sl_price,
        'target': target_price,
        'lot_size': calc_qty,
        'margin_inr': round((calc_qty * curr_price * USDT_INR_RATE) / leverage)
    }

def execute_broker_order(symbol, side, size):
    cfg = load_config()
    api_key = cfg.get("api_key", "").strip()
    api_secret = cfg.get("api_secret", "").strip()
    if not api_key or not api_secret:
        return {"status": "simulated", "message": "Demo Mode: Order Executed & Locked!"}
    try:
        timestamp = int(time.time())
        endpoint = "/v2/orders"
        body = {
            "product_symbol": symbol.replace('USDT', 'USD'),
            "size": max(1, int(float(size))),
            "side": side.lower(),
            "order_type": "market_order"
        }
        payload = json.dumps(body)
        signature = hmac.new(api_secret.encode(), f"POST{timestamp}{endpoint}{payload}".encode(), hashlib.sha256).hexdigest()
        headers = {"api-key": api_key, "signature": signature, "timestamp": str(timestamp), "Content-Type": "application/json"}
        res = requests.post("https://api.india.delta.exchange" + endpoint, data=payload, headers=headers, timeout=4)
        return {"status": "success", "message": f"Delta Live Order Placed: {res.text}"}
    except Exception as e:
        return {"status": "error", "message": f"Delta Error: {str(e)}"}

def place_system_trade(symbol, side, qty, price, sl, target, source="Dashboard"):
    state = load_trade_state()
    if state.get("active_trade"):
        return {"status": "error", "message": "1 Active Trade pehle se chalu hai!"}

    broker_res = execute_broker_order(symbol, side, qty)
    new_trade = {
        "symbol": symbol,
        "side": side.upper(),
        "qty": float(qty),
        "entry_price": float(price),
        "entry_time": time.strftime("%H:%M:%S"),
        "sl": float(sl),
        "target": float(target),
        "source": source
    }
    save_trade_state({"active_trade": new_trade})
    send_telegram_alert(
        f"✅ *TRADE EXECUTED ({source})*\n"
        f"📌 Coin: `{symbol}`\n"
        f"📈 Side: *{side.upper()}*\n"
        f"💰 Entry Price: `${price}`\n"
        f"📦 Auto Lot Size: `{qty}`\n"
        f"🛑 Buffer SL: `${sl}`\n"
        f"🎯 Target (1:2): `${target}`"
    )
    return {"status": "success", "message": broker_res.get("message", "Order Placed!"), "trade": new_trade}

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pro Crypto Scanner & Terminal</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
    <style>
        body { background-color: #0b1120; color: #f8fafc; font-family: system-ui, sans-serif; font-size: 13px; }
        .box { background-color: #151f32; border: 1px solid #263554; border-radius: 10px; }
        .table-dark { background-color: #151f32; --bs-table-bg: #151f32; }
        .sig-long { background-color: #10b981; color: #fff; padding: 4px 10px; border-radius: 6px; font-weight: 700; display: inline-block; }
        .sig-short { background-color: #ef4444; color: #fff; padding: 4px 10px; border-radius: 6px; font-weight: 700; display: inline-block; }
        .sig-neutral { background-color: #334155; color: #cbd5e1; padding: 4px 10px; border-radius: 6px; display: inline-block; }
        .coin-btn { cursor: pointer; color: #38bdf8; font-weight: 700; text-decoration: underline; }
        .tv-box { height: 320px; width: 100%; border-radius: 8px; overflow: hidden; }
        .modal-content { background-color: #1e293b; color: #fff; border: 1px solid #475569; }
        .form-control, .form-select { background-color: #0f172a; border: 1px solid #334155; color: #fff; }
    </style>
</head>
<body class="p-2 p-md-3">
    <div class="container-fluid">
        <!-- TOP BAR WITH DIRECT BUTTONS -->
        <div class="box p-2 px-3 mb-3 d-flex flex-wrap justify-content-between align-items-center gap-2">
            <div>
                <h5 class="fw-bold text-info mb-0">⚡ Multi-Exchange Crypto Scanner</h5>
                <div class="small text-secondary">
                    Capital: <b class="text-white" id="lbl-cap">₹10,000</b> | 
                    Risk: <b class="text-warning" id="lbl-risk">₹500</b> | 
                    Wallet: <b class="text-success" id="lbl-wallet">₹10,000</b>
                </div>
            </div>

            <div class="d-flex flex-wrap align-items-center gap-2">
                <button class="btn btn-warning btn-sm fw-bold" data-bs-toggle="modal" data-bs-target="#brokerModal" id="btn-broker-status">
                    🔌 Connect Broker
                </button>
                <button class="btn btn-primary btn-sm fw-bold" data-bs-toggle="modal" data-bs-target="#botModal">
                    🤖 Trading Bot & Alerts
                </button>
                <button class="btn btn-info btn-sm fw-bold" data-bs-toggle="modal" data-bs-target="#filterModal">
                    🎛 Custom Filters
                </button>
                <button class="btn btn-outline-success btn-sm fw-bold" id="btn-autotrade" onclick="toggleAutoTrade()">
                    ⚡ Auto-Trade: OFF
                </button>
                <button class="btn btn-outline-light btn-sm" data-bs-toggle="modal" data-bs-target="#journalModal">
                    📖 Journal
                </button>
            </div>
        </div>

        <!-- ACTIVE TRADE BAR -->
        <div id="active-trade-bar" class="box p-2 px-3 mb-3 d-none" style="border-color: #10b981; background: rgba(16, 185, 129, 0.12);">
            <div class="d-flex flex-wrap justify-content-between align-items-center">
                <div>
                    <span class="badge bg-success me-2">LIVE POSITION</span>
                    <span class="fw-bold" id="at-info"></span>
                </div>
                <div class="d-flex align-items-center gap-3 mt-1 mt-md-0">
                    <span class="fs-6 fw-bold" id="at-pnl">P&L: ₹0</span>
                    <button class="btn btn-danger btn-sm fw-bold py-0" onclick="exitTrade()">Exit Trade</button>
                </div>
            </div>
        </div>

        <!-- SIMPLE SCANNER TABLE -->
        <div class="box p-3 mb-3">
            <div class="d-flex flex-wrap justify-content-between align-items-center mb-2">
                <div class="d-flex align-items-center gap-2">
                    <h6 class="fw-bold text-warning mb-0">📋 Multi-Exchange Live Scanner</h6>
                    <div class="btn-group btn-group-sm ms-2">
                        <button class="btn btn-outline-info active" id="tab-swing" onclick="changeMode('swing')">Swing (1D + 1H)</button>
                        <button class="btn btn-outline-info" id="tab-intraday" onclick="changeMode('intraday')">Intraday (1H + 15m)</button>
                    </div>
                </div>
                <span class="small text-secondary" id="last-update">Updating live...</span>
            </div>

            <div class="table-responsive">
                <table class="table table-dark table-hover align-middle mb-0">
                    <thead>
                        <tr class="text-secondary small">
                            <th>Coin (Click Chart)</th>
                            <th>Live Price</th>
                            <th id="col-htf">Daily RSI</th>
                            <th id="col-ltf">1H RSI</th>
                            <th>Vol Spike %</th>
                            <th>Setup Signal</th>
                            <th>Buffer SL</th>
                            <th>1:2 Target</th>
                            <th class="text-warning">Auto Lot</th>
                            <th>Quick Trade</th>
                        </tr>
                    </thead>
                    <tbody id="scanner-tbody">
                        <tr><td colspan="10" class="text-center py-4 text-info">Scanning multi-exchange feeds...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- DUAL BINANCE/TRADINGVIEW CHARTS -->
        <div class="row g-2">
            <div class="col-md-6">
                <div class="box p-2">
                    <div class="small fw-bold text-warning mb-1" id="title-c1">HTF Chart</div>
                    <div id="tv-c1" class="tv-box"></div>
                </div>
            </div>
            <div class="col-md-6">
                <div class="box p-2">
                    <div class="small fw-bold text-info mb-1" id="title-c2">LTF Chart</div>
                    <div id="tv-c2" class="tv-box"></div>
                </div>
            </div>
        </div>
    </div>

    <!-- 1. BROKER MODAL -->
    <div class="modal fade" id="brokerModal" tabindex="-1">
        <div class="modal-dialog">
            <div class="modal-content">
                <div class="modal-header border-secondary">
                    <h6 class="modal-title fw-bold text-warning">🔌 Connect Broker (Delta Exchange)</h6>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <div class="mb-2"><label class="form-label small">Delta API Key</label><input type="text" class="form-control" id="cfg-key"></div>
                    <div class="mb-3"><label class="form-label small">Delta API Secret</label><input type="password" class="form-control" id="cfg-secret"></div>
                    <hr class="border-secondary">
                    <div class="row g-2">
                        <div class="col-6"><label class="small">Capital (₹)</label><input type="number" class="form-control" id="cfg-cap" value="10000"></div>
                        <div class="col-6"><label class="small">Risk per Trade (₹)</label><input type="number" class="form-control" id="cfg-risk" value="500"></div>
                    </div>
                </div>
                <div class="modal-footer border-secondary"><button class="btn btn-warning w-100 fw-bold" onclick="saveAllConfig()">Save & Connect</button></div>
            </div>
        </div>
    </div>

    <!-- 2. BOT MODAL -->
    <div class="modal fade" id="botModal" tabindex="-1">
        <div class="modal-dialog">
            <div class="modal-content">
                <div class="modal-header border-secondary">
                    <h6 class="modal-title fw-bold text-info">🤖 Telegram Trading Bot & Alerts</h6>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <div class="mb-2"><label class="form-label small">Bot Token</label><input type="text" class="form-control" id="cfg-token"></div>
                    <div class="mb-3"><label class="form-label small">Chat ID</label><input type="text" class="form-control" id="cfg-chat" value="7687426054"></div>
                    <button class="btn btn-outline-info w-100 mb-2 fw-bold" onclick="sendTestMobileAlert()">📲 Send Test Alert to Phone</button>
                </div>
                <div class="modal-footer border-secondary"><button class="btn btn-primary w-100 fw-bold" onclick="saveAllConfig()">Save Bot</button></div>
            </div>
        </div>
    </div>

    <!-- 3. CUSTOM FILTER BUILDER MODAL -->
    <div class="modal fade" id="filterModal" tabindex="-1">
        <div class="modal-dialog">
            <div class="modal-content">
                <div class="modal-header border-secondary">
                    <h6 class="modal-title fw-bold text-info">🎛 Custom Filter Builder</h6>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <div class="mb-3">
                        <label class="form-label small">Minimum Volume Spike % (e.g. 50% above avg)</label>
                        <input type="number" class="form-control" id="cfg-minvol" value="0">
                    </div>
                    <div class="mb-3">
                        <label class="form-label small">Minimum RSI Threshold</label>
                        <input type="number" class="form-control" id="cfg-minrsi" value="0">
                    </div>
                    <small class="text-secondary">Coins jo in conditions ko match nahi karenge, unhe scanner hide kar dega.</small>
                </div>
                <div class="modal-footer border-secondary"><button class="btn btn-info w-100 fw-bold" onclick="saveAllConfig()">Apply Filters</button></div>
            </div>
        </div>
    </div>

    <!-- 4. JOURNAL MODAL -->
    <div class="modal fade" id="journalModal" tabindex="-1">
        <div class="modal-dialog modal-lg">
            <div class="modal-content">
                <div class="modal-header border-secondary">
                    <h6 class="modal-title fw-bold text-info">📖 Trading Journal</h6>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <table class="table table-dark table-sm small">
                        <thead><tr><th>Time</th><th>Coin</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>P&L</th></tr></thead>
                        <tbody id="journal-tbody"></tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <script>
        let currentMode = 'swing';
        let currentSymbol = 'BTCUSDT';
        let hasActiveTrade = false;
        let autoTrade = false;
        let scanMap = {};

        function loadCharts(sym) {
            currentSymbol = sym;
            let tvSym = "BINANCE:" + sym;
            const htf = currentMode === 'swing' ? 'D' : '60';
            const ltf = currentMode === 'swing' ? '60' : '15';

            document.getElementById('title-c1').innerText = `HTF Chart (${htf}): ${tvSym}`;
            document.getElementById('title-c2').innerText = `LTF Chart (${ltf}): ${tvSym}`;
            document.getElementById('tv-c1').innerHTML = '';
            document.getElementById('tv-c2').innerHTML = '';

            new TradingView.widget({
                "autosize": true, "symbol": tvSym, "interval": htf, "timezone": "Asia/Kolkata", "theme": "dark", "style": "1", "container_id": "tv-c1",
                "studies": ["KeltnerChannels@tv-basicstudies", "RSI@tv-basicstudies"]
            });
            new TradingView.widget({
                "autosize": true, "symbol": tvSym, "interval": ltf, "timezone": "Asia/Kolkata", "theme": "dark", "style": "1", "container_id": "tv-c2",
                "studies": ["KeltnerChannels@tv-basicstudies", "RSI@tv-basicstudies"]
            });
        }

        function changeMode(m) {
            currentMode = m;
            document.getElementById('tab-swing').classList.toggle('active', m === 'swing');
            document.getElementById('tab-intraday').classList.toggle('active', m === 'intraday');
            document.getElementById('col-htf').innerText = m === 'swing' ? 'Daily RSI' : '1H RSI';
            document.getElementById('col-ltf').innerText = m === 'swing' ? '1H RSI' : '15m RSI';
            loadCharts(currentSymbol);
            refreshScanner();
        }

        async function toggleAutoTrade() {
            autoTrade = !autoTrade;
            await fetch('/api/config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ auto_trade: autoTrade }) });
            updateAutoTradeUI();
        }

        function updateAutoTradeUI() {
            const btn = document.getElementById('btn-autotrade');
            if (autoTrade) {
                btn.className = "btn btn-success btn-sm fw-bold"; btn.innerText = "⚡ Auto-Trade: ON";
            } else {
                btn.className = "btn btn-outline-success btn-sm fw-bold"; btn.innerText = "⚡ Auto-Trade: OFF";
            }
        }

        async function loadConfigAndWallet() {
            try {
                const res = await fetch('/api/config');
                const c = await res.json();
                document.getElementById('lbl-cap').innerText = "₹" + (c.capital_inr || 10000);
                document.getElementById('lbl-risk').innerText = "₹" + (c.max_risk_inr || 500);
                document.getElementById('cfg-cap').value = c.capital_inr || 10000;
                document.getElementById('cfg-risk').value = c.max_risk_inr || 500;
                document.getElementById('cfg-key').value = c.api_key || '';
                document.getElementById('cfg-secret').value = c.api_secret || '';
                document.getElementById('cfg-token').value = c.telegram_token || '';
                document.getElementById('cfg-chat').value = c.telegram_chat_id || '';
                document.getElementById('cfg-minvol').value = c.min_volume_spike || 0;
                document.getElementById('cfg-minrsi').value = c.min_rsi_filter || 0;
                autoTrade = Boolean(c.auto_trade);
                updateAutoTradeUI();

                const wRes = await fetch('/api/wallet');
                const w = await wRes.json();
                document.getElementById('lbl-wallet').innerText = `₹${w.balance_inr.toLocaleString()} ($${w.balance_usd.toFixed(2)})`;
                if (w.connected) {
                    document.getElementById('btn-broker-status').className = "btn btn-success btn-sm fw-bold";
                    document.getElementById('btn-broker-status').innerText = "✅ Broker Connected";
                }
            } catch(e){}
        }

        async function saveAllConfig() {
            const payload = {
                capital_inr: Number(document.getElementById('cfg-cap').value),
                max_risk_inr: Number(document.getElementById('cfg-risk').value),
                api_key: document.getElementById('cfg-key').value,
                api_secret: document.getElementById('cfg-secret').value,
                telegram_token: document.getElementById('cfg-token').value,
                telegram_chat_id: document.getElementById('cfg-chat').value,
                min_volume_spike: Number(document.getElementById('cfg-minvol').value),
                min_rsi_filter: Number(document.getElementById('cfg-minrsi').value)
            };
            await fetch('/api/config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
            alert("Settings Saved Successfully!");
            loadConfigAndWallet();
            refreshScanner();
        }

        async function sendTestMobileAlert() {
            const res = await fetch('/api/test_telegram', { method: 'POST' });
            const d = await res.json();
            alert(d.message);
        }

        async function manualOrder(sym, side) {
            const c = scanMap[sym];
            if (!c) return;
            const res = await fetch('/api/order', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ symbol: sym, side: side, qty: c.lot_size, price: c.price, sl: c.sl, target: c.target })
            });
            const d = await res.json();
            alert(d.message);
            checkTradeAndJournal();
        }

        async function exitTrade() {
            await fetch('/api/close_active_trade', { method: 'POST' });
            checkTradeAndJournal();
        }

        async function checkTradeAndJournal() {
            try {
                const res = await fetch('/api/trade_state');
                const d = await res.json();
                const bar = document.getElementById('active-trade-bar');
                if (d.active_trade) {
                    hasActiveTrade = true;
                    bar.classList.remove('d-none');
                    const t = d.active_trade;
                    document.getElementById('at-info').innerText = `${t.symbol} (${t.side}) | Lot: ${t.qty} | Entry: $${t.entry_price} | SL: $${t.sl} | TP: $${t.target}`;
                    const liveP = scanMap[t.symbol]?.price || t.entry_price;
                    const diff = t.side === 'BUY' ? (liveP - t.entry_price) : (t.entry_price - liveP);
                    const pnlInr = Math.round(diff * t.qty * 88);
                    document.getElementById('at-pnl').innerText = `Live P&L: ₹${pnlInr} ($${(diff*t.qty).toFixed(2)})`;
                } else {
                    hasActiveTrade = false;
                    bar.classList.add('d-none');
                }

                const jRes = await fetch('/api/journal');
                const jList = await jRes.json();
                document.getElementById('journal-tbody').innerHTML = jList.map(j => 
                    `<tr><td>${j.exit_time}</td><td>${j.symbol}</td><td>${j.side}</td><td>${j.qty}</td><td>$${j.entry}</td><td>$${j.exit}</td><td class="${j.pnl>=0?'text-success':'text-danger'} fw-bold">$${j.pnl}</td></tr>`
                ).join('') || '<tr><td colspan="7" class="text-center">No closed trades yet</td></tr>';
            } catch(e){}
        }

        async function refreshScanner() {
            try {
                const res = await fetch(`/api/scan?mode=${currentMode}`);
                const data = await res.json();
                const tbody = document.getElementById('scanner-tbody');
                if (!data || !data.length) return;

                let html = '';
                data.forEach(c => {
                    scanMap[c.symbol] = c;
                    let sClass = c.signal === 'STRONG LONG' ? 'sig-long' : (c.signal === 'STRONG SHORT' ? 'sig-short' : 'sig-neutral');
                    if(c.signal === 'FILTERED') return;
                    let dis = hasActiveTrade ? 'disabled' : '';

                    html += `
                        <tr>
                            <td class="coin-btn" onclick="loadCharts('${c.symbol}')">${c.symbol}</td>
                            <td class="fw-bold">$${c.price}</td>
                            <td class="${c.htf_rsi>60.5?'text-success fw-bold':(c.htf_rsi<40.5?'text-danger fw-bold':'')}">${c.htf_rsi}</td>
                            <td class="${c.ltf_rsi>60.5?'text-success fw-bold':(c.ltf_rsi<40.5?'text-danger fw-bold':'')}">${c.ltf_rsi}</td>
                            <td>${c.vol_spike}%</td>
                            <td><span class="${sClass}">${c.signal}</span></td>
                            <td class="text-danger">$${c.sl}</td>
                            <td class="text-success">$${c.target}</td>
                            <td class="fw-bold text-warning">${c.lot_size}</td>
                            <td>
                                <button class="btn btn-sm btn-success py-0 px-2" ${dis} onclick="manualOrder('${c.symbol}','buy')">Buy</button>
                                <button class="btn btn-sm btn-danger py-0 px-2 ms-1" ${dis} onclick="manualOrder('${c.symbol}','sell')">Sell</button>
                            </td>
                        </tr>
                    `;
                });
                tbody.innerHTML = html;
                document.getElementById('last-update').innerText = "Live: " + new Date().toLocaleTimeString();
            } catch(e){}
        }

        loadConfigAndWallet();
        loadCharts('BTCUSDT');
        refreshScanner();
        checkTradeAndJournal();
        setInterval(() => { refreshScanner(); checkTradeAndJournal(); }, 2500);
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

@app.route('/api/test_telegram', methods=['POST'])
def api_test_telegram():
    ok = send_telegram_alert(
        "📲 *Mobile Connection Successful!*\n"
        "Multi-Exchange Scanner & Bot are fully connected to your phone."
    )
    if ok:
        return jsonify({"status": "ok", "message": "Test alert successfully sent to your Telegram mobile!"})
    return jsonify({"status": "error", "message": "Alert failed! Please open @KeltnerPulseBot on Telegram and press Start."})

@app.route('/api/scan')
def api_scan():
    mode = request.args.get('mode', 'swing').lower()
    now = time.time()
    if SCAN_CACHE[mode]["data"] and (now - SCAN_CACHE[mode]["ts"] < 5):
        return jsonify(SCAN_CACHE[mode]["data"])

    cfg = load_config()
    results = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(analyze_coin, sym, mode, cfg) for sym in FUTURES_WATCHLIST]
        for f in futures:
            res = f.result()
            if res:
                results.append(res)
                if res['signal'] in ['STRONG LONG', 'STRONG SHORT']:
                    cd_key = f"{res['symbol']}_{res['signal']}"
                    if now - ALERT_COOLDOWN.get(cd_key, 0) > 600:
                        ALERT_COOLDOWN[cd_key] = now
                        send_telegram_alert(
                            f"🚨 *STRATEGY SIGNAL ALERT ({res['mode']})*\n"
                            f"🪙 Coin: `{res['symbol']}`\n"
                            f"⚡ Signal: *{res['signal']}*\n"
                            f"💵 Price: `${res['price']}`\n"
                            f"📊 Vol Spike: `{res['vol_spike']}%`\n"
                            f"🛑 SL: `${res['sl']}` | 🎯 TP: `${res['target']}`"
                        )
                    if cfg.get("auto_trade") and not load_trade_state().get("active_trade"):
                        side = "buy" if res['signal'] == 'STRONG LONG' else "sell"
                        place_system_trade(res['symbol'], side, res['lot_size'], res['price'], res['sl'], res['target'], "Auto-Trade Bot")

    SCAN_CACHE[mode] = {"ts": now, "data": results}
    return jsonify(results)

@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    if request.method == 'POST':
        old_cfg = load_config()
        new_cfg = request.json
        for k, v in new_cfg.items():
            if v is not None and v != "":
                old_cfg[k] = v
        save_config(old_cfg)
        return jsonify({"status": "saved"})
    return jsonify(load_config())

@app.route('/api/trade_state', methods=['GET'])
def get_trade_state(): return jsonify(load_trade_state())

@app.route('/api/close_active_trade', methods=['POST'])
def close_trade():
    state = load_trade_state()
    if state.get("active_trade"):
        t = state["active_trade"]
        raw = fetch_multi_exchange_candles(t['symbol'], '1h', 3)
        exit_price = float(raw[-1]['close']) if raw else t['entry_price']
        diff = (exit_price - t['entry_price']) if t['side'] == 'BUY' else (t['entry_price'] - exit_price)
        pnl_usd = round(diff * t['qty'], 2)
        save_to_journal({
            "symbol": t['symbol'], "side": t['side'], "qty": t['qty'],
            "entry": t['entry_price'], "exit": exit_price, "pnl": pnl_usd,
            "exit_time": time.strftime("%Y-%m-%d %H:%M:%S")
        })
        save_trade_state({"active_trade": None})
    return jsonify({"status": "trade_cleared"})

@app.route('/api/order', methods=['POST'])
def execute_order():
    d = request.json
    res = place_system_trade(d.get('symbol'), d.get('side'), d.get('qty', 1), d.get('price', 0), d.get('sl', 0), d.get('target', 0), "Dashboard")
    return jsonify(res)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5050))
    app.run(host='0.0.0.0', port=port, debug=False)
