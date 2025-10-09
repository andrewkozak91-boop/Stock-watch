from flask import Flask, jsonify, request
import os, time, math, requests
from datetime import datetime, timedelta, timezone

app = Flask(__name__)

API_KEY = os.getenv("FINNHUB_API_KEY")
BASE = "https://finnhub.io/api/v1"

# -------- Tunables ----------
PRICE_MAX = 30.0
FLOAT_MAX = 150_000_000         # 150M
VOLUME_SHARES_GATE = 2_000_000  # OR:
VOLUME_FLOAT_PCT_GATE = 0.0075  # 0.75%
MAX_UNIVERSE = int(os.getenv("MAX_UNIVERSE", "300"))  # cap daily auto universe
UNIVERSE_FALLBACK = [
    # safety net if Finnhub symbol pull is rate-limited
    "PLTR","SOFI","DNA","F","AAL","CCL","UAL","NCLH","RIVN","HOOD",
    "CHPT","LCID","NKLA","RUN","BLNK","RIOT","MARA","AI","BBD",
    "INTC","T","JOBY","PIII","XMTR","RUM","UWMC",
    "UPST","ENVX","MVIS","NU","QS","OPEN","COUR","ABNB","PFE","PTON",
    "PLUG","BBAI","SOUN"
]
# -----------------------------

near_trigger_board = []   # in-memory board cache
UNIVERSE = []             # dynamic list built daily
_last_universe_refresh = 0

# ---------- Helpers ----------
def fh(url, params=None):
    params = params or {}
    params["token"] = API_KEY
    r = requests.get(url, params=params, timeout=12)
    r.raise_for_status()
    return r.json()

def now_ts():
    return int(time.time())

def is_market_hours_toronto(ts=None):
    # America/Toronto market hours 09:30–16:00 ET (no DST library here; treat ET≈Toronto)
    ts = ts or now_ts()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()  # local tz of server
    # We won't hard-enforce here since server TZ may differ; scans are user-triggered anyway.
    return True

# ---------- Universe Builder ----------
def fetch_all_us_common():
    """
    Pull full US symbol list from Finnhub and filter to common stocks on main exchanges.
    This endpoint is available on free tiers but can be large; we only keep symbols/desc.
    """
    try:
        all_syms = fh(f"{BASE}/stock/symbol", {"exchange": "US"})
    except Exception:
        return []

    keep = []
    for s in all_syms:
        typ = (s.get("type") or "").lower()
        mic = (s.get("mic") or "").upper()
        sym = (s.get("symbol") or "").upper()
        cur = (s.get("currency") or "").upper()

        # Common stock only, USD, avoid OTC/OTCBB, and junky symbols
        if typ not in ("common stock", "cs"): 
            continue
        if cur != "USD":
            continue
        if mic in ("OTC", "PINX", "OTCB", "OTCQ", "OTCX"):
            continue
        if any(x in sym for x in (".","-","^","~","=")):  # skip weird classes/notes
            continue
        keep.append(sym)

    # Deduplicate while preserving order
    out, seen = [], set()
    for k in keep:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out

def quick_quote(symbol):
    try:
        q = fh(f"{BASE}/quote", {"symbol": symbol})
        price = float(q.get("c") or 0.0)
        pc = float(q.get("pc") or price)
        return price, pc
    except Exception:
        return 0.0, 0.0

def get_profile(symbol):
    # profile2 has ADR-ish hints and shareOutstanding (proxy for float)
    try:
        return fh(f"{BASE}/stock/profile2", {"symbol": symbol})
    except Exception:
        return {}

def maybe_adr(profile):
    # Finnhub doesn't flag ADR cleanly; heuristics only
    name = (profile.get("name") or "").upper()
    ticker = (profile.get("ticker") or "").upper()
    return ("ADR" in name) or ticker.endswith("Y")

def refresh_universe(limit=MAX_UNIVERSE, price_max=PRICE_MAX, float_max=FLOAT_MAX):
    """
    Build a fresh universe by walking all US common stocks and keeping the first N that
    pass *coarse* gates: price <= 30 (live quote) and float <= 150M (proxy via shareOutstanding).
    We stop at 'limit' to keep things fast and Finnhub-friendly.
    """
    global UNIVERSE, _last_universe_refresh

    syms = fetch_all_us_common()
    if not syms:
        # fallback to static if symbol feed failed
        UNIVERSE = UNIVERSE_FALLBACK[:limit]
        _last_universe_refresh = now_ts()
        return {"ok": True, "count": len(UNIVERSE), "source": "fallback", "ts": _last_universe_refresh}

    picked = []
    for sym in syms:
        # throttle: if we already have enough, stop
        if len(picked) >= limit:
            break

        price, _ = quick_quote(sym)
        if price <= 0 or price > price_max:
            continue

        prof = get_profile(sym)
        ff = None
        if prof.get("shareOutstanding"):
            # shareOutstanding is in *millions*; convert to shares
            try:
                ff = float(prof["shareOutstanding"]) * 1_000_000
            except Exception:
                ff = None

        # If float proxy present and exceeds max (and no institutional-grade catalyst known yet),
        # we skip here. The detailed catalyst override still happens at scan time if needed.
        if ff and ff > float_max:
            continue

        picked.append(sym)

    # If we ended up too small, pad from fallback for stability
    if len(picked) < max(50, limit // 3):
        for f in UNIVERSE_FALLBACK:
            if len(picked) >= limit:
                break
            if f not in picked:
                picked.append(f)

    UNIVERSE = picked
    _last_universe_refresh = now_ts()
    return {"ok": True, "count": len(UNIVERSE), "source": "finnhub", "ts": _last_universe_refresh}

def ensure_daily_universe():
    """
    Auto-refresh once after 09:00 America/Toronto each day, or if never built.
    We refresh lazily on the first call to /scan or /board after that time.
    """
    global _last_universe_refresh
    if not UNIVERSE:
        refresh_universe()
        return

    # Refresh if older than 20 hours (safe daily window)
    if now_ts() - _last_universe_refresh > 20 * 3600:
        refresh_universe()

# ---------- Scan Logic ----------
REAL_CATALYST_KEYWORDS = [
    "earnings","guidance","m&a","acquisition","merger","takeover",
    "13d","13g","insider","buyback","repurchase","contract","partnership","deal",
]
SPECULATIVE_KEYWORDS = ["strategic review","pipeline","explore options"]

def get_15m_volume(symbol):
    now = int(time.time())
    frm = now - 60*60*6  # last 6 hours
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

def has_real_catalyst(symbol):
    try:
        dto = datetime.utcnow()
        dfrom = dto - timedelta(days=7)
        news = fh(f"{BASE}/company-news", {
            "symbol": symbol,
            "from": dfrom.strftime("%Y-%m-%d"),
            "to": dto.strftime("%Y-%m-%d")
        })
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
        return ("Tier-3", "C")
    if is_adr:
        return ("Tier-2", "B")
    return ("Tier-1", "A")

def derive_trigger(price):
    trig = round(price * 1.02, 2)
    pct = f"+{round((trig/price-1)*100,2)}%"
    return trig, pct

def scan_symbol(symbol):
    profile = get_profile(symbol) or {}
    price, pc = quick_quote(symbol)
    if price <= 0 or price > PRICE_MAX:
        return None

    # float (fallback to shareOutstanding if float not available)
    free_float = None
    if profile.get("shareOutstanding"):
        try:
            free_float = float(profile["shareOutstanding"]) * 1_000_000
        except Exception:
            free_float = None
    if free_float and free_float > FLOAT_MAX:
        # allow override only if institutional-grade catalyst present (checked later)
        pass

    is_adr = maybe_adr(profile)

    vol15 = get_15m_volume(symbol)
    vol_ok = volume_gate_ok(vol15, free_float or 0)

    catalyst_kind, catalyst_note = has_real_catalyst(symbol)
    if free_float and free_float > FLOAT_MAX and catalyst_kind != "Real":
        return None

    vwap_status = "Above" if price >= (pc or price) else "Below"

    tier, grade = classify_tier(is_adr, catalyst_kind)
    trigger, pct_to_trigger = derive_trigger(price)

    note = []
    if catalyst_kind == "Spec":
        note.append("Spec PR — tiny size only")
    if is_adr:
        note.append("ADR (Tier-2+)")

    # rank score
    try:
        gap = float(pct_to_trigger.strip("%+"))
    except Exception:
        gap = 2.0
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

def run_scan():
    ensure_daily_universe()
    symbols = UNIVERSE if UNIVERSE else UNIVERSE_FALLBACK
    results = []
    for sym in symbols:
        try:
            row = scan_symbol(sym)
            if row:
                results.append(row)
        except Exception as e:
            # keep scanning even if some symbols fail
            continue
    results.sort(key=lambda r: r["_score"], reverse=True)
    for r in results:
        r.pop("_score", None)
    return results

# ---------- Routes ----------
@app.route("/")
def health():
    return jsonify({"ok": True, "ts": now_ts(), "universe": len(UNIVERSE)})

@app.route("/refresh_universe", methods=["POST","GET"])
def refresh_universe_route():
    res = refresh_universe()
    return jsonify({"message":"universe refreshed", **res})

@app.route("/scan")
def scan():
    mode = request.args.get("mode","balanced")
    # mode is a placeholder for future loosen/tighten, not used yet
    global near_trigger_board
    ensure_daily_universe()
    near_trigger_board = run_scan()
    return jsonify({"message":"scan complete","universe": len(UNIVERSE or UNIVERSE_FALLBACK), "count":len(near_trigger_board)})

@app.route("/board")
def board():
    return jsonify({
        "age_min": round((time.time() % 900)/60, 2),
        "count": len(near_trigger_board),
        "near_trigger_board": near_trigger_board,
        "stale": False,
        "universe": len(UNIVERSE or UNIVERSE_FALLBACK),
        "ts": now_ts()
    })

# Optional: only show names that are "ready now"
@app.route("/ready")
def ready():
    ready_names = []
    for r in near_trigger_board:
        vol_ok = str(r.get("15m_Vol_vs_Req","")).lower().startswith("meets")
        vwap_ok = r.get("VWAP_Status") == "Above"
        price_ok = r.get("price",0) >= r.get("trigger", 9e9)
        if vol_ok and vwap_ok and price_ok:
            ready_names.append(r)
    return jsonify({"count": len(ready_names), "ready": ready_names, "ts": now_ts()})

if __name__ == "__main__":
    near_trigger_board = []
    # build a universe at boot so first scan is fast
    try:
        refresh_universe()
    except Exception:
        UNIVERSE[:] = UNIVERSE_FALLBACK[:MAX_UNIVERSE]
        _last_universe_refresh = now_ts()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
