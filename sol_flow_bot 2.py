"""
╔══════════════════════════════════════════════════════════════════════╗
║          Solana Wallet Flow Tracker — Telegram Bot                   ║
╠══════════════════════════════════════════════════════════════════════╣
║                                                                      ║
║  HOW TO RUN LOCALLY                                                  ║
║  ──────────────────                                                  ║
║  1. pip install python-telegram-bot requests base58                  ║
║  2. python sol_flow_bot.py                                           ║
║                                                                      ║
║  HOW TO RUN ON RAILWAY                                               ║
║  ─────────────────────                                               ║
║  1. Push all files to a GitHub repo                                  ║
║  2. Create new Railway project → Deploy from GitHub                  ║
║  3. Railway auto-deploys and runs 24/7                               ║
║                                                                      ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

import base58
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ─────────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = "8769072406:AAGhfrKTBD9Uh1PDj2agKOSTVbjDaT_mGG8"
SOLANA_RPC_URL = os.environ.get(
    "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
)

MAX_SIGNATURES  = 50
TOP_N           = 5
LAMPORTS_TO_SOL = 1e-9
RPC_DELAY       = 0.15
RPC_RETRIES     = 3

# ─────────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════
#  HELPERS
# ═════════════════════════════════════════════════════════════════════

def is_valid_solana_address(address: str) -> bool:
    try:
        decoded = base58.b58decode(address)
        return len(decoded) == 32
    except Exception:
        return False


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
                SOLANA_RPC_URL, json=payload, headers=headers, timeout=20
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
            code = err.get("code", 0)
            msg  = err.get("message", str(err))
            if code == 429 or "rate" in msg.lower():
                wait = attempt * 3.0
                logger.warning("RPC rate limited — waiting %.0fs", wait)
                time.sleep(wait)
                last_error = f"Rate limited: {msg}"
                continue
            raise RuntimeError(f"Solana RPC error {code}: {msg}")

        return data

    raise RuntimeError(
        f"Solana RPC failed after {RPC_RETRIES} attempts. Last error: {last_error}\n"
        "Try again in a moment."
    )


def fetch_signatures(wallet: str) -> list:
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
    return [
        item["signature"]
        for item in result
        if isinstance(item, dict) and item.get("err") is None
    ]


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

    for sig in signatures:
        try:
            tx = fetch_transaction(sig)
        except RuntimeError as exc:
            logger.warning("Skipping %s: %s", sig, exc)
            continue

        if tx is None:
            continue

        meta      = tx.get("meta", {}) or {}
        pre_bals  = meta.get("preBalances", [])
        post_bals = meta.get("postBalances", [])

        transaction = tx.get("transaction", {}) or {}
        msg         = transaction.get("message", {}) or {}
        raw_keys    = msg.get("accountKeys", [])

        accounts = []
        for k in raw_keys:
            if isinstance(k, dict):
                accounts.append(k.get("pubkey", ""))
            elif isinstance(k, str):
                accounts.append(k)

        if not accounts or len(pre_bals) != len(accounts):
            continue

        try:
            wallet_idx = accounts.index(wallet)
        except ValueError:
            continue

        wallet_delta = post_bals[wallet_idx] - pre_bals[wallet_idx]
        if wallet_delta >= 0:
            continue

        txns_analysed += 1

        for i, (pre, post) in enumerate(zip(pre_bals, post_bals)):
            if i == wallet_idx:
                continue
            delta = post - pre
            if delta > 0 and i < len(accounts) and accounts[i]:
                recipient     = accounts[i]
                sol_received  = delta * LAMPORTS_TO_SOL
                recipients[recipient]["transfers"] += 1
                recipients[recipient]["sol"]       += sol_received
                total_sol_sent                     += sol_received

    return {
        "recipients":          dict(recipients),
        "total_sol_sent":      total_sol_sent,
        "total_txns_analysed": txns_analysed,
    }


# ═════════════════════════════════════════════════════════════════════
#  MESSAGE FORMATTING
# ═════════════════════════════════════════════════════════════════════

def format_report(wallet: str, analysis: dict) -> str:
    recipients = analysis["recipients"]
    total_sol  = analysis["total_sol_sent"]
    total_txns = analysis["total_txns_analysed"]

    if not recipients:
        return (
            f"🔍 Wallet Analyzed:\n{wallet}\n\n"
            f"ℹ️ No outgoing SOL transfers detected in the last {MAX_SIGNATURES} transactions."
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
        f"  Transactions scanned:    {MAX_SIGNATURES}",
        f"  Outgoing transfers found: {total_txns}",
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

    for rank, (addr, data) in enumerate(ranked[:TOP_N], 1):
        s = "s" if data["transfers"] != 1 else ""
        lines.append(
            f"  {rank}. {short(addr)}"
            f" — {data['transfers']} transfer{s}"
            f" — {data['sol']:.4f} SOL"
        )

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"  💸 Total SOL sent: {total_sol:.4f} SOL",
        f"⏱  Done — {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
    ]

    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════
#  BOT HANDLERS
# ═════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 Welcome to the Solana Wallet Flow Tracker!\n\n"
        "I trace where SOL moves from any Solana wallet.\n\n"
        "📖 Commands:\n"
        "  /start  — Show this help\n"
        "  /trace <wallet>  — Trace SOL flows\n\n"
        "Example:\n"
        "  /trace 9xQeWvG816bUx9EPf2nJk9h9v1n6jYtJm6u6H9P9qF1\n\n"
        f"⚙️ Scans the last {MAX_SIGNATURES} transactions.\n"
        "⚠️ Analysis takes 20–60 seconds."
    )
    await update.message.reply_text(text)


async def cmd_trace(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text(
            "❌ Please provide a Solana wallet address.\n\n"
            "Usage: /trace <wallet_address>"
        )
        return

    wallet = args[0].strip()

    if not is_valid_solana_address(wallet):
        await update.message.reply_text(
            "❌ Invalid Solana wallet address.\n\n"
            "A Solana address is a base58-encoded string, usually 32–44 characters.\n\n"
            "Example:\n"
            "/trace 9xQeWvG816bUx9EPf2nJk9h9v1n6jYtJm6u6H9P9qF1"
        )
        return

    await update.message.reply_text(
        f"⏳ Tracing SOL flows for:\n{wallet}\n\n"
        f"Fetching last {MAX_SIGNATURES} transactions… (up to 60s)"
    )

    try:
        logger.info("Fetching signatures for %s", wallet)
        signatures = fetch_signatures(wallet)

        if not signatures:
            await update.message.reply_text(
                f"ℹ️ No recent transactions found for:\n{wallet}\n\n"
                "The wallet may be empty, inactive, or the address may be wrong."
            )
            return

        logger.info("Got %d signatures for %s", len(signatures), wallet)
        analysis = analyse_transfers(wallet, signatures)
        report   = format_report(wallet, analysis)
        await update.message.reply_text(report)

    except RuntimeError as exc:
        logger.error("RuntimeError for %s: %s", wallet, exc)
        await update.message.reply_text(
            f"❌ RPC Error:\n{exc}\n\n"
            "💡 The public Solana RPC has rate limits.\n"
            "Wait 60 seconds and try again."
        )
    except Exception as exc:
        logger.exception("Unexpected error for %s", wallet)
        await update.message.reply_text(
            "❌ An unexpected error occurred. Please try again later."
        )


# ═════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════

def main() -> None:
    logger.info("Connecting to Solana RPC: %s", SOLANA_RPC_URL)
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("trace", cmd_trace))
    logger.info("🤖 Solana Flow Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
