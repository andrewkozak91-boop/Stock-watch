from flask import Flask, jsonify
import os, time, math
from datetime import datetime, timedelta, timezone

import yfinance as yf
import pandas as pd

app = Flask(__name__)

# -------- CONFIG (v7.5 rules, simplified) ----------
UNIVERSE = [
    "PLTR","SOFI","DNA","F","AAL","CCL","UAL","NCLH","RIVN","HOOD",
    "CHPT","QS","RIOT","MARA","AI","PFE","T","JOBY","BBAI","PTON",
    "RUM","SOUN","RIVN","NCLH","CCL","QS","MARA","RIOT","AAL","F"
]
PRICE_MAX = 30.0
FLOAT_MAX = 150_000_000   # used only if we later add float source
VOLUME_SHARES_GATE = 2_000_000
VOLUME_FLOAT_PCT_GATE = 0.0075  # 0.75%
# ---------------------------------------------------

near_trigger_board = []

def now_ts():
    return int(time.time())

def safe_last_15m_volume(sym: str) -> int:
    """
    Get the latest 15m bar volume using yfinance (1–2 min delayed).
    Returns 0 if not available.
    """
    try:
        # last day intraday 15m bars
        df = yf.download(sym, interval="15m", period="1d", progress=False, auto_adjust=False, threads=False)
        if df is None or df.empty:
            return 0
        # most recent completed bar
        vol = int(df["Volume"].iloc[-1])
        return vol if vol is not None else 0
    except Exception:
        return 0

def get_price(sym: str) -> float:
    try:
        q = yf.Ticker(sym).fast_info
        # fast_info has last_price; falls back to recent Close if needed
        px = q.get("last_price") or q.get("last_trade") or 0.0
        if not px or px <= 0:
            # fallback: use close from recent 15m bar
            df = yf.download(sym, interval="15m", period="1d", progress=False, auto_adjust=False, threads=False)
            if df is not None and not df.empty:
                px = float(df["Close"].iloc[-1])
        return float(px) if px else 0.0
    except Exception:
        return 0.0

def get_prev_close(sym: str) -> float:
    try:
        df = yf.download(sym, period="5d", interval="1d", progress=False, auto_adjust=False, threads=False)
        if df is not None and len(df) >= 2:
            return float(df["Close"].iloc[-2])
        if df is not None and len(df) >= 1:
            return float(df["Close"].iloc[-1])
    except Exception:
        pass
    return 0.0

def volume_gate_ok(last15_shares, free_float=None):
    gate1 = last15_shares >= VOLUME_SHARES_GATE
    gate2 = (free_float and last15_shares >= free_float * VOLUME_FLOAT_PCT_GATE)
    return gate1 or gate2

def classify_tier(is_adr: bool, has_real_catalyst: bool, has_spec: bool):
    if has_spec and not has_real_catalyst:
        return "Tier-3/C"
    if is_adr and not has_real_catalyst:
        return "Tier-2/B"
    return "Tier-1/A"

def derive_trigger(px: float) -> tuple[float, str]:
    trig = round(px * 1.02, 2)
    pct = round((trig / px - 1) * 100, 2)
    return trig, f"+{pct}%"

# Placeholder catalyst/ADR detectors (safe defaults); can be upgraded later
def detect_catalyst(sym: str) -> tuple[bool, bool, str]:
    # Without a news API, stay conservative: no catalyst by default
    return False, False, ""

def is_adr(sym: str) -> bool:
    # crude: tickers ending with 'Y' are often ADRs; refine later if needed
    return sym.endswith("Y")

def scan_symbol(sym: str):
    px = get_price(sym)
    if px <= 0 or px > PRICE_MAX:
        return None

    prev_close = get_prev_close(sym)
    vwap_status = "Above" if prev_close and px >= prev_close else "Below"

    vol15 = safe_last_15m_volume(sym)
    vol_ok = volume_gate_ok(vol15, None)

    real_cat, spec_cat, cat_note = detect_catalyst(sym)
    tier_grade = classify_tier(is_adr(sym), real_cat, spec_cat)

    trig, pct_to = derive_trigger(px)

    # Score: closer to trigger + volume ok + above prev close
    try:
        gap = float(pct_to.strip("%+"))
    except:
        gap = 2.0
    score = (3 if vol_ok else 0) + (3 if vwap_status == "Above" else 0) - gap

    return {
        "symbol": sym,
        "Tier/Grade": tier_grade,
        "trigger": trig,
        "%_to_trigger": pct_to,
        "VWAP_Status": vwap_status,
        "15m_Vol_vs_Req": f"{'Meets' if vol_ok else 'Below'} ({vol15:,})",
        "price": round(px, 3),
        "Catalyst": ("Real" if real_cat else ("Spec" if spec_cat else "None")) + (f": {cat_note}" if cat_note else ""),
        "Note": ("ADR (Tier-2+)" if is_adr(sym) else ""),
        "_score": score
    }

def run_scan():
    results = []
    for s in UNIVERSE:
        try:
            row = scan_symbol(s)
            if row:
                results.append(row)
        except Exception as e:
            print(f"Scan error {s}: {e}")
            continue
    results.sort(key=lambda r: r["_score"], reverse=True)
    for r in results:
        r.pop("_score", None)
    return results

@app.route("/")
def root():
    return jsonify({"ok": True, "ts": now_ts()})

@app.route("/universe")
def universe():
    return jsonify({"count": len(UNIVERSE), "symbols": UNIVERSE})

@app.route("/scan")
def scan():
    global near_trigger_board
    near_trigger_board = run_scan()
    return jsonify({"message": "scan complete", "count": len(near_trigger_board)})

@app.route("/board")
def board():
    return jsonify({
        "age_min": round((time.time() % 900)/60, 2),
        "count": len(near_trigger_board),
        "near_trigger_board": near_trigger_board,
        "stale": False,
        "ts": now_ts()
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
