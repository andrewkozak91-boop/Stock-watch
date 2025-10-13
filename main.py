from flask import Flask, jsonify, request
import os, time, math, requests
from datetime import datetime, timedelta

app = Flask(__name__)

API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
BASE = "https://finnhub.io/api/v1"

# -------- Configuration (v7.5, slightly softened volume gates) ----------
PRICE_MAX = 30.0
FLOAT_MAX = 150_000_000          # allow override only with institutional-grade (Real) catalyst

# 15m volume gates (softer than before to avoid empty boards on thin bars/premarket)
VOLUME_SHARES_GATE = 400_000     # was 2,000,000
VOLUME_FLOAT_PCT_GATE = 0.003    # was 0.0075 (0.3% of free float)
# ------------------------------------------------------------------------

near_trigger_board = []  # in-memory cache (cleared on /scan)

# ----------------------- Finnhub helpers -----------------------

def fh(url, params=None):
    """Thin GET wrapper with token + 10s timeout."""
    params = dict(params or {})
    if API_KEY:
        params["token"] = API_KEY
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()

def get_profile(symbol):
    # profile2 has ADR flags & shareOutstanding (proxy for float when float missing)
    return fh(f"{BASE}/stock/profile2", {"symbol": symbol})

def get_quote(symbol):
    return fh(f"{BASE}/quote", {"symbol": symbol})

def get_15m_volume(symbol):
    """
    Return last single 15-min bar volume. May be 0 or None when the latest bar
    isn't populated yet (premarket/lulls). We return None if no bars.
    """
    now = int(time.time())
    frm = now - 60 * 60 * 6  # last 6 hours window
    data = fh(f"{BASE}/stock/candle", {
        "symbol": symbol, "resolution": 15, "from": frm, "to": now
    })
    if data.get("s") != "ok" or not data.get("v"):
        return None
    return int(data["v"][-1]) if data["v"][-1] is not None else None

# ----------------------- Rules / gates -----------------------

REAL_CATALYST_KEYWORDS = [
    "earnings", "guidance", "m&a", "acquisition", "merger", "takeover",
    "13d", "13g", "insider", "buyback", "repurchase", "contract",
    "partnership", "deal"
]
SPECULATIVE_KEYWORDS = ["strategic review", "pipeline", "explore options"]

def has_real_catalyst(symbol):
    """Lightweight 7-day headline scan for catalyst tags."""
    try:
        today = datetime.utcnow().date()
        frm = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        to = today.strftime("%Y-%m-%d")
        news = fh(f"{BASE}/company-news", {"symbol": symbol, "from": frm, "to": to})
        titles = " ".join((n.get("headline") or "").lower() for n in news[:25])
        if any(k in titles for k in REAL_CATALYST_KEYWORDS):
            return ("Real", "Tier-1/2 catalyst")
        if any(k in titles for k in SPECULATIVE_KEYWORDS):
            return ("Spec", "Tier-3 speculative")
    except Exception:
        pass
    return ("None", "")

def classify_tier(is_adr, catalyst_kind):
    # ADRs are Tier-2+; speculative news only Tier-3
    if catalyst_kind == "Spec":
        return ("Tier-3", "C")
    if is_adr:
        return ("Tier-2", "B")
    return ("Tier-1", "A")

def derive_trigger(symbol, price):
    # placeholder trigger = +2% over current
    trig = round(price * 1.02, 2)
    pct = f"+{round((trig / price - 1) * 100, 2)}%"
    return trig, pct

def volume_gate_ok(last15_shares, free_float, catalyst_kind):
    """
    Soften behavior:
    - If volume is missing/None/0, allow pass only when catalyst is Real.
    - Otherwise apply either absolute or float-% gate.
    """
    if not last15_shares:
        return catalyst_kind == "Real"
    gate1 = last15_shares >= VOLUME_SHARES_GATE
    gate2 = (free_float and last15_shares >= free_float * VOLUME_FLOAT_PCT_GATE)
    return gate1 or gate2

def looks_like_adr(profile):
    name = (profile.get("name") or "").upper()
    ticker = (profile.get("ticker") or "").upper()
    isin = (profile.get("isin") or "").upper()
    # crude checks; different brokers flag ADR differently
    if " ADR" in name or ticker.endswith("Y"):
        return True
    if isin and not isin.startswith("US"):  # non-US ISIN often corresponds to ADR tickers
        return True
    return False

def avoid_pharma_binary(profile):
    # very light filter to avoid classic FDA binaries: biotech/pharma microcaps
    industry = (profile.get("finnhubIndustry") or "").lower()
    return "biotechnology" in industry or "pharmaceutical" in industry

# ----------------------- Scanner -----------------------

def scan_symbol(symbol):
    try:
        profile = get_profile(symbol) or {}
        quote = get_quote(symbol) or {}
    except Exception as e:
        print(f"Profile/quote error {symbol}: {e}")
        return None

    price = float(quote.get("c") or 0.0)
    prev_close = float(quote.get("pc") or price)
    if price <= 0 or price > PRICE_MAX:
        return None

    # float proxy (shareOutstanding in millions)
    free_float = None
    so = profile.get("shareOutstanding")
    if so:
        try:
            free_float = float(so) * 1_000_000.0
        except Exception:
            free_float = None

    # catalyst
    catalyst_kind, catalyst_note = has_real_catalyst(symbol)

    # float gate: allow override only if Real catalyst
    if free_float and free_float > FLOAT_MAX and catalyst_kind != "Real":
        return None

    # avoid pharma FDA binaries by default
    if avoid_pharma_binary(profile) and catalyst_kind != "Real":
        return None

    # ADR handling
    is_adr = looks_like_adr(profile)

    # 15m volume gate
    vol15 = None
    try:
        vol15 = get_15m_volume(symbol)
    except Exception as e:
        print(f"15m vol error {symbol}: {e}")
        vol15 = None
    vol_ok = volume_gate_ok(vol15, free_float or 0, catalyst_kind)

    # VWAP proxy using prev close
    vwap_status = "Above" if price >= prev_close else "Below"

    # class/tier
    tier, grade = classify_tier(is_adr, catalyst_kind)

    # triggers
    trigger, pct_to_trigger = derive_trigger(symbol, price)

    # notes/labels
    note_bits = []
    if catalyst_kind == "Spec":
        note_bits.append("Spec PR — tiny size only")
    if is_adr:
        note_bits.append("ADR (Tier-2+)")
    if not vol_ok and (vol15 is None or vol15 == 0):
        note_bits.append("Vol unknown (premkt/thin)")
    note = "; ".join(note_bits)

    # rank score (coarse): real catalyst + vol_ok + above vwap - distance
    try:
        gap = float(pct_to_trigger.strip("%+"))
    except Exception:
        gap = 2.0
    score = (5 if catalyst_kind == "Real" else 2) + (3 if vol_ok else 0) + (2 if vwap_status == "Above" else 0) - gap

    row = {
        "symbol": symbol,
        "Tier/Grade": f"{tier}/{grade}",
        "trigger": trigger,
        "%_to_trigger": pct_to_trigger,
        "VWAP_Status": vwap_status,
        "15m_Vol_vs_Req": (
            f"Meets ({vol15:,})" if vol_ok and vol15 else
            "Unknown (—)" if (vol15 is None or vol15 == 0) else
            f"Below ({vol15:,})"
        ),
        "price": round(price, 3),
        "Catalyst": f"{catalyst_kind}: {catalyst_note}",
        "Note": note,
        "_score": score
    }
    return row

def get_universe(limit=200):
    """
    Build a tradable universe on the fly using Finnhub's US symbols endpoint,
    then filter by price and basic criteria to keep it lightweight.
    """
    syms = []
    try:
        # Pull US symbols (Finnhub returns a lot; we cap to avoid free-tier pain)
        data = fh(f"{BASE}/stock/symbol", {"exchange": "US"})
        for d in data:
            sym = d.get("symbol")
            typ = (d.get("type") or "").lower()
            if not sym or typ not in ("common stock", "etf", "adrr", "adr", "reit", "us common stock"):
                continue
            syms.append(sym)
    except Exception as e:
        print(f"universe fetch error: {e}")
        # Fallback minimal set keeps the app usable if symbol list fails
        syms = ["T", "F", "AAL", "CCL", "NCLH", "RIOT", "MARA", "SOFI", "PLTR", "PFE", "RIVN", "QS", "AI", "JOBY"]

    # Light price screen using /quote
    trimmed = []
    for s in syms[: max(2000, limit * 10)]:  # sample a chunk
        try:
            q = get_quote(s)
            p = float(q.get("c") or 0.0)
            if 0 < p <= PRICE_MAX:
                trimmed.append(s)
        except Exception:
            continue
        if len(trimmed) >= limit:
            break
    return trimmed

def run_scan(symbols):
    results = []
    for sym in symbols:
        try:
            r = scan_symbol(sym)
            if r:
                results.append(r)
        except Exception as e:
            print(f"Scan error {sym}: {e}")
            continue
    results.sort(key=lambda r: r["_score"], reverse=True)
    for r in results:
        r.pop("_score", None)
    return results

# ----------------------- Routes -----------------------

@app.route("/")
def health():
    return jsonify({"ok": True, "ts": int(time.time())})

@app.route("/universe")
def universe():
    limit = int(request.args.get("limit", "200"))
    symbols = get_universe(limit=limit)
    return jsonify({"count": len(symbols), "symbols": symbols, "ts": int(time.time())})

@app.route("/scan")
def scan():
    """
    Run a full scan on a fresh universe (or a capped one via max=?),
    store the ranked results to /board.
    """
    global near_trigger_board
    maxn = int(request.args.get("max", "200"))
    universe_syms = get_universe(limit=maxn)
    near_trigger_board = run_scan(universe_syms)
    return jsonify({"message": "scan complete", "count": len(near_trigger_board), "ts": int(time.time())})

@app.route("/board")
def board():
    return jsonify({
        "age_min": round((time.time() % 900) / 60, 2),
        "count": len(near_trigger_board),
        "near_trigger_board": near_trigger_board,
        "stale": False,
        "ts": int(time.time())
    })

# ----------------------- Main -----------------------

if __name__ == "__main__":
    near_trigger_board = []
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
