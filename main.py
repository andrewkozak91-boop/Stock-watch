from flask import Flask, jsonify, request
import os, time, threading, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo  # stdlib

app = Flask(__name__)

# ====== CONFIG ======
API_KEY = os.getenv("FINNHUB_API_KEY")
BASE = "https://finnhub.io/api/v1"
TZ = ZoneInfo("America/Toronto")

# Default universe (sub-$30, liquid themes; tweak anytime)
UNIVERSE_DEFAULT = [
    "T","PFE","F","AAL","CCL","NCLH","RIVN","HOOD","SOFI","DNA",
    "RIOT","MARA","CHPT","BLNK","RUN","PLUG","NKLA","QS","OPEN","ENVX",
    "MVIS","JOBY","UWMC","BARK","GPRO","PTON","KODK","RUM","XMTR","PIII",
    "SOUN","BBAI","CXAI","CLSK","NU","AI","COUR"
]

PRICE_MAX_DEFAULT = 30.0
FLOAT_MAX = 150_000_000

# Original 7.5 hard gates
VOLUME_SHARES_GATE_HARD = 2_000_000
VOLUME_FLOAT_PCT_GATE_HARD = 0.0075  # 0.75%

# Balanced (early-session relaxed) gates
VOLUME_SHARES_GATE_EARLY = 1_200_000
VOLUME_FLOAT_PCT_GATE_EARLY = 0.0050  # 0.50%

# Aggressive (optional mode, early session only)
VOLUME_SHARES_GATE_AGGR = 1_000_000
VOLUME_FLOAT_PCT_GATE_AGGR = 0.0040  # 0.40%

REAL_CATALYST_KEYWORDS = [
    "earnings","guidance","m&a","acquisition","merger","takeover",
    "13d","13g","insider","buyback","repurchase","contract","partnership","deal"
]
SPECULATIVE_KEYWORDS = ["strategic review","pipeline","explore options"]

# ====== STATE ======
near_trigger_board = []
last_scan_meta = {"when": 0, "count": 0, "mode": "init"}
last_error = None

# Autoscan toggle + meta
AUTOSCAN_ENABLED = True if os.getenv("AUTOSCAN", "1") == "1" else False
last_autoscan_meta = {"when": 0, "count": 0, "mode": None, "ran": False}

# ====== HELPERS ======
def _fh(url, params=None):
    global last_error
    params = params or {}
    params["token"] = API_KEY or ""
    try:
        r = requests.get(url, params=params, timeout=12)
        if r.status_code == 429:
            last_error = "Finnhub rate limit (429)"
            return {}
        r.raise_for_status()
        return r.json()
    except Exception as e:
        last_error = f"{url} -> {e}"
        return {}

def get_profile(symbol):
    return _fh(f"{BASE}/stock/profile2", {"symbol": symbol}) or {}

def get_quote(symbol):
    return _fh(f"{BASE}/quote", {"symbol": symbol}) or {}

def get_15m_volume(symbol):
    now = int(time.time())
    frm = now - 60*60*6
    data = _fh(f"{BASE}/stock/candle", {
        "symbol": symbol, "resolution": 15, "from": frm, "to": now
    }) or {}
    if data.get("s") != "ok" or not data.get("v"):
        return 0
    return int(data["v"][-1])

def looks_like_adr(profile):
    name = (profile.get("name") or "").upper()
    ticker = (profile.get("ticker") or "").upper()
    country = (profile.get("country") or "").upper()
    return ("ADR" in name) or (ticker.endswith("Y") and country not in ("USA","US","UNITED STATES"))

def has_real_catalyst(symbol):
    try:
        to = datetime.utcnow().strftime("%Y-%m-%d")
        frm = (datetime.utcnow()-timedelta(days=7)).strftime("%Y-%m-%d")
        news = _fh(f"{BASE}/company-news", {"symbol": symbol, "from": frm, "to": to}) or []
        titles = " ".join(n.get("headline","").lower() for n in news[:30])
        if any(k in titles for k in REAL_CATALYST_KEYWORDS):
            return ("Real", "Tier-1/2 catalyst")
        if any(k in titles for k in SPECULATIVE_KEYWORDS):
            return ("Spec", "Tier-3 speculative")
    except Exception:
        pass
    return ("None","")

def classify_tier(is_adr, catalyst_kind):
    if catalyst_kind == "Spec":
        return ("Tier-3","C")
    if is_adr:
        return ("Tier-2","B")
    return ("Tier-1","A")

def derive_trigger(price):
    trig = round(price * 1.02, 2)  # simple 2% over price as placeholder
    pct = f"+{round((trig/price - 1)*100, 2)}%"
    return trig, pct

def to_bool(x, default=False):
    if x is None: return default
    return str(x).lower() in ("1","true","yes","on","y")

def in_market_hours(now_tz):
    # 9:30–16:00 ET
    if now_tz.weekday() >= 5:  # weekend
        return False
    if (now_tz.hour, now_tz.minute) < (9,30):
        return False
    if (now_tz.hour, now_tz.minute) > (16,0):
        return False
    return True

def get_dynamic_gates(mode, now_tz):
    """Return (shares_gate, float_pct_gate, prox_real, prox_other)."""
    # Defaults (strict)
    shares_gate = VOLUME_SHARES_GATE_HARD
    float_gate = VOLUME_FLOAT_PCT_GATE_HARD
    prox_real = 3.0  # % to trigger allowed for real catalysts (balanced/strict)
    prox_other = 2.0 # tighter for none/spec

    if mode == "strict":
        return shares_gate, float_gate, prox_real, prox_other

    # Balanced (default)
    if (now_tz.hour, now_tz.minute) < (10,30) and in_market_hours(now_tz):
        shares_gate = VOLUME_SHARES_GATE_EARLY
        float_gate = VOLUME_FLOAT_PCT_GATE_EARLY

    if mode == "aggressive":
        # Early-session only—we still cap looseness to first 60 minutes
        if (now_tz.hour, now_tz.minute) < (10,30) and in_market_hours(now_tz):
            shares_gate = VOLUME_SHARES_GATE_AGGR
            float_gate = VOLUME_FLOAT_PCT_GATE_AGGR
            prox_real = 3.5

    return shares_gate, float_gate, prox_real, prox_other

# ====== SCAN CORE ======
def scan_one(symbol, price_max, mode="balanced"):
    now_tz = datetime.now(TZ)
    shares_gate, float_gate, prox_real, prox_other = get_dynamic_gates(mode, now_tz)

    profile = get_profile(symbol)
    quote = get_quote(symbol)
    price = float(quote.get("c") or 0)
    prev_close = float(quote.get("pc") or price)
    if price <= 0 or price > price_max:
        return None

    # float proxy from shareOutstanding (Finnhub returns in shares or millions; normalize)
    free_float = None
    so = profile.get("shareOutstanding")
    if so:
        try:
            free_float = float(so) if so > 1_000_000 else float(so)*1_000_000
        except:
            free_float = None

    is_adr = looks_like_adr(profile)
    vol15 = get_15m_volume(symbol)
    vol_ok = (vol15 >= shares_gate) or (free_float and vol15 >= free_float * float_gate)
    catalyst_kind, catalyst_note = has_real_catalyst(symbol)

    # Override: float rule can be exceeded only if Real catalyst present
    if free_float and free_float > FLOAT_MAX and catalyst_kind != "Real":
        return None

    # ADRs allowed but auto-class as Tier-2+
    vwap_status = "Above" if price >= prev_close else "Below"

    # derive a simple technical trigger 2% above
    trigger, pct_to_trigger = derive_trigger(price)
    try:
        gap = float(pct_to_trigger.replace("%","").replace("+",""))
    except:
        gap = 2.0

    # Proximity rule based on catalyst strength
    max_gap = prox_real if catalyst_kind == "Real" else prox_other
    if gap > max_gap:
        return None

    # Catalyst NONE is allowed ONLY if strict volume passes and VWAP Above
    if catalyst_kind == "None":
        if not vol_ok or vwap_status != "Above":
            return None

    # Spec PR allowed (Tier-3 tiny size only)
    tier, grade = classify_tier(is_adr, catalyst_kind)

    note = []
    if catalyst_kind == "Spec": note.append("Spec PR — tiny size only")
    if is_adr: note.append("ADR (Tier-2+)")

    # rank score
    score = (5 if catalyst_kind=="Real" else (1 if catalyst_kind=="None" else 0))
    score += 3 if vol_ok else 0
    score += 3 if vwap_status=="Above" else 0
    score -= gap

    return {
        "symbol": symbol,
        "Tier/Grade": f"{tier}/{grade}",
        "trigger": trigger,
        "%_to_trigger": pct_to_trigger,
        "VWAP_Status": vwap_status,
        "15m_Vol_vs_Req": f"{'Meets' if vol_ok else 'Below'} ({vol15:,})",
        "price": round(price,3),
        "Catalyst": f"{catalyst_kind}: {catalyst_note}",
        "Note": "; ".join(note),
        "_score": score
    }

def run_scan(universe, price_max, mode="balanced"):
    rows = []
    for sym in universe:
        try:
            r = scan_one(sym, price_max, mode=mode)
            if r: rows.append(r)
        except Exception as e:
            global last_error
            last_error = f"scan {sym}: {e}"
            continue
    rows.sort(key=lambda r: r["_score"], reverse=True)
    for r in rows:
        r.pop("_score", None)
    return rows

# ====== HTTP ROUTES ======
@app.route("/")
def root():
    return jsonify({"ok": True, "ts": int(time.time())})

@app.route("/scan")
def http_scan():
    """ /scan?mode=balanced|strict|aggressive&price_max=30&symbols=AAPL,AMD """
    global near_trigger_board, last_scan_meta
    mode = (request.args.get("mode") or "balanced").lower().strip()
    if mode not in ("balanced","strict","aggressive"):
        mode = "balanced"
    price_max = float(request.args.get("price_max") or PRICE_MAX_DEFAULT)
    syms_arg = (request.args.get("symbols") or "").strip()
    universe = [s.strip().upper() for s in syms_arg.split(",") if s.strip()] if syms_arg else UNIVERSE_DEFAULT[:]
    near_trigger_board = run_scan(universe, price_max, mode=mode)
    last_scan_meta = {"when": int(time.time()), "count": len(near_trigger_board), "mode": mode, "price_max": price_max, "universe": len(universe)}
    return jsonify({"message":"scan complete", **last_scan_meta})

@app.route("/board")
def board():
    return jsonify({
        "age_min": round((time.time() % 900)/60, 2),
        "count": len(near_trigger_board),
        "near_trigger_board": near_trigger_board,
        "stale": False,
        "ts": int(time.time())
    })

@app.route("/reset")
def reset():
    global near_trigger_board
    near_trigger_board = []
    return jsonify({"message":"board cleared","count":0})

@app.route("/diag")
def diag():
    return jsonify({
        "api_key_present": bool(API_KEY),
        "universe_default": len(UNIVERSE_DEFAULT),
        "last_scan_meta": last_scan_meta,
        "last_autoscan_meta": last_autoscan_meta,
        "autoscan_enabled": AUTOSCAN_ENABLED,
        "last_error": last_error
    })

@app.route("/autoscan")
def autoscan_toggle():
    global AUTOSCAN_ENABLED
    val = request.args.get("on")
    if val is not None:
        AUTOSCAN_ENABLED = to_bool(val, AUTOSCAN_ENABLED)
    return jsonify({"autoscan_enabled": AUTOSCAN_ENABLED})

# ====== AUTOSCAN LOOP ======
def should_run_now(now_tz: datetime):
    # 09:25 pre-bell micro-refresh (balanced)
    if now_tz.hour == 9 and now_tz.minute == 25 and now_tz.weekday() < 5:
        return True, "balanced", "09:25 micro-refresh"
    # Market hours every 15 minutes
    if now_tz.weekday() < 5 and in_market_hours(now_tz):
        if now_tz.minute in (0, 15, 30, 45):
            return True, "balanced", "market 15-min cadence"
    return False, None, None

def autoscan_loop():
    global near_trigger_board, last_autoscan_meta
    last_stamp = None
    while True:
        try:
            if AUTOSCAN_ENABLED:
                now_tz = datetime.now(TZ).replace(second=0, microsecond=0)
                run, mode, mode_desc = should_run_now(now_tz)
                stamp = now_tz.strftime("%Y-%m-%d %H:%M")
                if run and stamp != last_stamp:
                    board = run_scan(UNIVERSE_DEFAULT, PRICE_MAX_DEFAULT, mode=mode)
                    near_trigger_board[:] = board
                    last_autoscan_meta = {
                        "when": int(time.time()),
                        "count": len(board),
                        "mode": mode_desc,
                        "ran": True
                    }
                    last_stamp = stamp
        except Exception:
            pass
        time.sleep(5)

def start_autoscan_thread():
    t = threading.Thread(target=autoscan_loop, daemon=True)
    t.start()

# ====== RUN ======
if __name__ == "__main__":
    start_autoscan_thread()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
