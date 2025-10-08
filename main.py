from flask import Flask, jsonify, request
import os, time
import finnhub

app = Flask(__name__)

API_KEY = "d3ir0o9r01qrurai8t9gd3ir0o9r01qrurai8ta0"  # your Finnhub key
fh = finnhub.Client(api_key=API_KEY)

@app.route("/health")
def health():
    return jsonify({"ok": True, "ts": int(time.time())})

@app.route("/quote")
def quote():
    sym = request.args.get("symbol", "TSLA").upper()
    try:
        return jsonify({"symbol": sym, **fh.quote(sym)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
