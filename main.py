import os, time, math, json, re
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import requests
from flask import Flask, jsonify, request

# ==== CONFIG ====
APP_TZ = "America/Toronto"  # display only
MARKET_TZ = timezone(timedelta(hours=-4))  # ET (no DST handling here; Render restarts often)
PRICE_CAP = 30.0
FLOAT_CAP_M = 150.0  # we approximate float using shares outstanding from Finnhub profile2
VOL_ABS_GATE = 2_000_000
VOL_FLOAT_PCT = 0.0075  # 0.75% of float
TRIGGER_PAD = 0.00      # trigger = prior day high (+ pad)

# Finnhub
FH_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
FH_BASE = "https://finnhub.io/api/v1"

if not FH_KEY:
    raise RuntimeError("Missing FINNHUB_API_KEY env var")

# Load tickers list (one per line, no commas)
def load_tickers():
    path = os.getenv("TICKERS_FILE", "tickers.txt")
    try:
        with open(path, "r") as f:
            syms = [ln.strip().upper() for ln in f if ln.strip()]
    except FileNotFoundError:
        syms = []
    return syms

# Simple in-memory cache to keep Finnhub usage low
_cache = {}
def cache_get(key, ttl=60):
    now = time.time()
    item = _cache.get(key)
    if item and now - item[0] < ttl:
        return item[1]
    return None

def cache_set(key, value):
    _cache[key] = (time.time(), value)

def fh_get(path, params, ttl=30):
    key = ("FH", path, tuple(sorted(params.items())))
    c = cache_get(key, ttl)
    if c is not None:
        return c
    params = dict(params)
    params["token"] = FH_KEY
    r = requests.get(f"{FH_BASE}/{path}", params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    cache_set(key, data)
    return data

# ---- Finnhub helpers ----

def get_quote(sym):
    """Current OHLC from Finnhub /quote."""
    q = fh_get("quote", {"symbol": sym}, ttl=5)
    # {c: current, h: high, l: low, o: open, pc: prev close, t: unix}
    return q

def get_profile(sym):
    """Company profile; we use sharesOutstanding as an (imperfect) float proxy."""
    prof = fh_get("stock/profile2", {"symbol": sym}, ttl=600)
    return prof or {}

def get_daily_candle(sym, days=5):
    """Last N daily candles; used for prior-day high as trigger."""
    now = int(time.time())
    frm = now - days * 86400 * 3  # generous window
    data = fh_get("stock/candle", {"symbol": sym, "resolution": "D", "from": frm, "to": now}, ttl=90)
    return data if data and data.get("s") == "ok" else {}

def get_intraday_1m(sym, minutes=60):
    """Fetch 1-minute candles for ~last 'minutes' (for VWAP calc)."""
    now = int(time.time())
    frm = now - minutes * 60 * 2  # leeway
    data = fh_get("stock/candle", {"symbol": sym, "resolution": "1", "from": frm, "to": now}, ttl=30)
    return data if data and data.get("s") == "ok" else {}

def get_intraday_15m(sym, lookback_minutes=120):
    """Grab last 15m bar to check fresh volume."""
    now = int(time.time())
    frm = now - lookback_minutes * 60
    data = fh_get("stock/candle", {"symbol": sym, "resolution": "15", "from": frm, "to": now}, ttl=30)
    return data if data and data.get("s") == "ok" else {}

def calc_vwap_1m(candles):
    """VWAP over the available 1m window."""
    if not candles:
        return None
    t_list = candles.get("t", [])
    c_list = candles.get("c", [])
    v_list = candles.get("v", [])
    if not t_list or not c_list or not v_list:
        return None
    pv = 0.0
    vv = 0.0
    for p, v in zip(c_list, v_list):
        pv += p * v
        vv += v
    if vv <= 0:
        return None
    return pv / vv

# ---- Rules / scoring ----

def prior_day_high(sym):
    d = get_daily_candle(sym, days=10)
    if not d:
        return None
    # use last completed day (exclude today)
    if len(d["h"]) >= 2:
        return float(d["h"][-2]) + TRIGGER_PAD
    return None

def latest_15m_volume(sym):
    c = get_intraday_15m(sym)
    if not c:
        return 0, None
    v = c["v"][-1] if c["v"] else 0
    t = c["t"][-1] if c["t"] else None
    return int(v or 0), t

def vwap_status(sym, last_price):
    one = get_intraday_1m(sym, minutes=90)
    vwap = calc_vwap_1m(one)
    if vwap is None:
        return "n/a", None
    return ("Above" if last_price >= vwap else "Below"), vwap

CATALYST_KEYWORDS = [
    ("earnings", "Earnings"),
    ("13d", "13D/13G"),
    ("13g", "13D/13G"),
    ("merger", "M&A"),
    ("acquisition", "M&A"),
    ("buyback", "Buyback"),
    ("repurchase", "Buyback"),
    ("contract", "Contract"),
    ("partnership", "Partnership"),
]

def detect_catalyst(sym):
    """Lightweight keyword search in recent company news (last 14 days)."""
    try:
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=14)
        news = fh_get("company-news", {"symbol": sym, "from": str(start), "to": str(end)}, ttl=900)
        cats = set()
        if isinstance(news, list):
            for item in news[:50]:
                text = f"{item.get('headline','')} {item.get('summary','')}".lower()
                for kw, label in CATALYST_KEYWORDS:
                    if kw in text:
                        cats.add(label)
        if cats:
            return ", ".join(sorted(cats))
    except Exception:
        pass
    return ""

def sector_heat(sym):
    """Very rough proxy using the symbol's mega sector (tech = hot most days)."""
    # You can expand with real sector once you save it in a local map/profile.
    if re.search(r"(NVDA|AMD|AAPL|AVGO|TSM|QCOM|PLTR|SOFI|IONQ|AI|SHOP|NVDA)", sym):
        return "🔥"
    return "⚪"

def passes_rules(sym):
    q = get_quote(sym)
    if not q or not q.get("c"):
        return None  # no data
    price = float(q["c"])
    if price <= 0 or price > PRICE_CAP:
        return None

    prof = get_profile(sym)  # sharesOutstanding as float proxy
    shares_out = prof.get("shareOutstanding")
    float_ok = True
    float_note = ""
    if shares_out:
        float_m = float(shares_out)
        if float_m > FLOAT_CAP_M:
            float_ok = False
            float_note = f"sharesOutstanding {float_m:.0f}M > {FLOAT_CAP_M}M"
    # if we can't fetch, allow (we still gate by absolute volume)
    if not float_ok:
        return None

    trig = prior_day_high(sym)
    if trig is None or trig <= 0:
        return None

    pct_to_trig = (trig - price) / trig * 100.0
    if pct_to_trig < 0:
        pct_to_trig = 0.0

    vol15, vol_ts = latest_15m_volume(sym)
    vol_req = VOL_ABS_GATE
    if shares_out:
        vol_req = max(VOL_ABS_GATE, int(shares_out * 1_000_000 * VOL_FLOAT_PCT))

    vstat, vwap_val = vwap_status(sym, price)

    # volume gate is evaluated later at alert time; for near-trigger board we show ratio
    vol_ratio = f"{(vol15 / vol_req):.1f}x" if vol_req > 0 else "n/a"

    cat = detect_catalyst(sym)

    row = {
        "symbol": sym,
        "price": round(price, 4),
        "trigger": round(trig, 4),
        "%_to_trigger": f"{pct_to_trig:.1f}%",
        "VWAP_Status": vstat,
        "Vol_15m_vs_Req": vol_ratio,
        "Catalyst": cat or "—",
        "Sector_Heat": sector_heat(sym),
        "Note": "" if not float_note else float_note,
    }
    return row

# ---- Flask app & endpoints ----

app = Flask(__name__)
_last_scan = {"ts": 0, "board": []}

def run_scan():
    syms = load_tickers()
    out = []
    for s in syms:
        try:
            row = passes_rules(s)
            if row:
                out.append(row)
        except Exception as e:
            # keep scanning even if a symbol fails
            continue
    # Rank: % to trigger ascending, then hottest sector
    def hot_key(r):
        pct = float(r["%_to_trigger"].strip("%"))
        heat = {"🔥": 0, "⚪": 1, "❄️": 2}.get(r["Sector_Heat"], 1)
        return (pct, heat)
    out.sort(key=hot_key)
    _last_scan["ts"] = int(time.time())
    _last_scan["board"] = out
    return out

@app.route("/")
def root():
    return jsonify({"ok": True, "msg": "StockWatch live", "endpoints": ["/scan", "/board", "/quote?symbol=PLTR"]})

@app.route("/scan")
def scan():
    board = run_scan()
    return jsonify({"count": len(board), "near_trigger_board": board, "ts": _last_scan["ts"], "stale": False})

@app.route("/board")
def board():
    age = (time.time() - _last_scan["ts"]) / 60 if _last_scan["ts"] else None
    return jsonify({
        "age_min": round(age, 2) if age is not None else None,
        "count": len(_last_scan["board"]),
        "near_trigger_board": _last_scan["board"],
        "stale": False if _last_scan["ts"] else True,
        "ts": _last_scan["ts"]
    })

@app.route("/quote")
def quote():
    sym = request.args.get("symbol", "").upper()
    if not sym:
        return jsonify({"error": "symbol required"}), 400
    try:
        q = get_quote(sym)
        return jsonify({"symbol": sym, **q})
    except Exception as e:
        return jsonify({"symbol": sym, "error": str(e)}), 500

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": int(time.time())})

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
