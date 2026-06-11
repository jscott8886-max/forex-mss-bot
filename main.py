# ForexAI MSS Bot - v1.1 (fixed candle fetch)
import os, time, logging, math
from datetime import datetime, timezone
from flask import Flask, jsonify, request
from flask_cors import CORS
import threading
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)
 
app = Flask(__name__)
CORS(app)
 
OANDA_API_KEY    = os.environ.get("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "")
PAPER_MODE       = os.environ.get("PAPER_MODE", "true").lower() == "true"
OANDA_ENV        = "practice" if PAPER_MODE else "live"
 
SYMBOLS = ["EUR_USD", "GBP_USD", "USD_JPY"]
 
STRATEGY = {
    "stop_loss_pips": 15, "take_profit_pips": 30,
    "position_units": 10000, "cooldown_minutes": 15,
    "swing_lookback": 5, "key_level_tolerance": 0.0005,
    "min_sl_pips": 5, "max_sl_pips": 25
}
 
bot_state = {
    "running": True, "killed": False, "positions": {},
    "closed_trades": [], "diary": [], "day_pnl": 0.0,
    "total_trades": 0, "win_count": 0,
    "signals": {s: {"trend_1h": "NEUTRAL", "mss_type": "Waiting for trend", "price": 0} for s in SYMBOLS},
    "account_balance": 0.0, "account_equity": 0.0, "account_nav": 0.0,
    "active_cooldowns": {}, "market_open": False,
    "trend_1h": {s: "NEUTRAL" for s in SYMBOLS},
    "version": "ForexMSS-1.2"
}
 
def get_oanda_client():
    import oandapyV20
    return oandapyV20.API(access_token=OANDA_API_KEY, environment=OANDA_ENV)
 
def get_candles(symbol, granularity="M5", count=100):
    """Fetch candles using only count - no from/to conflict"""
    try:
        import oandapyV20.endpoints.instruments as instruments
        client = get_oanda_client()
        params = {"granularity": granularity, "count": count, "price": "M"}
        r = instruments.InstrumentsCandles(instrument=symbol, params=params)
        client.request(r)
        candles = r.response.get("candles", [])
        result = []
        for c in candles:
            if c.get("complete", False):
                m = c["mid"]
                result.append({
                    "time": c["time"],
                    "open": float(m["o"]), "high": float(m["h"]),
                    "low": float(m["l"]), "close": float(m["c"]),
                    "volume": int(c.get("volume", 0))
                })
        return result
    except Exception as e:
        log.error(f"Candles error {symbol}: {e}")
        return []
 
def pip_value(symbol):
    return 0.0001 if "JPY" not in symbol else 0.01
 
def is_market_open():
    now = datetime.now(timezone.utc)
    wd = now.weekday()
    h = now.hour + now.minute / 60
    if wd == 4 and h >= 21: return False
    if wd == 5: return False
    if wd == 6 and h < 21: return False
    return True
 
def get_account_info():
    try:
        import oandapyV20.endpoints.accounts as accounts
        client = get_oanda_client()
        r = accounts.AccountSummary(OANDA_ACCOUNT_ID)
        client.request(r)
        acct = r.response["account"]
        bot_state["account_balance"] = float(acct.get("balance", 0))
        bot_state["account_nav"]     = float(acct.get("NAV", 0))
        bot_state["account_equity"]  = float(acct.get("NAV", 0))
    except Exception as e:
        log.error(f"Account info error: {e}")
 
def sync_positions():
    try:
        import oandapyV20.endpoints.trades as trades
        client = get_oanda_client()
        r = trades.OpenTrades(OANDA_ACCOUNT_ID)
        client.request(r)
        open_trades = r.response.get("trades", [])
        synced = {}
        for t in open_trades:
            sym = t["instrument"]
            synced[sym] = {
                "symbol": sym, "entry": float(t["price"]),
                "units": int(t["currentUnits"]), "trade_id": t["id"],
                "open_time": t.get("openTime", datetime.now(timezone.utc).isoformat()),
                "current_price": float(t["price"]),
                "unrealized_pnl": float(t.get("unrealizedPL", 0))
            }
        bot_state["positions"] = synced
    except Exception as e:
        log.error(f"Sync positions error: {e}")
 
def place_order(symbol, units, side):
    try:
        import oandapyV20.endpoints.orders as orders
        client = get_oanda_client()
        actual_units = units if side == "BUY" else -units
        data = {"order": {"type": "MARKET", "instrument": symbol, "units": str(actual_units)}}
        r = orders.OrderCreate(OANDA_ACCOUNT_ID, data=data)
        client.request(r)
        fill = r.response.get("orderFillTransaction", {})
        return float(fill.get("price", 0))
    except Exception as e:
        log.error(f"Order error {symbol}: {e}")
        return None
 
def close_position(symbol, trade_id):
    try:
        import oandapyV20.endpoints.trades as trades
        client = get_oanda_client()
        r = trades.TradeClose(OANDA_ACCOUNT_ID, trade_id)
        client.request(r)
        fill = r.response.get("orderFillTransaction", {})
        return float(fill.get("price", 0))
    except Exception as e:
        log.error(f"Close position error {symbol}: {e}")
        return None
 
def add_diary(symbol, text, entry_type="info"):
    entry = {"time": datetime.now(timezone.utc).strftime("%H:%M"), "symbol": symbol, "text": text, "type": entry_type}
    bot_state["diary"].insert(0, entry)
    if len(bot_state["diary"]) > 200:
        bot_state["diary"] = bot_state["diary"][:200]
 
def detect_trend_1h(symbol):
    candles = get_candles(symbol, "H1", 20)
    if len(candles) < 10:
        return "NEUTRAL"
    highs = [c["high"] for c in candles]
    lows  = [c["low"]  for c in candles]
    recent_high = max(highs[-5:])
    prev_high   = max(highs[-10:-5])
    recent_low  = min(lows[-5:])
    prev_low    = min(lows[-10:-5])
    if recent_high > prev_high and recent_low > prev_low:
        return "BULL"
    elif recent_high < prev_high and recent_low < prev_low:
        return "BEAR"
    return "NEUTRAL"
 
def detect_mss(symbol, trend):
    candles = get_candles(symbol, "M5", 30)
    if len(candles) < 15:
        return None, None
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    closes = [c["close"] for c in candles]
    price  = closes[-1]
    pv     = pip_value(symbol)
 
    if trend == "BULL":
        # Look for higher low after series of lower lows
        recent_lows = lows[-8:]
        if len(recent_lows) < 4:
            return None, None
        made_lower_low = recent_lows[-3] < recent_lows[-5]
        now_higher_low = recent_lows[-1] > recent_lows[-2]
        if made_lower_low and now_higher_low:
            swing_low = min(recent_lows[-4:])
            sl_pips = (price - swing_low) / pv
            if STRATEGY["min_sl_pips"] <= sl_pips <= STRATEGY["max_sl_pips"]:
                return "BUY", swing_low
 
    elif trend == "BEAR":
        recent_highs = highs[-8:]
        if len(recent_highs) < 4:
            return None, None
        made_higher_high = recent_highs[-3] > recent_highs[-5]
        now_lower_high   = recent_highs[-1] < recent_highs[-2]
        if made_higher_high and now_lower_high:
            swing_high = max(recent_highs[-4:])
            sl_pips = (swing_high - price) / pv
            if STRATEGY["min_sl_pips"] <= sl_pips <= STRATEGY["max_sl_pips"]:
                return "SELL", swing_high
 
    return None, None
 
def trading_loop():
    add_diary("SYSTEM", "ForexAI MSS Bot started | SL=15pips | TP=30pips | Cooldown=15min", "system")
    log.info("ForexAI MSS Bot v1.2 started")
    trend_check_time = {}
 
    while True:
        try:
            if not is_market_open():
                bot_state["market_open"] = False
                time.sleep(60)
                continue
 
            bot_state["market_open"] = True
            get_account_info()
            sync_positions()
            now = datetime.now(timezone.utc)
 
            # Clear expired cooldowns
            expired = [s for s, t in bot_state["active_cooldowns"].items()
                       if (now - datetime.fromisoformat(t)).total_seconds() > STRATEGY["cooldown_minutes"] * 60]
            for s in expired:
                del bot_state["active_cooldowns"][s]
 
            for symbol in SYMBOLS:
                if bot_state["killed"]:
                    break
 
                # Update 1H trend every 15 minutes
                last_check = trend_check_time.get(symbol)
                if not last_check or (now - last_check).total_seconds() > 900:
                    trend = detect_trend_1h(symbol)
                    bot_state["trend_1h"][symbol] = trend
                    trend_check_time[symbol] = now
                else:
                    trend = bot_state["trend_1h"][symbol]
 
                pv = pip_value(symbol)
 
                # Check exits
                if symbol in bot_state["positions"]:
                    pos = bot_state["positions"][symbol]
                    candles = get_candles(symbol, "M5", 3)
                    if not candles:
                        continue
                    price = candles[-1]["close"]
                    entry = pos["entry"]
                    pnl_pips = (price - entry) / pv
 
                    should_exit = False
                    reason = ""
                    if pnl_pips >= STRATEGY["take_profit_pips"]:
                        should_exit = True; reason = "Take profit"
                    elif pnl_pips <= -STRATEGY["stop_loss_pips"]:
                        should_exit = True; reason = "Stop loss"
                        bot_state["active_cooldowns"][symbol] = now.isoformat()
 
                    if should_exit:
                        exit_price = close_position(symbol, pos["trade_id"])
                        if exit_price:
                            if "JPY" in symbol:
                                pnl = (exit_price - entry) * pos["units"] / exit_price
                            else:
                                pnl = (exit_price - entry) * pos["units"]
                            win = pnl > 0
                            bot_state["day_pnl"] += pnl
                            bot_state["total_trades"] += 1
                            if win: bot_state["win_count"] += 1
                            bot_state["closed_trades"].append({"symbol": symbol, "entry": entry, "exit": exit_price,
                                "pnl": round(pnl,2), "pips": round(pnl_pips,1), "win": win, "reason": reason})
                            add_diary(symbol, f"{'WIN' if win else 'LOSS'} | {entry:.5f} -> {exit_price:.5f} | {round(pnl_pips,1)} pips | ${round(pnl,2)} | {reason}",
                                      "win" if win else "loss")
                            del bot_state["positions"][symbol]
 
                elif symbol not in bot_state["active_cooldowns"] and trend != "NEUTRAL" and not bot_state["killed"]:
                    direction, sl_level = detect_mss(symbol, trend)
                    candles = get_candles(symbol, "M5", 3)
                    price = candles[-1]["close"] if candles else 0
 
                    bot_state["signals"][symbol] = {"trend_1h": trend, "mss_type": direction or "Watching", "price": price}
 
                    if direction == "BUY":
                        entry_price = place_order(symbol, STRATEGY["position_units"], "BUY")
                        if entry_price:
                            bot_state["positions"][symbol] = {"symbol": symbol, "entry": entry_price,
                                "units": STRATEGY["position_units"], "trade_id": "pending",
                                "open_time": now.isoformat(), "current_price": entry_price, "unrealized_pnl": 0}
                            sync_positions()
                            add_diary(symbol, f"BUY MSS | Entry {entry_price:.5f} | Trend {trend} | SL level {sl_level:.5f}", "buy")
                else:
                    candles = get_candles(symbol, "M5", 3)
                    price = candles[-1]["close"] if candles else 0
                    bot_state["signals"][symbol] = {"trend_1h": trend, "mss_type": "Waiting for trend" if trend == "NEUTRAL" else "Watching", "price": price}
 
        except Exception as e:
            log.error(f"Loop error: {e}")
 
        time.sleep(60)
 
threading.Thread(target=trading_loop, daemon=True).start()
 
@app.after_request
def no_cache(r):
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return r
 
@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat(),
                    "version": bot_state["version"], "market_open": bot_state["market_open"]})
 
@app.route("/status")
def status():
    get_account_info()
    wins = bot_state["win_count"]
    total = bot_state["total_trades"]
    return jsonify({
        "running": bot_state["running"], "killed": bot_state["killed"],
        "paper_mode": PAPER_MODE, "market_open": bot_state["market_open"],
        "positions": bot_state["positions"], "closed_trades": bot_state["closed_trades"][-50:],
        "diary": bot_state["diary"][-100:], "day_pnl": bot_state["day_pnl"],
        "total_trades": total, "win_rate": round(wins/total*100) if total > 0 else 0,
        "signals": bot_state["signals"], "strategy": STRATEGY,
        "account_balance": bot_state["account_balance"],
        "account_equity": bot_state["account_equity"],
        "account_nav": bot_state["account_nav"],
        "active_cooldowns": bot_state["active_cooldowns"],
        "trend_1h": bot_state["trend_1h"],
        "version": bot_state["version"]
    })
 
@app.route("/diary")
def diary():
    return jsonify({"diary": bot_state["diary"]})
 
@app.route("/kill", methods=["POST"])
def kill():
    bot_state["killed"] = not bot_state["killed"]
    status = "KILLED" if bot_state["killed"] else "RESUMED"
    add_diary("SYSTEM", f"Kill switch {status}", "system")
    return jsonify({"killed": bot_state["killed"]})
 
@app.route("/bars")
def bars():
    symbol = request.args.get("symbol", "EUR_USD")
    tf = request.args.get("timeframe", "M5")
    candles = get_candles(symbol, tf, 150)
    result = [{"time": int(datetime.fromisoformat(c["time"].replace("Z","")).timestamp()),
               "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"]} for c in candles]
    return jsonify(result)
 
@app.route("/")
def index():
    try:
        with open("index.html") as f:
            return f.read()
    except:
        return jsonify({"status": "ForexAI MSS Bot v1.2 running"})
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
 
