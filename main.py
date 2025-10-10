import os, re, time, math, json
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Tuple
import requests
from flask import Flask, jsonify

app = Flask(__name__)

# -----------------------------
# Global state (persist in memory)
# -----------------------------
UNIVERSE: List[str] = []           # list of tickers
NEAR_TRIGGER_BOARD: List[Dict] = []  # last scan results
LAST_UNIVERSE_TS = 0
LAST_SCAN_TS = 0

# -----------------------------
# Config – Stock Game v7.5
# -----------------------------
PRICE_MAX = 30.0
FLOAT_MAX = 150_000_000          # shares
VOLUME_SHARES_GATE = 2_000_000   # 15m bar
VOLUME_FLOAT_PCT_GATE = 0.0075   # 0.75%
EXCLUDE_SECTORS = {"Biotechnology", "Pharmaceuticals"}  # avoid FDA binaries by default

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"}

# -----------------------------
# Yahoo helpers (no API key)
# -----------------------------

def yh_universe_pages() -> List[str]:
    return [
        "https://finance.yahoo.com/most-active",
        "https://finance.yahoo.com/gainers",
        "https://finance.yahoo.com/losers",
        "https://finance.yahoo.com/trending-tickers",
    ]

SYMB_RE = re.compile(r"/quote/([A-Z][A-Z0-9\.=-]{0,5})[/?\"]")

def scrape_symbols_from_html(html: str) -> List[str]:
    return list({m.group(1).replace('=F','') for m in SYMB_RE.finditer(html)})

def get_quote_summary(symbol: str) -> dict:
    url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
    params = {"modules": "price,summaryDetail,defaultKeyStatistics,assetProfile"}
    r = requests.get(url, params=params, headers=UA, timeout=10)
    r.raise_for_status()
    data = r.json()
    try:
        return data["quoteSummary"]["result"][0]
    except Exception:
        return {}

def get_chart_15m(symbol: str) -> dict:
    # 1d range, 15m bars – includes volume array
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": "15m", "range": "1d"}
    r = requests.get(url, params=params, headers=UA, timeout=10)
    r.raise_for_status()
    return r.json()

def last_15m_volume(symbol: str) -> int:
    try:
        ch = get_chart_15m(symbol)
        v = ch["chart"]["result"][0]["indicators"]["quote"][0]["volume"]
        if not v:
            return 0
        # last non-null bar
        for n in reversed(v):
            if n is not None:
                return int(n)
    except Exception:
        pass
    return 0

# -----------------------------
# Scan logic – Version 7.5 rules
# -----------------------------

REAL_CATALYST_KEYWORDS = {
    "earnings","guidance","m&a","acquisition","merger","takeover",
    "13d","13g","insider","buyback","repurchase","contract","partnership","deal"
}
SPECULATIVE_KEYWORDS = {"strategic review","pipeline","explore options"}

def classify_catalyst(summary: dict) -> Tuple[str, str]:
    """
    We use the Yahoo 'assetProfile' longBusinessSummary as a lightweight proxy.
    It’s not perfect, but it lets us tag obvious buybacks / deals that appear in the
    profile or description. For a stronger signal you can replace this with a news pulse.
    """
    text = ""
    try:
        text = " ".join([
            summary.get("assetProfile", {}).get("longBusinessSummary", ""),
            summary.get("price", {}).get("longName", "") or ""
        ]).lower()
    except Exception:
        pass

    if any(k in text for k in REAL_CATALYST_KEYWORDS):
        return ("Real", "Tier-1/2 catalyst")
    if any(k in text for k in SPECULATIVE_KEYWORDS):
        return ("Spec", "Tier-3 speculative")
    return ("None", "")

def vwap_status(price_now: float, prior_close: float) -> str:
    return "Above" if price_now >= (prior_close or price_now) else "Below"

def derive_trigger(price_now: float) -> Tuple[float, str]:
    trig = round(price_now * 1.02, 2)
    pct = f"+{round((trig/price_now - 1)*100, 2)}%"
    return trig, pct

def volume_gate_ok(vol15: int, free_float: float) -> bool:
    gate1 = vol15 >= VOLUME_SHARES_GATE
    gate2 = free_float and (vol15 >= free_float * VOLUME_FLOAT_PCT_GATE)
    return gate1 or gate2

def sector_from_summary(summary: dict) -> str:
    try:
        return summary.get("assetProfile", {}).get("sector", "") or ""
    except Exception:
        return ""

def is_adr(symbol: str, summary: dict) -> bool:
    # Basic proxy: Yahoo marks ADRs via country + exchange, but it varies.
    # Heuristic: ticker endswith 'Y' and not on NASDAQ/NYSE proper => often ADR.
    try:
        exch = summary.get("price", {}).get("exchangeName", "") or ""
        if symbol.endswith("Y") and "Nasdaq" not in exch and "NYQ" not in exch:
            return True
    except Exception:
        pass
    return False

def free_float_shares(summary: dict) -> float:
    """
    Use 'floatShares' if present; fallback to 'sharesOutstanding'.
    Yahoo returns raw shares (not millions) for these modules.
    """
    ks = summary.get("defaultKeyStatistics", {}) or {}
    try:
        if "floatShares" in ks and ks["floatShares"] and "raw" in ks["floatShares"]:
            return float(ks["floatShares"]["raw"])
    except Exception:
        pass
    try:
        so = summary.get("price", {}).get("sharesOutstanding", {})
        if so and "raw" in so:
            return float(so["raw"])
    except Exception:
        pass
    return 0.0

def price_now_and_prevclose(summary: dict) -> Tuple[float, float]:
    p = summary.get("price", {}) or {}
    now = p.get("regularMarketPrice", {}).get("raw", 0.0) or 0.0
    pc  = p.get("regularMarketPreviousClose", {}).get("raw", 0.0) or 0.0
    return float(now or 0.0), float(pc or 0.0)

def scan_symbol(symbol: str) -> Dict | None:
    try:
        summary = get_quote_summary(symbol)
        if not summary:
            return None

        # sector / avoid FDA binaries by default
        sector = sector_from_summary(summary)
        if sector in EXCLUDE_SECTORS:
            return None

        # price gate
        price_now, prev_close = price_now_and_prevclose(summary)
        if price_now <= 0 or price_now > PRICE_MAX:
            return None

        # float gate (override only if institutional-grade catalyst)
        ff = free_float_shares(summary)
        catalyst_kind, catalyst_note = classify_catalyst(summary)
        if ff > FLOAT_MAX and catalyst_kind != "Real":
            return None

        # ADR rule – allowed but Tier-2+
        adr = is_adr(symbol, summary)

        # 15m volume gate
        vol15 = last_15m_volume(symbol)
        vol_ok = volume_gate_ok(vol15, ff)

        # classify tier/grade
        if catalyst_kind == "Spec":
            tier, grade = ("Tier-3", "C")
        elif adr:
            tier, grade = ("Tier-2", "B")
        else:
            tier, grade = ("Tier-1", "A")

        vw = vwap_status(price_now, prev_close)
        trig, pct_to_trig = derive_trigger(price_now)

        note_bits = []
        if catalyst_kind == "Spec":
            note_bits.append("Spec PR — tiny size only")
        if adr:
            note_bits.append("ADR (Tier-2+)")

        # Score (rough): real catalyst + volume pass + above VWAP – distance to trigger
        try:
            gap = float(pct_to_trig.strip("%+"))
        except Exception:
            gap = 2.0
        score = (5 if catalyst_kind == "Real" else 2) + (3 if vol_ok else 0) + (3 if vw == "Above" else 0) - gap

        row = {
            "symbol": symbol,
            "Tier/Grade": f"{tier}/{grade}",
            "trigger": trig,
            "%_to_trigger": pct_to_trig,
            "VWAP_Status": vw,
            "15m_Vol_vs_Req": f"{'Meets' if vol_ok else 'Below'} ({vol15:,})",
            "price": round(price_now, 3),
            "Catalyst": f"{catalyst_kind}: {catalyst_note}",
            "Sector_Heat": "🔥" if vw == "Above" else "⚪",
            "Note": "; ".join(note_bits) if note_bits else "",
            "_score": score
        }
        return row
    except Exception:
        return None

# -----------------------------
# API routes
# -----------------------------

@app.route("/")
def ping():
    return jsonify({"ok": True, "ts": int(time.time())})

@app.route("/universe")
def show_universe():
    return jsonify({"count": len(UNIVERSE), "tickers": UNIVERSE, "ts": int(time.time())})

@app.route("/refresh-universe")
def refresh_universe():
    global UNIVERSE, LAST_UNIVERSE_TS
    tickers = set()
    for url in yh_universe_pages():
        try:
            r = requests.get(url, headers=UA, timeout=12)
            r.raise_for_status()
            tickers.update(scrape_symbols_from_html(r.text))
        except Exception:
            continue
    # simple clean: letters, numbers, dots; keep to reasonable length
    cleaned = []
    for t in sorted(tickers):
        if 1 <= len(t) <= 6 and re.match(r"^[A-Z][A-Z0-9\.]*$", t):
            cleaned.append(t)
    UNIVERSE = cleaned
    LAST_UNIVERSE_TS = int(time.time())
    return jsonify({"message": "universe refreshed", "count": len(UNIVERSE), "ts": LAST_UNIVERSE_TS})

@app.route("/scan")
def run_scan():
    """
    Build the Near-Trigger Board using Version 7.5 rules.
    """
    global NEAR_TRIGGER_BOARD, LAST_SCAN_TS
    results = []
    # if universe empty, auto-refresh first
    if not UNIVERSE:
        refresh_universe()
    for sym in UNIVERSE[:600]:  # safety cap
        row = scan_symbol(sym)
        if row:
            results.append(row)
    # rank
    results.sort(key=lambda r: r["_score"], reverse=True)
    for r in results:
        r.pop("_score", None)
    NEAR_TRIGGER_BOARD = results
    LAST_SCAN_TS = int(time.time())
    return jsonify({"message": "scan complete", "count": len(NEAR_TRIGGER_BOARD), "ts": LAST_SCAN_TS})

@app.route("/board")
def board():
    age_min = round((time.time() - LAST_SCAN_TS) / 60, 2) if LAST_SCAN_TS else None
    return jsonify({
        "age_min": age_min if age_min is not None else None,
        "count": len(NEAR_TRIGGER_BOARD),
        "near_trigger_board": NEAR_TRIGGER_BOARD,
        "stale": False,
        "ts": int(time.time())
    })

# -----------------------------
# main
# -----------------------------
if __name__ == "__main__":
    # local dev
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
