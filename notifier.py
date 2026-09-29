"""
notifier.py — Telegram notification formatter and sender.

Sends:
  - Full BUY/SELL desk cards (PERP + SPOT blocks) with chart image
  - WAIT cards (no image)
  - Fill / TP / SL update messages
  - Hourly summary
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests

from config import cfg, get_leverage, tv_url, NO_MARKET_TICKERS, CORE_TICKERS

log = logging.getLogger(__name__)

import html

_TG_BASE = f"https://api.telegram.org/bot{cfg.telegram_token}"


# ── Low-level HTTP helpers ────────────────────────────────────────────────────
def _post(endpoint: str, data: dict, files=None, retries: int = 3) -> bool:
    url = f"{_TG_BASE}/{endpoint}"
    for attempt in range(retries):
        try:
            if files:
                r = requests.post(url, data=data, files=files, timeout=30)
            else:
                r = requests.post(url, json=data, timeout=15)
            r.raise_for_status()
            return True
        except Exception as exc:
            err_body = ""
            if 'r' in locals() and hasattr(r, 'text'):
                err_body = f" — response: {r.text}"
            log.warning("Telegram attempt %d failed (%s): %s%s", attempt + 1, endpoint, exc, err_body)
    return False


def send_text(text: str, parse_mode: str = "HTML", disable_preview: bool = True) -> bool:
    return _post("sendMessage", {
        "chat_id": cfg.telegram_chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
    })


def send_photo(caption: str, photo_bytes: bytes, parse_mode: str = "HTML") -> bool:
    return _post(
        "sendPhoto",
        {"chat_id": cfg.telegram_chat_id, "caption": caption, "parse_mode": parse_mode},
        files={"photo": ("chart.png", photo_bytes, "image/png")},
    )


# ── Formatters ────────────────────────────────────────────────────────────────
def _mode_tag() -> str:
    return "🧪 DEMO" if cfg.trade_mode == "demo" else "🔴 LIVE"


def _price(val: Optional[float]) -> str:
    if val is None:
        return "—"
    if val >= 10000:
        return f"${val:,.0f}"
    if val >= 1000:
        return f"${val:,.1f}"
    if val >= 10:
        return f"${val:.2f}"
    if val >= 0.1:
        return f"${val:.4f}"
    return f"${val:.6f}"


def _market_clause(sig: dict) -> str:
    """Add 'Market allowed' footnote only if conditions are met."""
    tier_a = ["BTC", "ETH", "SOL", "XRP", "BNB"]
    if sig["ticker"] in tier_a and sig["ticker"] not in NO_MARKET_TICKERS:
        live = sig.get("live_price", 0)
        low = sig.get("entry_low", 0) or 0
        high = sig.get("entry_high", 0) or 0
        if low and high and low <= live <= high:
            return " Market allowed only inside this range."
    return ""


def format_buy_sell_card(sig: dict, chart_url: Optional[str] = None) -> str:
    """
    Format the full PERP + SPOT card for a BUY or SELL signal.
    Returns HTML-formatted Telegram message (no image — image sent separately).
    """
    t = sig["ticker"]
    side = sig["side"]
    trade_type = sig["trade_type"].capitalize()
    tf = sig["tf"]
    el = _price(sig["entry_low"])
    eh = _price(sig["entry_high"])
    tp1 = _price(sig["tp1"])
    tp2 = _price(sig["tp2"])
    sl = _price(sig["sl"])
    entry_range = f"{el}–{eh}"
    tv = sig.get("tv_url") or tv_url(t)
    structure = html.escape(sig.get("structure", ""))
    reason = html.escape(sig.get("reason", ""))
    lev_scalp = get_leverage(t, scalp=True)
    lev_day = get_leverage(t, scalp=False)
    lev_str = f"{lev_scalp}x" if trade_type.lower() == "scalp" else f"{lev_day}x"
    action = side.capitalize()
    order_word = "buy" if side == "BUY" else "sell"
    market_note = _market_clause(sig)

    card = (
        f"<b>{_mode_tag()} | {t} — {action} ({trade_type})</b>\n"
        f"{'─' * 36}\n\n"

        f"<b>📊 PERP</b>\n"
        f"Market: Perp\n"
        f"Chart: <a href='{tv}'>{tv}</a> ({tf})\n"
        f"Structure: {structure}\n"
        f"Entry (limit): {entry_range}\n"
        f"TP1: {tp1}\n"
        f"TP2: {tp2}\n"
        f"SL: {sl}\n"
        f"Leverage: {lev_str} (isolated)\n"
        f"Order: Limit {order_word} {entry_range}.{market_note}\n"
        f"Reason: {reason}\n\n"

        f"<b>💰 SPOT</b>\n"
        f"Market: Spot\n"
        f"Chart: <a href='{tv}'>{tv}</a> ({tf})\n"
        f"Structure: {structure}\n"
        f"Entry (limit): {entry_range}\n"
        f"TP1: {tp1}\n"
        f"TP2: {tp2}\n"
        f"SL: {sl}\n"
        f"Leverage: 1x / none\n"
        f"Order: Limit {order_word} {entry_range}\n"
        f"Reason: {reason}\n"
        f"{'─' * 36}\n"
        f"<i>Not financial advice. Verify price, funding, book before acting.</i>"
    )
    return card


def format_wait_card(sig: dict) -> str:
    t = sig["ticker"]
    tv = sig.get("tv_url") or tv_url(t)
    tf = sig["tf"]
    structure = html.escape(sig.get("structure", ""))
    reason = html.escape(sig.get("reason", ""))
    return (
        f"<b>{t} — WAIT</b>\n"
        f"Chart: <a href='{tv}'>{tv}</a> ({tf})\n"
        f"Structure: {structure}\n"
        f"Reason: {reason}"
    )


def format_desk_summary(signals: list, equity: float, stats: dict) -> str:
    """Format the hourly desk summary (tape + 24-core table)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    buys = [s for s in signals if s["side"] == "BUY"]
    sells = [s for s in signals if s["side"] == "SELL"]
    waits = [s for s in signals if s["side"] == "WAIT"]

    tape_lines = []
    if buys:
        tape_lines.append(f"• BUY signals: {', '.join(s['ticker'] for s in buys)}")
    if sells:
        tape_lines.append(f"• SELL signals: {', '.join(s['ticker'] for s in sells)}")
    tape_lines.append(f"• WAIT: {len(waits)} tickers mid-range or no clean level")
    tape_lines.append(f"• Equity ({cfg.trade_mode.upper()}): ${equity:,.2f}")
    tape_lines.append(
        f"• Session: {stats['wins']}W / {stats['losses']}L / {stats['breakevens']}BE | "
        f"PnL ${stats['total_pnl_usdt']:+.2f}"
    )

    rows = "\n".join(
        f"  {'→' if s['side'] != 'WAIT' else '·'} {s['ticker']:<6} {s['side']:<4}  "
        f"{'Live: ' + _price(s['live_price']) if s['live_price'] else ''}"
        for s in signals
        if s["ticker"] in CORE_TICKERS
    )

    return (
        f"<b>🖥 Desk — {now}  [{_mode_tag()}]</b>\n\n"
        + "\n".join(tape_lines) + "\n\n"
        + f"<b>Core 24</b>\n<pre>{rows}</pre>\n\n"
        + f"<i>Not financial advice. Isolated. SL = 0.5–1% equity each.</i>"
    )


def format_fill_message(pos: dict, fill_type: str) -> str:
    """
    fill_type: 'ENTRY_FILLED' | 'TP1_HIT' | 'TP2_HIT' | 'SL_HIT'
    """
    t = pos["ticker"]
    side = pos["side"]
    ep = _price(pos.get("entry_price"))
    exit_p = _price(pos.get("exit_price")) if "exit_price" in pos else "—"
    pnl = pos.get("pnl_pct", 0)
    pnl_usdt = pos.get("pnl_usdt", 0)
    mkt = pos.get("market", "perp").upper()

    icons = {
        "ENTRY_FILLED": "✅",
        "TP1_HIT": "💚",
        "TP2_HIT": "💰",
        "SL_HIT": "🛑",
    }
    icon = icons.get(fill_type, "📌")

    base = f"{icon} <b>{t} {side} {mkt} — {fill_type.replace('_', ' ')}</b>\n"
    base += f"Entry: {ep} | Exit: {exit_p}\n"
    if "pnl_pct" in pos:
        color = "+" if pnl >= 0 else ""
        base += f"PnL: {color}{pnl:.2f}% | ${color}{pnl_usdt:.2f} USDT\n"
    base += f"<i>[{_mode_tag()}]</i>"
    return base


# ── Main dispatch ─────────────────────────────────────────────────────────────
def notify_signal(sig: dict, chart_bytes: Optional[bytes] = None) -> None:
    """Send signal card to Telegram. Attaches chart image if available."""
    if sig["side"] == "WAIT":
        send_text(format_wait_card(sig))
        return

    card_text = format_buy_sell_card(sig)
    if chart_bytes:
        # Truncate caption to Telegram's 1024-char limit
        caption = card_text[:1020] + "…" if len(card_text) > 1024 else card_text
        send_photo(caption, chart_bytes)
        # Also send full card as text if truncated
        if len(card_text) > 1024:
            send_text(card_text)
    else:
        send_text(card_text)


def notify_fill(pos: dict, fill_type: str) -> None:
    send_text(format_fill_message(pos, fill_type))


def notify_summary(signals: list, equity: float, stats: dict) -> None:
    text = format_desk_summary(signals, equity, stats)
    send_text(text)


def send_startup_message(mode: str) -> None:
    mode_label = "DEMO (paper)" if mode == "demo" else "🔴 LIVE (real orders)"
    send_text(
        f"<b>🚀 Trade Desk Bot Started</b>\n"
        f"Mode: <b>{mode_label}</b>\n"
        f"Scalp scan: every 5 min (15m tf)\n"
        f"Day scan: every 30 min (4h tf)\n"
        f"Universe: 24 core + 12 extras\n\n"
        f"<i>Not financial advice.</i>"
    )
