# main.py
from __future__ import annotations
from flask import Flask, jsonify, request
import os, io, time, math, random
from datetime import datetime, timedelta
import pandas as pd
import yfinance as yf

app = Flask(__name__)

# ------------------ Stock Game v7.5 (current tweaks) ------------------
PRICE_MAX = 30.0
FLOAT_MAX = 150_000_000  # 150M (can override if institutional-grade catalyst)
VOLUME_SHARES_GATE = 2_000_000
VOLUME_FLOAT_PCT_GATE = 0.0075  # 0.75%
HIST_DAYS = 10  # keep small for speed, enough for last 15m bar

# Avoid pharma FDA binaries by default (we blank-list common tickers via simple heuristics)
BIOTECH_HINTS = ("BIO", "PHAR", "PHARM", "THERA", "GENE", "PHARMA", "BIOTECH")
# ----------------------------------------------------------------------

# Runtime state
UNIVERSE_STATE: list[str] = []
NEAR_TRIGGER_BOARD: list[dict] = []
UNIVERSE_MAX = 350  # guardrail

# -------------------------- helpers -----------------------------------
def safe_float(x, default=None):
    try:
        if x is None: 
            return default
        return float(x)
    except Exception:
        return default

def is_adr_symbol(sym: str) -> bool:
    # Basic ADR hint: many ADRs end with 'Y' (not perfect)
    return sym.endswith("Y")

def blocked_biotech(sym: str) -> bool:
    # crude symptom guard; you can improve later
    upper = sym.upper()
    return any(h in upper for h in BIOTECH_HINTS)

def classify_tier(is_adr: bool, catalyst_kind: str):
    if catalyst_kind == "Spec":
        return ("Tier-3", "C")
    if is_adr:
        return ("Tier-2", "B")
    return ("Tier-1", "A")

def derive_trigger(price: float):
    trig = round(price * 1.02, 2)
    pct = round((trig / price - 1) * 100, 2)
    return trig, f"+{pct}%"

def volume_gate_ok(last15_shares: int, free_float: float):
    gate1 = last15_shares >= VOLUME_SHARES_GATE
    gate2 = (free_float and last15_shares >= free_float * VOLUME_FLOAT_PCT_GATE)
    return gate1 or gate2

def score_row(r: dict) -> float:
    base = 0.0
    base += 3.0 if r.get("VWAP_Status") == "Above" else 0.0
    base += 3.0 if r.get("15m_Vol_vs_Req", "").startswith("Meets") else 0.0
    base += 5.0 if str(r.get("Catalyst", "")).startswith("Real") else 0.0
    # closer to trigger = higher score
    try:
        pct = float(str(r.get("%_to_trigger", "2").strip("+%")))
    except Exception:
        pct = 2.0
    return base - pct

# ---------------------- “Catalyst” heuristics --------------------------
REAL_CATALYST_KEYWORDS = {
    "earnings","guidance","m&a","acquisition","merger","takeover",
    "13d","13g","insider","buyback","repurchase","contract","partnership","deal"
}
SPECULATIVE_KEYWORDS = {"strategic review","pipeline","explore options"}

def catalyst_tag(symbol: str) -> tuple[str, str]:
    """
    Lightweight, API-key-free attempt:
    Use Yahoo headlines via yfinance (best effort, not guaranteed).
    """
    kind, note = "None", ""
    try:
        news = (yf.Ticker(symbol).news or [])[:15]
        titles = " ".join((n.get("title") or "").lower() for n in news)
        if any(k in titles for k in REAL_CATALYST_KEYWORDS):
            return "Real", "Tier-1/2 catalyst"
        if any(k in titles for k in SPECULATIVE_KEYWORDS):
            return "Spec", "Tier-3 speculative"
    except Exception:
        pass
    return kind, note

# ---------------------- Universe building ------------------------------
SEED = [
    # liquid, options-active, < $30 prone movers (keep growing this list)
    "AAL","UAL","DAL","CCL","NCLH","RCL","F","T","PFE","BAC","C","SOFI","PLTR",
    "RIVN","LCID","QS","NIO","CHPT","RUN","BLNK","RIOT","MARA","AI","SOUN",
    "PTON","JOBY","RUM","PLUG","INTC","SNAP","NU","UWMC","OPEN","IONQ","UPST",
    "ENVX","MVIS","NKLA","COIN","HOOD","BABA","BBD","ABNB","U","COUR","XPEV",
    "BA","AMC","GME","BILI","VALE","KVUE","WBD","KHC","GPS","TME","PINS",
    "SWN","TELL","BB","NOK","TLRY","CGC"
]

def read_tickers_txt() -> list[str]:
    """If a Tickets.txt is present in the repo root, use it (one symbol per line)."""
    path = os.path.join(os.getcwd(), "Tickets.txt")
    if not os.path.exists(path):
        return []
    syms = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            t = line.strip().upper()
            if t and t.isascii():
                syms.append(t)
    return syms

def price_filter(symbols: list[str]) -> list[str]:
    """Keep tickers priced <= $30 (fast_info)."""
    keep = []
    for chunk in [symbols[i:i+80] for i in range(0, len(symbols), 80)]:
        for s in chunk:
            try:
                fi = yf.Ticker(s).fast_info or {}
                p = safe_float(fi.get("lastPrice"))
                if p is not None and p > 0 and p <= PRICE_MAX:
                    keep.append(s)
            except Exception:
                continue
    return keep[:UNIVERSE_MAX]

def build_universe() -> list[str]:
    # 1) Prefer Tickets.txt if present; otherwise seed
    base = read_tickers_txt()
    if not base:
        base = SEED[:]
    # 2) Price filter to <$30 and drop obvious biotech
    base = [s for s in base if not blocked_biotech(s)]
    result = price_filter(list(dict.fromkeys(base)))
    return result

# ---------------------- Batched market data ----------------------------
def batch_last_bar(tickers: list[str]) -> dict[str, dict]:
    """
    One batched yfinance call for all symbols -> last completed 15m bar + prev close.
    Returns: {symbol: {"v": volume, "pc": prev_close}}
    """
    out: dict[str, dict] = {}
    if not tickers:
        return out
    try:
        df = yf.download(
            tickers=tickers,
            period=f"{HIST_DAYS}d",
            interval="15m",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
        multi = isinstance(df.columns, pd.MultiIndex)
        for t in tickers:
            try:
                sub = df[t] if multi else df
                if sub.empty:
                    continue
                last = sub.iloc[-1]
                pc = sub["Close"].shift(1).iloc[-1]
                out[t] = {"v": int(last["Volume"]), "pc": float(pc)}
            except Exception:
                continue
    except Exception:
        pass
    return out

# ------------------------- Scanner core --------------------------------
def scan_universe(universe: list[str]) -> list[dict]:
    rows = []
    lastbars = batch_last_bar(universe)

    for sym in universe:
        try:
            tk = yf.Ticker(sym)
            fi = tk.fast_info or {}

            price = safe_float(fi.get("lastPrice"))
            prev_close = safe_float(fi.get("previousClose"))
            shares_out = safe_float(fi.get("sharesOutstanding"))

            lb = lastbars.get(sym, {})
            v15 = int(lb.get("v", 0))
            if prev_close is None:
                prev_close = safe_float(lb.get("pc"), default=price)

            if price is None or price <= 0 or price > PRICE_MAX:
                continue

            # pharma/fda binaries
            if blocked_biotech(sym):
                continue

            # catalyst check
            kind, note = catalyst_tag(sym)

            # float gate override rule
            if shares_out and shares_out > FLOAT_MAX and kind != "Real":
                continue

            v_ok = volume_gate_ok(v15, shares_out or 0)
            vw = "Above" if (price >= (prev_close or price)) else "Below"
            tier, grade = classify_tier(is_adr_symbol(sym), kind)
            trig, pct = derive_trigger(price)

            rows.append({
                "symbol": sym,
                "Tier/Grade": f"{tier}/{grade}",
                "trigger": trig,
                "%_to_trigger": pct,
                "VWAP_Status": vw,
                "15m_Vol_vs_Req": f"{'Meets' if v_ok else 'Below'} ({v15:,})",
                "price": round(price, 3),
                "Catalyst": f"{kind}: {note}",
                "Note": "ADR (Tier-2+)" if is_adr_symbol(sym) else ""
            })

        except Exception as e:
            print(f"scan error {sym}: {e}")
            continue

    rows.sort(key=score_row, reverse=True)
    return rows

# -------------------------- API routes ---------------------------------
@app.route("/")
def health():
    return jsonify({"ok": True, "ts": int(time.time())})

@app.route("/universe")
def universe_route():
    global UNIVERSE_STATE
    UNIVERSE_STATE = build_universe()
    return jsonify({"count": len(UNIVERSE_STATE), "universe": UNIVERSE_STATE, "ts": int(time.time())})

@app.route("/scan")
def scan_route():
    global NEAR_TRIGGER_BOARD
    limit = request.args.get("limit", type=int)
    tickers = UNIVERSE_STATE[:limit] if (limit and UNIVERSE_STATE) else UNIVERSE_STATE
    NEAR_TRIGGER_BOARD = scan_universe(tickers or build_universe())
    return jsonify({"message": "scan complete", "count": len(NEAR_TRIGGER_BOARD), "ts": int(time.time())})

@app.route("/board")
def board_route():
    age_min = round((time.time() % 900) / 60, 2)
    return jsonify({
        "age_min": age_min,
        "count": len(NEAR_TRIGGER_BOARD),
        "near_trigger_board": NEAR_TRIGGER_BOARD,
        "stale": False,
        "ts": int(time.time())
    })

# --------------------------- entrypoint --------------------------------
if __name__ == "__main__":
    # local run
    UNIVERSE_STATE = build_universe()
    NEAR_TRIGGER_BOARD = []
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
