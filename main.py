from flask import Flask, jsonify, request
import os, time, datetime as dt
import finnhub
import math

app = Flask(__name__)

# ====== YOUR API KEY ======
FINNHUB_API_KEY = "d3ir0o9r01qrurai8t9gd3ir0o9r01qrurai8ta0"
fh = finnhub.Client(api_key=FINNHUB_API_KEY)

# ====== CACHE for last scan ======
CACHE = {"board": {"count": 0, "near_trigger_board": [], "ts": 0}}

# ====== STARTER UNIVERSE (safe size for free tier / rate limits) ======
UNIVERSE = [
    "AMD","PLTR","APLD","BBAI","NOK","AI","ONDS","MVIS","SOFI",
    "F","T","RIOT","MARA","CHPT","DKNG","RUN","ENVX","IQ","LCID",
    "RIVN","XPEV","NIO","BILI","SWN","CHK","CANO","SOUN","ARMN",
]

# ====== RULES (v7.5 – simplified gating to avoid heavy calls) ======
PRICE_CEIL = 30.0
FLOAT_CEIL = 150_000_000
VOL_REQ_MIN = 2_000_000         # 15m fresh bar floor
VOL_REQ_FLOAT_PCT = 0.0075      # 0.75% of free float
MINUTES_BACK_FOR_VWAP = 240     # use up to last ~4 hours of 1m for intraday VWAP

def now_ts():
    return int(time.time())

def toronto_market_open_utc_ts():
    # 9:30 America/Toronto approximated as UTC-4 (EDT) for simplicity
    # Good enough for MVP (free tier). We can add pytz later.
    today = dt.datetime.utcnow().date()
    open_dt = dt.datetime(today.year, today.month, today.day, 13, 30)  # 9:30 ET = 13:30 UTC in EDT
    return int(open_dt.timestamp())

def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def get_float_shares(sym):
    """Try to fetch free float from basic financials (best-effort)."""
    try:
        bf = fh.company_basic_financials(sym, "all") or {}
        m = bf.get("metric") or {}
        # Finnhub has a few different keys across listings; try several
        for k in ("shareFloat","floatSharesOutstanding","sharesFloat","sharesOutstanding"):
            v = m.get(k)
            if v and v > 0:
                return float(v)
    except Exception:
        pass
    return None

def get_vwap_from_1m(sym, start_ts, end_ts):
    """Compute simple intraday VWAP from 1m candles (typical price * volume / sum volume)."""
    try:
        d = fh.stock_candles(sym, "1", start_ts, end_ts)
        if d.get("s") != "ok":
            return None
        t, h, l, c, v = d["t"], d["h"], d["l"], d["c"], d["v"]
        pv = 0.0
        vv = 0.0
        for i in range(len(t)):
            typ = (safe_float(h[i]) + safe_float(l[i]) + safe_float(c[i])) / 3.0
            vol = safe_float(v[i])
            pv += typ * vol
            vv += vol
        return (pv / vv) if vv > 0 else None
    except Exception:
        return None

def get_last_15m_volume(sym, end_ts):
    """Get the most recent 15m candle volume."""
    try:
        d = fh.stock_candles(sym, "15", end_ts - 60*60*8, end_ts)  # last ~8 hours
        if d.get("s") != "ok" or not d.get("v"):
            return None
        return safe_float(d["v"][-1], 0.0)
    except Exception:
        return None

def is_pharma(profile):
    ind = (profile.get("finnhubIndustry") or "").lower()
    return any(k in ind for k in ("pharmaceutical", "biotech", "drug"))

def is_adr(profile):
    name = (profile.get("name") or "").upper()
    exch = (profile.get("exchange") or "").upper()
    return ("ADR" in name) or ("ADR" in exch)

# ---------- ROUTES ----------
@app.route("/health")
def health():
    return jsonify({"ok": True, "ts": now_ts()})

@app.route("/quote")
def quote():
    sym = request.args.get("symbol", "TSLA").upper()
    try:
        q = fh.quote(sym)
        return jsonify({"symbol": sym, **q})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/scan")
def scan():
    """
    Real v7.5 MVP scan on a safe-size universe:
    - Price < $30
    - Avoid pharma FDA binaries (industry heuristic)
    - Float < 150M (if float not available, skip)
    - Volume gate: fresh 15m >= max(2,000,000, 0.75% of float)
    - Above intraday VWAP
    - Coil proxy: % range of last 60–90 mins compressed (approx via 1m candles)
    ADRs allowed but rank as Tier 2+
    """
    end_ts = now_ts()
    start_vwap = max(toronto_market_open_utc_ts(), end_ts - MINUTES_BACK_FOR_VWAP*60)

    results = []

    for sym in UNIVERSE:
        try:
            q = fh.quote(sym)
            price = safe_float(q.get("c"))
            if not price or price <= 0 or price >= PRICE_CEIL:
                continue

            # Basic profile (industry + ADR check)
            profile = fh.company_profile2(symbol=sym) or {}
            if is_pharma(profile):
                continue
            adr_flag = is_adr(profile)

            # Float
            flt = get_float_shares(sym)
            if not flt or flt <= 0 or flt > FLOAT_CEIL:
                continue

            # 15m volume gate
            vol15 = get_last_15m_volume(sym, end_ts) or 0.0
            vol_req = max(VOL_REQ_MIN, VOL_REQ_FLOAT_PCT * flt)
            if vol15 < vol_req:
                continue

            # VWAP (intraday) gate
            vwap = get_vwap_from_1m(sym, start_vwap, end_ts)
            if not vwap or not (price >= vwap):
                continue

            # Coil proxy (last ~90 mins range compression using 1m)
            d1 = fh.stock_candles(sym, "1", end_ts - 60*90, end_ts)
            if d1.get("s") != "ok":
                continue
            highs = d1["h"]; lows = d1["l"]
            if not highs or not lows:
                continue
            hi = max(safe_float(x) for x in highs)
            lo = min(safe_float(x) for x in lows)
            rng = (hi - lo) / price if price else 0
            coil_score = 1.0 / (rng + 1e-6)  # higher = tighter

            # Quick proximity to intraday high as trigger proxy
            dday = fh.stock_candles(sym, "1", toronto_market_open_utc_ts(), end_ts)
            if dday.get("s") != "ok":
                continue
            day_hi = max(safe_float(x) for x in dday["h"])
            pct_to_trigger = max(0.0, (day_hi - price) / max(day_hi, 1e-6) * 100.0)

            # Grade / Tier (simple rules for MVP)
            tier = "Tier 1" if not adr_flag else "Tier 2"
            grade = "A" if coil_score > 35 else ("B+" if coil_score > 20 else "B")

            # Draft entry/stop/targets per v7.5 style
            trigger = round(day_hi + max(0.01, 0.002 * price), 3)  # tiny cushion above HOD
            stop = round(max(lo, price * 0.97), 3)                 # 3% or coil low
            t1 = round(trigger + 0.03 * price, 3)
            t2 = round(trigger + 0.06 * price, 3)
            t3 = round(trigger + 0.10 * price, 3)
            risk = max(0.01, trigger - stop)
            reward = max(0.01, (t2 - trigger))  # use T2 for R:R
            rr = round(reward / risk, 2)

            results.append({
                "Ticker": sym,
                "Tier": tier,
                "Grade": grade,
                "Price": round(price, 3),
                "VWAP": round(vwap, 3),
                "15mVol": int(vol15),
                "15mVolReq": int(vol_req),
                "Trigger": trigger,
                "%ToTrigger": round(pct_to_trigger, 2),
                "Stop": stop,
                "Targets": [t1, t2, t3],
                "R:R": rr,
                "ADR": adr_flag,
                "Note": "Above VWAP + 15m vol gate + coil compression"
            })

        except Exception:
            # Skip noisy errors so scan keeps going
            continue

    # Rank by (coil strength high) & (closer to trigger)
    results.sort(key=lambda x: (x["Tier"] != "Tier 1", -x["R:R"], x["%ToTrigger"]))  # Tier1 first, better R:R, closer trigger

    board = {
        "count": len(results),
        "near_trigger_board": results[:25],
        "ts": end_ts
    }
    CACHE["board"] = board
    return jsonify({"ok": True, "scanned": len(results), "ts": end_ts})

@app.route("/board")
def board():
    b = CACHE["board"]
    age_min = (now_ts() - (b["ts"] or 0)) / 60.0
    out = dict(b)
    out["age_min"] = round(age_min, 2)
    out["stale"] = age_min > 15
    return jsonify(out)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
