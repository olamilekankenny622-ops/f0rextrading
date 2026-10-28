
import logging
import os
from datetime import datetime, timezone

import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
TIMEFRAME = os.getenv("TIMEFRAME", "1h").strip()
API_URL = "https://api.twelvedata.com/time_series"

PAIRS = {
    "EUR/USD": "EUR/USD",
    "GBP/USD": "GBP/USD",
    "USD/JPY": "USD/JPY",
    "USD/CHF": "USD/CHF",
    "AUD/USD": "AUD/USD",
    "USD/CAD": "USD/CAD",
    "NZD/USD": "NZD/USD",
    "EUR/GBP": "EUR/GBP",
}

INTERVALS = {"1h", "4h", "1day"}

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("forexbot")


def menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Signals", callback_data="signals"),
            InlineKeyboardButton("🔎 Analysis", callback_data="analysis"),
        ],
        [
            InlineKeyboardButton("💱 Currency Pairs", callback_data="pairs"),
            InlineKeyboardButton("🛡️ Risk Calculator", callback_data="risk"),
        ],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")],
    ])


def ema(values, period):
    if len(values) < period:
        raise ValueError("Not enough candle data for EMA.")
    result = sum(values[:period]) / period
    multiplier = 2 / (period + 1)
    for value in values[period:]:
        result = value * multiplier + result * (1 - multiplier)
    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        raise ValueError("Not enough candle data for RSI.")

    changes = [
        values[i] - values[i - 1]
        for i in range(len(values) - period, len(values))
    ]
    gains = sum(max(change, 0) for change in changes) / period
    losses = sum(max(-change, 0) for change in changes) / period

    if losses == 0:
        return 100.0 if gains > 0 else 50.0

    return 100 - (100 / (1 + gains / losses))


def atr(candles, period=14):
    if len(candles) < period + 1:
        raise ValueError("Not enough candle data for ATR.")

    ranges = []
    for i in range(1, len(candles)):
        current = candles[i]
        previous_close = candles[i - 1]["close"]
        ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous_close),
            abs(current["low"] - previous_close),
        ))

    return sum(ranges[-period:]) / period


def precision(pair, price):
    return 3 if "JPY" in pair else 5


async def fetch_candles(pair):
    if not TWELVEDATA_API_KEY:
        raise RuntimeError(
            "Market data is not configured. Add TWELVEDATA_API_KEY in Render."
        )

    if TIMEFRAME not in INTERVALS:
        raise RuntimeError("TIMEFRAME must be 1h, 4h, or 1day.")

    params = {
        "symbol": PAIRS[pair],
        "interval": TIMEFRAME,
        "outputsize": 120,
        "apikey": TWELVEDATA_API_KEY,
    }

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(API_URL, params=params)
        response.raise_for_status()
        data = response.json()

    if "values" not in data:
        message = data.get("message", "No candle data returned.")
        raise RuntimeError(f"Market data provider: {message}")

    # Provider normally returns newest candle first.
    # Skip the newest candle because it may still be forming.
    raw = data["values"]
    if len(raw) < 60:
        raise RuntimeError("Not enough candles returned for analysis.")

    candles = []
    for row in reversed(raw[1:]):
        candles.append({
            "time": row["datetime"],
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        })

    return candles


async def analyze_pair(pair):
    candles = await fetch_candles(pair)
    closes = [c["close"] for c in candles]

    ema20 = ema(closes[-100:], 20)
    ema50 = ema(closes[-100:], 50)
    rsi_value = rsi(closes)
    atr_value = atr(candles)

    entry = closes[-1]
    stop_distance = atr_value * 1.5

    signal = "NO TRADE"
    reason = "The strategy conditions do not currently align."

    if ema20 > ema50 and 50 < rsi_value < 70:
        signal = "BUY"
        reason = "EMA 20 is above EMA 50 and RSI supports bullish momentum."
    elif ema20 < ema50 and 30 < rsi_value < 50:
        signal = "SELL"
        reason = "EMA 20 is below EMA 50 and RSI supports bearish momentum."

    if signal == "BUY":
        stop = entry - stop_distance
        target = entry + (stop_distance * 2)
    elif signal == "SELL":
        stop = entry + stop_distance
        target = entry - (stop_distance * 2)
    else:
        stop = None
        target = None

    digits = precision(pair, entry)
    return {
        "pair": pair,
        "signal": signal,
        "reason": reason,
        "entry": entry,
        "stop": stop,
        "target": target,
        "ema20": ema20,
        "ema50": ema50,
        "rsi": rsi_value,
        "atr": atr_value,
        "time": candles[-1]["time"],
        "digits": digits,
    }


def format_analysis(result):
    digits = result["digits"]
    lines = [
        f"📊 Forex Analysis: {result['pair']}",
        f"Timeframe: {TIMEFRAME}",
        f"Last closed candle: {result['time']}",
        f"Signal: {result['signal']}",
        "",
        f"Reference price: {result['entry']:.{digits}f}",
        f"EMA 20: {result['ema20']:.{digits}f}",
        f"EMA 50: {result['ema50']:.{digits}f}",
        f"RSI 14: {result['rsi']:.2f}",
        f"ATR 14: {result['atr']:.{digits}f}",
        f"Reason: {result['reason']}",
    ]

    if result["signal"] != "NO TRADE":
        lines.extend([
            "",
            f"Illustrative Stop Loss: {result['stop']:.{digits}f}",
            f"Illustrative Take Profit (2R): {result['target']:.{digits}f}",
            "Illustrative risk/reward: 1:2 before costs and slippage.",
        ])

    lines.extend([
        "",
        "Source: Twelve Data candle API.",
        "Educational algorithmic analysis, not a guarantee.",
        "Confirm data freshness, spreads and broker prices before trading.",
    ])
    return "\n".join(lines)


async def send_analysis(update, context, pair):
    message = update.effective_message
    if message is None:
        return

    try:
        result = await analyze_pair(pair)
        await message.reply_text(format_analysis(result))
    except (httpx.HTTPError, ValueError, RuntimeError, KeyError) as exc:
        logger.warning("Analysis unavailable for %s: %s", pair, exc)
        await message.reply_text(
            "⚠️ I couldn't complete the analysis.\n"
            "Market data may be unavailable, rate-limited, or not configured.\n\n"
            "Details: " + str(exc)[:300]
        )
    except Exception:
        logger.exception("Unexpected analysis error")
        await message.reply_text("An unexpected error occurred. Please try again.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🤖 Welcome to Forex Trading Bot!\n\n"
        "Analyze major Forex pairs using market candles and technical indicators.\n"
        "Choose an option below.\n\n"
        "⚠️ Signals are algorithmic analysis, not guaranteed trade outcomes.",
        reply_markup=menu(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "/start - Main menu\n"
        "/signals [pair] - Generate a strategy result\n"
        "/analyze [pair] - Analyze a currency pair\n"
        "/pairs - Supported pairs\n"
        "/risk balance risk_percent stop_pips pip_value_per_lot\n"
        "/status - Check configuration\n"
        "/help - Show help\n\n"
        "Example: /analyze EUR/USD\n"
        "Example: /risk 1000 1 20 10\n\n"
        "Signals require a valid Twelve Data API key."
    )


async def pairs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = [
        [InlineKeyboardButton(pair, callback_data=f"pair:{pair}")]
        for pair in PAIRS
    ]
    await update.effective_message.reply_text(
        "Choose a currency pair:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pair = context.args[0].upper().replace("-", "/") if context.args else "EUR/USD"
    if pair not in PAIRS:
        await update.effective_message.reply_text(
            "Unsupported pair. Try /pairs or /analyze EUR/USD."
        )
        return
    await send_analysis(update, context, pair)


async def signals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await analyze_command(update, context)


async def risk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 4:
        await update.effective_message.reply_text(
            "Usage:\n/risk balance risk_percent stop_pips pip_value_per_lot\n\n"
            "Example: /risk 1000 1 20 10\n"
            "This example uses a pip value of 10 account-currency units per lot."
        )
        return

    try:
        balance, risk_pct, stop_pips, pip_value = map(float, context.args)
        if min(balance, risk_pct, stop_pips, pip_value) <= 0:
            raise ValueError
        if risk_pct > 100:
            raise ValueError

        risk_amount = balance * risk_pct / 100
        lots = risk_amount / (stop_pips * pip_value)

        await update.effective_message.reply_text(
            "🛡️ Position Size Estimate\n\n"
            f"Account balance: {balance:.2f}\n"
            f"Risk: {risk_pct:.2f}% ({risk_amount:.2f})\n"
            f"Stop distance: {stop_pips:g} pips\n"
            f"Assumed pip value per lot: {pip_value:.2f}\n"
            f"Estimated position size: {lots:.3f} lots\n\n"
            "Check contract size, pip value, account currency, broker rules, "
            "spread and commissions before using this estimate."
        )
    except ValueError:
        await update.effective_message.reply_text(
            "Enter four valid positive numbers. Risk percentage must not exceed 100."
        )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🤖 Bot status: Running\n"
        f"Telegram token: {'Configured' if BOT_TOKEN else 'Missing'}\n"
        f"Market-data key: {'Configured' if TWELVEDATA_API_KEY else 'Missing'}\n"
        f"Timeframe: {TIMEFRAME}\n"
        f"Checked at: {datetime.now(timezone.utc).isoformat()}\n\n"
        "A configured key does not guarantee that the data provider is available."
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "signals":
        await query.message.reply_text(
            "Choose a pair with /pairs, or try /signals EUR/USD."
        )
    elif query.data == "analysis":
        await query.message.reply_text(
            "Try /analyze EUR/USD or use /pairs to choose another pair."
        )
    elif query.data == "pairs":
        await pairs_command(update, context)
    elif query.data == "risk":
        await query.message.reply_text(
            "Use /risk balance risk_percent stop_pips pip_value_per_lot\n"
            "Example: /risk 1000 1 20 10"
        )
    elif query.data == "help":
        await help_command(update, context)
    elif query.data.startswith("pair:"):
        pair = query.data.split(":", 1)[1]
        if pair in PAIRS:
            await send_analysis(update, context, pair)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(
        "Unhandled Telegram update error",
        exc_info=context.error,
    )


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing. Configure it in Render Environment."
        )

    if TIMEFRAME not in INTERVALS:
        raise RuntimeError("TIMEFRAME must be 1h, 4h, or 1day.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("pairs", pairs_command))
    app.add_handler(CommandHandler("analyze", analyze_command))
    app.add_handler(CommandHandler("signals", signals_command))
    app.add_handler(CommandHandler("risk", risk_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(error_handler)

    logger.info("Forex bot starting with long polling.")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
