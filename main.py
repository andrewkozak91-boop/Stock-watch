import os
import time
import math
from typing import List, Dict, Any, Tuple
from functools import lru_cache
from flask import Flask, jsonify, request
import requests

app = Flask(__name__)

# =========================
# Config
# =========================
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
BASE = "https://finnhub.io/api/v1"
TIMEOUT = 8
UNIVERSE_LIMIT_DEFAULT = 300
UNIVERSE_LIMIT_MAX = 500
BOARD_CACHE_SECONDS = 120
UNIVERSE_CACHE_SECONDS = 300

# Stock Game v7.5 gates (lightweight approximations using fields we can get fast)
PRICE_MAX = 30.0
FLOAT_MAX = 150_000_000  # override allowed w/ catalyst flag (best-effort)
FIFTEEN_MIN_VOL_ABS = 2_000_000
FIFTEEN_MIN_VOL_FF_PCT = 0.0075  # 0.75% of free float
ALLOW_TIER2_ADR_ONLY = True

# =========================
# Utilities
# =========================
def now_ts() -> int:
    return int(time.time())

def ok(payload: Dict[str, Any]):
    out = dict(payload)
    out["ts"] = now_ts()
    return jsonify(out)

def err(msg: str, code: int = 400):
    return jsonify({"error": msg, "ts": now_ts()}), code

def need_key() -> bool:
    return len(FINNHUB_KEY) == 0

def qparams(**kwargs) -> Dict[str, Any]:
    d = {k: v for k, v in kwargs.items() if v is not None}
    if FINNHUB_KEY:
        d["token"] = FINNHUB_KEY
    return d

def http_get(path: str, **params) -> Tuple[bool, Any]:
    """Returns (ok, json|error)."""
    try:
        r = requests.get(f"{BASE}{path}", params=qparams(**params), timeout=TIMEOUT)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        return True, r.json()
    except Exception as e:
        return False, str(e)

# =========================
# Finnhub wrappers (fast)
# =========================
def fh_symbols(exchange: str = "US") -> List[Dict[str, Any]]:
    ok_, data = http_get("/stock/symbol", exchange=exchange)
    if not ok_:
        return []
    # Filter tradable common stocks quickly
    out = []
    for s in data:
        typ = (s.get("type") or "").upper()
        if typ in ("COMMON STOCK", "EQUITY") and s.get("symbol"):
            out.append(s)
    return out

def fh_quote(sym: str) -> Dict[str, Any]:
    ok_, data = http_get("/quote", symbol=sym)
    return data if ok_ and isinstance(data, dict) else {}

def fh_profile(sym: str) -> Dict[str, Any]:
    ok_, data = http_get("/stock/profile2", symbol=sym)
    return data if ok_ and isinstance(data, dict) else {}

def fh_candles_1min(sym: str, cnt: int = 20) -> Dict[str, Any]:
    """
    Get up to last cnt 1-minute bars (close to live).
    We fetch last ~30 minutes and slice.
    """
    now = int(time.time())
    fr = now - 60 * (cnt + 10)
    ok_, data = http_get("/stock/candle", symbol=sym, resolution="1", _from=fr, to=now)
    return data if ok_ and isinstance(data, dict) else {}

# =========================
# Caching (in-memory)
# =========================
_cache = {"universe": {"ts": 0, "data": []}, "board": {"ts": 0, "data": {}}}

def cache_get(name: str, ttl: int):
    slot = _cache.get(name, {})
    if not slot:
        return None
    if now_ts() - slot.get("ts", 0) <= ttl:
        return slot.get("data")
    return None

def cache_set(name: str, data: Any):
    _cache[name] = {"ts": now_ts(), "data": data}

# =========================
# Core logic
# =========================
def estimate_free_float(profile: Dict[str, Any]) -> int:
    # Finnhub profile2: shareOutstanding may exist; free float not always provided.
    # Approximate free float as shares_outstanding - insider_held (if any).
    so = profile.get("shareOutstanding")
    if so is None:
        return 0
    so = int(so)
    insider = profile.get("insiderOwn")  # fraction 0..1 on some tickers
    if isinstance(insider, (int, float)) and insider > 0 and insider < 0.9:
        ff = int(so * (1 - insider))
    else:
        ff = so
    return max(ff, 0)

def is_adr(profile: Dict[str, Any]) -> bool:
    # Finnhub profile doesn't flag ADR cleanly for all; infer by country vs exchange & ADR field if present
    adr = profile.get("adr")
    if isinstance(adr, bool):
        return adr
    # heuristic: if exchange in US but country not US, could be ADR.
    country = (profile.get("country") or "").upper()
    exch = (profile.get("exchange") or "").upper()
    if exch in ("NASDAQ NMS - GLOBAL MARKET", "NEW YORK STOCK EXCHANGE", "NASDAQGS", "NASDAQ", "NYSE") and country not in ("USA", "UNITED STATES"):
        return True
    return False

def catalyst_grade(profile: Dict[str, Any]) -> str:
    """
    Placeholder “best-effort” catalyst tag:
    - If company has 'ipo' within last 365d => mark as 'Insider/Float Event' (loosening float rule)
    - If marketCap changed strongly vs. 52w metrics would require more endpoints; we keep it simple.
    You can later wire real event feeds here.
    """
    ipo = profile.get("ipo")
    if ipo:
        try:
            y, m, d = (int(x) for x in str(ipo).split("-"))
            # recently public → often institutional-grade flows
            # (We won't compute actual days to keep lib-light; flag anyway)
            return "Institutional-grade (recent IPO)"
        except Exception:
            pass
    return "None"

def volume_gate(sym: str, free_float_shares: int) -> Tuple[int, bool]:
    """
    15-min volume gate = last 15 one-minute bars sum
    """
    candles = fh_candles_1min(sym, cnt=20)
    if not candles or candles.get("s") != "ok":
        return 0, False
    v = candles.get("v") or []
    # last 15 bars
    vol15 = int(sum(v[-15:])) if len(v) >= 15 else int(sum(v))
    need_ff = int(math.ceil(max(FIFTEEN_MIN_VOL_ABS, free_float_shares * FIFTEEN_MIN_VOL_FF_PCT)))
    return vol15, vol15 >= need_ff

def build_universe(limit: int) -> List[str]:
    """
    Universe = first N tradable US tickers by Finnhub list, then we price-filter to < $30
    (We avoid thousands of quote calls; we cap at 'limit').
    """
    if need_key():
        # No key → fall back to static but broad list
        base = [
            "AAPL","MSFT","AMZN","NVDA","GOOGL","META","TSLA","AMD","INTC","NFLX","PLTR","SOFI","F","RIVN",
            "PFE","T","CCL","UAL","AAL","LCID","BABA","KO","PEP","DIS","SQ","PYPL","UBER","ABNB","BA","X","ET",
            "WFC","BAC","C","JPM","GME","AMC","MRNA","ORCL","IBM","CRM","QCOM","MU","BBD","NIO","SHOP","UBS",
        ]
        return base[:limit]

    syms = fh_symbols(exchange="US")
    # just take first K that are priced < $30
    out: List[str] = []
    for s in syms:
        if len(out) >= limit:
            break
        sym = s["symbol"]
        q = fh_quote(sym)
        price = q.get("c")
        if isinstance(price, (int, float)) and price is not None and price > 0 and price < PRICE_MAX:
            out.append(sym)
    return out

def scan_symbols(symbols: List[str]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for sym in symbols:
        q = fh_quote(sym)
        price = q.get("c") or 0
        vwap_status = "Above" if (q.get("c") or 0) >= (q.get("pc") or 0) else "Below"  # crude proxy
        prof = fh_profile(sym)
        free_float = estimate_free_float(prof)
        adr = is_adr(prof)

        # ADR rule
        if ALLOW_TIER2_ADR_ONLY and adr:
            # keep but mark Tier-2 unless other signals graduate it later
            adr_tier = "Tier-2 ADR"
        else:
            adr_tier = "Domestic"

        cat = catalyst_grade(prof)
        # float rule w/ override
        float_ok = (free_float > 0 and free_float < FLOAT_MAX) or ("Institutional-grade" in cat)

        # 15-min volume gate
        vol15, vol_ok = volume_gate(sym, free_float if free_float > 0 else 200_000_000)

        # trigger = round up 2% above last price (placeholder until you specify exact trigger logic)
        trigger = round(price * 1.02, 2) if price else None
        pct_to_trig = None
        if price and trigger:
            pct_to_trig = f"+{round((trigger - price) / price * 100, 2)}%"

        # quick score for ranking
        score = 0
        if price and price < PRICE_MAX:
            score += 2
        if float_ok:
            score += 2
        if vol_ok:
            score += 3
        if vwap_status == "Above":
            score += 1
        if "Institutional-grade" in cat:
            score += 1

        row = {
            "symbol": sym,
            "price": round(price, 2) if price else 0,
            "trigger": trigger,
            "%_to_trigger": pct_to_trig or "n/a",
            "VWAP_Status": vwap_status,
            "15m_Vol": int(vol15),
            "Vol_OK": bool(vol_ok),
            "Catalyst": "Real" if "Institutional-grade" in cat else "None",
            "ADR": adr_tier,
            "Score": score,
        }

        # Only keep if basic v7.5 gates pass (price and either vol gate or close)
        if price and price < PRICE_MAX:
            rows.append(row)

    # rank: higher score, then closer to trigger, then higher vol
    def sort_key(r):
        try:
            pct = float(r["%_to_trigger"].strip("+%"))
        except Exception:
            pct = 999
        return (-r["Score"], pct, -r["15m_Vol"])

    rows.sort(key=sort_key)
    return rows

# =========================
# Routes
# =========================
@app.route("/", methods=["GET"])
def root():
    return "stock-watch live (finnhub scanner)"

@app.route("/health", methods=["GET"])
def health():
    return ok({"status": "ok", "requires_api_key": bool(not FINNHUB_KEY)})

@app.route("/universe", methods=["GET"])
def universe():
    limit_s = request.args.get("limit", str(UNIVERSE_LIMIT_DEFAULT))
    try:
        limit = int(limit_s)
    except ValueError:
        limit = UNIVERSE_LIMIT_DEFAULT
    limit = max(1, min(limit, UNIVERSE_LIMIT_MAX))

    cached = cache_get("universe", UNIVERSE_CACHE_SECONDS)
    if cached and isinstance(cached, list) and len(cached) >= min(limit, len(cached)):
        return ok({"count": min(limit, len(cached)), "symbols": cached[:limit]})

    symbols = build_universe(limit=limit)
    cache_set("universe", symbols)
    return ok({"count": len(symbols), "symbols": symbols})

@app.route("/scan", methods=["GET"])
def scan():
    # Always rebuild the board on demand; caching handled in /board
    uni = cache_get("universe", UNIVERSE_CACHE_SECONDS)
    if not uni:
        uni = build_universe(limit=UNIVERSE_LIMIT_DEFAULT)
        cache_set("universe", uni)

    board = scan_symbols(uni)
    cache_set("board", {"rows": board})
    return ok({"message": "scan complete", "count": len(board)})

@app.route("/board", methods=["GET"])
def board():
    cached = cache_get("board", BOARD_CACHE_SECONDS)
    if cached:
        rows = cached.get("rows", [])
        return ok({"count": len(rows), "near_trigger_board": rows})

    # No fresh board in cache → run a quick scan using current universe
    uni = cache_get("universe", UNIVERSE_CACHE_SECONDS)
    if not uni:
        uni = build_universe(limit=UNIVERSE_LIMIT_DEFAULT)
        cache_set("universe", uni)

    rows = scan_symbols(uni)
    cache_set("board", {"rows": rows})
    return ok({"count": len(rows), "near_trigger_board": rows})

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Route not found", "hint": "Use /health, /universe, /scan, /board"}), 404

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
