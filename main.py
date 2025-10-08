from flask import Flask, jsonify, request
import os, time
import finnhub

# --- App & API client ---
app = Flask(__name__)
API_KEY = "d3ir0o9r01qrurai8t9gd3ir0o9r01qrurai8ta0"   # your Finnhub key
fh = finnhub.Client(api_key=API_KEY)

# --- Health ---
@app.route("/health")
def health():
    return jsonify({"ok": True, "ts": int(time.time())})

# --- Quote ---
@app.route("/quote")
def quote():
    sym = request.args.get("symbol", "TSLA").upper()
    try:
        data = fh.quote(sym)
        return jsonify({"symbol": sym, **data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- Simple Near-Trigger Board ---
# (starter version; we can upgrade rules once this is stable)
WATCHLIST = ["TSLA","AMD","PLTR","APLD","BBAI","NOK","AI","ONDS","ZENA"]

@app.route("/board")
def board():
    out = []
    for sym in WATCHLIST:
        try:
            q = fh.quote(sym)
            # "Near trigger" placeholder: modest daily move window
            dp = q.get("dp", 0.0)
            if -1.5 < dp < 3.5:
                out.append({
                    "symbol": sym,
                    "price": q.get("c"),
                    "dp": dp,
                    "prev_close": q.get("pc"),
                    "high": q.get("h"),
                    "low": q.get("l")
                })
        except Exception as e:
            # Don’t crash the service if one symbol fails
            out.append({"symbol": sym, "error": str(e)})
            continue
    return jsonify({"count": len(out), "near_trigger_board": out, "ts": int(time.time())})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
