# ===============================================
#  STOCK WATCH – MAIN SERVER (v7.5.2-U)
#  Full-Market Universe Expansion
# ===============================================

from flask import Flask, request, jsonify
import time, json

app = Flask(__name__)

# --------------------------------------------------
#  DATA SOURCE
# --------------------------------------------------
def fetch_from_finnhub():
    """
    Fetch the full list of tradable tickers.
    Replace this function with your actual data provider if available.
    """
    try:
        with open("symbol_source.json") as f:
            data = json.load(f)
        return data
    except Exception:
        # fallback minimal set if no file
        return [
            "AAPL","TSLA","AMD","PLTR","SOFI","DNA","F","AAL","CCL","UAL","NCLH",
            "HOOD","CHPT","LCID","RUN","BLNK","RIVN","AI","BBD","PFE","T","BARK",
            "JOBY","UPST","MVIS","NU","QS","OPEN","COUR","ARMK"
        ]

# --------------------------------------------------
#  GET SYMBOLS (unlimited universe)
# --------------------------------------------------
def get_symbols(limit=None):
    """
    Fetches tradable tickers. If limit is None or 0, fetches the full list.
    """
    data = fetch_from_finnhub()
    data = [s for s in data if s.isalpha() and len(s) <= 5]

    if limit and limit > 0:
        data = data[:limit]
    return data

# --------------------------------------------------
#  ROUTE: UNIVERSE
# --------------------------------------------------
@app.route("/universe", methods=["GET"])
def universe():
    """
    Returns the full or partial stock universe.
    Supports ?force=1 to refresh, ?limit=1000 to restrict manually.
    """
    try:
        force = request.args.get("force", "0") == "1"
        limit_param = request.args.get("limit", "")
        limit = int(limit_param) if limit_param.isdigit() else 0

        symbols = get_symbols(limit=limit or None)
        ts = int(time.time())

        response = {
            "count": len(symbols),
            "symbols": symbols,
            "ts": ts
        }

        with open("universe_cache.json", "w") as f:
            json.dump(response, f, indent=2)

        print(f"[Universe] Pulled {len(symbols)} tickers at {ts}")
        return jsonify(response)

    except Exception as e:
        print(f"[Universe Error] {e}")
        return jsonify({"error": str(e)}), 500

# --------------------------------------------------
#  ROUTE: SCAN
# --------------------------------------------------
@app.route("/scan", methods=["GET"])
def scan():
    """
    Placeholder scan endpoint for Stock Game Version 7.5 logic.
    """
    try:
        ts = int(time.time())
        results = {
            "count": 0,
            "message": "scan complete",
            "ts": ts
        }
        print(f"[Scan] Completed at {ts}")
        return jsonify(results)
    except Exception as e:
        print(f"[Scan Error] {e}")
        return jsonify({"error": str(e)}), 500

# --------------------------------------------------
#  ROUTE: BOARD
# --------------------------------------------------
@app.route("/board", methods=["GET"])
def board():
    """
    Returns the latest Near-Trigger Board.
    Replace this section with live data logic when ready.
    """
    try:
        ts = int(time.time())
        board_data = {
            "count": 0,
            "near_trigger_board": [],
            "ts": ts
        }
        print(f"[Board] Returned empty board at {ts}")
        return jsonify(board_data)
    except Exception as e:
        print(f"[Board Error] {e}")
        return jsonify({"error": str(e)}), 500

# --------------------------------------------------
#  MAIN ENTRY POINT
# --------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
