"""
Solana Wallet Flow Tracker — Telegram Bot
"""

import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ─────────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = "8769072406:AAGhfrKTBD9Uh1PDj2agKOSTVbjDaT_mGG8"
SOLANA_RPC_URL = os.environ.get(
    "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
)

MAX_SIGNATURES  = 30
TOP_N           = 5
LAMPORTS_TO_SOL = 1e-9
RPC_DELAY       = 0.5
RPC_RETRIES     = 3

# ─────────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.DEBUG,
)
logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════
#  HELPERS
# ═════════════════════════════════════════════════════════════════════

BASE58_CHARS = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")

def is_valid_solana_address(address: str) -> bool:
    if not address:
        return False
    if not (32 <= len(address) <= 44):
        return False
    return all(c in BASE58_CHARS for c in address)


def short(address: str) -> str:
    return f"{address[:6]}...{address[-4:]}" if len(address) >= 10 else address


# ═════════════════════════════════════════════════════════════════════
#  SOLANA RPC
# ═════════════════════════════════════════════════════════════════════

def _rpc_post(payload: dict) -> dict:
    headers    = {"Content-Type": "application/json"}
    last_error = "Unknown error"

    for attempt in range(1, RPC_RETRIES + 1):
        time.sleep(RPC_DELAY)
        try:
            resp = requests.post(
                SOLANA_RPC_URL, json=payload, headers=headers, timeout=30
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            last_error = f"Network error: {exc}"
            logger.warning("RPC attempt %d/%d failed: %s", attempt, RPC_RETRIES, exc)
            time.sleep(attempt * 2.0)
            continue

        try:
            data = resp.json()
        except ValueError:
            last_error = "Invalid JSON from RPC"
            time.sleep(attempt * 2.0)
            continue

        if "error" in data:
            err  = data["error"]
            code = err.get("code", 0) if isinstance(err, dict) else 0
            msg  = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            if code == 429 or "rate" in msg.lower():
                wait = attempt * 4.0
                logger.warning("Rate limited — waiting %.0fs", wait)
                time.sleep(wait)
                last_error = f"Rate limited: {msg}"
                continue
            raise RuntimeError(f"Solana RPC error {code}: {msg}")

        return data

    raise RuntimeError(
        f"Solana RPC failed after {RPC_RETRIES} attempts. Last: {last_error}"
    )


def fetch_signatures(wallet: str) -> list:
    logger.info("Fetching signatures for %s", wallet)
    payload = {
        "jsonrpc": "2.0",
        "id":      1,
        "method":  "getSignaturesForAddress",
        "params":  [wallet, {"limit": MAX_SIGNATURES, "commitment": "finalized"}],
    }
    data   = _rpc_post(payload)
    result = data.get("result", [])
    if not isinstance(result, list):
        return []
    sigs = [
        item["signature"]
        for item in result
        if isinstance(item, dict) and item.get("err") is None
    ]
    logger.info("Got %d signatures", len(sigs))
    return sigs


def fetch_transaction(signature: str) -> dict | None:
    payload = {
        "jsonrpc": "2.0",
        "id":      1,
        "method":  "getTransaction",
        "params":  [
            signature,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
        ],
    }
    data   = _rpc_post(payload)
    result = data.get("result")
    return result if isinstance(result, dict) else None


# ═════════════════════════════════════════════════════════════════════
#  ANALYSE TRANSFERS
# ═════════════════════════════════════════════════════════════════════

def analyse_transfers(wallet: str, signatures: list) -> dict:
    recipients     = defaultdict(lambda: {"transfers": 0, "sol": 0.0})
    total_sol_sent = 0.0
    txns_analysed  = 0
    errors         = 0

    for i, sig in enumerate(signatures):
        logger.info("Processing tx %d/%d", i + 1, len(signatures))
        try:
            tx = fetch_transaction(sig)
        except RuntimeError as exc:
            logger.warning("Skipping tx: %s", exc)
            errors += 1
            continue

        if tx is None:
            continue

        meta        = tx.get("meta") or {}
        pre_bals    = meta.get("preBalances", [])
        post_bals   = meta.get("postBalances", [])
        transaction = tx.get("transaction") or {}
        msg         = transaction.get("message") or {}
        raw_keys    = msg.get("accountKeys", [])

        accounts = []
        for k in raw_keys:
            if isinstance(k, dict):
                accounts.append(k.get("pubkey", ""))
            elif isinstance(k, str):
                accounts.append(k)

        if not accounts or len(pre_bals) != len(accounts):
            continue

        if wallet not in accounts:
            continue

        wallet_idx   = accounts.index(wallet)
        wallet_delta = post_bals[wallet_idx] - pre_bals[wallet_idx]

        if wallet_delta >= 0:
            continue

        txns_analysed += 1

        for j, (pre, post) in enumerate(zip(pre_bals, post_bals)):
            if j == wallet_idx:
                continue
            delta = post - pre
            if delta > 0 and j < len(accounts) and accounts[j]:
                recipient    = accounts[j]
                sol_received = delta * LAMPORTS_TO_SOL
                recipients[recipient]["transfers"] += 1
                recipients[recipient]["sol"]       += sol_received
                total_sol_sent                     += sol_received

    return {
        "recipients":          dict(recipients),
        "total_sol_sent":      total_sol_sent,
        "total_txns_analysed": txns_analysed,
        "errors":              errors,
    }


# ═════════════════════════════════════════════════════════════════════
#  MESSAGE FORMATTING + INLINE KEYBOARD
# ═════════════════════════════════════════════════════════════════════

def format_report(wallet: str, analysis: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    """
    Returns (report_text, inline_keyboard).
    The keyboard has one '📋 Copy' button per top recipient wallet.
    Tapping a button sends the full address as a new message.
    """
    recipients = analysis["recipients"]
    total_sol  = analysis["total_sol_sent"]
    total_txns = analysis["total_txns_analysed"]
    errors     = analysis["errors"]

    if not recipients:
        return (
            f"🔍 Wallet Analyzed:\n{wallet}\n\n"
            f"ℹ️ No outgoing SOL transfers detected.\n"
            f"Scanned: {MAX_SIGNATURES} txns | Errors: {errors}",
            None,
        )

    ranked = sorted(
        recipients.items(),
        key=lambda x: (x[1]["transfers"], x[1]["sol"]),
        reverse=True,
    )

    top_addr, top_data = ranked[0]

    lines = [
        "🔍 Wallet Analyzed:",
        f"  {wallet}",
        f"  Txns scanned: {MAX_SIGNATURES} | Outgoing: {total_txns}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "🥇 Top Recipient:",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"  {top_addr}",
        f"  🔁 Transfers:  {top_data['transfers']}",
        f"  💰 Total Sent: {top_data['sol']:.4f} SOL",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 Top {min(TOP_N, len(ranked))} Recipients:",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    # Build report lines + one copy button per recipient
    keyboard_rows = []
    for rank, (addr, data) in enumerate(ranked[:TOP_N], 1):
        s = "s" if data["transfers"] != 1 else ""
        lines.append(
            f"  {rank}. {short(addr)}"
            f" — {data['transfers']} transfer{s}"
            f" — {data['sol']:.4f} SOL"
        )
        # callback_data = "copy:<full_address>"
        keyboard_rows.append([
            InlineKeyboardButton(
                text=f"📋 Copy #{rank}  {short(addr)}",
                callback_data=f"copy:{addr}",
            )
        ])

    # Also add a button for the analyzed wallet itself
    keyboard_rows.append([
        InlineKeyboardButton(
            text=f"📋 Copy analyzed wallet",
            callback_data=f"copy:{wallet}",
        )
    ])

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"  💸 Total SOL sent: {total_sol:.4f} SOL",
        f"⏱  Done — {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "👇 Tap a button below to copy any address:",
    ]

    return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows)


# ═════════════════════════════════════════════════════════════════════
#  BOT HANDLERS
# ═════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Received /start from user %s", update.effective_user.id)
    text = (
        "👋 Welcome to the Solana Wallet Flow Tracker!\n\n"
        "I trace where SOL moves from any Solana wallet.\n\n"
        "📖 Commands:\n"
        "  /start  — Show this help\n"
        "  /trace <wallet>  — Trace SOL flows\n\n"
        "Example:\n"
        "  /trace 9xQeWvG816bUx9EPf2nJk9h9v1n6jYtJm6u6H9P9qF1\n\n"
        f"⚙️ Scans the last {MAX_SIGNATURES} transactions.\n"
        "⚠️ Analysis takes 20–60 seconds.\n\n"
        "💡 After results appear, tap 📋 Copy buttons to copy any wallet address."
    )
    await update.message.reply_text(text)


async def cmd_trace(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(
        "Received /trace from user %s, args: %s",
        update.effective_user.id,
        context.args,
    )

    args = context.args
    if not args:
        await update.message.reply_text(
            "❌ Please provide a Solana wallet address.\n\n"
            "Usage: /trace <wallet_address>"
        )
        return

    wallet = args[0].strip()
    logger.info("Tracing wallet: %s (len=%d)", wallet, len(wallet))

    if not is_valid_solana_address(wallet):
        await update.message.reply_text(
            f"❌ Invalid Solana address.\n"
            f"Got: {wallet}\n"
            f"Length: {len(wallet)}\n\n"
            "Must be 32–44 base58 characters."
        )
        return

    await update.message.reply_text(
        f"⏳ Tracing SOL flows for:\n{wallet}\n\n"
        f"Fetching last {MAX_SIGNATURES} transactions… please wait."
    )

    try:
        signatures = fetch_signatures(wallet)

        if not signatures:
            await update.message.reply_text(
                f"ℹ️ No recent transactions found for:\n{wallet}\n\n"
                "The wallet may be empty or inactive."
            )
            return

        analysis        = analyse_transfers(wallet, signatures)
        report, keyboard = format_report(wallet, analysis)

        await update.message.reply_text(
            report,
            reply_markup=keyboard,
        )

    except RuntimeError as exc:
        logger.error("RuntimeError: %s", exc)
        await update.message.reply_text(
            f"❌ RPC Error:\n{exc}\n\n"
            "💡 Set SOLANA_RPC_URL to your Helius endpoint in Railway Variables.\n"
            "Get a free key at helius.dev"
        )
    except Exception as exc:
        logger.exception("Unexpected error: %s", exc)
        await update.message.reply_text(f"❌ Unexpected error: {exc}")


async def handle_copy_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Called when user taps a 📋 Copy button.
    Sends the full wallet address as a plain text message so the user can copy it.
    """
    query = update.callback_query
    await query.answer()  # dismiss the loading spinner on the button

    data = query.data  # e.g. "copy:9xQeWvG816..."
    if not data.startswith("copy:"):
        return

    address = data[len("copy:"):]
    logger.info("Copy button tapped for address: %s", address)

    # Send the address as a plain message — easy to long-press and copy on mobile
    await query.message.reply_text(
        f"📋 Wallet address:\n\n`{address}`",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catches any plain text — confirms bot is alive."""
    logger.info("Got plain message: %s", update.message.text)
    await update.message.reply_text(
        "👋 I received your message!\n\n"
        "Use /trace <wallet> to trace a Solana wallet.\n"
        "Use /start for help."
    )


# ═════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════

def main() -> None:
    logger.info("=== Solana Flow Bot starting ===")
    logger.info("RPC endpoint: %s", SOLANA_RPC_URL)

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("trace", cmd_trace))
    app.add_handler(CallbackQueryHandler(handle_copy_button, pattern="^copy:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Handlers registered. Starting polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
