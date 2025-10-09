from flask import Flask, jsonify
import os, time, math, requests
from datetime import datetime, timedelta

app = Flask(__name__)

API_KEY = os.getenv("FINNHUB_API_KEY")
BASE = "https://finnhub.io/api/v1"

# -------- Configuration ----------
MAX_UNIVERSE = int(os.getenv("MAX_UNIVERSE", "300"))
PRICE_MAX = 30.0
FLOAT_MAX = 150_000_000
VOLUME_SHARES_GATE = 2_000_000
VOLUME_FLOAT_PCT_GATE = 0.0075

UNIVERSE = []
_last_universe_refresh = 0
near_trigger_board = []

# ---------------- Utility ----------------
def fh(url, params=None):
    params = params or {}
    params["token"] = API_KEY
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()

def now_ts(): return int(time.time())

def fetch_all_us_common():
    try:
        data = fh(f"{BASE}/stock/symbol", {"exchange": "US"})
        symbols = [d["symbol"] for d in data if d.get("type") == "Common Stock"]
        return symbols
    except Exception as e:
        print("Fetch symbol list error:", e)
        return []

# -------- NEW: Lite Universe Refresh --------
def refresh_universe(limit=MAX_UNIVERSE, price_max=PRICE_MAX, float_max=FLOAT_MAX):
    """
    Lite refresh: builds a large universe list with no per-symbol API calls.
    Price/float/ADR filtering happens later during scan_symbol().
    This avoids Finnhub rate limits during refresh.
    """
    global UNIVERSE, _last_universe_refresh

    syms = fetch_all_us_common()
    if not syms:
        # Fallback minimal set
        UNIVERSE = [
            "PLTR","SOFI","MARA","RIOT","QS","CCL","AAL","RIVN","JOBY","AI",
            "T","F","BBAI","PTON","PFE","SOUN","NCLH","RUM","PLUG"
        ][:limit]
        _last_universe_refresh = now_ts()
        return {"ok": True, "count": len(UNIVERSE), "source": "fallback", "ts": _last_universe_refresh}

    picked = []
    for sym in syms:
        if len(picked) >= limit:
            break
        picked.append(sym)

    UNIVERSE = picked
    _last_universe_refresh = now_ts()
    return {"ok": True, "count": len(UNIVERSE), "source": "lite", "ts": _last_universe_refresh}

# -------------- Core Logic -----------------
def get_profile(symbol):
    return fh(f"{BASE}/stock/profile2", {"symbol": symbol})

def get_quote(symbol):
    return fh(f"{BASE}/quote", {"symbol": symbol})

def get_15m_volume(symbol):
    now = int(time.time())
    frm = now - 60*60*6
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

REAL_CATALYST_KEYWORDS = [
    "earnings","guidance","m&a","acquisition","merger","takeover",
    "13d","13g","insider","buyback","repurchase","contract","partnership","deal",
]
SPECULATIVE_KEYWORDS = ["strategic review","pipeline","explore options"]

def has_real_catalyst(symbol):
    try:
        since = int((datetime.utcnow()-timedelta(days=7)).timestamp())
        news = fh(f"{BASE}/company-news", {
            "symbol": symbol,
            "from": datetime.utcfromtimestamp(since).strftime("%Y-%m-%d"),
            "to": datetime.utcnow().strftime("%Y-%m-%d")
        })
        titles = " ".join(n.get("headline","").lower() for n in news[:20])
        if any(k in titles for k in REAL_CATALYST_KEYWORDS):
            return ("Real", "Tier-1/2 catalyst")
        if any(k in titles for k in SPECULATIVE_KEYWORDS):
            return ("Spec", "Tier-3 speculative")
    except Exception:
        pass
    return ("None","")

def classify_tier(is_adr, catalyst_kind):
    if catalyst_kind == "Spec": return ("Tier-3", "C")
    if is_adr: return ("Tier-2", "B")
    return ("Tier-1", "A")

def derive_trigger(symbol, price):
    trig = round(price * 1.02, 2)
    return trig, f"+{round((trig/price-1)*100,2)}%"

def scan_symbol(symbol):
    try:
        profile = get_profile(symbol) or {}
        quote = get_quote(symbol) or {}

        price = quote.get("c") or 0.0
        pc = quote.get("pc") or price
        if price <= 0 or price > PRICE_MAX: return None

        free_float = None
        if "shareOutstanding" in profile and profile["shareOutstanding"]:
            free_float = float(profile["shareOutstanding"]) * 1_000_000
        if free_float and free_float > FLOAT_MAX: pass

        is_adr = "ADR" in (profile.get("name","").upper()+" "+profile.get("ticker","").upper())
        vol15 = get_15m_volume(symbol)
        vol_ok = volume_gate_ok(vol15, free_float or 0)
        catalyst_kind, catalyst_note = has_real_catalyst(symbol)

        if free_float and free_float > FLOAT_MAX and catalyst_kind != "Real": return None
        vwap_status = "Above" if price >= pc else "Below"
        tier, grade = classify_tier(is_adr, catalyst_kind)
        trigger, pct_to_trigger = derive_trigger(symbol, price)

        note = []
        if catalyst_kind == "Spec": note.append("Spec PR — tiny size only")
        if is_adr: note.append("ADR (Tier-2+)")

        try: gap = float(pct_to_trigger.strip("%+"))
        except: gap = 2.0
        score = (5 if catalyst_kind=="Real" else 2) + (3 if vol_ok else 0) + (3 if vwap_status=="Above" else 0) - gap

        return {
            "symbol": symbol,
            "Tier/Grade": f"{tier}/{grade}",
            "trigger": trigger,
            "%_to_trigger": pct_to_trigger,
            "VWAP_Status": vwap_status,
            "15m_Vol_vs_Req": f"{'Meets' if vol_ok else 'Below'} ({vol15:,})",
            "price": round(price, 3),
            "Catalyst": f"{catalyst_kind}: {catalyst_note}",
            "Note": "; ".join(note) if note else "",
            "_score": score
        }
    except Exception as e:
        print(f"Scan error {symbol}: {e}")
        return None

def run_scan():
    results = []
    for sym in UNIVERSE:
        row = scan_symbol(sym)
        if row: results.append(row)
    results.sort(key=lambda r: r["_score"], reverse=True)
    for r in results: r.pop("_score", None)
    return results

# -------------- Routes -----------------
@app.route("/")
def health():
    return jsonify({"ok": True, "ts": int(time.time())})

@app.route("/refresh_universe_lite", methods=["POST","GET"])
def refresh_universe_lite_route():
    res = refresh_universe()
    return jsonify({"message":"universe refreshed (lite)", **res})

@app.route("/scan")
def scan():
    global near_trigger_board
    near_trigger_board = run_scan()
    return jsonify({"message":"scan complete","count":len(near_trigger_board)})

@app.route("/board")
def board():
    return jsonify({
        "age_min": round((time.time() % 900)/60, 2),
        "count": len(near_trigger_board),
        "near_trigger_board": near_trigger_board,
        "stale": False,
        "ts": int(time.time())
    })

# ---------------------------------------
if __name__ == "__main__":
    near_trigger_board = []
    refresh_universe()  # Initialize at startup
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
