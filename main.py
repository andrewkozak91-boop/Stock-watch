from flask import Flask, jsonify
import os, time, requests, re

app = Flask(__name__)

# --------- Version 7.5 gates (lite but faithful) ---------
PRICE_MAX = 30.0
FLOAT_MAX = 150_000_000        # 150M
VOL_SHARES_GATE = 2_000_000    # last 15m bar
VOL_FLOAT_GATE  = 0.0075       # 0.75% of float

REAL_CATALYST_WORDS = [
    "earnings","guidance","m&a","acquisition","merger","takeover",
    "13d","13g","insider","buyback","repurchase","contract","partnership","deal"
]
SPECULATIVE_WORDS   = ["strategic review","pipeline","explore options"]
BIOTECH_BAD_WORDS   = ["biotech","biopharma","pharmaceutical","drug manufacturer","drug manufacturers"]

# --------- Yahoo (no key) ---------
YQ = "https://query1.finance.yahoo.com"
UA = {"User-Agent":"Mozilla/5.0 (compatible; StockWatch75/1.0)"}

def yget(url, params=None):
    r = requests.get(url, params=params or {}, headers=UA, timeout=12)
    r.raise_for_status()
    return r.json()

# ---- Auto-universe sources (no key) ----
PRESETS = [
    "most_actives",      # top liquid movers
    "day_gainers",       # strong momentum
    "day_losers",        # capitulation bases (still filtered later)
    "undervalued_growth_stocks",  # adds variety under $30 sometimes
]
TREND_COUNTRIES = ["US"]  # extend if you want

def fetch_preset(scr_id, count=120):
    out = []
    try:
        j = yget(f"{YQ}/v1/finance/screener/predefined/saved",
                 {"count": count, "scrIds": scr_id})
        res = (j.get("finance",{}).get("result") or [])
        if not res: return out
        for q in res[0].get("quotes",[]) or []:
            sym = q.get("symbol")
            if sym: out.append(sym)
    except Exception:
        pass
    return out

def fetch_trending(country="US"):
    out = []
    try:
        j = yget(f"{YQ}/v1/finance/trending/{country}")
        res = (j.get("finance",{}).get("result") or [])
        if not res: return out
        for it in res[0].get("quotes",[]) or []:
            sym = it.get("symbol")
            if sym: out.append(sym)
    except Exception:
        pass
    return out

def build_universe():
    # Pull from multiple live sources, dedupe, keep only U.S. commons that *can* pass < $30 later
    syms = []
    for s in PRESETS:
        syms.extend(fetch_preset(s, count=150))
    for c in TREND_COUNTRIES:
        syms.extend(fetch_trending(c))
    # Dedup while preserving order
    seen, uniq = set(), []
    for s in syms:
        if s and s not in seen:
            seen.add(s); uniq.append(s)
    # Trim huge OTC/PNK noise by quick quote filter (only keep symbols that return a quote)
    trimmed = []
    for s in uniq[:800]:  # safety cap
        q = y_quote(s)
        if q.get("price") is not None:
            trimmed.append(s)
    return trimmed

# ---- Yahoo quote/profile/volume/news helpers ----
def y_quote(symbol):
    try:
        data = yget(f"{YQ}/v7/finance/quote", {"symbols": symbol})
        q = (data.get("quoteResponse",{}).get("result") or [{}])[0]
    except Exception:
        q = {}
    return {
        "symbol": q.get("symbol", symbol),
        "price": q.get("regularMarketPrice"),
        "prevClose": q.get("regularMarketPreviousClose"),
        "exchange": q.get("fullExchangeName",""),
        "shortName": q.get("shortName",""),
        "marketCap": q.get("marketCap")
    }

def y_profile(symbol):
    try:
        j = yget(f"{YQ}/v10/finance/quoteSummary/{symbol}",
                 {"modules":"price,summaryProfile,defaultKeyStatistics"})
        res = (j.get("quoteSummary",{}).get("result") or [{}])[0]
    except Exception:
        res = {}
    stats = res.get("defaultKeyStatistics",{}) or {}
    prof  = res.get("summaryProfile",{}) or {}
    price_mod = res.get("price",{}) or {}
    float_sh = (stats.get("floatShares") or {}).get("raw")
    out_sh   = (stats.get("sharesOutstanding") or {}).get("raw")
    sector   = (prof.get("sector") or "")
    industry = (prof.get("industry") or "")
    sym = price_mod.get("symbol", symbol) or symbol
    exch= price_mod.get("exchangeName","") or ""
    # ADR heuristic
    is_adr = bool(sym.endswith("Y") or "PNK" in exch or "OTC" in exch)
    return {"float": float_sh, "outstanding": out_sh, "sector": sector, "industry": industry, "is_adr": is_adr}

def y_15m_last_volume(symbol):
    try:
        data = yget(f"{YQ}/v8/finance/chart/{symbol}",
                    {"range":"1d","interval":"15m","includePrePost":"true"})
        res = (data.get("chart",{}).get("result") or [])
        if not res: return 0
        vols = (res[0].get("indicators",{}).get("quote") or [{}])[0].get("volume") or []
        for v in reversed(vols):
            if v is not None:
                return int(v)
        return 0
    except Exception:
        return 0

def y_recent_headlines(symbol, limit=12):
    try:
        j = yget(f"{YQ}/v1/finance/search", {"q": symbol, "quotesCount": 0, "newsCount": limit})
        news = j.get("news",[]) or []
        return " ".join((n.get("title","") or "") for n in news).lower()
    except Exception:
        return ""

# --------- 7.5 logic helpers ---------
def avoid_fda(industry, sector):
    blob = f"{sector} {industry}".lower()
    return any(k in blob for k in BIOTECH_BAD_WORDS)

def has_real_catalyst(headlines):
    if any(w in headlines for w in REAL_CATALYST_WORDS):
        return ("Real","Tier-1/2 catalyst")
    if any(w in headlines for w in SPECULATIVE_WORDS):
        return ("Spec","Tier-3 speculative")
    return ("None","")

def classify(is_adr, catalyst_kind):
    if catalyst_kind == "Spec":
        return ("Tier-3","C")
    if is_adr:
        return ("Tier-2","B")
    return ("Tier-1","A")

def passes_vol(last15, free_float):
    gate1 = last15 >= VOL_SHARES_GATE
    gate2 = bool(free_float) and last15 >= free_float * VOL_FLOAT_GATE
    return gate1 or gate2

def derive_trigger(price):
    trg = round(price * 1.02, 2)
    pct = round((trg/price - 1) * 100, 2)
    return trg, f"+{pct}%"

# --------- Scanner ---------
UNIVERSE = []
NEAR_TRIGGER = []
LAST_SCAN_TS = 0
LAST_UNIVERSE_TS = 0
UNIVERSE_SRC = {"presets": PRESETS, "trending": TREND_COUNTRIES}

def scan_one(symbol):
    q = y_quote(symbol)
    price = q.get("price")
    if not price or price <= 0 or price > PRICE_MAX:
        return None

    prof = y_profile(symbol)
    if avoid_fda(prof.get("industry",""), prof.get("sector","")):
        return None

    free_float = prof.get("float") or prof.get("outstanding")
    if not free_float and q.get("marketCap"):
        try: free_float = int(q["marketCap"] / price)
        except Exception: free_float = None

    headlines = y_recent_headlines(symbol)
    catalyst_kind, catalyst_note = has_real_catalyst(headlines)

    if free_float and free_float > FLOAT_MAX and catalyst_kind != "Real":
        return None

    v15 = y_15m_last_volume(symbol)
    vol_ok = passes_vol(v15, free_float or 0)

    prev = q.get("prevClose") or price
    vwap_status = "Above" if price >= prev else "Below"

    is_adr = bool(prof.get("is_adr"))
    tier, grade = classify(is_adr, catalyst_kind)
    trigger, pct_to_trigger = derive_trigger(price)

    gap = float(pct_to_trigger.strip("%+")) if re.search(r"[\d.]+", pct_to_trigger) else 2.0
    score = (5 if catalyst_kind=="Real" else 2) + (3 if vol_ok else 0) + (2 if vwap_status=="Above" else 0) - gap

    note = []
    if catalyst_kind == "Spec": note.append("Spec PR — tiny size only")
    if is_adr: note.append("ADR (Tier-2+)")
    if not vol_ok: note.append("Below 15m vol gate")

    return {
        "symbol": symbol,
        "Tier/Grade": f"{tier}/{grade}",
        "trigger": trigger,
        "% to Trigger": pct_to_trigger,
        "VWAP Status": vwap_status,
        "15m Vol vs Req": f"{'Meets' if vol_ok else 'Below'} ({v15:,})",
        "price": round(price, 3),
        "Catalyst": f"{catalyst_kind}: {catalyst_note}",
        "Note": "; ".join(note),
        "_score": score
    }

def refresh_universe():
    global UNIVERSE, LAST_UNIVERSE_TS
    UNIVERSE = build_universe()
    LAST_UNIVERSE_TS = int(time.time())
    return UNIVERSE

def run_scan():
    global NEAR_TRIGGER, LAST_SCAN_TS
    # always refresh universe right before scan (keeps it automatic)
    if not UNIVERSE or (time.time() - LAST_UNIVERSE_TS) > 15*60:
        refresh_universe()
    out = []
    for s in UNIVERSE[:600]:  # safety cap
        try:
            row = scan_one(s)
            if row: out.append(row)
        except Exception as e:
            print("scan error", s, e)
            continue
    out.sort(key=lambda r: r["_score"], reverse=True)
    for r in out: r.pop("_score", None)
    NEAR_TRIGGER = out
    LAST_SCAN_TS = int(time.time())
    return out

# --------- Routes ---------
@app.route("/")
def health():
    return jsonify({
        "ok": True,
        "version": "7.5-auto (Yahoo/no-key)",
        "universe_count": len(UNIVERSE),
        "last_universe_min": round((time.time()-LAST_UNIVERSE_TS)/60,2) if LAST_UNIVERSE_TS else None
    })

@app.route("/universe")
def universe():
    return jsonify({
        "count": len(UNIVERSE),
        "source": UNIVERSE_SRC,
        "last_refresh_ts": LAST_UNIVERSE_TS,
        "symbols": UNIVERSE[:200]  # preview first 200 to keep payload small
    })

@app.route("/refresh-universe")
def refresh_universe_route():
    syms = refresh_universe()
    return jsonify({"message":"universe refreshed","count":len(syms)})

@app.route("/scan")
def scan_route():
    res = run_scan()
    return jsonify({"message":"scan complete","count":len(res)})

@app.route("/board")
def board():
    age = round((time.time()-LAST_SCAN_TS)/60,2) if LAST_SCAN_TS else None
    return jsonify({
        "age_min": age,
        "count": len(NEAR_TRIGGER),
        "near_trigger_board": NEAR_TRIGGER[:150],  # trim payload
        "stale": not bool(LAST_SCAN_TS),
        "ts": int(time.time())
    })

if __name__ == "__main__":
    # eager warm-up to avoid first blank board
    try: refresh_universe()
    except Exception as e: print("warmup universe error:", e)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","10000")))
