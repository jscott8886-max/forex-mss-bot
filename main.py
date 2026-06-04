"""
ForexAI Bot 2 - Fefa Market Structure Shift Strategy
Pairs: EUR/USD, GBP/USD, USD/JPY via OANDA API
"""
import os, time, logging, json, math
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request
from flask_cors import CORS
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.accounts as accounts
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.trades as trades
import oandapyV20.endpoints.instruments as instruments

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

API_KEY     = os.getenv("OANDA_API_KEY", "")
ACCOUNT_ID  = os.getenv("OANDA_ACCOUNT_ID", "")
PAPER_MODE  = os.getenv("PAPER_MODE", "true").lower() == "true"
ENVIRONMENT = "practice" if PAPER_MODE else "live"
PAIRS       = ["EUR_USD", "GBP_USD", "USD_JPY"]
STATE_FILE  = "/tmp/forex_mss_state.json"
PIP_MULT    = {"EUR_USD": 10000, "GBP_USD": 10000, "USD_JPY": 100}

STRATEGY = {
    "stop_loss_pips":    15,
    "take_profit_pips":  30,
    "position_units":    10000,
    "swing_lookback":    3,
    "key_level_tolerance": 0.0005,  # price tolerance for key level proximity
    "cooldown_minutes":  15,
    "min_sl_pips":       5,
    "max_sl_pips":       25,
}

bot_state = {
    "running":         True,
    "killed":          False,
    "positions":       {},
    "closed_trades":   [],
    "diary":           [],
    "day_pnl":         0.0,
    "total_trades":    0,
    "win_count":       0,
    "account_balance": 0.0,
    "account_equity":  0.0,
    "signals":         {},
    "market_open":     False,
    "trend_1h_cache":  {},
    "trend_1h_time":   {},
    "key_levels_cache":{},
    "key_levels_time": {},
    "cooldowns":       {},
}

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "diary":         bot_state["diary"][-200:],
                "closed_trades": bot_state["closed_trades"][-100:],
                "day_pnl":       bot_state["day_pnl"],
                "total_trades":  bot_state["total_trades"],
                "win_count":     bot_state["win_count"],
            }, f)
    except Exception as e:
        log.error(f"Save state error: {e}")

def diary_entry(symbol, text, entry_type="trade"):
    bot_state["diary"].append({
        "time":   datetime.now().strftime("%H:%M"),
        "symbol": symbol,
        "text":   text,
        "type":   entry_type,
    })
    save_state()

def get_oanda_client():
    return oandapyV20.API(access_token=API_KEY, environment=ENVIRONMENT)

def is_market_open():
    now = datetime.now(timezone.utc)
    day = now.weekday()
    hour = now.hour
    if day == 5: return False
    if day == 6 and hour < 21: return False
    if day == 4 and hour >= 21: return False
    return True

def get_account_data():
    try:
        client = get_oanda_client()
        r = accounts.AccountSummary(ACCOUNT_ID)
        client.request(r)
        acc = r.response["account"]
        bot_state["account_balance"] = float(acc.get("balance", 0))
        bot_state["account_equity"]  = float(acc.get("NAV", 0))
    except Exception as e:
        log.error(f"Account fetch error: {e}")

def sync_positions():
    try:
        client = get_oanda_client()
        r = trades.OpenTrades(ACCOUNT_ID)
        client.request(r)
        open_trades = r.response.get("trades", [])
        live_ids = set()
        for t in open_trades:
            inst = t["instrument"]
            live_ids.add(inst)
            if inst not in bot_state["positions"]:
                bot_state["positions"][inst] = {
                    "trade_id":       t["id"],
                    "entry":          float(t["price"]),
                    "units":          float(t["currentUnits"]),
                    "open_time":      t.get("openTime", "")[:16].replace("T", " "),
                    "symbol":         inst,
                    "unrealized_pnl": float(t.get("unrealizedPL", 0)),
                }
            else:
                bot_state["positions"][inst]["unrealized_pnl"] = float(t.get("unrealizedPL", 0))
        for inst in list(bot_state["positions"].keys()):
            if inst not in live_ids:
                del bot_state["positions"][inst]
    except Exception as e:
        log.error(f"Position sync error: {e}")

def get_candles(pair, count=100, granularity="M5"):
    try:
        client = get_oanda_client()
        end   = datetime.now(timezone.utc)
        start = end - timedelta(hours=12)
        params = {
            "count": count,
            "granularity": granularity,
            "price": "M",
            "from": start.isoformat(),
            "to":   end.isoformat(),
        }
        r = instruments.InstrumentsCandles(pair, params=params)
        client.request(r)
        candles = r.response.get("candles", [])
        data = []
        for c in candles:
            if c.get("complete", False):
                mid = c["mid"]
                data.append({
                    "time":  c["time"],
                    "open":  float(mid["o"]),
                    "high":  float(mid["h"]),
                    "low":   float(mid["l"]),
                    "close": float(mid["c"]),
                })
        if not data:
            return None
        df = pd.DataFrame(data)
        df["time"] = pd.to_datetime(df["time"])
        df.set_index("time", inplace=True)
        return df
    except Exception as e:
        log.error(f"Candles error {pair}: {e}")
        return None

def detect_swing_points(close, lookback=3):
    prices = close.values
    n = len(prices)
    swing_highs, swing_lows = [], []
    for i in range(lookback, n - lookback):
        if all(prices[i] > prices[i-j] for j in range(1, lookback+1)) and \
           all(prices[i] > prices[i+j] for j in range(1, lookback+1)):
            swing_highs.append((i, float(prices[i])))
        if all(prices[i] < prices[i-j] for j in range(1, lookback+1)) and \
           all(prices[i] < prices[i+j] for j in range(1, lookback+1)):
            swing_lows.append((i, float(prices[i])))
    return swing_highs, swing_lows

def get_1h_trend(pair):
    try:
        df = get_candles(pair, count=50, granularity="H1")
        if df is None or len(df) < 10:
            return "NEUTRAL"
        swing_highs, swing_lows = detect_swing_points(df["close"], lookback=3)
        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return "NEUTRAL"
        recent_highs = [h[1] for h in swing_highs[-2:]]
        recent_lows  = [l[1] for l in swing_lows[-2:]]
        if recent_highs[-1] > recent_highs[-2] and recent_lows[-1] > recent_lows[-2]:
            return "BULL"
        elif recent_highs[-1] < recent_highs[-2] and recent_lows[-1] < recent_lows[-2]:
            return "BEAR"
        return "NEUTRAL"
    except Exception as e:
        log.error(f"1H trend error {pair}: {e}")
        return "NEUTRAL"

def get_key_levels(pair):
    try:
        df = get_candles(pair, count=48, granularity="H1")
        if df is None:
            return []
        swing_highs, swing_lows = detect_swing_points(df["close"], lookback=2)
        return [h[1] for h in swing_highs[-4:]] + [l[1] for l in swing_lows[-4:]]
    except:
        return []

def detect_mss(pair, trend_1h):
    try:
        df = get_candles(pair, count=60, granularity="M5")
        if df is None or len(df) < 20:
            return "HOLD", {}

        close  = df["close"]
        lows   = df["low"].values
        price  = float(close.iloc[-1])
        pip    = 1 / PIP_MULT[pair]

        swing_highs, swing_lows = detect_swing_points(close, lookback=STRATEGY["swing_lookback"])

        sig_data = {"price": round(price, 5), "trend_1h": trend_1h, "mss_type": "No MSS"}

        if trend_1h == "BULL" and len(swing_lows) >= 2 and len(swing_highs) >= 1:
            last_low1     = swing_lows[-1][1]
            last_low2     = swing_lows[-2][1]
            last_high_idx = swing_highs[-1][0]

            if last_low1 > last_low2 and last_high_idx > swing_lows[-1][0]:
                key_levels = bot_state["key_levels_cache"].get(pair, [])
                near_level = any(abs(price - lv) < STRATEGY["key_level_tolerance"]
                                for lv in key_levels) if key_levels else True

                if near_level:
                    sl_price = float(min(lows[-8:]))
                    sl_pips  = (price - sl_price) * PIP_MULT[pair]

                    if STRATEGY["min_sl_pips"] <= sl_pips <= STRATEGY["max_sl_pips"]:
                        tp_price = price + (sl_pips * 2 * pip)
                        sig_data.update({
                            "mss_type":  "Bullish MSS",
                            "sl_price":  round(sl_price, 5),
                            "tp_price":  round(tp_price, 5),
                            "sl_pips":   round(sl_pips, 1),
                            "near_level": near_level,
                        })
                        log.info(f"{pair} | BULL MSS | price={price:.5f} SL={sl_price:.5f} ({sl_pips:.1f}pips)")
                        return "BUY", sig_data

        sig_data["mss_type"] = "Waiting for MSS"
        return "HOLD", sig_data
    except Exception as e:
        log.error(f"MSS error {pair}: {e}")
        return "HOLD", {"price": 0, "trend_1h": trend_1h}

def is_in_cooldown(pair):
    cooldown_until = bot_state["cooldowns"].get(pair)
    if cooldown_until and datetime.now() < cooldown_until:
        return True
    return False

def set_cooldown(pair):
    bot_state["cooldowns"][pair] = datetime.now() + timedelta(minutes=STRATEGY["cooldown_minutes"])

def place_order(pair, units, sl_price, tp_price):
    try:
        client = get_oanda_client()
        data = {
            "order": {
                "type":        "MARKET",
                "instrument":  pair,
                "units":       str(int(units)),
                "timeInForce": "FOK",
                "stopLossOnFill":   {"price": f"{sl_price:.5f}"},
                "takeProfitOnFill": {"price": f"{tp_price:.5f}"},
            }
        }
        r = orders.Orders(ACCOUNT_ID, data=data)
        client.request(r)
        return r.response
    except Exception as e:
        log.error(f"Order error {pair}: {e}")
        return None

def close_trade(trade_id):
    try:
        client = get_oanda_client()
        r = trades.TradeClose(ACCOUNT_ID, tradeID=trade_id)
        client.request(r)
        return r.response
    except Exception as e:
        log.error(f"Close trade error: {e}")
        return None

def clean_nan(obj):
    if obj is None: return None
    if isinstance(obj, datetime): return obj.isoformat()
    if hasattr(obj, '__module__') and type(obj).__module__ == 'numpy':
        try: obj = obj.item()
        except: return 0
    if isinstance(obj, bool): return obj
    if isinstance(obj, float):
        return 0.0 if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, int): return obj
    if isinstance(obj, str): return obj
    if isinstance(obj, dict): return {str(k): clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)): return [clean_nan(v) for v in obj]
    try: return str(obj)
    except: return None

def trading_loop():
    if not API_KEY or not ACCOUNT_ID:
        log.warning("No OANDA credentials — bot idle")
        return

    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)

    get_account_data()
    sync_positions()

    log.info(f"ForexAI MSS Bot started | Paper={PAPER_MODE}")
    diary_entry("SYSTEM",
        f"ForexAI MSS Bot started | SL={STRATEGY['stop_loss_pips']}pips | "
        f"TP={STRATEGY['take_profit_pips']}pips | Cooldown={STRATEGY['cooldown_minutes']}min", "system")

    while True:
        try:
            if bot_state["killed"]:
                time.sleep(5)
                continue

            market_open = is_market_open()
            bot_state["market_open"] = market_open

            if not market_open:
                log.info("Forex market closed — waiting")
                time.sleep(300)
                continue

            get_account_data()
            sync_positions()
            now = datetime.now()

            for pair in PAIRS:
                # Update 1H trend every 15 minutes
                last_trend = bot_state["trend_1h_time"].get(pair)
                if last_trend is None or (now - last_trend).seconds >= 900:
                    trend = get_1h_trend(pair)
                    bot_state["trend_1h_cache"][pair] = trend
                    bot_state["trend_1h_time"][pair]  = now
                    log.info(f"{pair} 1H trend: {trend}")
                else:
                    trend = bot_state["trend_1h_cache"].get(pair, "NEUTRAL")

                # Update key levels every 30 minutes
                last_levels = bot_state["key_levels_time"].get(pair)
                if last_levels is None or (now - last_levels).seconds >= 1800:
                    levels = get_key_levels(pair)
                    bot_state["key_levels_cache"][pair] = levels
                    bot_state["key_levels_time"][pair]  = now

                if trend == "NEUTRAL":
                    bot_state["signals"][pair] = {"trend_1h": "NEUTRAL", "mss_type": "Waiting for trend", "price": 0}
                    continue

                signal, sig_data = detect_mss(pair, trend)
                bot_state["signals"][pair] = sig_data

                in_position = pair in bot_state["positions"]

                if in_position:
                    pos   = bot_state["positions"][pair]
                    price = sig_data.get("price", pos["entry"])
                    pnl   = pos.get("unrealized_pnl", 0)
                    pips  = (price - pos["entry"]) * PIP_MULT[pair]

                    if signal == "SELL" and pips > 3:
                        result = close_trade(pos["trade_id"])
                        if result:
                            win = pnl > 0
                            bot_state["closed_trades"].append({
                                "symbol": pair, "entry": pos["entry"], "exit": price,
                                "units": pos["units"], "pnl": round(pnl, 2),
                                "pips": round(pips, 1), "win": win,
                                "time": pos["open_time"],
                                "close_time": now.strftime("%H:%M"),
                                "signal": "MSS reversal"
                            })
                            bot_state["day_pnl"]       = round(bot_state["day_pnl"] + pnl, 2)
                            bot_state["total_trades"] += 1
                            if win: bot_state["win_count"] += 1
                            del bot_state["positions"][pair]
                            diary_entry(pair,
                                f"{'WIN' if win else 'LOSS'} | {pos['entry']:.5f} → {price:.5f} | "
                                f"P&L ${pnl:.2f} | {pips:+.1f} pips",
                                "win" if win else "loss")
                            if not win:
                                set_cooldown(pair)
                            save_state()

                elif signal == "BUY" and not bot_state["killed"]:
                    if is_in_cooldown(pair):
                        continue
                    price    = sig_data.get("price", 0)
                    sl_price = sig_data.get("sl_price", 0)
                    tp_price = sig_data.get("tp_price", 0)
                    if price <= 0 or sl_price <= 0:
                        continue
                    result = place_order(pair, STRATEGY["position_units"], sl_price, tp_price)
                    if result:
                        bot_state["positions"][pair] = {
                            "trade_id":       result.get("orderFillTransaction", {}).get("tradeOpened", {}).get("tradeID", ""),
                            "entry":          price,
                            "units":          STRATEGY["position_units"],
                            "open_time":      now.strftime("%H:%M"),
                            "symbol":         pair,
                            "unrealized_pnl": 0,
                            "sl": sl_price, "tp": tp_price,
                        }
                        diary_entry(pair,
                            f"BUY | {price:.5f} | {STRATEGY['position_units']:,} units | "
                            f"SL={sl_price:.5f} TP={tp_price:.5f} | "
                            f"1H: {trend} | {sig_data.get('mss_type','MSS')}",
                            "trade")
                        save_state()

            time.sleep(60)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(30)

app = Flask(__name__)
CORS(app)

@app.after_request
def add_no_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"]        = "no-cache"
    response.headers["Expires"]       = "0"
    return response

@app.route("/status")
def status():
    get_account_data()
    sync_positions()
    wins  = bot_state["win_count"]
    total = bot_state["total_trades"]
    payload = {
        "running":          bot_state["running"],
        "killed":           bot_state["killed"],
        "paper_mode":       PAPER_MODE,
        "market_open":      bot_state["market_open"],
        "positions":        bot_state["positions"],
        "closed_trades":    bot_state["closed_trades"][-50:],
        "diary":            bot_state["diary"][-100:],
        "day_pnl":          bot_state["day_pnl"],
        "total_trades":     total,
        "win_rate":         round(wins/total*100) if total > 0 else 0,
        "strategy":         STRATEGY,
        "signals":          bot_state["signals"],
        "trend_1h":         bot_state["trend_1h_cache"],
        "account_balance":  bot_state["account_balance"],
        "account_equity":   bot_state["account_equity"],
        "version":          "ForexMSS-1.0",
    }
    return jsonify(clean_nan(payload))

@app.route("/killswitch", methods=["POST"])
def killswitch():
    data = request.json or {}
    bot_state["killed"] = data.get("kill", True)
    diary_entry("SYSTEM", f"Kill switch {'KILLED' if bot_state['killed'] else 'RESUMED'}", "system")
    return jsonify({"killed": bot_state["killed"]})

@app.route("/diary")
def get_diary():
    return jsonify({"diary": bot_state["diary"]})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat(),
                    "version": "ForexMSS-1.0", "market_open": is_market_open()})

@app.route("/")
def index():
    try:
        return open("/app/index.html").read()
    except Exception:
        return open("index.html").read()

if __name__ == "__main__":
    import threading
    t = threading.Thread(target=trading_loop, daemon=True)
    t.start()
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
