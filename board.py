from flask import Blueprint, jsonify
import finnhub
import time

bp = Blueprint("board", __name__)
finnhub_client = finnhub.Client(api_key="d3ir0o9r01qrurai8t9gd3ir0o9r01qrurai8ta0")

watchlist = ["TSLA", "AMD", "PLTR", "APLD", "BBAI", "NOK", "AI", "ONDS", "ZENA"]

@bp.route("/board")
def get_board():
    near_trigger = []
    for symbol in watchlist:
        try:
            q = finnhub_client.quote(symbol)
            change_pct = q["dp"]
            if -1.5 < change_pct < 3.5:
                near_trigger.append({
                    "symbol": symbol,
                    "price": q["c"],
                    "change_pct": change_pct
                })
        except Exception as e:
            print(f"Error {symbol}: {e}")
            continue
    return jsonify({
        "count": len(near_trigger),
        "near_trigger_board": near_trigger,
        "ts": int(time.time())
    })
  from board import bp as board_bp
app.register_blueprint(board_bp)
