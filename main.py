from __future__ import annotations
from flask import Flask, jsonify, request
import os, time, math, textwrap, pathlib
from datetime import datetime, timedelta

# 3rd party (resolved by requirements.txt)
import yfinance as yf
import pandas as pd

app = Flask(__name__)

# -------------------------
# 7.5-Lite knobs (tunable)
# -------------------------
PRICE_MAX = 40.0                # relaxed (was 30)
FLOAT_MAX = 250_000_000         # 250M (was 150M)
FIFTEEN_MIN_VOL_ABS = 500_000   # relaxed (was 2,000,000)
FIFTEEN_MIN_VOL_FLOAT = 0.003   # 0.3% of free float (was 0.75%)
AVOID_FDA_KEYWORDS = ("PDUFA", "FDA", "advisory committee")
UNIVERSE_MIN = 120              # we’ll try to keep at least this many symbols
CACHE_TTL_SEC = 12 * 60         # board freshness window (12 minutes)
HIST_DAYS = 20                  # pull ~1 month for VWAP-ish/vol calculations

# -------------------------
# Universe builder
# -------------------------
def read_optional_tickers() -> list[str]:
    """
    Load extra tickers if the user provides:
    - repo Tickets.txt  (one symbol per line)
    - render secret file at /etc/secrets/Tickets.txt (if you add one)
    Duplicates are removed & normalized.
    """
    candidates = []

    # Repo copy
    p_repo = pathlib.Path(__file__).parent / "Tickets.txt"
    if p_repo.exists():
        candidates += [x.strip().upper() for x in p_repo.read_text().splitlines() if x.strip()]

    # Render secret file (optional)
    p_secret = pathlib.Path("/etc/secrets/Tickets.txt")
    if p_secret.exists():
        candidates += [x.strip().upper() for x in p_secret.read_text().splitlines() if x.strip()]

    # De-dupe; keep only sensible tickers
    out = []
    seen = set()
    for t in candidates:
        if not t.isalpha():       # keep this simple (filters out junk)
            continue
        if len(t) > 5:            # most US tickers ≤ 5 chars
            continue
        if t not in seen:
            seen.add(t); out.append(t)
    return out

def seed_universe() -> list[str]:
    """
    A broad, liquid seed. This prevents empty scans even if no custom file is provided.
    - High-volume tech/AI/EV/social/fin/airlines/gaming/energy/travel
    - Mix of small/mid caps <= ~$40 typical range
    """
    core = [
        # AI / Chips / Compute-adjacent
        "NVDA","AMD","ARM","SMCI","MU","INTC","PLTR","AI","SOUN","BBAI","IONQ","NVTS","AEHR",
        # EV / Batteries / Alt-Energy
        "TSLA","RIVN","LCID","QS","CHPT","BLNK","RUN","ENPH","FSLR","PLUG","NOVA",
        # Crypto miners / related
        "RIOT","MARA","HUT","CLSK","IREN",
        # Airlines / Travel / Leisure / Hotels / Cruises
        "AAL","UAL","DAL","LUV","NCLH","CCL","RCL","ABNB","BKNG","MAR","H","HTHT",
        # Auto & legacy semi-liquid
        "F","GM","STLA","NIO","XPEV","LI",
        # Social / Consumer internet
        "SNAP","PINS","RUM","TME","BILI","HUYA",
        # Commerce / Fintech
        "SHOP","PYPL","SQ","SOFI","UPST","HOOD","AFRM","COIN",
        # Cyber / Cloud / SAAS (midcaps ≤ $40 fluctuate)
        "NET","DDOG","ZS","CRWD","OKTA","MDB","ESTC","APP","U","COUR","S","CFLT","SPLK",
        # Biopharma (non-binary, liquid-ish midcaps)
        "MRNA","PFE","BMY","GILD","VRTX","REGN",
        # Industrials / Materials / Energy
        "X","AA","CLF","FCX","CCJ","BTU","APA","MRO","SLB","HAL","PSX","VLO",
        # Media / Gaming / Streamers
        "NFLX","WBD","DIS","RBLX","TTWO","EA","CHWY",
        # Telecom / Staples / Diversifiers
        "T","VZ","KHC","CPB","KO","PEP",
        # Robotics / Lidar / Mobility
        "LAZR","VLDR","MVIS","JOBY","ACHR",
        # Misc high-turnover small/mids
        "PTON","ENVX","NU","OPEN","UWMC","DNA","IONQ","SOUN","QS","PLUG","RUN","BLNK",
    ]

    extras = read_optional_tickers()

    # normalize and dedupe
    uni = []
    seen = set()
    for t in core + extras:
        t = t.strip().upper()
        if not t or t in seen: 
            continue
        seen.add(t)
        uni.append(t)

    # Make sure we have a decent pool
    return uni

def current_universe() -> list[str]:
    # cache + allow manual refresh with /universe?refresh=1
    global _UNIVERSE, _UNIVERSE_TS
    now = time.time()
    if _UNIVERSE and (now - _UNIVERSE_TS < 15 * 60) and request.args.get("refresh") != "1":
        return _UNIVERSE
    _UNIVERSE = seed_universe()
    _UNIVERSE_TS = now
    return _UNIVERSE

_UNIVERSE: list[str] = []
_UNIVERSE_TS: float = 0.0

# -------------------------
# Helpers
# -------------------------
def safe_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default

def get_yf_info(symbol: str) -> dict:
    """
    Pulls:
      - current price
      - previous close
      - short % of float proxy via sharesOutstanding
      - last 20d history (for 15m volume & vwap-ish proxy)
    """
    tk = yf.Ticker(symbol)
    info = tk.fast_info or {}
    price = safe_float(info.get("lastPrice"), default=None)
    prev_close = safe_float(info.get("previousClose"), default=price)

    shares_out = safe_float(info.get("sharesOutstanding"), default=None)
    # yfinance sometimes returns None; try fallback via .info
    if shares_out is None:
        try:
            fallback = tk.info
            shares_out = safe_float(fallback.get("sharesOutstanding"), default=None)
        except Exception:
            shares_out = None

    # historical for last ~20 trading days
    try:
        hist = tk.history(period=f"{HIST_DAYS}d", interval="15m", auto_adjust=False)
    except Exception:
        hist = pd.DataFrame()

    return {
        "price": price,
        "prev_close": prev_close,
        "shares_out": shares_out,
        "hist": hist
    }

def last_15m_volume(hist: pd.DataFrame) -> int:
    if hist is None or hist.empty or "Volume" not in hist.columns:
        return 0
    # last completed bar
    return int(hist["Volume"].iloc[-1])

def vwap_status(hist: pd.DataFrame, price: float, prev_close: float) -> str:
    """
    VWAP requires intraday ticks; we’ll proxy with
    prev_close & rolling typical price. Keep it simple and stable.
    """
    if price is None:
        return "NA"
    return "Above" if (price >= (prev_close or price)) else "Below"

def catalyst_tag(symbol: str) -> tuple[str,str]:
    """
    Light heuristic: tag tier by sector/symbol class & common catalyst presence.
    Without paid news, we mark as 'Real' for earnings week windows
    and keep 'Spec' otherwise. It’s a soft label to preserve your 7.5 feel.
    """
    # Earnings windows: ±7 days of next/last earnings (if available)
    kind, note = "Spec", ""
    try:
        cal = yf.Ticker(symbol).get_earnings_dates(limit=1)
        if not cal.empty:
            edate = pd.to_datetime(cal.index[0]).tz_localize(None)
            if abs((datetime.utcnow() - edate).days) <= 7:
                kind, note = "Real", "Earnings window"
    except Exception:
        pass
    return kind, note

def blocked_biotech(symbol: str) -> bool:
    name = (symbol or "").upper()
    return any(k in name for k in AVOID_FDA_KEYWORDS)

def classify_tier(is_adr: bool, catalyst_kind: str) -> tuple[str, str]:
    if catalyst_kind == "Spec":
        return "Tier-3", "C"
    if is_adr:
        return "Tier-2", "B"
    return "Tier-1", "A"

def derive_trigger(price: float) -> tuple[float, str]:
    if not price:
        return 0.0, "+0%"
    trig = round(price * 1.02, 2)
    return trig, f"+{round((trig/price - 1.0) * 100.0, 2)}%"

def is_adr_symbol(symbol: str) -> bool:
    # lightweight: ADRs often end in 'Y' or trade primarily as foreign listings
    return symbol.endswith("Y")

def volume_gate_ok(v15: int, float_sh: float|None) -> bool:
    gate1 = v15 >= FIFTEEN_MIN_VOL_ABS
    gate2 = (float_sh and v15 >= float_sh * FIFTEEN_MIN_VOL_FLOAT)
    return bool(gate1 or gate2)

# -------------------------
# Scanner (7.5-Lite)
# -------------------------
def score_row(row: dict) -> float:
    # Simple ranking: prefer Real catalyst, VWAP Above, closer to trigger, and volume pass
    s = 0.0
    s += 5.0 if row["Catalyst"].startswith("Real") else 2.0
    s += 3.0 if row["VWAP_Status"] == "Above" else 0.0
    s += 3.0 if "Meets" in row["15m_Vol_vs_Req"] else 0.0
    try:
        gap = float(row["%_to_trigger"].strip("%+"))
        s -= gap
    except Exception:
        pass
    return s

def scan_symbol(sym: str) -> dict|None:
    data = get_yf_info(sym)

    price = data["price"]
    if price is None or price <= 0:
        return None
    if price > PRICE_MAX:
        return None
    if blocked_biotech(sym):
        return None

    shares_out = data["shares_out"]
    if shares_out and shares_out > FLOAT_MAX:
        # allow only when Real catalyst (institutional-grade override)
        kind, note = catalyst_tag(sym)
        if kind != "Real":
            return None
    else:
        kind, note = catalyst_tag(sym)

    hist = data["hist"]
    v15 = last_15m_volume(hist)
    v_ok = volume_gate_ok(v15, shares_out or 0)

    vw = vwap_status(hist, price, data["prev_close"])
    tier, grade = classify_tier(is_adr_symbol(sym), kind)
    trig, pct = derive_trigger(price)

    return {
        "symbol": sym,
        "Tier/Grade": f"{tier}/{grade}",
        "trigger": trig,
        "%_to_trigger": pct,
        "VWAP_Status": vw,
        "15m_Vol_vs_Req": f"{'Meets' if v_ok else 'Below'} ({v15:,})",
        "price": round(price, 3),
        "Catalyst": f"{kind}: {note}",
        "Note": "ADR (Tier-2+)" if is_adr_symbol(sym) else ""
    }

def run_scan(universe: list[str]) -> list[dict]:
    rows = []
    for s in universe:
        try:
            r = scan_symbol(s)
            if r:
                rows.append(r)
        except Exception as e:
            # keep scanning if one symbol fails
            print(f"scan error {s}: {e}")
            continue
    rows.sort(key=score_row, reverse=True)
    return rows

# -------------------------
# In-memory board cache
# -------------------------
_BOARD: list[dict] = []
_BOARD_TS: float = 0.0

def board_fresh() -> bool:
    return (time.time() - _BOARD_TS) < CACHE_TTL_SEC

# -------------------------
# Routes
# -------------------------
@app.route("/")
def root():
    return jsonify({
        "ok": True,
        "service": "stock-watch 7.5-Lite",
        "ts": int(time.time())
    })

@app.route("/ping")
def ping():
    return "pong", 200

@app.route("/universe")
def universe():
    uni = current_universe()
    return jsonify({"count": len(uni), "universe": uni, "ts": int(time.time())})

@app.route("/scan")
def scan():
    global _BOARD, _BOARD_TS
    uni = current_universe()
    _BOARD = run_scan(uni)
    _BOARD_TS = time.time()
    return jsonify({"message": "scan complete", "count": len(_BOARD), "ts": int(_BOARD_TS)})

@app.route("/board")
def board():
    age_min = round((time.time() - _BOARD_TS) / 60.0, 2) if _BOARD_TS else None
    return jsonify({
        "age_min": age_min,
        "count": len(_BOARD),
        "near_trigger_board": _BOARD,
        "stale": False if board_fresh() else True,
        "ts": int(time.time())
    })

# -------------------------
# Entrypoint
# -------------------------
if __name__ == "__main__":
    # warm up universe so /board isn’t empty forever
    current_universe()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
