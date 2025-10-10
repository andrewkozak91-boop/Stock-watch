from flask import Flask, jsonify
import os, time, math, requests, pytz
from datetime import datetime, timedelta

app = Flask(__name__)

# ========== CONFIGURATION ==========
API_KEY = os.getenv("FINNHUB_API_KEY")
BASE = "https://finnhub.io/api/v1"

# Universe (expand or modify anytime)
UNIVERSE = [
    "PLTR","SOFI","DNA","F","AAL","CCL","UAL","NCLH","RIVN","MAR","HOOD",
    "CHPT","LCID","NKLA","RUN","BLNK","RIOT","MARA","AI","BBD","INTC",
    "PFE","T","BARK","IONQ","JOBY","PIII","XMTR","RUM","UWMC","UPST",
    "ENVX","MVIS","NU","U","QS","OPEN","COUR","ARMK","ABNB","SOUN","PTON",
    "PLUG","BBAI"
]

PRICE_MAX = 30.0
FLOAT_MAX = 150_000_000
VOLUME_SHARES_GATE = 2_000_000
VOLUME_FLOAT_PCT_GATE = 0.0075  # 0.75%

TORONTO = pytz.timezone("America/Toronto")

IGNORE_NEWS = os.getenv("DISABLE_NEWS", "0") == "1"
PREMARKET_RELAX = os.getenv("PREMARKET_RELAX", "1") == "1"

REAL_CATALYST_KEYWORDS = [
    "earnings","guidance","m&a","acquisition","merger","takeover",
    "13d","13g","insider","buyback","repurchase","contract","partnership","deal",
]
SPECULATIVE_KEYWORDS = ["strategic review","pipeline","explore options"]

near_trigger_board = []

# ===================================

def fh(url, params=None):
    params = params or {}
    params["token"] = API_KEY
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()

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

def has_real_catalyst(symbol):
    if IGNORE_NEWS:
        return ("None","")
    try:
        now = datetime.utcnow()
        since = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        today = now.strftime("%Y-%m-%d")
        news = fh(f"{BASE}/company-news", {"symbol": symbol, "from": since, "to": today})
        titles = " ".join((n.get("headline") or "").lower() for n in news[:20])
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

def derive_trigger(symbol, price):
    trig = round(price * 1.02, 2)
    return trig, f"+{round((trig/price-1)*100,2)}%"

def scan_symbol(symbol):
    profile = get_profile(symbol) or {}
    quote = get_quote(symbol) or {}

    price = quote.get("c") or 0.0
    pc = quote.get("pc") or price
    if price <= 0:
        return None
    if price > PRICE_MAX:
        return None

    free_float = None
    if profile.get("shareOutstanding"):
        free_float = float(profile["shareOutstanding"]) * 1_000_000

    is_adr = bool(profile.get("isin","").startswith("US") is False and profile.get("ticker","",).endswith("Y")) \
             or bool(profile.get("ipo","")=="" and "ADR" in (profile.get("name","").upper()+" "+profile.get("ticker","").upper()))

    vol15 = get_15m_volume(symbol)
    vol_ok = volume_gate_ok(vol15, free_float or 0)
    catalyst_kind, catalyst_note = has_real_catalyst(symbol)

    if free_float and free_float > FLOAT_MAX and catalyst_kind != "Real":
        return None

    # --- Pre-market relaxed mode ---
    now_et = datetime.now(TORONTO).time()
    premarket = now_et < datetime.strptime("09:30","%H:%M").time()
    effective_vol_ok = vol_ok or (premarket and PREMARKET_RELAX)
    # -------------------------------

    vwap_status = "Above" if price >= pc else "Below"
    tier, grade = classify_tier(is_adr, catalyst_kind)
    trigger, pct_to_trigger = derive_trigger(symbol, price)

    note = []
    if catalyst_kind == "Spec":
        note.append("Spec PR — tiny size only")
    if is_adr:
        note.append("ADR (Tier-2+)")

    try:
        gap = float(pct_to_trigger.strip("%+"))
    except:
        gap = 2.0
    score = (5 if catalyst_kind=="Real" else 2) + (3 if effective_vol_ok else 0) + (3 if vwap_status=="Above" else 0) - gap

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
    results = []
    for sym in UNIVERSE:
        try:
            row = scan_symbol(sym)
            if row:
                results.append(row)
        except Exception as e:
            print(f"Scan error {sym}: {e}")
            continue
    results.sort(key=lambda r: r["_score"], reverse=True)
    for r in results:
        r.pop("_score", None)
    return results

# ================= ROUTES =================
@app.route("/")
def health():
    return jsonify({"ok": True, "ts": int(time.time())})

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

@app.route("/diag")
def diag():
    sample = {}
    try:
        q = fh(f"{BASE}/quote", {"symbol": "SOFI"})
        sample = q
    except Exception as e:
        sample = {"error": str(e)}
    return jsonify({
        "server_ok": True,
        "universe_size": len(UNIVERSE),
        "premarket_relax": PREMARKET_RELAX,
        "ignore_news": IGNORE_NEWS,
        "sample_SOFi_quote": sample
    })

@app.route("/scan_debug")
def scan_debug():
    reasons = {"price_cap":0,"float_cap":0,"quote_zero":0,"kept":0,"other":0}
    kept = []
    for sym in UNIVERSE[:150]:
        try:
            profile = get_profile(sym) or {}
            quote = get_quote(sym) or {}
            price = quote.get("c") or 0.0
            if price <= 0:
                reasons["quote_zero"] += 1
                continue
            if price > PRICE_MAX:
                reasons["price_cap"] += 1
                continue
            ff = (float(profile["shareOutstanding"])*1_000_000) if profile.get("shareOutstanding") else None
            if ff and ff > FLOAT_MAX:
                ck,_ = has_real_catalyst(sym)
                if ck != "Real":
                    reasons["float_cap"] += 1
                    continue
            reasons["kept"] += 1
            kept.append(sym)
        except Exception:
            reasons["other"] += 1
            continue
    return jsonify({"reasons": reasons, "kept_preview": kept[:25]})

# ==========================================

if __name__ == "__main__":
    near_trigger_board = []
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
