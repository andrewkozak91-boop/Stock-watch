from flask import Flask, request, jsonify
import finnhub
import os

app = Flask(__name__)

# Your Finnhub API key
FINNHUB_API_KEY = "d3ir0o9r01qrurai8t9gd3ir0o9r01qrurai8ta0"
finnhub_client = finnhub.Client(api_key=FINNHUB_API_KEY)

# Health check
@app.route("/health")
def health():
    return jsonify({"status": "ok"})

# Quote endpoint
@app.route("/quote")
def quote():
    symbol = request.args.get("symbol", "AAPL")
    try:
        data = finnhub_client.quote(symbol)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)})

# Market scan placeholder
@app.route("/scan")
def scan():
    return jsonify({
        "status": "ok",
        "message": "Manual scan simulated successfully",
        "results": [
            {"symbol": "MVIS", "tier": "Tier 1", "grade": "A", "note": "Coil near breakout"},
            {"symbol": "SMCI", "tier": "Tier 2", "grade": "B+", "note": "Strong setup building"},
            {"symbol": "SOFI", "tier": "Tier 3", "grade": "B", "note": "Needs volume confirmation"}
        ]
    })

# Near-trigger board placeholder
@app.route("/board")
def board():
    return jsonify({
        "count": 3,
        "near_trigger_board": [
            {"symbol": "MVIS", "tier": "Tier 1", "grade": "A"},
            {"symbol": "SMCI", "tier": "Tier 2", "grade": "B+"},
            {"symbol": "SOFI", "tier": "Tier 3", "grade": "B"}
        ]
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
