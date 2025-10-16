from flask import Flask, jsonify, request
import os, time, math, requests
from datetime import datetime, timedelta

app = Flask(__name__)

# ========= ENV & API =========
API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
BASE = "https://finnhub.io/api/v1"

def fh(path, params=None, timeout=10):
    """Finnhub helper (gracefully handles missing key)."""
    params = (params or {}).copy()
    if API_KEY:
        params["token"] = API_KEY
    r = requests.get(f"{BASE}{path}", params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

# ========= v7.5.1 CONFIG =========
PRICE_MAX = 30.0
FLOAT_MAX = 150_000_000  # 150M
# Volume gates (loosened)
VOLUME_SHARES_GATE = 1_000_000        # was 2,000,000
VOLUME_FLOAT_PCT_GATE = 0.004         # 0.40% of float (was 0.75%)
INCREASING_BARS_N = 3                 # override on 3 rising 15m bars
INCREASING_BARS_MIN_SUM = 750_000     # safety floor for the trend override

# ========= State =========
near_trigger_board = []
UNIVERSE = [
    # Keep your existing seed list; this can be extended by calling /universe
    "PLTR","SOFI","DNA","F","AAL","CCL","UAL","NCLH","RIVN","HOOD",
    "CHPT","LCID","RUN","BLNK","RIOT","MARA","AI","BBD",
    "INTC","PFE","T","BARK","JOBY","RUM","UWMC",
    "UPST","ENVX","MVIS","NU","QS","OPEN","COUR","ARMK"
]

# ========= Utilities =========
def safe_get(d, k, default=None):
    return d.get(k, default) if isinstance(d, dict) else default

def get_profile(symbol):
    try:
        return fh("/stock/profile2", {"symbol": symbol})
    except Exception:
        return {}

def get_quote(symbol):
    try:
        return fh("/quote", {"symbol": symbol})
    except Exception:
        return {}

def get_15m_candles(symbol, lookback_hours=6):
    """Return full last 6h (or less) 15m candles dict; fallback empty."""
    try:
        now = int(time.time())
        frm = now - 60 * 60 * lookback_hours
        data = fh("/stock/candle", {"symbol": symbol, "resolution": 15, "from": frm, "to": now})
        if data.get("s") == "ok":
            return data
    except Exception:
        pass
    return {"s":"no_data", "t":[], "v":[], "c":[]}

def last_n_volumes(candles, n=INCREASING_BARS_N):
    v = candles.get("v") or []
    return v[-n:] if len(v) >= n else v

def is_strictly_increasing(arr):
    return all(arr[i] < arr[i+1] for i in range(len(arr)-1))

def volume_gate_ok(vol_last, free_float, recent_vols):
    gate1 = vol_last >= VOLUME_SHARES_GATE
    gate2 = (free_float and vol_last >= free_float * VOLUME_FLOAT_PCT_GATE)
    # v7.5.1 override: 3 rising 15m bars (accumulation)
    gate3 = False
    if len(recent_vols) >= INCREASING_BARS_N:
        if is_strictly_increasing(recent_vols[-INCREASING_BARS_N:]) and sum(recent_vols[-INCREASING_BARS_N:]) >= INCREASING_BARS_MIN_SUM:
            gate3 = True
    return gate1 or gate2 or gate3

REAL_CATALYST_KEYWORDS = [
    "earnings","guidance","m&a","acquisition","merger","takeover",
    "13d","13g","insider","buyback","repurchase","contract","partnership","deal",
]
SPECULATIVE_KEYWORDS = ["strategic review","pipeline","explore options"]

def has_real_catalyst(symbol):
    """Returns (kind, note) where kind in {'Real','Spec','None'}"""
    try:
        end = datetime.utcnow().date()
        start = end - timedelta(days=10)
        news = fh("/company-news", {"symbol": symbol, "from": start.isoformat(), "to": end.isoformat()})
        titles = " ".join((safe_get(n,"headline","") or "").lower() for n in news[:25])
        if any(k in titles for k in REAL_CATALYST_KEYWORDS):
            return ("Real","Tier-1/2 catalyst")
        if any(k in titles for k in SPECULATIVE_KEYWORDS):
            return ("Spec","Tier-3 speculative")
    except Exception:
        pass
    return ("None","")

def classify_tier(is_adr, catalyst_kind):
    # ADRs allowed but Tier-2+ only; Spec PRs => Tier-3/C
    if catalyst_kind == "Spec":
        return ("Tier-3","C")
    if is_adr:
        return ("Tier-2","B")
    return ("Tier-1","A")

def detect_adr(profile):
    # loose ADR detection (Finnhub profile lacks consistent ADR flag)
    name = (safe_get(profile,"name","") or "").upper()
    ticker = (safe_get(profile,"ticker","") or "").upper()
    isin = (safe_get(profile,"isin","") or "")
    # Basic heuristics
    if "ADR" in name or ticker.endswith("Y"):
        return True
    if isin and not isin.startswith("US"):
        return True
    return False

def derive_trigger(price):
    # simple placeholder trigger: +2%
    trig = round(price * 1.02, 2)
    pct = round((trig/price - 1) * 100, 2)
    return trig, f"+{pct}%"

def scan_one(symbol):
    profile = get_profile(symbol)
    quote = get_quote(symbol)

    price = float(safe_get(quote,"c",0.0) or 0.0)
    prev_close = float(safe_get(quote,"pc",0.0) or price)
    if price <= 0: 
        return None
    if price > PRICE_MAX:
        return None

    # float proxy (shareOutstanding in *millions* on Finnhub)
    free_float = None
    so = safe_get(profile,"shareOutstanding")
    if so:
        try:
            free_float = float(so) * 1_000_000
        except Exception:
            free_float = None

    # ADR handling
    is_adr = detect_adr(profile)

    # candles & volume gates
    candles = get_15m_candles(symbol)
    vols = candles.get("v") or []
    vol_last = int(vols[-1]) if vols else 0
    vol_ok = volume_gate_ok(vol_last, free_float or 0, vols)

    # catalysts
    catalyst_kind, catalyst_note = has_real_catalyst(symbol)

    # v7.5 float override: only if institutional-grade (Real) catalyst
    if free_float and free_float > FLOAT_MAX and catalyst_kind != "Real":
        return None

    # v7.5.1 catalyst override for volume: allow Tier-2 real + >=5% day move
    day_change_pct = 0.0
    if prev_close > 0:
        day_change_pct = (price/prev_close - 1.0) * 100.0
    if not vol_ok and catalyst_kind == "Real" and day_change_pct >= 5.0:
        vol_ok = True

    # VWAP proxy = price vs prev_close (true VWAP needs tick/1m data)
    vwap_status = "Above" if price >= prev_close else "Below"

    # classification
    tier, grade = classify_tier(is_adr, catalyst_kind)
    trigger, pct_to_trigger = derive_trigger(price)

    note_bits = []
    if catalyst_kind == "Spec":
        note_bits.append("Spec PR — tiny size only")
    if is_adr:
        note_bits.append("ADR (Tier-2+)")
    if len(vols) >= INCREASING_BARS_N and is_strictly_increasing(vols[-INCREASING_BARS_N:]):
        note_bits.append("Rising 15m volume (accumulation)")

    # score: real catalyst + vol_ok + vwap + closeness
    try:
        gap = float(pct_to_trigger.strip("%+"))
    except Exception:
        gap = 2.0
    score = (5 if catalyst_kind=="Real" else 2) + (3 if vol_ok else 0) + (2 if vwap_status=="Above" else 0) - gap

    return {
        "symbol": symbol,
        "Tier/Grade": f"{tier}/{grade}",
        "trigger": trigger,
        "%_to_trigger": pct_to_trigger,
        "VWAP_Status": vwap_status,
        "15m_Vol": vol_last,
        "Vol_OK": vol_ok,
        "Catalyst": catalyst_kind,
        "price": round(price, 3),
        "Note": "; ".join(note_bits),
        "Score": round(score, 2)
    }

def run_scan(symbols):
    rows = []
    for s in symbols:
        try:
            r = scan_one(s)
            if r: rows.append(r)
        except Exception:
            continue
    rows.sort(key=lambda x: x["Score"], reverse=True)
    return rows

# ========= Routes =========
@app.route("/")
def health():
    return jsonify({"ok": True, "ts": int(time.time())})

@app.route("/universe")
def universe():
    """
    Extends/refreshes the in-memory universe list.
    If FINNHUB_API_KEY present, try to fetch US symbols and keep up to 300 sub-$30 names
    with valid quotes. Otherwise, keep seed list.
    """
    global UNIVERSE
    limit = int(request.args.get("limit", "300"))
    force = request.args.get("force", "0") == "1"

    if not API_KEY and not force:
        return jsonify({"count": len(UNIVERSE), "symbols": UNIVERSE[:limit], "ts": int(time.time())})

    try:
        # pull a broad US symbols list; then sample by live quote price < 30
        syms = fh("/stock/symbol", {"exchange": "US"})
        picked = []
        for item in syms:
            sym = item.get("symbol")
            if not sym or not sym.isupper(): 
                continue
            # quick price check
            try:
                q = get_quote(sym)
                px = float(q.get("c") or 0.0)
            except Exception:
                px = 0.0
            if 0 < px <= PRICE_MAX:
                picked.append(sym)
            if len(picked) >= limit:
                break

        if picked:
            UNIVERSE = picked
        return jsonify({"count": len(UNIVERSE), "symbols": UNIVERSE[:limit], "ts": int(time.time())})
    except Exception:
        # fallback to whatever we already have
        return jsonify({"count": len(UNIVERSE), "symbols": UNIVERSE[:limit], "ts": int(time.time())})

@app.route("/scan")
def scan():
    """Run the v7.5.1 scan over the current universe."""
    global near_trigger_board
    near_trigger_board = run_scan(UNIVERSE)
    return jsonify({"count": len(near_trigger_board), "near_trigger_board": near_trigger_board, "ts": int(time.time())})

@app.route("/board")
def board():
    """Return the current ranked board (last scan results)."""
    return jsonify({"count": len(near_trigger_board), "near_trigger_board": near_trigger_board, "ts": int(time.time())})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","10000")))
