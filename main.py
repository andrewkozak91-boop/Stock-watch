from flask import Flask, jsonify
import requests
import threading
import time
import os

app = Flask(__name__)

# ---- CONFIG ----
API_KEY = "d3ir0o9r01qrurai8t9gd3ir0o9r01qrurai8ta0"  # Your Finnhub API key
BASE_URL = "https://finnhub.io/api/v1"
SCAN_INTERVAL = 900  # 15 minutes (900 seconds)

# ---- GLOBAL BOARD ----
board_data = {
    "near_trigger_board": [],
    "count": 0,
    "age_min": 0,
    "stale": False,
    "ts": int(time.time())
}

# ---- CORE FUNCTIONS ----
def fetch_stock_quote(symbol):
    """Get latest quote data from Finnhub."""
    url = f"{BASE_URL}/quote?symbol={symbol}&token={API_KEY}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        print(f"[Error fetching {symbol}] {e}")
    return None

def scan_market():
    """Simulated scan logic — replace with real criteria when needed."""
    print("[SCAN] Running market scan...")
    test_symbols = ["TSLA", "AAPL", "NVDA", "AMD", "AMZN"]
    results = []

    for sym in test_symbols:
        data = fetch_stock_quote(sym)
        if data:
            price = data.get("c")
            if price and price > 0:  # placeholder logic
                results.append({
                    "symbol": sym,
                    "price": price,
                    "trigger": round(price * 1.02, 2),
                    "%_to_trigger": "+2%",
                    "VWAP_Status": "Above" if data.get("c", 0) > data.get("pc", 0) else "Below",
                    "Vol_15m_vs_Req": "1.5x",
                    "Catalyst": "Earnings Watch",
                    "Sector_Heat": "⚪",
                    "Note": "Test data only"
                })
                print(f"[SCAN] Added {sym} to board")

    # Update global board
    board_data["near_trigger_board"] = results
    board_data["count"] = len(results)
    board_data["age_min"] = 0
    board_data["ts"] = int(time.time())
    board_data["stale"] = False
    print(f"[SCAN] Completed with {len(results)} tickers.")

# ---- AUTO UPDATE ----
def auto_scan():
    """Runs scan automatically every 15 minutes."""
    while True:
        try:
            scan_market()
            print(f"[AutoScan] Refresh OK {time.ctime()}")
        except Exception as e:
            print(f"[AutoScan] Error: {e}")
        time.sleep(SCAN_INTERVAL)

# ---- ROUTES ----
@app.route("/")
def home():
    return jsonify({"status": "ok", "message": "Stock-Watch API running"})

@app.route("/scan")
def manual_scan():
    scan_market()
    return jsonify({"message": "Manual scan complete", "count": board_data["count"]})

@app.route("/board")
def get_board():
    age = (time.time() - board_data["ts"]) / 60
    board_data["age_min"] = round(age, 2)
    return jsonify(board_data)

# ---- STARTUP ----
if __name__ == "__main__":
    # Start background auto-scan thread
    threading.Thread(target=auto_scan, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
