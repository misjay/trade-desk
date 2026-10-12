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

from datetime import datetime, timezone
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
    call_cfg = state.get_call_bot_config()
    tok = call_cfg.get("token") or cfg.call_bot_token or cfg.telegram_token
    chats = call_cfg.get("all_chats")
    if not chats:
        cid = call_cfg.get("chat_id") or cfg.call_bot_chat_id or cfg.telegram_chat_id
        chats = [cid] if cid else []

    if tok and chats:
        url = f"https://api.telegram.org/bot{tok}/sendMessage"
        for target_chat in chats:
            try:
                requests.post(url, json={
                    "chat_id": target_chat,
                    "text": msg,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                }, timeout=8)
            except Exception as exc:
                log.warning("Telegram call tracker alert failed for chat %s: %s", target_chat, exc)

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
    now_utc = datetime.now(timezone.utc)

    for cid, call in list(tracked.items()):
        # 1. Skip already closed or finished trades
        if call.get("is_closed") or call.get("status") in ("CLOSED_TP2", "CLOSED_SL", "CLOSED_BREAKEVEN", "EXPIRED"):
            continue

        ticker = call.get("ticker", "").upper()
        side = call.get("side", "BUY").upper()
        tf = call.get("tf", "15m")
        entry_low = float(call.get("entry_low") or 0.0)
        entry_high = float(call.get("entry_high") or 0.0)
        entry_mid = (entry_low + entry_high) / 2.0 if (entry_low and entry_high) else float(call.get("live_price") or 1.0)
        tp1 = float(call.get("tp1") or 0.0)
        tp2 = float(call.get("tp2") or 0.0)
        sl = float(call.get("sl") or 0.0)

        # 2. Check Expiration & Lifetime TTL
        created_str = call.get("created_at")
        age_seconds = 0.0
        if created_str:
            try:
                created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                age_seconds = (now_utc - created_dt).total_seconds()
            except Exception:
                pass

        is_scalp = (tf in ("15m", "15"))
        is_day = (tf in ("4h", "240"))
        is_spot = call.get("trade_type") == "spot" or tf in ("spot", "1D")
        type_prefix = "💎 SPOT " if is_spot else ""

        # TTL limits:
        # Active (filled) trades stay alive across multiple days until targets or stop are hit!
        # - Scalp: 72 hours (3 days)
        # - Day: 14 days (336 hours)
        # - Spot: 30 days (720 hours)
        # Unfilled setups (waiting for entry retest):
        # - Scalp: 24 hours
        # - Day: 5 days
        # - Spot: 14 days
        entry_filled = bool(call.get("entry_filled", False))
        if is_scalp:
            if not entry_filled and age_seconds > 24 * 3600:
                log.info("Expiring unfilled scalp call %s (age %.1f hours)", cid, age_seconds / 3600)
                state.update_tracked_call(cid, {"status": "EXPIRED", "is_closed": True})
                continue
            elif entry_filled and age_seconds > 72 * 3600:
                log.info("Expiring active scalp call %s after 3 days", cid)
                state.update_tracked_call(cid, {"status": "EXPIRED", "is_closed": True})
                continue
        elif is_day:
            if not entry_filled and age_seconds > 5 * 86400:
                log.info("Expiring unfilled day call %s (age %.1f days)", cid, age_seconds / 86400)
                state.update_tracked_call(cid, {"status": "EXPIRED", "is_closed": True})
                continue
            elif entry_filled and age_seconds > 14 * 86400:
                log.info("Expiring active day call %s after 14 days", cid)
                state.update_tracked_call(cid, {"status": "EXPIRED", "is_closed": True})
                continue
        elif is_spot:
            if not entry_filled and age_seconds > 14 * 86400:
                log.info("Expiring unfilled spot call %s after 14 days", cid)
                state.update_tracked_call(cid, {"status": "EXPIRED", "is_closed": True})
                continue
            elif entry_filled and age_seconds > 30 * 86400:
                log.info("Expiring active spot call %s after 30 days", cid)
                state.update_tracked_call(cid, {"status": "EXPIRED", "is_closed": True})
                continue

        # Format age note if trade was dispatched earlier/days ago
        age_str = ""
        if age_seconds >= 86400:
            days = int(age_seconds // 86400)
            age_str = f" _(Called {days}d ago)_"
        elif age_seconds >= 3600 * 4:
            hours = int(age_seconds // 3600)
            age_str = f" _(Called {hours}h ago)_"

        # 3. Fetch Live Market Price from Bybit
        live_price = fetch_live_price(ticker)
        if not live_price or live_price <= 0:
            continue

        updates = {}

        # 4. Check Entry Fill Status (Only filled trades can hit targets/stops)
        if not entry_filled:
            if side == "BUY" and live_price <= (entry_high * 1.0005):
                entry_filled = True
                updates["entry_filled"] = True
                updates["status"] = "ACTIVE"
                log.info("Tracked call %s BUY entry filled at mark $%.4f (zone $%.4f–$%.4f)", cid, live_price, entry_low, entry_high)
            elif side == "SELL" and live_price >= (entry_low * 0.9995):
                entry_filled = True
                updates["entry_filled"] = True
                updates["status"] = "ACTIVE"
                log.info("Tracked call %s SELL entry filled at mark $%.4f (zone $%.4f–$%.4f)", cid, live_price, entry_low, entry_high)
            else:
                # Still waiting for price to retest entry band
                continue

        # ── 5. Calculate percentage gain / loss from mid entry ────────────────
        if side == "BUY":
            gain_pct = ((live_price - entry_mid) / entry_mid) * 100.0
            drop_pct = ((entry_mid - live_price) / entry_mid) * 100.0
        else: # SELL
            gain_pct = ((entry_mid - live_price) / entry_mid) * 100.0
            drop_pct = ((live_price - entry_mid) / entry_mid) * 100.0

        # ── 6. Check Stop-Loss / Breakeven Exit ──────────────────────────────
        tp1_hit = call.get("tp1_hit", False)
        if tp1_hit:
            # Trailing Stop to Entry Breakeven
            be_triggered = (live_price <= entry_mid) if side == "BUY" else (live_price >= entry_mid)
            if be_triggered:
                updates["sl_hit"] = True
                updates["is_closed"] = True
                updates["status"] = "CLOSED_BREAKEVEN"
                _send_alert(
                    f"🛡 *{type_prefix}BREAKEVEN STOP TRIGGERED* 🛡\n\n"
                    f"• *Asset*: `${ticker}` ({tf} {side}){age_str}\n"
                    f"• *Exit Price*: `{fmt_dollar(live_price)}`\n\n"
                    f"💡 *Outcome:* TP1 profit (+50%) was locked in earlier. Remaining 50% exited at breakeven without risk. Trade finalized!"
                )
                state.update_tracked_call(cid, updates)
                continue
        else:
            sl_triggered = (live_price <= sl) if side == "BUY" else (live_price >= sl)
            if sl_triggered:
                updates["sl_hit"] = True
                updates["is_closed"] = True
                updates["status"] = "CLOSED_SL"
                _send_alert(
                    f"🛑 *{type_prefix}STOP LOSS HIT / SETUP INVALIDATED* 🛑\n\n"
                    f"• *Asset*: `${ticker}` ({tf}){age_str}\n"
                    f"• *Side*: `{side}`\n"
                    f"• *Invalidation Level*: `{fmt_dollar(sl)}`\n"
                    f"• *Exit Price*: `{fmt_dollar(live_price)}` (-{drop_pct:.1f}%)\n\n"
                    f"⚠️ *Action:* Setup consumed. Position closed and removed from active tracking."
                )
                state.update_tracked_call(cid, updates)
                continue

        # ── 7. Check Final Take-Profit 2 (Full Profit Banked) ────────────────
        if not call.get("tp2_hit"):
            tp2_triggered = (live_price >= tp2) if side == "BUY" else (live_price <= tp2)
            if tp2_triggered:
                updates["tp2_hit"] = True
                updates["is_closed"] = True
                updates["status"] = "CLOSED_TP2"
                _send_alert(
                    f"🏆 *{type_prefix}FINAL TARGET SMASHED (TP2 HIT)* 🏆\n\n"
                    f"• *Asset*: `${ticker}` ({tf}){age_str}\n"
                    f"• *Side*: `{side}`\n"
                    f"• *TP2 Target*: `{fmt_dollar(tp2)}`\n"
                    f"• *Exit Price*: `{fmt_dollar(live_price)}` (+{gain_pct:.1f}%)\n\n"
                    f"🎉 *Trade Complete!* 100% of profit targets banked. Setup finalized."
                )
                state.update_tracked_call(cid, updates)
                continue

        # ── 8. Check Take-Profit 1 (Partial Bank & Trail Stop to Breakeven) ───
        if not tp1_hit:
            tp1_triggered = (live_price >= tp1) if side == "BUY" else (live_price <= tp1)
            if tp1_triggered:
                updates["tp1_hit"] = True
                _send_alert(
                    f"🎯 *{type_prefix}TARGET 1 REACHED (TP1 HIT)* 🎯\n\n"
                    f"• *Asset*: `${ticker}` ({tf}){age_str}\n"
                    f"• *Side*: `{side}`\n"
                    f"• *TP1 Target*: `{fmt_dollar(tp1)}`\n"
                    f"• *Current Price*: `{fmt_dollar(live_price)}` (+{gain_pct:.1f}%)\n\n"
                    f"💡 *Action:* Bank 50% profit now & Stop-Loss is automatically moved to Entry Breakeven (`{fmt_dollar(entry_mid)}`). Remaining 50% runs risk-free to TP2!"
                )

        # ── 9. Check Every +10% Increase Milestone ──────────────────────────
        highest_step = int(call.get("highest_gain_step", 0))
        current_step = int(gain_pct // 10) * 10
        if current_step >= 10 and current_step > highest_step:
            updates["highest_gain_step"] = current_step
            _send_alert(
                f"🚀 *{type_prefix}+{current_step}% PROFIT MILESTONE!* 🚀\n\n"
                f"• *Asset*: `${ticker}` ({tf} {side})\n"
                f"• *Gain from Entry*: `+{gain_pct:.1f}%`\n"
                f"• *Live Price*: `{fmt_dollar(live_price)}` (Entry: `{fmt_dollar(entry_mid)}`)\n\n"
                f"💰 Consider locking in partial profits or trailing stops!"
            )

        # ── 10. Check Every -20% Decrease Milestone ─────────────────────────
        lowest_drop_step = int(call.get("lowest_drop_step", 0))
        current_drop_step = int(drop_pct // 20) * 20
        if current_drop_step >= 20 and current_drop_step > lowest_drop_step:
            updates["lowest_drop_step"] = current_drop_step
            updates["drop_alert_triggered"] = True
            _send_alert(
                f"📉 *{type_prefix}-{current_drop_step}% DRAWDOWN WARNING!* 📉\n\n"
                f"• *Asset*: `${ticker}` ({tf} {side})\n"
                f"• *Drawdown from Entry*: `-{drop_pct:.1f}%`\n"
                f"• *Live Price*: `{fmt_dollar(live_price)}` (Entry: `{fmt_dollar(entry_mid)}`)\n\n"
                f"⚠️ High volatility drawdown step reached. Verify structure before adding risk."
            )

        if updates:
            state.update_tracked_call(cid, updates)

    # 11. Purge all closed / completed / expired calls from active state
    removed = state.clear_closed_tracked_calls()
    if removed > 0:
        log.info("Call tracker purged %d closed/expired calls from memory", removed)


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
