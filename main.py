import os
import io
import time
import math
import json
import gzip
import csv
import random
import threading
from datetime import datetime, timedelta

import requests
import pandas as pd
import numpy as np
import yfinance as yf
from flask import Flask, jsonify

app = Flask(__name__)

# -------------------- Version 7.5 knobs --------------------
PRICE_MAX = float(os.getenv("PRICE_MAX", "30"))
FLOAT_MAX = float(os.getenv("FLOAT_MAX", "150_000_000"))   # 150M shares
FIFTEEN_MIN_VOL_GATE = int(os.getenv("FIFTEEN_MIN_VOL_GATE", "2000000"))
FIFTEEN_MIN_FLOAT_PCT = float(os.getenv("FIFTEEN_MIN_FLOAT_PCT", "0.0075"))  # 0.75%
ADR_TIER_MIN = 2  # ADRs allowed Tier-2+
ALLOW_OTC = False # default off per prior pain with junk
# ------------------------------------------------------------

# Caches (in-memory)
UNIVERSE = []                 # all symbols we can consider today
UNIVERSE_TS = 0
NEAR_TRIGGER_BOARD = []       # last scan results
BOARD_TS = 0

# Screener / fetch caps to stay within Render Free limits
MAX_UNIVERSE_CANDIDATES = 1200   # cap symbols after basic rules (price prefilter)
MAX_SCAN_SYMBOLS = 300           # deeper intraday checks on this many only

# Keywords
REAL_CATALYST = [
    "earnings", "guidance", "m&a", "acquisition", "merger", "takeover",
    "13d", "13g", "insider", "buyback", "repurchase", "contract",
    "partnership", "deal"
]
SPECULATIVE_CATALYST = ["strategic review", "pipeline", "explore options"]

# ---------- Helpers ----------
def _log(msg):
    print(f"[{datetime.utcnow().isoformat()}] {msg}", flush=True)

def _is_market_hours_toronto():
    # 9:30–16:00 America/Toronto (assume server UTC; soft gate only)
    now = datetime.utcnow()
    # crude window: 14:30–21:00 UTC roughly matches Toronto during most of year
    hm = now.hour * 60 + now.minute
    return (hm >= 14*60+30) and (hm <= 21*60)

def _is_probably_adr(sym, info):
    # quick ADR heuristics
    longname = (info.get("longName") or "").upper()
    exchange = (info.get("exchange") or "").upper()
    # If Yahoo exchange includes "PNK" we treat as OTC -> drop unless ALLOW_OTC
    if not ALLOW_OTC and ("PNK" in exchange or "OTC" in exchange):
        return True
    if "ADR" in longname:
        return True
    # trailing 'Y' is common but noisy; we only use it if exchange indicates ADR/OTC
    if sym.endswith("Y") and ("PNK" in exchange or "OTC" in exchange or "ADR" in longname):
        return True
    return False

def _fetch_symbol_dirs():
    # Nasdaq official symbol directories (public)
    urls = [
        "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
        "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
    ]
    frames = []
    for u in urls:
        r = requests.get(u, timeout=20)
        r.raise_for_status()
        txt = r.text
        # Skip footer lines starting with "File Creation Time"
        lines = [ln for ln in txt.splitlines() if ln and not ln.startswith("File Creation Time")]
        # tab-delimited
        reader = csv.DictReader(lines, delimiter='|')
        frames.append(pd.DataFrame(reader))
    df = pd.concat(frames, ignore_index=True)

    # Standardize symbol column name
    sym_col = "Symbol" if "Symbol" in df.columns else ("ACT Symbol" if "ACT Symbol" in df.columns else None)
    if sym_col is None:
        raise RuntimeError("Could not find symbol column in NASDAQ symbol directories.")
    symbols = df[sym_col].astype(str).str.upper().tolist()

    # Remove obvious non-commons: ETFs/ETNs/Units/Warrants/Prefs
    ban_frag = [
        "WS", "W", "U", "PU", "PRA", "PRB", "PRC", "PRD", "PRE", "PRF", "PRG",
        "PRH", "PRI", "PRJ", "PRK", "PRL", "PRM", "PRN", "PRO", "PRP", "PRQ",
        "PRR", "PRS", "PRT", "PRU", "PRV", "PRW", "PRX", "PRY", "PRZ"
    ]
    clean = []
    for s in symbols:
        if "-" in s or "^" in s or "." in s:  # class/when-issued
            continue
        # avoid clear ETF/ETN suffixes
        if any(s.endswith(x) for x in ["U", "W"]):
            continue
        # preferred-like
        if any(s.endswith(x) for x in ban_frag):
            continue
        clean.append(s)

    return sorted(set(clean))

def _yfi_fast_batch(symbols):
    # yfinance multi-ticker fast_info fetch (daily-level info; fast)
    tick = yf.Tickers(" ".join(symbols))
    out = {}
    for sym, tk in tick.tickers.items():
        try:
            fi = getattr(tk, "fast_info", {}) or {}
            out[sym.upper()] = {
                "last_price": float(fi.get("last_price") or 0.0),
                "prev_close": float(fi.get("previous_close") or 0.0),
                "shares_outstanding": float(fi.get("shares_outstanding") or 0.0),
                "exchange": fi.get("exchange", ""),
                "last_volume": float(fi.get("last_volume") or 0.0),
                "currency": fi.get("currency", "USD"),
            }
        except Exception:
            continue
    return out

def _yfi_info_batch(symbols):
    # Pull a small amount of meta to detect ADRs by name/exchange
    results = {}
    for sym in symbols:
        try:
            tk = yf.Ticker(sym)
            info = tk.info or {}
            results[sym] = {
                "longName": info.get("longName") or info.get("shortName") or "",
                "exchange": info.get("exchange") or "",
            }
        except Exception:
            results[sym] = {"longName": "", "exchange": ""}
    return results

def _yfi_intraday_15m(symbol):
    # fetch last 15m bar volume and VWAP-ish context
    try:
        df = yf.download(symbol, period="1d", interval="15m", progress=False, prepost=False)
        if df is None or df.empty:
            return 0, None, None
        # last bar
        last = df.tail(1)
        vol = int(last["Volume"].iloc[0] or 0)
        # Approx VWAP with intraday OHLCV
        # True VWAP needs tick data; here we approximate with typical price
        vwap = None
        try:
            pv = (df["High"] + df["Low"] + df["Close"]) / 3.0
            vwap = float((pv * df["Volume"]).sum() / max(1, df["Volume"].sum()))
        except Exception:
            vwap = None
        last_close = float(last["Close"].iloc[0] or np.nan)
        return vol, vwap, last_close
    except Exception:
        return 0, None, None

def _has_real_catalyst(symbol):
    # yfinance news is best-effort; keep light
    try:
        tk = yf.Ticker(symbol)
        news = tk.news or []
        titles = " ".join((n.get("title") or "").lower() for n in news[:20])
        if any(k in titles for k in REAL_CATALYST):
            return "Real", "Tier-1/2 catalyst"
        if any(k in titles for k in SPECULATIVE_CATALYST):
            return "Spec", "Tier-3 speculative"
    except Exception:
        pass
    return "None", ""

def _classify_tier(is_adr, catalyst_kind):
    if catalyst_kind == "Spec":
        return "Tier-3", "C"
    if is_adr:
        return "Tier-2", "B"
    return "Tier-1", "A"

def _derive_trigger(price):
    # placeholder: 2% above current
    trig = round(price * 1.02, 2)
    pct = round((trig / price - 1.0) * 100.0, 2)
    return trig, f"+{pct}%"

# -------------------- Endpoints --------------------
@app.route("/")
def root():
    return jsonify({
        "ok": True,
        "universe_count": len(UNIVERSE),
        "board_count": len(NEAR_TRIGGER_BOARD),
        "universe_ts": UNIVERSE_TS,
        "board_ts": BOARD_TS
    })

@app.route("/universe")
def build_universe():
    global UNIVERSE, UNIVERSE_TS

    _log("Building universe from NASDAQ symbol directories")
    syms = _fetch_symbol_dirs()

    # fetch fast batch for a subset to find price < $30
    # We can’t query 5000 symbols at once without getting throttled; batch
    filtered = []
    batch = 250
    for i in range(0, len(syms), batch):
        chunk = syms[i:i+batch]
        info = _yfi_fast_batch(chunk)
        for s in chunk:
            meta = info.get(s, {})
            px = meta.get("last_price") or 0.0
            exch = (meta.get("exchange") or "").upper()
            # keep U.S. primary exchanges only
            if not exch or ("NASDAQ" not in exch and "NYSE" not in exch and "AMEX" not in exch):
                continue
            # price gate
            if px <= 0 or px > PRICE_MAX:
                continue
            # skip OTC/PNK unless explicitly allowed
            if not ALLOW_OTC and ("PNK" in exch or "OTC" in exch):
                continue
            filtered.append(s)
        # soft cap to keep it fast on free tier
        if len(filtered) >= MAX_UNIVERSE_CANDIDATES:
            break

    UNIVERSE = sorted(set(filtered))
    UNIVERSE_TS = int(time.time())
    _log(f"Universe built: {len(UNIVERSE)} symbols")
    return jsonify({"count": len(UNIVERSE), "symbols": UNIVERSE, "ts": UNIVERSE_TS})

@app.route("/scan")
def run_scan():
    global NEAR_TRIGGER_BOARD, BOARD_TS
    if not UNIVERSE:
        return jsonify({"count": 0, "near_trigger_board": [], "ts": int(time.time())})

    # Pull additional meta for ADR detection
    subset = UNIVERSE[:MAX_SCAN_SYMBOLS]
    info_meta = _yfi_info_batch(subset)
    fast_meta = _yfi_fast_batch(subset)

    rows = []
    for sym in subset:
        fm = fast_meta.get(sym, {})
        price = float(fm.get("last_price") or 0.0)
        prev_close = float(fm.get("previous_close") or fm.get("prev_close") or 0.0)
        shares_out = float(fm.get("shares_outstanding") or 0.0)
        exch = (fm.get("exchange") or "").upper()
        if price <= 0 or price > PRICE_MAX:
            continue

        # ADR detection + OTC exclusion
        is_adr = _is_probably_adr(sym, {"exchange": exch, "longName": info_meta.get(sym, {}).get("longName", "")})
        if not ALLOW_OTC and ("PNK" in exch or "OTC" in exch):
            continue

        # light catalyst check
        catalyst_kind, catalyst_note = _has_real_catalyst(sym)

        # float proxy rule
        free_float = shares_out  # proxy (we swap to true float if you add a paid API)
        if free_float and free_float > FLOAT_MAX and catalyst_kind != "Real":
            # too big, and no institutional-grade catalyst to override
            continue

        # intraday checks (15m volume + VWAP status)
        vol15, vwap, last_close = _yfi_intraday_15m(sym)
        vwap_status = "Above"
        try:
            ref = vwap if vwap else (prev_close or price)
            vwap_status = "Above" if price >= ref else "Below"
        except Exception:
            vwap_status = "Above"

        # 15-min volume gate
        vol_ok = (vol15 >= FIFTEEN_MIN_VOL_GATE) or (
            free_float and vol15 >= free_float * FIFTEEN_MIN_FLOAT_PCT
        )

        # ADRs allowed but only Tier-2+
        tier, grade = _classify_tier(is_adr, catalyst_kind)
        if is_adr and tier == "Tier-1":
            tier, grade = "Tier-2", "B"

        # Spec PRs => Tier-3 tiny size (still can show)
        note = []
        if catalyst_kind == "Spec":
            note.append("Spec PR — tiny size only")
        if is_adr:
            note.append("ADR (Tier-2+ only)")
        if not vol_ok:
            note.append("Below 15m gate")

        trigger, pct_to_trigger = _derive_trigger(price)

        # score: real catalyst + vol ok + vwap above - distance to trigger
        try:
            dist = float(pct_to_trigger.replace("+","").replace("%",""))
        except Exception:
            dist = 2.0
        score = (5 if catalyst_kind == "Real" else 2) + (3 if vol_ok else 0) + (2 if vwap_status == "Above" else 0) - dist

        rows.append({
            "symbol": sym,
            "Tier/Grade": f"{tier}/{grade}",
            "trigger": trigger,
            "%_to_trigger": pct_to_trigger,
            "VWAP_Status": vwap_status,
            "15m_Vol": int(vol15),
            "Vol_OK": bool(vol_ok),
            "Catalyst": catalyst_kind,
            "price": round(price, 4),
            "Note": "; ".join(note),
            "Score": round(score, 2)
        })

    # Rank
    rows.sort(key=lambda r: (r["Vol_OK"], r["Catalyst"] == "Real", r["VWAP_Status"] == "Above", -float(r["%_to_trigger"].replace("+","").replace("%","")) , r["Score"]), reverse=True)
    NEAR_TRIGGER_BOARD = rows
    BOARD_TS = int(time.time())
    return jsonify({"count": len(rows), "near_trigger_board": rows, "ts": BOARD_TS})

@app.route("/board")
def get_board():
    return jsonify({
        "count": len(NEAR_TRIGGER_BOARD),
        "near_trigger_board": NEAR_TRIGGER_BOARD,
        "ts": BOARD_TS
    })

@app.route("/force_refresh")
def force_refresh():
    # Convenience: rebuild universe then scan
    build_universe()
    return run_scan()

# -------------------- Main --------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
