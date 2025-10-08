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
UNIVERSE = ["TSLA","AMD","PLTR","APLD","BBAI","NOK","AI","ONDS","ZENA",
            "MVIS","U","F","T","SOFI","RIOT","MARA","CHPT","DKNG",
            "RUN","ENVX","BBBYQ","IQ","LCID","RIVN","XPEV","NIO","BILI"]

def now_ts():
    return int(time.time())

def is_market_hours_toronto(t=None):
    t = t or dt.datetime.utcnow()
    toronto = t - dt.timedelta(hours=4)
    if toronto.weekday() >= 5:
        return False
    h, m = toronto.hour, toronto.minute
    start = 9*60 + 30
    end   = 16*60
    mins = h*60 + m
    return start <= mins <= end

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
    near = []
    for sym in UNIVERSE:
        try:
            q = fh.quote(sym)
            price = q.get("c")
            if not price or price >= 30:
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
        except Exception:
            continue

    near.sort(key=lambda x: abs(x.get("dp", 0.0)))

    CACHE["board"] = {
        "count": len(near),
        "near_trigger_board": near[:25],
        "ts": now_ts(),
        "market_hours": is_market_hours_toronto()
    }
    return jsonify({"ok": True, "scanned": len(near), "ts": now_ts()})

@app.route("/board")
def board():
    b = CACHE["board"]
    age_min = (now_ts() - (b["ts"] or 0)) / 60.0
    b_out = dict(b)
    b_out["age_min"] = round(age_min, 2)
    b_out["stale"] = age_min > 15
    return jsonify(b_out)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
    Add /scan and /board endpoints.
