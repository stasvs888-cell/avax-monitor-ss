"""
AVAXUSDT Long-Only Monitor
--------------------------
Pulls AVAX_USDT / BTC_USDT perpetual data from MEXC's public contract API,
evaluates the long-only breakout / pullback strategy, and sends a Telegram
message only when something meaningful changes (not on every check).

This is a monitoring/alerting tool, not an auto-trader — it never places
orders. Nothing here is financial advice; thresholds are starting points
and should be reviewed and tuned by you.

Env vars (set on Railway -> Variables):
    TELEGRAM_BOT_TOKEN      required
    TELEGRAM_CHAT_ID        required
    CHECK_INTERVAL_SECONDS  optional, default 300 (5 min)
"""

import os
import json
import time
import traceback
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "300"))

AVAX_SYMBOL = "AVAX_USDT"
BTC_SYMBOL = "BTC_USDT"

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

MEXC_BASE = "https://contract.mexc.com/api/v1/contract"

# interval name -> MEXC interval string, seconds per candle
TIMEFRAMES = {
    "15m": ("Min15", 15 * 60),
    "1h": ("Min60", 60 * 60),
    "4h": ("Hour4", 4 * 60 * 60),
    "1d": ("Day1", 24 * 60 * 60),
}

PIVOT_LOOKBACK = 3          # candles each side to confirm a swing high/low
LEVEL_TOUCH_PCT = 0.003     # 0.3% proximity counts as "at the level"
VOLUME_CONFIRM_MULT = 1.2   # breakout volume must exceed avg vol * this
BTC_DROP_ALERT_PCT = -0.015  # 1.5% drop over last 3 closed 1h candles = negative filter


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_klines(symbol: str, interval_key: str, limit: int = 200):
    """Return list of closed candles: [{time, open, high, low, close, vol}, ...] oldest->newest.
    Drops the last candle if it is still forming (not closed)."""
    interval, seconds = TIMEFRAMES[interval_key]
    url = f"{MEXC_BASE}/kline/{symbol}"
    resp = requests.get(url, params={"interval": interval}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"MEXC kline error for {symbol} {interval_key}: {data}")
    d = data["data"]
    candles = []
    for i in range(len(d["time"])):
        candles.append({
            "time": d["time"][i],
            "open": float(d["open"][i]),
            "high": float(d["high"][i]),
            "low": float(d["low"][i]),
            "close": float(d["close"][i]),
            "vol": float(d["vol"][i]),
        })
    candles = candles[-limit:]
    now = time.time()
    if candles and (now - candles[-1]["time"]) < seconds:
        candles = candles[:-1]  # last candle still forming, drop it
    return candles


def fetch_ticker(symbol: str):
    url = f"{MEXC_BASE}/ticker"
    resp = requests.get(url, params={"symbol": symbol}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"MEXC ticker error for {symbol}: {data}")
    return data["data"]


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------

def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def trend_direction(candles):
    """Rough ↑ / → / ↓ classification from SMA20 level + slope."""
    closes = [c["close"] for c in candles]
    if len(closes) < 25:
        return "→"
    sma_now = sma(closes, 20)
    sma_prev = sma(closes[:-5], 20)
    if sma_now is None or sma_prev is None:
        return "→"
    price = closes[-1]
    slope = sma_now - sma_prev
    if price > sma_now and slope > 0:
        return "↑"
    if price < sma_now and slope < 0:
        return "↓"
    return "→"


def find_pivots(candles, lookback=PIVOT_LOOKBACK):
    """Return (resistance_levels, support_levels) from confirmed swing highs/lows,
    most recent first."""
    highs, lows = [], []
    n = len(candles)
    for i in range(lookback, n - lookback):
        window = candles[i - lookback:i + lookback + 1]
        h = candles[i]["high"]
        l = candles[i]["low"]
        if h == max(c["high"] for c in window):
            highs.append((candles[i]["time"], h))
        if l == min(c["low"] for c in window):
            lows.append((candles[i]["time"], l))
    highs.sort(key=lambda x: -x[0])
    lows.sort(key=lambda x: -x[0])
    resistance = [h for _, h in highs[:5]]
    support = [l for _, l in lows[:5]]
    return resistance, support


def nearest_above(levels, price):
    above = [l for l in levels if l > price]
    return min(above) if above else None


def nearest_below(levels, price):
    below = [l for l in levels if l < price]
    return max(below) if below else None


def near_level(price, level, pct=LEVEL_TOUCH_PCT):
    if level is None:
        return False
    return abs(price - level) / level <= pct


def btc_filter(btc_1h, btc_4h):
    if not btc_1h or len(btc_1h) < 4:
        return "unavailable"
    closes = [c["close"] for c in btc_1h[-4:]]
    change = (closes[-1] - closes[0]) / closes[0]
    trend1h = trend_direction(btc_1h)
    trend4h = trend_direction(btc_4h) if btc_4h else "→"
    if change <= BTC_DROP_ALERT_PCT:
        return "negative"
    if trend1h == "↑" and trend4h == "↑":
        return "positive"
    return "neutral"


def evaluate(avax_candles, btc_1h, btc_4h, current_price):
    c15, c1h, c4h, c1d = (avax_candles["15m"], avax_candles["1h"],
                           avax_candles["4h"], avax_candles["1d"])

    trends = {
        "15m": trend_direction(c15) if c15 else "→",
        "1h": trend_direction(c1h) if c1h else "→",
        "4h": trend_direction(c4h) if c4h else "→",
        "1d": trend_direction(c1d) if c1d else "→",
    }

    resistance, support = find_pivots(c4h) if c4h else ([], [])
    res_level = nearest_above(resistance, current_price)
    sup_level = nearest_below(support, current_price)

    btc_state = btc_filter(btc_1h, btc_4h)

    signal = "NO LONG"
    reason = "Нет структуры для входа"
    entry = stop = tp1 = tp2 = rr = None
    invalidation = "-"

    if not c1h or len(c1h) < 25:
        return {
            "trends": trends, "resistance": resistance, "support": support,
            "signal": "WAIT", "reason": "ДАННЫХ НЕДОСТАТОЧНО (мало 1H свечей)",
            "entry": None, "stop": None, "tp1": None, "tp2": None, "rr": None,
            "btc_filter": btc_state, "invalidation": "-",
        }

    last_closed = c1h[-1]
    prev_avg_vol = sma([c["vol"] for c in c1h[-21:-1]], 20) or 0

    # --- Scenario A: breakout ---
    if res_level and last_closed["close"] > res_level and last_closed["vol"] > prev_avg_vol * VOLUME_CONFIRM_MULT:
        held = current_price > res_level * 0.995  # hasn't fallen back under level
        if held and btc_state != "negative":
            signal = "LONG SETUP"
            reason = f"Подтверждённый пробой {res_level:.3f} с объёмом, цена удерживается выше"
            entry = res_level
            stop_level = nearest_below(support, res_level) or res_level * 0.97
            stop = stop_level
            risk = entry - stop
            if risk > 0:
                tp1 = entry + risk * 2
                tp2 = entry + risk * 3
                rr = round((tp1 - entry) / risk, 2)
            invalidation = f"Возврат цены под {res_level:.3f} закрытием 1H"
        else:
            signal = "WAIT"
            reason = "Пробой есть, но нет удержания или BTC мешает — ждём ретест"
            invalidation = f"Слом выше {res_level:.3f} без удержания = ложный пробой"

    # --- Scenario B: pullback (only checked if no breakout setup found) ---
    elif sup_level and near_level(last_closed["low"], sup_level) and last_closed["close"] > last_closed["open"]:
        prev_close = c1h[-2]["close"] if len(c1h) > 1 else None
        reversal = prev_close is not None and last_closed["close"] > prev_close
        vol_rising = last_closed["vol"] > (c1h[-2]["vol"] if len(c1h) > 1 else 0)
        if reversal and vol_rising and btc_state != "negative":
            signal = "LONG SETUP"
            reason = f"Отскок от поддержки {sup_level:.3f} с разворотной свечой и объёмом"
            entry = last_closed["close"]
            stop = sup_level * 0.99
            risk = entry - stop
            if res_level and risk > 0:
                tp1 = res_level
                tp2 = res_level + (res_level - entry)
                rr = round((tp1 - entry) / risk, 2)
            invalidation = f"Пробой поддержки {sup_level:.3f} закрытием 1H вниз"
        else:
            signal = "WAIT"
            reason = f"Цена у поддержки {sup_level:.3f}, разворот не подтверждён"
            invalidation = f"Пробой {sup_level:.3f} без разворота = отмена сценария"
    else:
        signal = "WAIT" if (res_level or sup_level) else "NO LONG"
        reason = "Цена между уровнями, нет сетапа"

    return {
        "trends": trends, "resistance": resistance, "support": support,
        "signal": signal, "reason": reason,
        "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2, "rr": rr,
        "btc_filter": btc_state, "invalidation": invalidation,
    }


# ---------------------------------------------------------------------------
# State (to avoid spamming — only alert on meaningful change)
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"signal": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[warn] Telegram not configured, message not sent:\n", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print("[error] Telegram send failed:", e)


def format_message(price_avax, result):
    icon = {"LONG SETUP": "🟢", "WAIT": "🟡", "NO LONG": "🔴"}.get(result["signal"], "🟡")
    lines = [
        f"AVAX: ${price_avax:.3f}",
        "",
        "TREND",
        f"15m: {result['trends']['15m']}  1H: {result['trends']['1h']}  "
        f"4H: {result['trends']['4h']}  1D: {result['trends']['1d']}",
        "",
        "RESISTANCE: " + (", ".join(f"{r:.3f}" for r in result["resistance"][:2]) or "-"),
        "SUPPORT: " + (", ".join(f"{s:.3f}" for s in result["support"][:2]) or "-"),
        "",
        f"SIGNAL: {icon} {result['signal']}",
        result["reason"],
    ]
    if result["signal"] == "LONG SETUP" and result["entry"]:
        lines += [
            "",
            f"ENTRY: {result['entry']:.3f}",
            f"STOP: {result['stop']:.3f}",
            f"TP1: {result['tp1']:.3f}" if result["tp1"] else "TP1: -",
            f"TP2: {result['tp2']:.3f}" if result["tp2"] else "TP2: -",
            f"R:R: {result['rr']}" if result["rr"] else "R:R: -",
        ]
    lines += [
        "",
        f"BTC FILTER: {result['btc_filter']}",
        f"INVALIDATION: {result['invalidation']}",
        "",
        f"ACTION: {result['signal']}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_once(state):
    avax_candles = {}
    for tf in TIMEFRAMES:
        try:
            avax_candles[tf] = fetch_klines(AVAX_SYMBOL, tf, limit=200)
        except Exception as e:
            print(f"[error] fetching AVAX {tf}:", e)
            avax_candles[tf] = []

    try:
        btc_1h = fetch_klines(BTC_SYMBOL, "1h", limit=50)
    except Exception as e:
        print("[error] fetching BTC 1h:", e)
        btc_1h = []
    try:
        btc_4h = fetch_klines(BTC_SYMBOL, "4h", limit=50)
    except Exception as e:
        print("[error] fetching BTC 4h:", e)
        btc_4h = []

    try:
        ticker = fetch_ticker(AVAX_SYMBOL)
        price = float(ticker["lastPrice"])
    except Exception as e:
        print("[error] fetching AVAX ticker:", e)
        return state

    result = evaluate(avax_candles, btc_1h, btc_4h, price)

    changed = result["signal"] != state.get("signal")
    if changed:
        msg = format_message(price, result)
        send_telegram(msg)
        print(datetime.now(timezone.utc).isoformat(), "sent:", result["signal"])
    else:
        print(datetime.now(timezone.utc).isoformat(), "no change:", result["signal"])

    state["signal"] = result["signal"]
    return state


def main():
    print("AVAX monitor starting. Interval:", CHECK_INTERVAL_SECONDS, "sec")
    state = load_state()
    while True:
        try:
            state = run_once(state)
            save_state(state)
        except Exception:
            print("[error] unhandled exception in loop:")
            traceback.print_exc()
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
