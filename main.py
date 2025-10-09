from flask import Flask, jsonify, request
import os, time, threading, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo  # stdlib, no extra dependency

app = Flask(__name__)

# ====== CONFIG ======
API_KEY = os.getenv("FINNHUB_API_KEY")
BASE = "https://finnhub.io/api/v1"
TZ = ZoneInfo("America/Toronto")

UNIVERSE_DEFAULT = [
    "T","PFE","F","AAL","CCL","NCLH","RIVN","HOOD","SOFI","DNA",
    "RIOT","MARA","CHPT","BLNK","RUN","PLUG","NKLA","QS","OPEN","ENVX",
    "MVIS","JOBY","UWMC","BARK","GPRO","PTON","KODK","RUM","XMTR","PIII",
    "SOUN","BBAI","CXAI","CLSK","NU","AI","COUR"
]

PRICE_MAX_DEFAULT = 30.0
FLOAT_MAX = 150_000_000
VOLUME_SHARES_GATE = 2_000_000
VOLUME_FLOAT_PCT_GATE = 0.0075

REAL_CATALYST_KEYWORDS = [
    "earnings","guidance","m&a","acquisition","merger","takeover",
    "13d","13g","insider","buyback","repurchase","contract","partnership","deal"
]
SPECULATIVE_KEYWORDS = ["strategic review","pipeline","explore options"]

# ====== STATE ======
near_trigger_board = []
last_scan_meta = {"when": 0, "count": 0, "mode": "init"}
last_error = None

# Autoscan (in-process, no cron)
AUTOSCAN_ENABLED = True if os.getenv("AUTOSCAN", "1") == "1" else False
last_autoscan_meta = {"when": 0, "count": 0, "mode": None, "ran": False}


# ====== FINNHUB HELPERS ======
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

def volume_gate_ok(vol15, free_float):
    gate1 = vol15 >= VOLUME_SHARES_GATE
    gate2 = free_float and vol15 >= free_float * VOLUME_FLOAT_PCT_GATE
    return gate1 or gate2

def has_real_catalyst(symbol):
    try:
        to = datetime.utcnow().strftime("%Y-%m-%d")
        frm = (datetime.utcnow()-timedelta(days=7)).strftime("%Y-%m-%d")
        news = _fh(f"{BASE}/company-news", {"symbol": symbol, "from": frm, "to": to}) or []
        titles = " ".join(n.get("headline","").lower() for n in news[:25])
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

def looks_like_adr(profile):
    name = (profile.get("name") or "").upper()
    ticker = (profile.get("ticker") or "").upper()
    country = (profile.get("country") or "").upper()
    return ("ADR" in name) or (ticker.endswith("Y") and country not in ("USA","US","UNITED STATES"))

def derive_trigger(price):
    trig = round(price * 1.02, 2)
    pct = f"+{round((trig/price - 1)*100, 2)}%"
    return trig, pct

def to_bool(x, default=False):
    if x is None: return default
    return str(x).lower() in ("1","true","yes","on","y")


# ====== SCAN CORE ======
def scan_one(symbol, price_max, strict_volume=True, fast=False):
    profile = {} if fast else get_profile(symbol)
    quote = get_quote(symbol)
    price = float(quote.get("c") or 0)
    prev_close = float(quote.get("pc") or price)
    if price <= 0 or price > price_max:
        return None

    free_float = None
    if not fast:
        so = profile.get("shareOutstanding")
        if so:
            try:
                free_float = float(so) if so > 1_000_000 else float(so)*1_000_000
            except:
                free_float = None

    is_adr = looks_like_adr(profile) if not fast else False
    vol15 = get_15m_volume(symbol)
    vol_ok = volume_gate_ok(vol15, free_float or 0)
    catalyst_kind, catalyst_note = ("None","") if fast else has_real_catalyst(symbol)

    if not fast and free_float and free_float > FLOAT_MAX and catalyst_kind != "Real":
        return None
    if strict_volume and not vol_ok:
        return None

    vwap_status = "Above" if price >= prev_close else "Below"
    tier, grade = classify_tier(is_adr, catalyst_kind)
    trigger, pct_to_trigger = derive_trigger(price)

    note = []
    if catalyst_kind == "Spec": note.append("Spec PR — tiny size only")
    if is_adr: note.append("ADR (Tier-2+)")

    try:
        gap = float(pct_to_trigger.replace("%","").replace("+",""))
    except:
        gap = 2.0
    score = (5 if catalyst_kind=="Real" else 2) + (3 if vol_ok else 0) + (3 if vwap_status=="Above" else 0) - gap

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

def run_scan(universe, price_max, strict_volume=True, fast=False):
    rows = []
    for sym in universe:
        try:
            r = scan_one(sym, price_max, strict_volume, fast)
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
    """ /scan?fast=1&force=1&price_max=30&symbols=AAPL,AMD """
    global near_trigger_board, last_scan_meta
    fast = to_bool(request.args.get("fast"), False)
    force = to_bool(request.args.get("force"), False)
    price_max = float(request.args.get("price_max") or PRICE_MAX_DEFAULT)
    syms_arg = (request.args.get("symbols") or "").strip()
    universe = [s.strip().upper() for s in syms_arg.split(",") if s.strip()] if syms_arg else UNIVERSE_DEFAULT[:]
    mode = ("fast-" if fast else "") + ("lenient" if force else "strict")
    near_trigger_board = run_scan(universe, price_max, strict_volume=not force, fast=fast)
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


# ====== AUTOSCAN LOOP (runs in background) ======
def should_run_now(now_tz: datetime):
    """Return (run, fast, force, mode_string) for current time in Toronto."""
    # Pre-bell micro-refresh at 09:25 (fast lenient)
    if now_tz.hour == 9 and now_tz.minute == 25:
        return True, True, True, "fast-lenient (09:25 micro-refresh)"
    # Market hours strict every 20 minutes (e.g., :10, :30, :50)
    if 9 <= now_tz.hour <= 15 or (now_tz.hour == 16 and now_tz.minute == 0):
        if now_tz.hour < 9 or (now_tz.hour == 9 and now_tz.minute < 30):
            return False, False, False, None  # before 9:30
        if now_tz.minute in (10, 30, 50):
            return True, False, False, "strict (20-min cadence)"
    return False, False, False, None

def autoscan_loop():
    global near_trigger_board, last_autoscan_meta
    # To avoid duplicate runs within the same minute
    last_run_stamp = None
    while True:
        try:
            if AUTOSCAN_ENABLED:
                now_tz = datetime.now(TZ).replace(second=0, microsecond=0)
                stamp = now_tz.strftime("%Y-%m-%d %H:%M")
                run, fast, force, mode = should_run_now(now_tz)
                if run and stamp != last_run_stamp:
                    # run scan
                    board = run_scan(UNIVERSE_DEFAULT, PRICE_MAX_DEFAULT, strict_volume=not force, fast=fast)
                    near_trigger_board[:] = board
                    last_autoscan_meta = {
                        "when": int(time.time()),
                        "count": len(board),
                        "mode": mode,
                        "ran": True
                    }
                    last_run_stamp = stamp
        except Exception as e:
            # swallow and keep loop alive
            pass
        time.sleep(5)  # check every 5s

# Start background thread (daemon)
def start_autoscan_thread():
    t = threading.Thread(target=autoscan_loop, daemon=True)
    t.start()

# ====== RUN LOCAL ======
if __name__ == "__main__":
    start_autoscan_thread()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
