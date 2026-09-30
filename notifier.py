"""
notifier.py — Formats and delivers Trade Desk cards and summaries.

Strict Formatting Mandates:
  - NO IMAGES: Zero chart image generation or photo attachments. Chart field is a TradingView URL only.
  - BUY/SELL CARD: PERP block + matching SPOT block.
  - WAIT: TICKER - WAIT / Chart / Structure / Reason.
  - OUTPUT: # Desk — [WAT date] [HH:00] / Tape / Core 24 / Extra / Rules / EXECUTOR CONTRACT.
  - Terse. No hype. No emojis. No images. No correlation talk.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import requests

from config import cfg, tv_url, CORE_TICKERS, EXTRA_TICKERS
from scanner import fmt_dollar

log = logging.getLogger(__name__)

_TG_BASE = f"https://api.telegram.org/bot{cfg.telegram_token}" if cfg.telegram_token else ""


# ── Telegram HTTP ───────────────────────────────────────────────────────────
def send_text(text: str, parse_mode: Optional[str] = None) -> bool:
    if not cfg.telegram_token or not cfg.telegram_chat_id:
        return False
    url = f"{_TG_BASE}/sendMessage"
    payload = {
        "chat_id": cfg.telegram_chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as exc:
        log.warning("Telegram send failed: %s", exc)
        return False


# ── Card Formatters ─────────────────────────────────────────────────────────
def format_card(sig: dict) -> str:
    """Format single ticker card according to exact Desk card specification."""
    t = sig["ticker"].upper()
    side = sig["side"].upper()
    tf = sig["tf"]
    tv = sig.get("tv_url") or tv_url(t, tf)

    if side == "WAIT":
        return (
            f"{t} - WAIT\n"
            f"Chart: {tv} ({tf})\n"
            f"Structure: {sig.get('structure', '')}\n"
            f"Reason: {sig.get('reason', '')}"
        )

    trade_type = sig.get("trade_type", "scalp").capitalize()
    el = fmt_dollar(sig.get("entry_low"))
    eh = fmt_dollar(sig.get("entry_high"))
    tp1 = fmt_dollar(sig.get("tp1"))
    tp2 = fmt_dollar(sig.get("tp2"))
    sl = fmt_dollar(sig.get("sl"))
    entry_band = f"{el}–{eh}"
    order_side = "buy" if side == "BUY" else "sell"
    lev = sig.get("leverage", 3)
    structure = sig.get("structure", "")
    reason = sig.get("reason", "")

    perp_block = (
        f"{t} - {side.capitalize()} ({trade_type} trade) — PERP\n"
        f"Market: Perp\n"
        f"Chart: {tv} ({tf})\n"
        f"Structure: {structure}\n"
        f"Entry (limit): {entry_band}\n"
        f"TP1: {tp1}\n"
        f"TP2: {tp2}\n"
        f"SL: {sl}\n"
        f"Suggested leverage: {lev}x (isolated)\n"
        f"Order: Limit {order_side} {entry_band}\n"
        f"Reason: {reason}"
    )

    spot_block = (
        f"{t} - {side.capitalize()} ({trade_type} trade) — SPOT\n"
        f"Market: Spot\n"
        f"Chart: {tv} ({tf})\n"
        f"Structure: {structure}\n"
        f"Entry (limit): {entry_band}\n"
        f"TP1: {tp1}\n"
        f"TP2: {tp2}\n"
        f"SL: {sl}\n"
        f"Suggested leverage: 1x / none\n"
        f"Order: Limit {order_side} {entry_band}\n"
        f"Reason: {reason}"
    )

    footer = "Not financial advice. Open the TradingView link. Verify price, funding, book."
    return f"{perp_block}\n\n{spot_block}\n\n{footer}"


def format_buy_sell_card(sig: dict) -> str:
    """Explicit function for Buy/Sell card."""
    return format_card(sig)


def format_wait_card(sig: dict) -> str:
    """Explicit function for WAIT card."""
    t = sig["ticker"].upper()
    tf = sig.get("tf", "15m")
    tv = sig.get("tv_url") or tv_url(t, tf)
    return (
        f"{t} - WAIT\n"
        f"Chart: {tv} ({tf})\n"
        f"Structure: {sig.get('structure', '')}\n"
        f"Reason: {sig.get('reason', '')}"
    )


def format_desk_summary(signals: List[dict], equity: float = 10000.0, stats: Optional[dict] = None) -> str:
    """Format desk summary text."""
    core_sigs = [s for s in signals if s["ticker"] in CORE_TICKERS]
    extra_sigs = [s for s in signals if s["ticker"] in EXTRA_TICKERS]
    buys = [s["ticker"] for s in signals if s["side"] == "BUY"]
    sells = [s["ticker"] for s in signals if s["side"] == "SELL"]
    tape = [
        f"Order desk: {len(buys)} BUY, {len(sells)} SELL, {len(signals) - len(buys) - len(sells)} WAIT",
        f"Current equity: ${equity:,.2f}",
        "Trade only the printed shelf. Stand aside in the middle.",
    ]
    return format_desk_report(core_sigs, extra_sigs, tape, include_executor_contract=True)


def format_desk_report(
    core_signals: List[dict],
    extra_signals: List[dict],
    tape_bullets: List[str],
    include_executor_contract: bool = True,
) -> str:
    """
    Format complete Desk publication:
      # Desk — [WAT date] [HH:00]
      Tape: 3-5 bullets.
      ## Core 24
      All 24 spelled out.
      ## Extra
      ## Rules
      EXECUTOR CONTRACT
    """
    # WAT is UTC+1
    wat_time = datetime.now(timezone.utc) + timedelta(hours=1)
    wat_header = wat_time.strftime("%Y-%m-%d %H:00 WAT")

    lines = [f"# Desk — {wat_header}", "", "Tape:"]
    for bullet in tape_bullets:
        lines.append(f"• {bullet}")

    lines.append("")
    lines.append("## Core 24")
    lines.append("")
    for sig in core_signals:
        lines.append(format_card(sig))
        lines.append("")

    lines.append("## Extra")
    lines.append("")
    if not extra_signals:
        lines.append("NONE")
        lines.append("")
    else:
        for sig in extra_signals:
            lines.append(format_card(sig))
            lines.append("")

    lines.append("## Rules")
    lines.append("Isolated. Each SL = 0.5%-1% equity.")
    lines.append("Footer: Not financial advice. Open the TradingView link. Verify price, funding, book.")
    lines.append("Terse. No hype. No emojis. No images. No correlation talk.")
    lines.append("")

    if include_executor_contract:
        lines.append("EXECUTOR CONTRACT:")
        for sig in core_signals:
            lines.append(sig.get("bot_line", f"BOT|{sig['ticker']}|WAIT||||||"))
        for sig in extra_signals:
            lines.append(sig.get("bot_line", f"BOT|{sig['ticker']}|WAIT||||||"))

    return "\n".join(lines)


def notify_signal(sig: dict) -> None:
    """Send card to Telegram if configured."""
    text = format_card(sig)
    send_text(text)


def notify_order_placed(pos: dict) -> None:
    t = pos.get("ticker", "UNKNOWN")
    side = pos.get("side", "")
    mkt = pos.get("market", "perp").upper()
    ep = fmt_dollar(pos.get("entry_price"))
    qty = pos.get("qty", 0.0)
    lev = pos.get("leverage", 1)
    msg = f"ORDER_PLACED: {t} {side} {mkt} Limit @ {ep} (Qty: {qty:.4f}, Lev: {lev}x) [Resting in book]"
    send_text(msg)


def notify_entry_filled(pos: dict) -> None:
    t = pos.get("ticker", "UNKNOWN")
    side = pos.get("side", "")
    mkt = pos.get("market", "perp").upper()
    ep = fmt_dollar(pos.get("entry_price"))
    qty = pos.get("qty", 0.0)
    msg = f"ENTRY_FILLED: {t} {side} {mkt} @ {ep} (Qty: {qty:.4f})"
    send_text(msg)


def notify_tp1_be(pos: dict, mark_price: float) -> None:
    t = pos.get("ticker", "UNKNOWN")
    side = pos.get("side", "")
    mkt = pos.get("market", "perp").upper()
    ep = fmt_dollar(pos.get("entry_price"))
    mark_str = fmt_dollar(mark_price)
    msg = f"TP1_HIT: {t} {side} {mkt} reached {mark_str} | Trailing SL adjusted to Break-Even ({ep})"
    send_text(msg)


def notify_trade_closed(closed: dict) -> None:
    t = closed.get("ticker", "UNKNOWN")
    side = closed.get("side", "")
    mkt = closed.get("market", "perp").upper()
    ep = fmt_dollar(closed.get("entry_price"))
    xp = fmt_dollar(closed.get("exit_price"))
    pnl_usdt = closed.get("pnl_usdt", 0.0)
    pnl_pct = closed.get("pnl_pct", 0.0)
    reason = closed.get("exit_reason", "CLOSED")
    pnl_sign = "+" if pnl_usdt >= 0 else ""
    msg = f"TRADE_CLOSED: {t} {side} {mkt} | Entry: {ep} | Exit: {xp} | PnL: {pnl_sign}${pnl_usdt:.2f} ({pnl_sign}{pnl_pct:.2f}%) | Reason: {reason}"
    send_text(msg)


def notify_fill(pos: dict, fill_type: str) -> None:
    t = pos.get("ticker", "UNKNOWN")
    side = pos.get("side", "")
    mkt = pos.get("market", "perp").upper()
    ep = fmt_dollar(pos.get("entry_price"))
    msg = f"{fill_type}: {t} {side} {mkt} @ {ep}"
    send_text(msg)

