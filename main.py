# main.py
# Dynamic Universe + 7.5-lite scan (free data only)

from flask import Flask, jsonify
import os, time, math, io, re, traceback, requests
from datetime import datetime
from typing import List, Tuple

import yfinance as yf

app = Flask(__name__)

# ---------------- Version 7.5 (lite—API free) ----------------
PRICE_MAX = 30.0
FLOAT_MAX = 150_000_000      # used only if we can read sharesOutstanding
ENFORCE_FLOAT_GATE = False   # keep False until a reliable float source is added

VOLUME_SHARES_GATE = 2_000_000
FALLBACK_VOL_GATE   = 300_000
FALLBACK_ALLOW_PCT  = 0.003  # 0.3% of sharesOut if we have it

REAL_CATALYST_KEYWORDS = {
    "earnings","guidance","m&a","acquisition","merger","takeover",
    "13d","13g","insider","buyback","repurchase","contract","partnership","deal"
}
SPECULATIVE_KEYWORDS = {"strategic review","pipeline","explore options"}

# Universe build controls
MAX_CANDIDATES = 1200     # after symbol cleanup (raw)
MAX_LIQUID     = 600      # top by 10-day dollar volume to keep in active universe
BATCH_SIZE     = 180      # yfinance multi-download chunk size

UNIVERSE: List[str] = []
BOARD = []

NASDAQ_FILES = [
    "https://ftp.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://ftp.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
]

STOCK_RE = re.compile(r"^[A-Z]{1,5}$")  # keep simple 1–5 letter common stocks

# -------------------- helpers --------------------

def ts() -> int:
    return int(time.time())

def fetch_symbol_files() -> List[str]:
    syms = []
    for url in NASDAQ_FILES:
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            text = r.text.splitlines()
            # Files are pipe-delimited with headers; “ETF|Y/N” exists in nasdaqlisted.
            header = text[0].split("|")
            for ln in text[1:]:
                if "File Creation Time" in ln or not ln.strip():  # trailer/footer
                    continue
                parts = ln.split("|")
                sym = parts[0].strip().upper()
                # Drop ETFs if that column exists
                if "ETF" in header:
                    etf_idx = header.index("ETF")
                    if etf_idx < len(parts) and parts[etf_idx].strip().upper() == "Y":
                        continue
                # Basic keep rules: 1–5 letters, avoid when-issued, preferred, etc.
                if STOCK_RE.match(sym):
                    syms.append(sym)
        except Exception as e:
            print(f"Fetch symbols error {url}: {e}")
    # de-dupe, keep order
    dedup = list(dict.fromkeys(syms))
    # cap to keep first N (we’ll rank by liquidity next)
    return dedup[:MAX_CANDIDATES]

def batched(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

def pick_most_liquid(symbols: List[str]) -> List[str]:
    """Use yfinance to compute 10-day dollar volume and keep the top N under $30."""
    scored = []
    for chunk in batched(symbols, BATCH_SIZE):
        try:
            # Pull one day of data (includes Volume) + fast last prices via info
            hist = yf.download(chunk, period="11d", interval="1d", threads=True, group_by="ticker", progress=False)
            # If multiple tickers: hist is dict-like columns; if single: DataFrame
            for sym in chunk:
                try:
                    # get last close and 10-day avg volume
                    if len(chunk) == 1:
                        h = hist
                    else:
                        h = hist.get(sym)
                    if h is None or h.empty:
                        continue
                    h = h.tail(10)
                    avg_vol = float(h["Volume"].mean())
                    last_close = float(h["Close"].iloc[-1])
                    if last_close <= 0 or last_close > PRICE_MAX:
                        continue
                    dollar_vol = avg_vol * last_close
                    scored.append((sym, dollar_vol, last_close))
                except Exception:
                    continue
        except Exception as e:
            print(f"yf.download batch failed: {e}")

    # Sort by dollar volume, desc, keep top MAX_LIQUID
    scored.sort(key=lambda t: t[1], reverse=True)
    picked = [s for s, dv, p in scored[:MAX_LIQUID]]
    return picked

def get_snapshot(symbol: str):
    t = yf.Ticker(symbol)
    info = {}
    fast = {}
    try:
        fast = t.fast_info or {}
    except Exception:
        pass
    try:
        info = t.get_info() or {}
    except Exception:
        pass

    price = fast.get("last_price", info.get("currentPrice"))
    prev_close = fast.get("previous_close", info.get("previousClose"))
    shares_out = info.get("sharesOutstanding")
    long_name = info.get("longName") or ""
    return t, price, prev_close, shares_out, long_name

def last_15m_volume(tkr: yf.Ticker) -> int:
    try:
        df = tkr.history(period="1d", interval="15m")
        if df is None or df.empty:
            return 0
        return int(df["Volume"].iloc[-1] or 0)
    except Exception:
        return 0

def detect_catalyst(tkr: yf.Ticker) -> Tuple[str, str]:
    titles = []
    try:
        for n in (tkr.news or [])[:15]:
            title = n.get("title")
            if title:
                titles.append(title.lower())
    except Exception:
        pass
    blob = " ".join(titles)
    if any(k in blob for k in REAL_CATALYST_KEYWORDS):
        return "Real", "Tier-1/2 catalyst"
    if any(k in blob for k in SPECULATIVE_KEYWORDS):
        return "Spec", "Tier-3 speculative"
    return "None", ""

def looks_like_adr(symbol: str, long_name: str) -> bool:
    if symbol.endswith("Y") and 4 <= len(symbol) <= 5:
        return True
    return " adr" in long_name.lower()

def volume_gate_ok(vol15: int, shares_out: int | None) -> bool:
    if vol15 >= VOLUME_SHARES_GATE:
        return True
    if vol15 == 0:
        if shares_out and shares_out > 0:
            return shares_out * FALLBACK_ALLOW_PCT >= FALLBACK_VOL_GATE
        return True  # allow on missing intraday bar
    return False

def classify_tier(is_adr: bool, catalyst_kind: str):
    if catalyst_kind == "Spec":
        return "Tier-3", "C"
    if is_adr:
        return "Tier-2", "B"
    return "Tier-1", "A"

def derive_trigger(price: float):
    trig = round(price * 1.02, 2)
    return trig, f"+{round((trig/price - 1) * 100, 2)}%"

# -------------------- scan core --------------------

def scan_symbol(sym: str):
    try:
        tkr, price, prev_close, shares_out, long_name = get_snapshot(sym)
        if not price or price <= 0 or price > PRICE_MAX:
            return None

        vol15 = last_15m_volume(tkr)
        vol_ok = volume_gate_ok(vol15, shares_out)
        catalyst_kind, catalyst_note = detect_catalyst(tkr)

        if ENFORCE_FLOAT_GATE and shares_out and shares_out > FLOAT_MAX and catalyst_kind != "Real":
            return None

        is_adr = looks_like_adr(sym, long_name)
        vwap_status = "Above" if (prev_close is not None and price >= prev_close) else "Below"
        tier, grade = classify_tier(is_adr, catalyst_kind)
        trigger, pct_to = derive_trigger(price)

        note = []
        if catalyst_kind == "Spec":
            note.append("Spec PR — tiny size only")
        if is_adr:
            note.append("ADR (Tier-2+)")

        try:
            gap = float(pct_to.strip("%+"))
        except Exception:
            gap = 2.0
        score = (5 if catalyst_kind == "Real" else 2) + (3 if vol_ok else 0) + (2 if vwap_status == "Above" else 0) - gap

        return {
            "symbol": sym,
            "Tier/Grade": f"{tier}/{grade}",
            "trigger": trigger,
            "%_to_trigger": pct_to,
            "VWAP_Status": vwap_status,
            "15m_Vol_vs_Req": f"{'Meets' if vol_ok else 'Below'} ({vol15:,})",
            "Catalyst": f"{catalyst_kind}: {catalyst_note}",
            "Sector_Heat": "⚪",
            "Note": "; ".join(note),
            "price": round(float(price), 3),
            "_score": round(score, 3),
        }
    except Exception as e:
        print(f"Scan error {sym}: {e}")
        traceback.print_exc()
        return None

def run_scan(symbols: List[str]):
    rows = []
    for s in symbols:
        r = scan_symbol(s)
        if r:
            rows.append(r)
    rows.sort(key=lambda r: r["_score"], reverse=True)
    for r in rows:
        r.pop("_score", None)
    return rows

# ---------------------- routes ----------------------

@app.route("/")
def root():
    return jsonify({"ok": True, "ts": ts(), "universe_count": len(UNIVERSE), "board_count": len(BOARD)})

@app.route("/build-universe")
def build_universe():
    global UNIVERSE
    raw = fetch_symbol_files()
    picked = pick_most_liquid(raw)
    UNIVERSE = picked
    return jsonify({"message": "universe built", "candidates": len(raw), "kept": len(UNIVERSE), "ts": ts()})

@app.route("/refresh-universe")
def refresh_universe():
    return build_universe()

@app.route("/universe")
def universe():
    return jsonify({"count": len(UNIVERSE), "symbols": UNIVERSE, "ts": ts()})

@app.route("/scan")
def scan():
    global BOARD
    if not UNIVERSE:
        # safety: build on-the-fly if not ready
        _ = build_universe()
    BOARD = run_scan(UNIVERSE)
    return jsonify({"message": "scan complete", "count": len(BOARD), "ts": ts()})

@app.route("/board")
def board():
    return jsonify({
        "age_min": round((time.time() % (15*60))/60, 2),
        "count": len(BOARD),
        "near_trigger_board": BOARD,
        "stale": False,
        "ts": ts()
    })

# ----------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
