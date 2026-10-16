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

DEFAULT_WATCHLIST = ['BTCUSD', 'ETHUSD', 'XAUTUSD', 'SOLUSD', 'EVAAUSD', 'LABUSD', 'MUSD', 'XRPUSE', 'BLURUSD', 'HUSD']
USDT_INR_RATE = 88.0

MARKET_CACHE = {}
SCAN_CACHE = {"data": []}

def clean_old_memory():
    now = time.time()
    expired = [k for k, v in MARKET_CACHE.items() if now - v['ts'] > 30]
    for k in expired:
        del MARKET_CACHE[k]
    gc.collect()

def load_config():
    default_cfg = {
        "api_key": "",
        "api_secret": "",
        "telegram_token": "8847204764:AAEopmSPVSusOSy8tbDBINE6FGOv1hWn7iE",
        "telegram_chat_id": "7687426054",
        "capital_inr": 10000,
        "max_risk_inr": 500,
        "rr_ratio": 2.0,
        "leverage": 5,
        "rsi_period": 14,
        "ema_fast": 20,
        "ema_slow": 50,
        "timeframe": "1h",
        "watchlist": DEFAULT_WATCHLIST,
        "scanning_active": False
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

def send_telegram_alert(message):
    cfg = load_config()
    token = cfg.get("telegram_token", "").strip()
    chat_id = cfg.get("telegram_chat_id", "").strip()
    if not token or not chat_id: return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}, timeout=2)
    except Exception:
        pass

def fetch_candles(symbol, resolution, limit=60):
    cache_key = f"{symbol}_{resolution}"
    now = int(time.time())
    if cache_key in MARKET_CACHE and (now - MARKET_CACHE[cache_key]['ts'] < 5):
        return MARKET_CACHE[cache_key]['data']

    sec_map = {'15minute': 900, '1h': 3600, '4h': 14400, '1d': 86400, '15m': 900}
    step = sec_map.get(resolution, 3600)
    start_time = now - (step * (limit + 5))
    delta_sym = symbol.replace('USDT', 'USD') if 'GOLD' not in symbol else 'XAUTUSD'

    try:
        params = {"resolution": '1h' if '15' not in resolution else '15m', "symbol": delta_sym, "start": start_time, "end": now}
        res = requests.get("https://api.india.delta.exchange/v2/history/candles", params=params, timeout=2.5)
        if res.status_code == 200:
            candles = res.json().get("result", [])
            if candles and len(candles) > 10:
                candles = sorted(candles, key=lambda x: int(x['time']))
                formatted = [{'time': int(c['time']), 'open': float(c['open']), 'high': float(c['high']), 'low': float(c['low']), 'close': float(c['close']), 'volume': float(c.get('volume', 0))} for c in candles]
                MARKET_CACHE[cache_key] = {'ts': now, 'data': formatted[-limit:]}
                return formatted[-limit:]
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

def calculate_ema(series, period):
    if not series: return 0.0
    mult = 2 / (period + 1)
    ema = series[0]
    for price in series[1:]:
        ema = (price - ema) * mult + ema
    return round(ema, 4)

def analyze_coin(sym, cfg):
    tf = cfg.get("timeframe", "1h")
    raw = fetch_candles(sym, tf, 50)
    if not raw or len(raw) < 10:
        return None

    closes = [k['close'] for k in raw]
    opens = [k['open'] for k in raw]
    close = closes[-1]
    open_p = opens[-1]

    rsi_per = int(cfg.get("rsi_period", 14))
    ema_f_per = int(cfg.get("ema_fast", 20))
    ema_s_per = int(cfg.get("ema_slow", 50))

    rsi = calculate_rsi(closes, rsi_per)
    ema_fast = calculate_ema(closes, ema_f_per)
    ema_slow = calculate_ema(closes, ema_s_per)

    candle_col = "Green" if close >= open_p else "Red"
    momentum = "Bullish" if close > ema_fast else ("Bearish" if close < ema_fast else "Flat")
    trend = "Up" if ema_fast > ema_slow else "Down"

    signal = "NEUTRAL"
    if rsi > float(cfg.get("rsi_bull", 60)) and trend == "Up" and momentum == "Bullish":
        signal = "BUY"
    elif rsi < float(cfg.get("rsi_bear", 40)) and trend == "Down" and momentum == "Bearish":
        signal = "SELL"

    return {
        'symbol': sym,
        'close': close,
        'open': open_p,
        'rsi': rsi,
        'ema_fast': ema_fast,
        'ema_slow': ema_slow,
        'candle': candle_col,
        'momentum': momentum,
        'trend': trend,
        'signal': signal
    }

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Delta Exchange x Tradehull Scanner Algo</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <style>
        body { background-color: #060e18; color: #cbd5e1; font-family: 'Inter', sans-serif; font-size: 13px; }
        .top-nav { background-color: #0b1728; border-bottom: 1px solid #1e293b; padding: 10px 20px; display: flex; justify-content: space-between; align-items: center; }
        .panel { background-color: #0d1b2a; border: 1px solid #1b2a4a; border-radius: 8px; padding: 15px; margin-bottom: 15px; }
        .table-custom { background-color: #0d1b2a; color: #fff; font-size: 12px; }
        .table-custom th { background-color: #112238; color: #94a3b8; border-color: #1b2a4a; }
        .table-custom td { border-color: #1b2a4a; vertical-align: middle; }
        .badge-buy { background-color: #064e3b; color: #34d399; border: 1px solid #059669; padding: 4px 12px; border-radius: 4px; font-weight: bold; }
        .badge-sell { background-color: #7f1d1d; color: #f87171; border: 1px solid #dc2626; padding: 4px 12px; border-radius: 4px; font-weight: bold; }
        .badge-neutral { background-color: #1e293b; color: #94a3b8; padding: 4px 12px; border-radius: 4px; }
        .badge-pending { background-color: #1e293b; color: #f59e0b; border: 1px solid #d97706; padding: 4px 10px; border-radius: 4px; }
        .form-control, .form-select { background-color: #060e18; border: 1px solid #1e293b; color: #fff; font-size: 12px; }
        .pill-tag { background: #112238; border: 1px solid #1e293b; padding: 5px 12px; border-radius: 20px; display: inline-block; margin: 3px; font-size: 11px; }
        .pill-tag span { cursor: pointer; color: #f87171; margin-left: 6px; }
    </style>
</head>
<body>
    <!-- TOP NAVBAR -->
    <div class="top-nav">
        <div class="d-flex align-items-center gap-3">
            <div style="background:#059669; color:#fff; padding:6px 12px; border-radius:6px; font-weight:bold;">A</div>
            <div>
                <h6 class="mb-0 fw-bold text-white">DELTA EXCHANGE X TRADEHULL</h6>
                <small class="text-secondary">Scanner Algo Terminal</small>
            </div>
        </div>
        <div>
            <span class="badge bg-success px-3 py-2" id="conn-status">🟢 Connected to Delta Exchange</span>
        </div>
    </div>

    <div class="container-fluid p-3">
        <div class="row">
            <!-- LEFT CONTROL CENTRE -->
            <div class="col-lg-4">
                <!-- CONNECTION PANEL -->
                <div class="panel">
                    <h6 class="text-white fw-bold mb-3">CONNECTION</h6>
                    <div class="mb-2">
                        <label class="form-label text-secondary small">API Key</label>
                        <input type="password" class="form-control" id="api-key" value="************************">
                    </div>
                    <div class="mb-3">
                        <label class="form-label text-secondary small">API Secret</label>
                        <input type="password" class="form-control" id="api-secret" value="************************">
                    </div>
                    <button class="btn btn-outline-light w-100 btn-sm fw-bold" onclick="toggleConnect()" id="conn-btn">Disconnect</button>
                </div>

                <!-- WATCHLIST PANEL -->
                <div class="panel">
                    <h6 class="text-white fw-bold mb-3">WATCHLIST</h6>
                    <div id="watchlist-pills" class="mb-3"></div>
                    <div class="input-group input-group-sm">
                        <input type="text" class="form-control" id="new-symbol" placeholder="Add symbol e.g. DOGEUSD">
                        <button class="btn btn-success" onclick="addSymbol()">Add</button>
                    </div>
                </div>

                <!-- INDICATORS & CONFIG PANEL -->
                <div class="panel">
                    <h6 class="text-white fw-bold mb-3">INDICATORS & SETTINGS</h6>
                    <div class="row g-2 mb-2">
                        <div class="col-6">
                            <label class="text-secondary small">RSI Period</label>
                            <input type="number" class="form-control" id="cfg-rsi" value="14">
                        </div>
                        <div class="col-6">
                            <label class="text-secondary small">History (days)</label>
                            <input type="number" class="form-control" id="cfg-hist" value="7">
                        </div>
                        <div class="col-6">
                            <label class="text-secondary small">EMA Fast</label>
                            <input type="number" class="form-control" id="cfg-emaf" value="20">
                        </div>
                        <div class="col-6">
                            <label class="text-secondary small">EMA Slow</label>
                            <input type="number" class="form-control" id="cfg-emas" value="50">
                        </div>
                        <div class="col-6">
                            <label class="text-secondary small">RSI Bull ></label>
                            <input type="number" class="form-control" id="cfg-rsib" value="60">
                        </div>
                        <div class="col-6">
                            <label class="text-secondary small">RSI Bear <</label>
                            <input type="number" class="form-control" id="cfg-rsis" value="40">
                        </div>
                        <div class="col-12 mt-2">
                            <label class="text-secondary small">Timeframe</label>
                            <select class="form-select" id="cfg-tf">
                                <option value="15m">15 Minute</option>
                                <option value="1h" selected>1 Hour</option>
                                <option value="4h">4 Hour</option>
                                <option value="1d">Daily</option>
                            </select>
                        </div>
                    </div>
                    <button class="btn btn-success w-100 btn-sm fw-bold mt-2" onclick="saveSettings()">Apply Settings</button>
                </div>

                <!-- CONTINUOUS SCAN CONTROLS -->
                <div class="panel">
                    <h6 class="text-white fw-bold mb-3">CONTINUOUS SCAN</h6>
                    <div class="d-flex gap-2">
                        <button class="btn btn-success w-50 btn-sm fw-bold" onclick="toggleScan(true)" id="start-btn">Start</button>
                        <button class="btn btn-outline-danger w-50 btn-sm fw-bold" onclick="toggleScan(false)" id="stop-btn">Stop</button>
                    </div>
                    <div class="small text-secondary mt-2" id="scan-status-text">🔴 Stopped • Click Start to begin live scan</div>
                </div>
            </div>

            <!-- RIGHT LIVE MARKET WORKSPACE -->
            <div class="col-lg-8">
                <div class="panel">
                    <h5 class="text-white fw-bold mb-1">Signal monitor</h5>
                    <small class="text-secondary">Live market workspace & automated classification</small>

                    <!-- COUNTER BOXES -->
                    <div class="row g-2 my-3">
                        <div class="col-md-4">
                            <div class="p-3 rounded" style="background:#061325; border:1px solid #1b2a4a;">
                                <div class="text-secondary small">Symbols monitored</div>
                                <div class="fs-4 fw-bold text-white" id="cnt-total">0</div>
                            </div>
                        </div>
                        <div class="col-md-4">
                            <div class="p-3 rounded" style="background:#061325; border:1px solid #1b2a4a;">
                                <div class="text-secondary small">Buy signals</div>
                                <div class="fs-4 fw-bold text-success" id="cnt-buy">0</div>
                            </div>
                        </div>
                        <div class="col-md-4">
                            <div class="p-3 rounded" style="background:#061325; border:1px solid #1b2a4a;">
                                <div class="text-secondary small">Sell signals</div>
                                <div class="fs-4 fw-bold text-danger" id="cnt-sell">0</div>
                            </div>
                        </div>
                    </div>

                    <!-- MAIN SIGNAL TABLE -->
                    <div class="table-responsive">
                        <table class="table table-custom table-hover align-middle mb-0">
                            <thead>
                                <tr>
                                    <th>SYMBOL</th>
                                    <th>CLOSE</th>
                                    <th>OPEN</th>
                                    <th>RSI</th>
                                    <th>EMA FAST</th>
                                    <th>EMA SLOW</th>
                                    <th>CANDLE</th>
                                    <th>MOMENTUM</th>
                                    <th>TREND</th>
                                    <th>SIGNAL</th>
                                </tr>
                            </thead>
                            <tbody id="scanner-tbody">
                                <tr><td colspan="10" class="text-center py-4 text-secondary">Awaiting first scan... Click Start</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        let isScanning = false;
        let scanTimer = null;
        let watchlist = [];

        async function loadState() {
            const res = await fetch('/api/state');
            const data = await res.json();
            watchlist = data.watchlist || [];
            document.getElementById('cfg-rsi').value = data.rsi_period || 14;
            document.getElementById('cfg-emaf').value = data.ema_fast || 20;
            document.getElementById('cfg-emas').value = data.ema_slow || 50;
            document.getElementById('cfg-rsib').value = data.rsi_bull || 60;
            document.getElementById('cfg-rsis').value = data.rsi_bear || 40;
            document.getElementById('cfg-tf').value = data.timeframe || '1h';
            
            renderWatchlist();
            if (data.scanning_active) {
                toggleScan(true);
            }
        }

        function renderWatchlist() {
            const box = document.getElementById('watchlist-pills');
            box.innerHTML = watchlist.map(s => `<div class="pill-tag">${s} <span onclick="removeSymbol('${s}')">✕</span></div>`).join('');
        }

        async function addSymbol() {
            const sym = document.getElementById('new-symbol').value.trim().toUpperCase();
            if (!sym || watchlist.includes(sym)) return;
            watchlist.push(sym);
            document.getElementById('new-symbol').value = '';
            await saveSettingsToBackend();
            renderWatchlist();
        }

        async function removeSymbol(sym) {
            watchlist = watchlist.filter(s => s !== sym);
            await saveSettingsToBackend();
            renderWatchlist();
        }

        async function saveSettings() {
            await saveSettingsToBackend();
            alert("Settings Applied Successfully!");
        }

        async function saveSettingsToBackend() {
            const payload = {
                rsi_period: Number(document.getElementById('cfg-rsi').value),
                ema_fast: Number(document.getElementById('cfg-emaf').value),
                ema_slow: Number(document.getElementById('cfg-emas').value),
                rsi_bull: Number(document.getElementById('cfg-rsib').value),
                rsi_bear: Number(document.getElementById('cfg-rsis').value),
                timeframe: document.getElementById('cfg-tf').value,
                watchlist: watchlist,
                scanning_active: isScanning
            };
            await fetch('/api/state', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
        }

        function toggleScan(status) {
            isScanning = status;
            const statusTxt = document.getElementById('scan-status-text');
            if (isScanning) {
                statusTxt.innerHTML = "🟢 Live Scanning Active (Every 3s)";
                document.getElementById('start-btn').className = "btn btn-success w-50 btn-sm fw-bold active";
                document.getElementById('stop-btn').className = "btn btn-danger w-50 btn-sm fw-bold";
                runScanLoop();
            } else {
                statusTxt.innerHTML = "🔴 Stopped • Click Start to begin live scan";
                document.getElementById('start-btn').className = "btn btn-success w-50 btn-sm fw-bold";
                document.getElementById('stop-btn').className = "btn btn-outline-danger w-50 btn-sm fw-bold active";
                if (scanTimer) clearTimeout(scanTimer);
            }
            saveSettingsToBackend();
        }

        async function runScanLoop() {
            if (!isScanning) return;
            try {
                const res = await fetch('/api/scan');
                const data = await res.json();
                renderTable(data);
            } catch(e){}
            if (isScanning) {
                scanTimer = setTimeout(runScanLoop, 3000);
            }
        }

        function renderTable(data) {
            const tbody = document.getElementById('scanner-tbody');
            if (!data || data.length === 0) {
                tbody.innerHTML = `<tr><td colspan="10" class="text-center py-4 text-secondary">No symbols found in watchlist.</td></tr>`;
                return;
            }

            let buyCnt = 0, sellCnt = 0;
            let html = '';
            data.forEach(c => {
                if (c.signal === 'BUY') buyCnt++;
                if (c.signal === 'SELL') sellCnt++;

                let sigBadge = c.signal === 'BUY' ? 'badge-buy' : (c.signal === 'SELL' ? 'badge-sell' : 'badge-neutral');

                html += `
                    <tr>
                        <td class="fw-bold text-white">${c.symbol}</td>
                        <td>$${c.close}</td>
                        <td>$${c.open}</td>
                        <td>${c.rsi}</td>
                        <td>${c.ema_fast}</td>
                        <td>${c.ema_slow}</td>
                        <td class="${c.candle==='Green'?'text-success':'text-danger'} fw-bold">${c.candle}</td>
                        <td>${c.momentum}</td>
                        <td>${c.trend}</td>
                        <td><span class="${sigBadge}">${c.signal}</span></td>
                    </tr>
                `;
            });

            tbody.innerHTML = html;
            document.getElementById('cnt-total').innerText = data.length;
            document.getElementById('cnt-buy').innerText = buyCnt;
            document.getElementById('cnt-sell').innerText = sellCnt;
        }

        function toggleConnect() {
            const btn = document.getElementById('conn-btn');
            const status = document.getElementById('conn-status');
            if (btn.innerText === 'Disconnect') {
                btn.innerText = 'Connect';
                btn.className = 'btn btn-success w-100 btn-sm fw-bold';
                status.innerText = '🔴 Disconnected';
                status.className = 'badge bg-danger px-3 py-2';
            } else {
                btn.innerText = 'Disconnect';
                btn.className = 'btn btn-outline-light w-100 btn-sm fw-bold';
                status.innerText = '🟢 Connected to Delta Exchange';
                status.className = 'badge bg-success px-3 py-2';
            }
        }

        loadState();
    </script>
</body>
</html>
"""

@app.route('/')
def home():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/state', methods=['GET', 'POST'])
def handle_state():
    if request.method == 'POST':
        cfg = load_config()
        cfg.update(request.json)
        save_config(cfg)
        return jsonify({"status": "saved"})
    return jsonify(load_config())

@app.route('/api/scan')
def api_scan():
    cfg = load_config()
    watchlist = cfg.get("watchlist", DEFAULT_WATCHLIST)
    results = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(analyze_coin, sym, cfg) for sym in watchlist]
        for f in futures:
            res = f.result()
            if res:
                results.append(res)
                if res['signal'] in ['BUY', 'SELL']:
                    send_telegram_alert(f"🚨 *Tradehull Signal Alert*\n📌 Symbol: `{res['symbol']}`\n⚡ Signal: *{res['signal']}* @ `${res['close']}`\n📊 RSI: `{res['rsi']}`")
    return jsonify(results)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5050))
    app.run(host='0.0.0.0', port=port, debug=False)
