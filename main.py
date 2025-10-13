from flask import Flask, jsonify, request
import os, time, math, requests
from datetime import datetime, timedelta, timezone

# ----------------- App & Config -----------------
app = Flask(__name__)
API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
BASE = "https://finnhub.io/api/v1"

# 7.5 rules (edit if you want tighter/looser)
PRICE_MAX = float(os.getenv("PRICE_MAX", "30"))
FLOAT_MAX = int(os.getenv("FLOAT_MAX", "150000000"))       # 150M
VOLUME_SHARES_GATE = int(os.getenv("VOLUME_SHARES_GATE", "2000000"))
VOLUME_FLOAT_PCT_GATE = float(os.getenv("VOLUME_FLOAT_PCT_GATE", "0.0075"))
NEWS_LOOKBACK_DAYS = int(os.getenv("NEWS_LOOKBACK_DAYS", "7"))
AUTO_UNIVERSE_LIMIT = int(os.getenv("AUTO_UNIVERSE_LIMIT", "250"))  # cap to stay within free rate limit
CANDLE_RES = 15   # minutes for volume gate

# in-memory state
universe = []            # list[str]
near_trigger_board = []  # list[dict]
last_universe_built_at = 0
last_scan_ran_at = 0

# ----------------- Helpers -----------------
def fh(path, params=None, timeout=10):
    """Finnhub GET with basic error handling."""
    if not API_KEY:
        raise RuntimeError("Missing FINNHUB_API_KEY")
    params = params or {}
    params["token"] = API_KEY
    r = requests.get(f"{BASE}{path}", params=params, timeout=timeout)
    # 403 on free tier happens if key is wrong or plan doesn’t allow an endpoint
    if r.status_code == 403:
        raise RuntimeError("Finnhub 403 (Forbidden): check key/plan or rate limit.")
    r.raise_for_status()
    return r.json()

def get_symbols_auto(limit=AUTO_UNIVERSE_LIMIT):
    """
    Pull a broad list of US common stocks. We filter to USD common stock,
    exclude pink sheets/OTC as best as possible and cap the count.
    """
    raw = fh("/stock/symbol", {"exchange": "US"})
    # Keep common stock, USD, active, normal length tickers (no weird exts)
    cand = []
    for row in raw:
        if (row.get("type") == "Common Stock" and
            row.get("currency") == "USD" and
            row.get("symbol") and
            len(row["symbol"]) <= 5):
            cand.append(row["symbol"])
        if len(cand) >= limit:
            break
    return cand

def get_profile(symbol):
    # profile2 has ADR-ish hints + shares outstanding (as proxy float when true float isn’t available)
    return fh("/stock/profile2", {"symbol": symbol})

def get_quote(symbol):
    return fh("/quote", {"symbol": symbol})

def get_last15_volume(symbol):
    now = int(time.time())
    frm = now - 60 * 60 * 6  # last 6 hours to cover session
    data = fh("/stock/candle", {
        "symbol": symbol,
        "resolution": CANDLE_RES,
        "from": frm,
        "to": now
    })
    if data.get("s") != "ok" or not data.get("v"):
        return 0
    return int(data["v"][-1])

def volume_gate_ok(last15, free_float):
    gate1 = last15 >= VOLUME_SHARES_GATE
    gate2 = (free_float and last15 >= free_float * VOLUME_FLOAT_PCT_GATE)
    return gate1 or gate2

REAL_CATALYST_KEYWORDS = [
    "earnings","guidance","m&a","acquisition","merger","takeover",
    "13d","13g","insider","buyback","repurchase","contract","partnership","deal",
]
SPECULATIVE_KEYWORDS = ["strategic review","pipeline","explore options"]

def catalyst_kind(symbol):
    """Lightweight headline scan for last N days."""
    try:
        to_dt = datetime.utcnow()
        fr_dt = to_dt - timedelta(days=NEWS_LOOKBACK_DAYS)
        news = fh("/company-news", {
            "symbol": symbol,
            "from": fr_dt.strftime("%Y-%m-%d"),
            "to": to_dt.strftime("%Y-%m-%d")
        })
        titles = " ".join(n.get("headline","").lower() for n in news[:25])
        if any(k in titles for k in REAL_CATALYST_KEYWORDS):
            return ("Real", "Tier-1/2 catalyst")
        if any(k in titles for k in SPECULATIVE_KEYWORDS):
            return ("Spec", "Tier-3 speculative")
    except Exception:
        pass
    return ("None", "")

def classify_tier(is_adr, cat_kind):
    # ADRs allowed but Tier-2+ only; spec PRs => Tier-3 tiny size
    if cat_kind == "Spec":
        return ("Tier-3", "C")
    if is_adr:
        return ("Tier-2", "B")
    return ("Tier-1", "A")

def derive_trigger(px):
    trg = round(px * 1.02, 2)   # +2% rough trigger
    pct = f"+{round((trg/px-1)*100,2)}%"
    return trg, pct

def scan_symbol(sym):
    try:
        prof = get_profile(sym) or {}
        q = get_quote(sym) or {}
        price = float(q.get("c") or 0.0)
        pc = float(q.get("pc") or price)

        if price <= 0 or price > PRICE_MAX:
            return None

        # float proxy
        free_float = None
        so = prof.get("shareOutstanding")
        if so:
            # Finnhub returns millions; convert to shares
            free_float = float(so) * 1_000_000

        # ADR heuristic
        name = (prof.get("name") or "") + " " + (prof.get("ticker") or "")
        is_adr = " ADR" in name.upper() or (prof.get("ticker","").upper().endswith("Y") and prof.get("exchange") != "US")

        # last 15m volume
        v15 = get_last15_volume(sym)
        vol_ok = volume_gate_ok(v15, free_float or 0)

        c_kind, c_note = catalyst_kind(sym)

        # float override only if institutional-grade catalyst present (Real)
        if free_float and free_float > FLOAT_MAX and c_kind != "Real":
            return None

        vwap_status = "Above" if price >= pc else "Below"
        tier, grade = classify_tier(is_adr, c_kind)
        trigger, pct_to_trigger = derive_trigger(price)

        note = []
        if c_kind == "Spec":
            note.append("Spec PR — tiny size only")
        if is_adr:
            note.append("ADR (Tier-2+)")

        # ranking score: closer to trigger + real catalyst + vol + VWAP
        try:
            gap = float(pct_to_trigger.strip("%+"))
        except:
            gap = 2.0
        score = (5 if c_kind == "Real" else 2) + (3 if vol_ok else 0) + (3 if vwap_status == "Above" else 0) - gap

        return {
            "symbol": sym,
            "Tier/Grade": f"{tier}/{grade}",
            "trigger": trigger,
            "%_to_trigger": pct_to_trigger,
            "VWAP_Status": vwap_status,
            "15m_Vol_vs_Req": f"{'Meets' if vol_ok else 'Below'} ({v15:,})",
            "price": round(price, 3),
            "Catalyst": f"{c_kind}: {c_note}",
            "Note": "; ".join(note) if note else "",
            "_score": round(score, 3),
        }
    except Exception as e:
        print(f"Scan error {sym}: {e}")
        return None

# ----------------- Routes -----------------
@app.get("/")
def health():
    return jsonify(ok=True, ts=int(time.time()))

@app.post("/reset")
def reset():
    global universe, near_trigger_board, last_universe_built_at, last_scan_ran_at
    universe = []
    near_trigger_board = []
    last_universe_built_at = 0
    last_scan_ran_at = 0
    return jsonify(message="reset", ts=int(time.time()))

@app.get("/universe")
def build_universe():
    """
    Auto-build universe from Finnhub symbol list.
    Query params:
      limit  -> max symbols to include (default AUTO_UNIVERSE_LIMIT)
    """
    global universe, last_universe_built_at
    limit = int(request.args.get("limit") or AUTO_UNIVERSE_LIMIT)
    try:
        universe = get_symbols_auto(limit=limit)
        last_universe_built_at = int(time.time())
        return jsonify(count=len(universe), universe=universe[:50], ts=last_universe_built_at)
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.get("/scan")
def scan():
    """
    Run the 7.5 scan over the current universe.
    Query params:
      max    -> max names to evaluate this run (prevent timeouts). Default 150
      offset -> start index in the universe (for chunked runs)
    """
    global universe, near_trigger_board, last_scan_ran_at
    if not universe:
        return jsonify(message="no universe; call /universe first"), 400

    maxn = int(request.args.get("max") or 150)
    offset = int(request.args.get("offset") or 0)
    syms = universe[offset: offset+maxn]

    results = []
    for s in syms:
        row = scan_symbol(s)
        if row:
            results.append(row)

    results.sort(key=lambda r: r["_score"], reverse=True)
    for r in results:
        r.pop("_score", None)

    near_trigger_board = results
    last_scan_ran_at = int(time.time())
    return jsonify(message="scan complete", count=len(near_trigger_board), ts=last_scan_ran_at)

@app.get("/board")
def board():
    age_min = round(((time.time() - last_scan_ran_at) / 60.0) if last_scan_ran_at else 0.0, 2)
    return jsonify(
        age_min=age_min,
        count=len(near_trigger_board),
        near_trigger_board=near_trigger_board,
        stale=False,
        ts=int(time.time())
    )

# --------------- Serve ----------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
