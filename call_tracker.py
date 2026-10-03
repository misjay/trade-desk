"""
call_tracker.py — Live Real-Time Trade Milestone Tracker for Manual & Winz Calls.
Monitors all 10 hourly calls (and active signals) against live Bybit prices:
  1. 🎯 Take-Profit 1 Hit (TP1)
  2. 🎯 Take-Profit 2 Hit (TP2)
  3. 🛑 Stop-Loss Hit (SL)
  4. 🚀 Every +10% Gain Milestones (+10%, +20%, +30%...)
  5. 📉 -20% Drop Alert
Dispatches notifications simultaneously to Telegram (Winz / main) and Discord.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional

import requests

from config import cfg, bybit_linear_symbol, bybit_spot_symbol
from scanner import fmt_dollar
import state

log = logging.getLogger(__name__)

_tracker_running = False


def _send_alert(msg: str) -> None:
    """Send alert to Telegram calls destination and Discord if configured."""
    # 1. Telegram dispatch
    call_cfg = state.get_call_bot_config()
    tok = call_cfg.get("token") or cfg.call_bot_token or cfg.telegram_token
    cid = call_cfg.get("chat_id") or cfg.call_bot_chat_id or cfg.telegram_chat_id
    if tok and cid:
        url = f"https://api.telegram.org/bot{tok}/sendMessage"
        try:
            requests.post(url, json={
                "chat_id": cid,
                "text": msg,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }, timeout=8)
        except Exception as exc:
            log.warning("Telegram call tracker alert failed: %s", exc)

    # 2. Discord dispatch (via winz_discord if running)
    try:
        import winz_discord
        winz_discord.broadcast_discord_message(msg)
    except Exception:
        pass


def check_tracked_calls_once() -> None:
    """Evaluate live mark prices against all tracked calls and emit alerts."""
    tracked = state.get_tracked_calls()
    if not tracked:
        return

    from scanner import fetch_live_price

    for cid, call in list(tracked.items()):
        ticker = call.get("ticker", "").upper()
        side = call.get("side", "BUY").upper()
        tf = call.get("tf", "15m")
        entry_low = float(call.get("entry_low") or 0.0)
        entry_high = float(call.get("entry_high") or 0.0)
        entry_mid = (entry_low + entry_high) / 2.0 if (entry_low and entry_high) else float(call.get("live_price") or 1.0)
        tp1 = float(call.get("tp1") or 0.0)
        tp2 = float(call.get("tp2") or 0.0)
        sl = float(call.get("sl") or 0.0)

        live_price = fetch_live_price(ticker)
        if not live_price or live_price <= 0:
            continue

        updates = {}

        # ── 1. Calculate percentage gain / loss from mid entry ────────────────
        if side == "BUY":
            gain_pct = ((live_price - entry_mid) / entry_mid) * 100.0
            drop_pct = ((entry_mid - live_price) / entry_mid) * 100.0
        else: # SELL
            gain_pct = ((entry_mid - live_price) / entry_mid) * 100.0
            drop_pct = ((live_price - entry_mid) / entry_mid) * 100.0

        # ── 2. Check TP1 Hit ────────────────────────────────────────────────
        if not call.get("tp1_hit"):
            tp1_triggered = (live_price >= tp1) if side == "BUY" else (live_price <= tp1)
            if tp1_triggered:
                updates["tp1_hit"] = True
                _send_alert(
                    f"🎯 *TARGET 1 REACHED (TP1 HIT)* 🎯\n\n"
                    f"• *Asset*: `${ticker}` ({tf})\n"
                    f"• *Side*: `{side}`\n"
                    f"• *TP1 Target*: `{fmt_dollar(tp1)}`\n"
                    f"• *Current Price*: `{fmt_dollar(live_price)}` (+{gain_pct:.1f}%)\n\n"
                    f"💡 *Action:* Bank 50% profit now & adjust Stop-Loss to Entry Breakeven (`{fmt_dollar(entry_mid)}`). Let remaining 50% run to TP2!"
                )

        # ── 3. Check TP2 Hit ────────────────────────────────────────────────
        if not call.get("tp2_hit"):
            tp2_triggered = (live_price >= tp2) if side == "BUY" else (live_price <= tp2)
            if tp2_triggered:
                updates["tp2_hit"] = True
                _send_alert(
                    f"🏆 *FINAL TARGET SMASHED (TP2 HIT)* 🏆\n\n"
                    f"• *Asset*: `${ticker}` ({tf})\n"
                    f"• *Side*: `{side}`\n"
                    f"• *TP2 Target*: `{fmt_dollar(tp2)}`\n"
                    f"• *Current Price*: `{fmt_dollar(live_price)}` (+{gain_pct:.1f}%)\n\n"
                    f"🎉 *Trade Complete!* 100% of targets hit. Full profit banked."
                )

        # ── 4. Check SL Hit ─────────────────────────────────────────────────
        if not call.get("sl_hit"):
            sl_triggered = (live_price <= sl) if side == "BUY" else (live_price >= sl)
            if sl_triggered:
                updates["sl_hit"] = True
                _send_alert(
                    f"🛑 *STOP LOSS HIT / SETUP INVALIDATED* 🛑\n\n"
                    f"• *Asset*: `${ticker}` ({tf})\n"
                    f"• *Side*: `{side}`\n"
                    f"• *Invalidation Level*: `{fmt_dollar(sl)}`\n"
                    f"• *Current Price*: `{fmt_dollar(live_price)}`\n\n"
                    f"⚠️ *Action:* Setup consumed. Stand aside and close remaining exposure."
                )

        # ── 5. Check Every +10% Increase Milestone ──────────────────────────
        highest_step = int(call.get("highest_gain_step", 0))
        current_step = int(gain_pct // 10) * 10
        if current_step >= 10 and current_step > highest_step:
            updates["highest_gain_step"] = current_step
            _send_alert(
                f"🚀 *+{current_step}% PROFIT MILESTONE!* 🚀\n\n"
                f"• *Asset*: `${ticker}` ({tf} {side})\n"
                f"• *Gain from Entry*: `+{gain_pct:.1f}%`\n"
                f"• *Live Price*: `{fmt_dollar(live_price)}` (Entry: `{fmt_dollar(entry_mid)}`)\n\n"
                f"💰 Consider locking in partial profits or trailing stops!"
            )

        # ── 6. Check -20% Drop Alert ─────────────────────────────────────────
        if not call.get("drop_alert_triggered"):
            if drop_pct >= 20.0:
                updates["drop_alert_triggered"] = True
                _send_alert(
                    f"📉 *SHARP PULLBACK WARNING (-20% DROP)* 📉\n\n"
                    f"• *Asset*: `${ticker}` ({tf})\n"
                    f"• *Drawdown from Call*: `-{drop_pct:.1f}%`\n"
                    f"• *Live Price*: `{fmt_dollar(live_price)}`\n\n"
                    f"⚠️ High volatility pullback detected. Verify structure before adding risk."
                )

        if updates:
            state.update_tracked_call(cid, updates)


def _tracker_loop() -> None:
    log.info("Call Tracker background monitor started (polling every 15s)")
    while _tracker_running:
        try:
            check_tracked_calls_once()
        except Exception as exc:
            log.debug("Call tracker loop error: %s", exc)
        time.sleep(15)


def start_call_tracker() -> None:
    global _tracker_running
    if not _tracker_running:
        _tracker_running = True
        t = threading.Thread(target=_tracker_loop, daemon=True, name="call-tracker")
        t.start()


def stop_call_tracker() -> None:
    global _tracker_running
    _tracker_running = False
