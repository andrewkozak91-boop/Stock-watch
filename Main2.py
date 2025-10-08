from flask import Flask, jsonify, request
import os, time, datetime as dt
import finnhub

app = Flask(__name__)

API_KEY = "d3ir0o9r01qrurai8t9gd3ir0o9r01qrurai8ta0"  # your Finnhub key
fh = finnhub.Client(api_key=API_KEY)

# --------- simple in-memory cache ----------
CACHE = {
    "board": {"count": 0, "near_trigger_board": [], "ts": 0}
}

# ------- universe (starter) -------
# We’ll expand this later to a fuller universe under $30.
UNIVERSE = ["TSLA","AMD","PLTR","APLD","BBAI","NOK","AI","ONDS","ZENA",
            "MVIS","IONQ","U","F","T","SOFI","RIOT","MARA","CHPT","DKNG",
            "RUN","ENVX","BBBYQ","IQ","LCID","RIVN","XPEV","NIO","BILI"]

# ---------- helpers ----------
def now_ts():
    return int(time.time())

def is_market_hours_toronto(t=None):
    # 9:30-16:00 America/Toronto, Mon-Fri (simple check without pytz)
    # This is a light approximation; we can refine with a proper tz lib later.
    t = t or dt.datetime.utcnow()
    # Toronto is UTC-4 or -5; use -4 as default (EDT). Good enough for MVP.
    toronto = t - dt.timedelta(hours=4)
    if toronto.weekday() >= 5:  # Sat/Sun
        return False
    h, m = toronto.hour, toronto.minute
    start = 9*60 + 30
    end   = 16*60
    mins = h*60 + m
    return start <= mins <= end

# ---------- routes ----------
@app.route("/health")
def health():
    return jsonify({"ok": True, "ts": now_ts()})

@app.route("/quote")
def quote():
    sym = request.args.get("symbol", "TSLA").upper()
    try:
        return jsonify({"symbol": sym, **fh.quote(sym)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/scan")
def scan():
    """
    Rebuilds the near-trigger board and stores it in CACHE["board"].
    Call this via browser, or schedule with Render Cron.
    """
    near = []
    for sym in UNIVERSE:
        try:
            q = fh.quote(sym)
            price = q.get("c")
            if not price:
                continue
            # Basic gates (MVP): price < $30 and modest day move band
            if price >= 30:
                continue
            dp = q.get("dp", 0.0)
            if -2.0 < dp < 4.0:
                near.append({
                    "symbol": sym,
                    "price": price,
                    "dp": dp,
                    "h": q.get("h"),
                    "l": q.get("l"),
                    "pc": q.get("pc")
                })
        except Exception as e:
            continue

    # Rank by absolute % change (closest to flat = “near trigger” feel)
    near.sort(key=lambda x: abs(x.get("dp", 0.0)))

    CACHE["board"] = {
        "count": len(near),
        "near_trigger_board": near[:25],  # top 25
        "ts": now_ts(),
        "market_hours": is_market_hours_toronto()
    }
    return jsonify({"ok": True, "scanned": len(near), "ts": now_ts()})

@app.route("/board")
def board():
    """
    Returns the most recent cached scan.
    If it’s stale (>15 min) we tell you so in the response.
    """
    b = CACHE["board"]
    age_min = (now_ts() - (b["ts"] or 0)) / 60.0
    b_out = dict(b)
    b_out["age_min"] = round(age_min, 2)
    b_out["stale"] = age_min > 15
    return jsonify(b_out)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
