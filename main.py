from flask import Flask, jsonify, request
import os, time, requests
from datetime import datetime, timedelta

app = Flask(__name__)

# ================= CONFIG =================
API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
BASE = "https://finnhub.io/api/v1"

PRICE_MAX = 30.0
FLOAT_MAX = 150_000_000
VOLUME_SHARES_GATE = 2_000_000
VOLUME_FLOAT_PCT_GATE = 0.0075  # 0.75%

SEED = [
    "T", "F", "AAL", "CCL", "NCLH", "SOFI", "PLTR", "PFE", "RIVN",
    "QS", "AI", "RIOT", "MARA", "JOBY", "OPEN", "HOOD", "ABNB",
    "U", "INTC", "IONQ", "UPST", "SMCI", "RUN", "COIN", "BBAI"
]

near_trigger_board = []

# ================= HELPERS =================
def fh(url, params=None):
    """Safe Finnhub API call"""
    if not API_KEY:
        raise RuntimeError("No FINNHUB_API_KEY set in Render environment.")
    params = dict(params or {})
    params["token"] = API_KEY
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()

def get_quote(symbol):
    return fh(f"{BASE}/quote", {"symbol": symbol})

def get_profile(symbol):
    return fh(f"{BASE}/stock/profile2", {"symbol": symbol})

def get_15m_volume(symbol):
    now = int(time.time())
    frm = now - 60 * 60 * 6
    data = fh(f"{BASE}/stock/candle", {
        "symbol": symbol, "resolution": 15, "from": frm, "to": now
    })
    if data.get("s") != "ok" or not data.get("v"):
        return 0
    return int(data["v"][-1])

def volume_gate_ok(last15_shares, free_float):
    gate1 = last15_shares >= VOLUME_SHARES_GATE
    gate2 = (free_float and last15_shares >= free_float * VOLUME_FLOAT_PCT_GATE)
    return gate1 or gate2

# ================= CATALYSTS =================
REAL_CATALYSTS = [
    "earnings", "guidance", "m&a", "acquisition", "merger", "takeover",
    "13d", "13g", "insider", "buyback", "repurchase", "contract",
    "partnership", "deal"
]
SPEC_CATALYSTS = ["strategic review", "pipeline", "explore options"]

def get_catalyst(symbol):
    try:
        frm = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
        to = datetime.utcnow().strftime("%Y-%m-%d")
        news = fh(f"{BASE}/company-news", {"symbol": symbol, "from": frm, "to": to})
        titles = " ".join(n.get("headline", "").lower() for n in news[:20])
        if any(k in titles for k in REAL_CATALYSTS):
            return "Real"
        if any(k in titles for k in SPEC_CATALYSTS):
            return "Spec"
    except Exception:
        pass
    return "None"

# ================= UNIVERSE =================
def get_universe(limit=200, fast=False):
    """Pulls tradable US stocks, fallback-safe."""
    try:
        data = fh(f"{BASE}/stock/symbol", {"exchange": "US"})
        syms = [d["symbol"] for d in data if "symbol" in d][:limit * 3]
        if fast:
            return syms[:limit]
        trimmed = []
        for s in syms:
            try:
                q = get_quote(s)
                p = float(q.get("c") or 0)
                if 0 < p <= PRICE_MAX:
                    trimmed.append(s)
                if len(trimmed) >= limit:
                    break
            except Exception:
                pass
        return trimmed or SEED[:limit]
    except Exception as e:
        print("Universe fallback:", e)
        return SEED[:limit]

# ================= SCANNER =================
def scan_symbol(symbol):
    try:
        q = get_quote(symbol)
        p = float(q.get("c") or 0)
        pc = float(q.get("pc") or p)
        if p <= 0 or p > PRICE_MAX:
            return None
        catalyst = get_catalyst(symbol)
        vwap = "Above" if p >= pc else "Below"
        vol15 = 0
        try:
            vol15 = get_15m_volume(symbol)
        except Exception:
            pass
        meets_vol = volume_gate_ok(vol15, 100_000_000)
        trig = round(p * 1.02, 2)
        pct = f"+{round((trig / p - 1) * 100, 2)}%"
        score = (5 if catalyst == "Real" else 2) + (3 if meets_vol else 0) + (2 if vwap == "Above" else 0)
        return {
            "symbol": symbol,
            "price": round(p, 2),
            "trigger": trig,
            "%_to_trigger": pct,
            "VWAP_Status": vwap,
            "Catalyst": catalyst,
            "15m_Vol": vol15,
            "Vol_OK": meets_vol,
            "Score": score
        }
    except Exception:
        return None

def run_scan(symbols):
    results = []
    for s in symbols:
        r = scan_symbol(s)
        if r:
            results.append(r)
    results.sort(key=lambda x: x["Score"], reverse=True)
    return results

# ================= ROUTES =================
@app.route("/")
def health():
    return jsonify({"ok": True, "ts": int(time.time())})

@app.route("/universe")
def universe():
    limit = int(request.args.get("limit", "200"))
    fast = request.args.get("fast", "1") in ("1", "true", "yes")
    syms = get_universe(limit, fast)
    return jsonify({"count": len(syms), "symbols": syms, "ts": int(time.time())})

@app.route("/scan")
def scan():
    global near_trigger_board
    maxn = int(request.args.get("max", "200"))
    fast = request.args.get("fast", "1") in ("1", "true", "yes")
    syms = get_universe(limit=maxn, fast=fast)
    near_trigger_board = run_scan(syms)
    return jsonify({"count": len(near_trigger_board), "message": "scan complete", "ts": int(time.time())})

@app.route("/board")
def board():
    return jsonify({
        "count": len(near_trigger_board),
        "near_trigger_board": near_trigger_board,
        "ts": int(time.time())
    })

@app.route("/quickscan")
def quickscan():
    """One-tap full refresh: universe + scan + return ranked board"""
    global near_trigger_board
    syms = get_universe(limit=200, fast=True)
    near_trigger_board = run_scan(syms)
    return jsonify({
        "count": len(near_trigger_board),
        "near_trigger_board": near_trigger_board,
        "ts": int(time.time())
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
