# main.py
# Minimal, Render-friendly Flask API for Stock Watch v7.5
# - /universe returns symbols only (no price calls)  << updated
# - /scan and /board apply the v7.5 rules
# - Uses Finnhub if FINNHUB_API_KEY is present; otherwise falls back to a static list

from __future__ import annotations
import os, time, math, threading
from typing import Dict, Any, List, Optional, Tuple
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# --------------------------
# Configuration (env overrides)
# --------------------------
FINNHUB_TOKEN = os.getenv("FINNHUB_API_KEY", "").strip()
FINNHUB_BASE  = "https://finnhub.io/api/v1"

UNIVERSE_LIMIT_DEFAULT = int(os.getenv("UNIVERSE_LIMIT_DEFAULT", "300"))
UNIVERSE_LIMIT_MAX     = int(os.getenv("UNIVERSE_LIMIT_MAX", "1000"))

# Scan caps to keep free tier healthy
SCAN_LIMIT_DEFAULT = int(os.getenv("SCAN_LIMIT_DEFAULT", "150"))
SCAN_LIMIT_MAX     = int(os.getenv("SCAN_LIMIT_MAX", "500"))

# v7.5 knobs
PRICE_MAX = float(os.getenv("PRICE_MAX", "30"))                 # <$30
FLOAT_MAX = float(os.getenv("FLOAT_MAX", "150000000"))          # <150M unless strong catalyst
FIFTEEN_MIN_VOL_GATE   = int(os.getenv("VOL_GATE_SHARES", "2000000"))
FIFTEEN_MIN_VOL_GATE_P = float(os.getenv("VOL_GATE_FF_PCT", "0.0075"))  # 0.75% of free float (approx)

# Timeouts / retries
HTTP_TIMEOUT = (5, 10)  # (connect, read)
SESSION = requests.Session()
ADAPTER = requests.adapters.HTTPAdapter(max_retries=2, pool_connections=50, pool_maxsize=50)
SESSION.mount("https://", ADAPTER)
SESSION.mount("http://", ADAPTER)
LOCK = threading.Lock()

# --------------------------
# Tiny in-memory cache
# --------------------------
_cache: Dict[str, Tuple[float, Any]] = {}

def cache_get(key: str, ttl: int) -> Optional[Any]:
    now = time.time()
    with LOCK:
        item = _cache.get(key)
        if not item:
            return None
        ts, val = item
        if now - ts <= ttl:
            return val
        _cache.pop(key, None)
        return None

def cache_set(key: str, val: Any) -> None:
    with LOCK:
        _cache[key] = (time.time(), val)

def ok(payload: Any, code: int = 200):
    return jsonify(payload), code

def err(message: str, code: int = 400):
    return jsonify({"error": message}), code

def need_key() -> bool:
    return not bool(FINNHUB_TOKEN)

# --------------------------
# Finnhub helpers (guarded)
# --------------------------
def fh_get(path: str, params: Dict[str, Any]) -> Optional[Any]:
    if need_key():
        return None
    p = dict(params or {})
    p["token"] = FINNHUB_TOKEN
    url = f"{FINNHUB_BASE}{path}"
    r = SESSION.get(url, params=p, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None

def fh_symbols(exchange: str = "US") -> List[Dict[str, Any]]:
    # cache for 30 minutes
    key = f"fh_symbols_{exchange}"
    cached = cache_get(key, 1800)
    if cached is not None:
        return cached
    data = fh_get("/stock/symbol", {"exchange": exchange}) or []
    # Filter to normal stocks/ADRs; keep ETFs/ETNs out by default
    cleaned = [s for s in data if s.get("type") in ("Common Stock", "ADR", "REIT", "Preferred Stock")]
    cache_set(key, cleaned)
    return cleaned

def fh_quote(sym: str) -> Optional[Dict[str, Any]]:
    return fh_get("/quote", {"symbol": sym})

def fh_profile(sym: str) -> Optional[Dict[str, Any]]:
    # shareOutstanding, marketCapitalization
    return fh_get("/stock/profile2", {"symbol": sym})

def fh_last_15m_volume(sym: str) -> int:
    # last 2 bars to be safe
    now = int(time.time())
    frm = now - 3600
    data = fh_get("/stock/candle", {"symbol": sym, "resolution": 15, "from": frm, "to": now})
    try:
        if data and data.get("s") == "ok" and data.get("v"):
            return int(data["v"][-1])
    except Exception:
        pass
    return 0

def fh_recent_news_grade(sym: str) -> str:
    # Grade catalysts quickly via title sniffing (free tier friendly).
    # Real: earnings, merger/M&A, buyback, insider/13D/13G, contract/partnership
    if need_key():
        return "None"
    now = int(time.time())
    then = now - 7 * 86400
    data = fh_get("/company-news", {"symbol": sym, "from": time.strftime("%Y-%m-%d", time.gmtime(then)),
                                    "to": time.strftime("%Y-%m-%d", time.gmtime(now))}) or []
    title = " ".join([str(x.get("headline") or "") for x in data]).lower()
    real_hits = any(k in title for k in [
        "earnings", "eps", "guidance", "merger", "acquisition", "acquires", "buyback",
        "repurchase", "13d", "13g", "insider buys", "contract", "partnership", "customer win"
    ])
    speculative = any(k in title for k in ["strategic review", "pipeline"])
    if real_hits:
        return "Real"
    if speculative:
        return "Speculative"
    return "None"

# --------------------------
# Universe builder (NO quotes here)  << updated
# --------------------------
def build_universe(limit: int) -> List[str]:
    """
    Universe = first N tradable US tickers from Finnhub, no price filtering here.
    Avoids rate-limit surprises that previously caused count=0.
    """
    if need_key():
        fallback = [
            "AAPL","MSFT","AMZN","NVDA","GOOGL","META","TSLA","AMD","INTC","NFLX","PLTR","SOFI","F","RIVN",
            "PFE","T","CCL","UAL","AAL","LCID","BABA","KO","PEP","DIS","SQ","PYPL","UBER","ABNB","BA","X","ET",
            "WFC","BAC","C","JPM","GME","AMC","MRNA","ORCL","IBM","CRM","QCOM","MU","BBD","NIO","SHOP","UBS",
        ]
        return fallback[:limit]
    syms = fh_symbols(exchange="US")
    return [s.get("symbol") for s in syms[:limit] if s.get("symbol")]

# --------------------------
# v7.5 filter helpers (applied in /scan and /board)
# --------------------------
def within_price_gate(q: Dict[str, Any]) -> bool:
    try:
        price = float(q.get("c") or 0)
        return price > 0 and price < PRICE_MAX
    except Exception:
        return False

def approx_float_ok(profile: Optional[Dict[str, Any]], catalyst: str) -> bool:
    # If we know sharesOutstanding, use it. Otherwise allow (cannot judge).
    if not profile:
        return True
    try:
        shares = float(profile.get("shareOutstanding") or 0)
    except Exception:
        shares = 0.0
    if shares <= 0:
        return True
    if shares <= FLOAT_MAX:
        return True
    # allow oversize float only with institutional-grade "Real" catalyst
    return catalyst == "Real"

def volume_gate_ok(sym: str, profile: Optional[Dict[str, Any]]) -> Tuple[bool, int, int]:
    vol_15 = fh_last_15m_volume(sym) if not need_key() else 0
    # derive free float proxy: if shareOutstanding present assume 85% float
    ff = 0
    if profile:
        try:
            so = float(profile.get("shareOutstanding") or 0)
            if so > 0:
                ff = int(so * 0.85)
        except Exception:
            pass
    rule_b = int(ff * FIFTEEN_MIN_VOL_GATE_P) if ff > 0 else FIFTEEN_MIN_VOL_GATE
    required = max(FIFTEEN_MIN_VOL_GATE, rule_b)
    return (vol_15 >= required, vol_15, required)

def vwap_status(q: Dict[str, Any]) -> str:
    # proxy: above yesterday's close -> "Above" else "Below"
    try:
        c = float(q.get("c") or 0)
        pc = float(q.get("pc") or 0)
        if c > 0 and pc > 0 and c >= pc:
            return "Above"
    except Exception:
        pass
    return "Below"

def make_trigger(price: float) -> float:
    # Simple 2% trigger above current
    return round(price * 1.02, 2)

def score_row(catalyst: str, vwap: str, pct_to_trig: float) -> int:
    s = 0
    if catalyst == "Real":
        s += 3
    elif catalyst == "Speculative":
        s += 1
    if vwap == "Above":
        s += 1
    if pct_to_trig <= 2.5:
        s += 1
    if pct_to_trig <= 1.0:
        s += 1
    return s

# --------------------------
# Routes
# --------------------------
@app.route("/")
def root():
    return ok({"ok": True, "endpoints": ["/health", "/universe", "/scan", "/board"]})

@app.route("/health")
def health():
    return ok({
        "service": "stock-watch",
        "ts": int(time.time()),
        "requires_api_key": need_key(),
        "universe_default": UNIVERSE_LIMIT_DEFAULT
    })

@app.route("/universe", methods=["GET"])
def universe():
    limit_s = request.args.get("limit", str(UNIVERSE_LIMIT_DEFAULT))
    force = request.args.get("force") in ("1", "true", "True")
    try:
        limit = int(limit_s)
    except ValueError:
        limit = UNIVERSE_LIMIT_DEFAULT
    limit = max(1, min(limit, UNIVERSE_LIMIT_MAX))

    if not force:
        cached = cache_get("universe", 900)  # 15 minutes
        if cached:
            return ok({"count": min(limit, len(cached)), "symbols": cached[:limit], "ts": int(time.time())})

    symbols = build_universe(limit=limit)
    cache_set("universe", symbols)
    return ok({"count": len(symbols), "symbols": symbols, "ts": int(time.time())})

@app.route("/scan", methods=["GET"])
def scan():
    # pull universe, then apply v7.5 filters with real data
    limit_s = request.args.get("limit", str(SCAN_LIMIT_DEFAULT))
    try:
        limit = int(limit_s)
    except ValueError:
        limit = SCAN_LIMIT_DEFAULT
    limit = max(1, min(limit, SCAN_LIMIT_MAX))

    uni = cache_get("universe", 900) or build_universe(limit=limit)
    if not uni:
        return ok({"count": 0, "message": "scan complete", "ts": int(time.time())})

    results: List[Dict[str, Any]] = []
    checked = 0
    for sym in uni[:limit]:
        checked += 1
        q = fh_quote(sym) if not need_key() else None
        if not q or not within_price_gate(q):
            continue
        price = float(q["c"])
        trig = make_trigger(price)
        vwap = vwap_status(q)
        catalyst = fh_recent_news_grade(sym)
        prof = fh_profile(sym) if not need_key() else None
        if not approx_float_ok(prof, catalyst):
            continue
        vol_ok, v15, vreq = volume_gate_ok(sym, prof)
        row = {
            "symbol": sym,
            "trigger": trig,
            "price": round(price, 2),
            "%_to_trigger": f"+{round(((trig - price) / price) * 100, 2)}%",
            "VWAP_Status": vwap,
            "15m_Vol": v15,
            "Vol_OK": bool(vol_ok),
            "Catalyst": catalyst
        }
        # provisional score for board ranking
        pct_to = abs(((trig - price) / price) * 100.0)
        row["Score"] = score_row(catalyst, vwap, pct_to)
        results.append(row)

    return ok({"count": len(results), "near_trigger_board": results, "ts": int(time.time())})

@app.route("/board", methods=["GET"])
def board():
    # reuse /scan computation but sort and trim for presentation
    payload, _ = scan()
    data = payload.json if hasattr(payload, "json") else payload[0].json  # safety if Flask tuple
    rows = data.get("near_trigger_board", [])
    rows.sort(key=lambda r: (-int(r.get("Score", 0)), r.get("symbol", "")))
    return ok({"count": len(rows), "near_trigger_board": rows, "ts": int(time.time())})

@app.route("/clear_cache", methods=["POST", "GET"])
def clear_cache():
    with LOCK:
        _cache.clear()
    return ok({"cleared": True, "ts": int(time.time())})

# --------------------------
# Entrypoint
# --------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
