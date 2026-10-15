"""
telegram_bot.py — Interactive two-way Telegram command handler for Xira.
Allows user to check status, view positions, scan, pause/resume, and close positions.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Union

import requests

from config import cfg, bybit_linear_symbol, bybit_spot_symbol, CORE_TICKERS
from notifier import send_text, fmt_dollar
import state
import user_store
import exchange_manager
import access_manager

log = logging.getLogger(__name__)

_bot_running = False
_last_update_id = 0

MASTER_DESK_ONLY_COMMANDS = {
    "/scan",
    "/cancelorder", "/cancelallorders", "/cancel", "/cancelall",
    "/onspot", "/offspot", "/spoton", "/spotoff",
    "/drop",
    "/probation", "/probations", "/quarantine",
    "/dailyreport", "/hourlyreport", "/weeklyreport", "/monthlyreport",
    "/feedback", "/dailyfeedback", "/learning",
    "/setcallbot", "/setfeedbackbot", "/callbot", "/feedbackbot",
    "/gencode", "/createcode", "/newcode",
    "/codes", "/listcodes",
    "/revokecode",
    "/grant", "/revokeuser",
    "/users", "/accessusers",
    "/broadcast", "/signal",
}
ADMIN_COMMANDS = MASTER_DESK_ONLY_COMMANDS


def is_admin(chat_id: Union[str, int]) -> bool:
    cid_str = str(chat_id).strip()
    admin_ids = set()
    if cfg.telegram_chat_id:
        admin_ids.add(str(cfg.telegram_chat_id).strip())
    extra_admins = os.getenv("ADMIN_CHAT_IDS", "")
    for aid in extra_admins.split(","):
        aid_clean = aid.strip()
        if aid_clean:
            admin_ids.add(aid_clean)
    admin_ids.add("6724880386")
    return cid_str in admin_ids


_current_bot_token: Optional[str] = None


def _reply(chat_id: str, text: str, bot_token: Optional[str] = None, reply_markup: Optional[dict] = None) -> None:
    token = bot_token or _current_bot_token or cfg.telegram_token
    if not token:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    close_btn = {"text": "❌ Close", "callback_data": "close_msg"}
    if reply_markup is None:
        payload["reply_markup"] = {"inline_keyboard": [[close_btn]]}
    elif isinstance(reply_markup, dict) and "inline_keyboard" in reply_markup:
        rows = [list(r) for r in reply_markup["inline_keyboard"]]
        has_close = any(any(b.get("callback_data") in ("close_msg", "dismiss_msg") for b in row) for row in rows)
        if not has_close:
            rows.append([close_btn])
        payload["reply_markup"] = {**reply_markup, "inline_keyboard": rows}
    else:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(url, json=payload, timeout=8)
    except Exception as exc:
        log.warning("Telegram reply failed: %s", exc)


def _delete_message(chat_id: str, message_id: Optional[int], bot_token: Optional[str] = None) -> None:
    """Delete a sensitive message (e.g. API keys) from chat immediately."""
    if not message_id:
        return
    token = bot_token or _current_bot_token or cfg.telegram_token
    if not token:
        return
    url = f"https://api.telegram.org/bot{token}/deleteMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "message_id": message_id}, timeout=4)
    except Exception as exc:
        log.debug("Telegram deleteMessage failed: %s", exc)


def _paused_footer(chat_id: Optional[str] = None) -> str:
    """Returns a footer reminder if automated execution is currently paused or in single-mode."""
    if chat_id and not is_admin(chat_id):
        if user_store.is_user_paused(chat_id):
            return "\n\n_(⏸ Personal execution is PAUSED. Will NOT resume automatically until you type `/resume`.)_"
        elif user_store.is_user_scalp_paused(chat_id):
            return "\n\n_(🟡 Scalp execution is PAUSED | Day Trades ONLY active. Type `/resume` to trade all.)_"
        elif user_store.is_user_day_paused(chat_id):
            return "\n\n_(🟡 Day trade execution is PAUSED | Scalps ONLY active. Type `/resume` to trade all.)_"
        return ""
    if state.is_paused():
        return "\n\n_(⏸ Execution remains PAUSED. Will NOT resume automatically until you type `/resume`.)_"
    elif state.is_scalp_paused():
        return "\n\n_(🟡 Master Desk: Scalp execution is PAUSED | Day Trades ONLY active. Type `/resume` to trade all.)_"
    elif state.is_day_paused():
        return "\n\n_(🟡 Master Desk: Day trade execution is PAUSED | Scalps ONLY active. Type `/resume` to trade all.)_"
    return ""


def format_active_calls_summary() -> Tuple[str, List[List[dict]]]:
    """
    Format live active (triggered) trades and pending limit setups with interactive buttons.
    """
    from scanner import fetch_live_price
    tracked = state.get_tracked_calls()
    if not tracked:
        return (
            "📋 *Winz Active Calls Desk*\n\n"
            "ℹ️ *No active calls in memory right now.*\n"
            "_All prior setups reached their profit targets or invalidation levels._\n\n"
            "👉 Type `/hourlycalls` to generate fresh high-conviction trade setups.",
            []
        )

    active_calls = []
    pending_calls = []

    for cid, c in tracked.items():
        if c.get("is_closed") or c.get("status") in ("CLOSED_TP2", "CLOSED_SL", "CLOSED_BREAKEVEN", "EXPIRED"):
            continue
        if c.get("entry_filled") or c.get("status") == "ACTIVE":
            active_calls.append((cid, c))
        else:
            pending_calls.append((cid, c))

    if not active_calls and not pending_calls:
        return (
            "📋 *Winz Active Calls Desk*\n\n"
            "ℹ️ *No active calls right now.*\n"
            "_All prior setups completed or closed. Type `/hourlycalls` to generate fresh setups._",
            []
        )

    lines = [
        "📋 *WINZ LIVE CALLS & TRADE TRACKER*",
        f"⏱ *Timestamp:* {datetime.now(timezone.utc).strftime('%H:%M UTC')} | _Monitored Live 24/7_",
        "",
    ]

    inline_keyboard = []
    current_btn_row = []

    if active_calls:
        lines.append(f"🟢 *ACTIVE TRADES ({len(active_calls)} Triggered & Running):*")
        for cid, c in active_calls:
            t = c.get("ticker", "").upper()
            side = c.get("side", "BUY").upper()
            tf = c.get("tf", "15m")
            is_spot = c.get("trade_type") == "spot" or tf in ("spot", "1D")
            badge = "💎 Spot" if is_spot else tf
            lp = fetch_live_price(t) or float(c.get("live_price") or 0.0)
            el = float(c.get("entry_low") or 0.0)
            eh = float(c.get("entry_high") or 0.0)
            emid = (el + eh) / 2.0 if (el and eh) else lp
            tp1 = float(c.get("tp1") or 0.0)
            tp2 = float(c.get("tp2") or 0.0)
            sl = float(c.get("sl") or 0.0)

            if emid > 0:
                pnl_pct = ((lp - emid) / emid * 100.0) if side == "BUY" else ((emid - lp) / emid * 100.0)
            else:
                pnl_pct = 0.0

            pnl_str = f"+{pnl_pct:.1f}% 🟢" if pnl_pct >= 0 else f"{pnl_pct:.1f}% 🔴"
            tp1_stat = "✅ TP1 Hit" if c.get("tp1_hit") else f"TP1: {fmt_dollar(tp1)}"

            lines.append(f"• *${t}* `{side}` ({badge}) — Triggered @ `{fmt_dollar(emid)}`")
            lines.append(f"  └ Live Mark: `{fmt_dollar(lp)}` | PnL: `{pnl_str}` | {tp1_stat} | SL: `{fmt_dollar(sl)}`")
            if c.get("reversal_reason"):
                lines.append(f"  ⚠️ *Reversal Alert:* _{c.get('reversal_reason')}_")

            cb_tf = "spot" if is_spot else tf
            current_btn_row.append({"text": f"📈 {t} ({badge})", "callback_data": f"call_view_{t}_{cb_tf}"})
            if len(current_btn_row) == 2:
                inline_keyboard.append(current_btn_row)
                current_btn_row = []
        lines.append("")

    if pending_calls:
        lines.append(f"⏳ *PENDING SETUPS ({len(pending_calls)} Waiting for Entry Zone):*")
        for cid, c in pending_calls:
            t = c.get("ticker", "").upper()
            side = c.get("side", "BUY").upper()
            tf = c.get("tf", "15m")
            is_spot = c.get("trade_type") == "spot" or tf in ("spot", "1D")
            badge = "💎 Spot" if is_spot else tf
            lp = fetch_live_price(t) or float(c.get("live_price") or 0.0)
            el = float(c.get("entry_low") or 0.0)
            eh = float(c.get("entry_high") or 0.0)
            emid = (el + eh) / 2.0 if (el and eh) else lp

            dist_pct = abs(lp - emid) / emid * 100.0 if emid > 0 else 0.0
            lines.append(f"• *${t}* `{side}` ({badge}) — Entry Band: `{fmt_dollar(el)}–{fmt_dollar(eh)}`")
            lines.append(f"  └ Live Mark: `{fmt_dollar(lp)}` ({dist_pct:.1f}% from entry) | Status: Resting Limit")

            cb_tf = "spot" if is_spot else tf
            current_btn_row.append({"text": f"⏳ {t} ({badge})", "callback_data": f"call_view_{t}_{cb_tf}"})
            if len(current_btn_row) == 2:
                inline_keyboard.append(current_btn_row)
                current_btn_row = []
        lines.append("")

    if current_btn_row:
        inline_keyboard.append(current_btn_row)

    lines.append("👇 *Tap any button below for live chart markup & thesis:*")
    return "\n".join(lines), inline_keyboard


def _build_notifications_panel(chat_id: str) -> Tuple[str, dict]:
    """Build the interactive notification preferences text and inline keyboard."""
    prefs = state.get_chat_notification_prefs(chat_id)
    trig = prefs.get("triggers", True)
    rev = prefs.get("reversals", True)
    mile = prefs.get("milestones", True)
    dig = prefs.get("digests", True)

    s_trig = "🟢 *Active*" if trig else "⏸ *Paused*"
    s_rev = "🟢 *Active*" if rev else "⏸ *Paused*"
    s_mile = "🟢 *Active*" if mile else "⏸ *Paused*"
    s_dig = "🟢 *Active*" if dig else "⏸ *Paused*"

    text = (
        "🔔 *Winz Notification & Alert Preferences*\n\n"
        "Customize which real-time market and trade alerts are sent to this chat:\n\n"
        f"• *🎯 Trade Triggers*: {s_trig}\n"
        f"  _(Entry fills, setup invalidations, instant trend alerts)_\n\n"
        f"• *⚠️ Reversals & Warnings*: {s_rev}\n"
        f"  _(15m structure rejection, drawdown warnings, BTC macro shifts)_\n\n"
        f"• *💰 TP & Trailing Stops*: {s_mile}\n"
        f"  _(TP0/TP1/TP2 hits, Breakeven snap, profit-lock ratchets)_\n\n"
        f"• *📋 2-Hour Desk Digests*: {s_dig}\n"
        f"  _(Scheduled 4 scalps + 3 day trades & spot lists)_\n\n"
        "👇 _Tap any button below to toggle categories or pause/resume all:_"
    )

    b_trig = f"🎯 Triggers: {'ON 🟢' if trig else 'OFF ⏸'}"
    b_rev = f"⚠️ Reversals: {'ON 🟢' if rev else 'OFF ⏸'}"
    b_mile = f"💰 TP/Stops: {'ON 🟢' if mile else 'OFF ⏸'}"
    b_dig = f"📋 Digests: {'ON 🟢' if dig else 'OFF ⏸'}"

    keyboard = {
        "inline_keyboard": [
            [
                {"text": b_trig, "callback_data": "toggle_notif_triggers"},
                {"text": b_rev, "callback_data": "toggle_notif_reversals"},
            ],
            [
                {"text": b_mile, "callback_data": "toggle_notif_milestones"},
                {"text": b_dig, "callback_data": "toggle_notif_digests"},
            ],
            [
                {"text": "⏸ Pause All", "callback_data": "toggle_notif_pause_all"},
                {"text": "▶️ Resume All", "callback_data": "toggle_notif_resume_all"},
            ],
            [
                {"text": "❌ Close Panel", "callback_data": "close_msg"}
            ]
        ]
    }
    return text, keyboard


def handle_command(
    cmd_text: str,
    chat_id: str,
    scan_trigger_fn=None,
    bot_token: Optional[str] = None,
    message_id: Optional[int] = None,
) -> None:
    global _current_bot_token
    if bot_token:
        _current_bot_token = bot_token

    raw_text = cmd_text.strip()
    if not raw_text:
        return

    # Natural Language Interceptors
    lower_raw = raw_text.lower()
    if (
        "scan the market" in lower_raw
        or "best setups" in lower_raw
        or "show me the best setups" in lower_raw
        or "setups to watch" in lower_raw
        or "watchlist" in lower_raw
    ):
        parts = ["/watchlist"]
    elif lower_raw in ("calls", "active calls", "show calls", "view calls", "tracked calls", "show active calls"):
        parts = ["/calls"]
    elif "convert to twitter post" in lower_raw or "convert to twitter" in lower_raw or "convert to tweet" in lower_raw or lower_raw == "tweet":
        parts = ["/tweet"]
    elif lower_raw.startswith("tweet ") or lower_raw.startswith("/tweet "):
        parts = ["/tweet"] + raw_text.split()[1:]
    elif "research" in lower_raw and "(" in lower_raw and ")" in lower_raw:
        # e.g. research (day and scalp) BTC or /research (scalp) ETH
        import re
        m = re.search(r"research\s*\(([^)]+)\)\s*(?:for\s+)?([A-Za-z0-9_-]+)?", raw_text, re.IGNORECASE)
        if m:
            mode_arg = m.group(1).strip()
            ticker_arg = (m.group(2) or "").strip()
            parts = ["/research", mode_arg]
            if ticker_arg:
                parts.append(ticker_arg)
        else:
            parts = raw_text.split()
    elif any(k in lower_raw for k in [
        "pause automated scalp and trade only day",
        "pause automated scalps and trade only day",
        "pause scalp and trade only day",
        "pause scalps and trade only day",
        "pause scalp trade only day",
        "trade only day trade",
        "trade only day trades",
        "trade only day",
        "trade day only",
        "day trade only",
        "day trades only",
        "day only",
    ]):
        parts = ["/onlyday"]
    elif any(k in lower_raw for k in [
        "pause day trade and trade only scalp",
        "pause day trades and trade only scalp",
        "pause automated day trade and trade only scalp",
        "pause automated day trades and trade only scalp",
        "pause day and trade only scalp",
        "pause day trade and trade only automated scalp",
        "trade only scalp",
        "trade only scalps",
        "trade only automated scalp",
        "trade scalp only",
        "scalp only",
        "scalps only",
    ]):
        parts = ["/onlyscalp"]
    elif lower_raw in (
        "pause automated scalp", "pause automated scalps",
        "pause scalp", "pause scalps", "stop scalp", "stop scalps"
    ):
        parts = ["/pausescalp"]
    elif lower_raw in (
        "pause automated day trade", "pause automated day trades",
        "pause day trade", "pause day trades", "pause day", "stop day", "stop day trade"
    ):
        parts = ["/pauseday"]
    elif lower_raw in ("resume scalp", "resume scalps", "resume automated scalp", "unpause scalp", "unpause scalps"):
        parts = ["/resumescalp"]
    elif lower_raw in ("resume day", "resume day trade", "resume day trades", "unpause day"):
        parts = ["/resumeday"]
    elif lower_raw in ("resume all", "resume both", "trade all", "trade both", "resume scalp and day", "trade scalp and day"):
        parts = ["/resume", "all"]
    elif lower_raw in ("autotrade on", "auto on", "enable autotrade", "start autotrade", "turn on autotrade", "start auto trade", "enable auto trade"):
        parts = ["/autotrade", "on"]
    elif lower_raw in ("autotrade off", "auto off", "disable autotrade", "stop autotrade", "turn off autotrade", "pause autotrade", "stop auto trade", "disable auto trade"):
        parts = ["/autotrade", "off"]
    elif any(k in lower_raw for k in [
        "pause trade trigger", "pause trade triggers", "pause triggers", "pause trigger",
        "mute trade trigger", "mute trade triggers", "mute triggers", "stop triggers", "stop trigger alerts"
    ]):
        parts = ["/pausealerts", "triggers"]
    elif any(k in lower_raw for k in [
        "resume trade trigger", "resume trade triggers", "resume triggers", "resume trigger",
        "unmute trade trigger", "unmute trade triggers", "unmute triggers", "start triggers"
    ]):
        parts = ["/resumealerts", "triggers"]
    elif any(k in lower_raw for k in [
        "pause reversal", "pause reversals", "pause reversal alert", "pause reversal alerts",
        "mute reversal", "mute reversals", "stop reversals", "stop reversal alerts"
    ]):
        parts = ["/pausealerts", "reversals"]
    elif any(k in lower_raw for k in [
        "resume reversal", "resume reversals", "resume reversal alert", "resume reversal alerts",
        "unmute reversal", "unmute reversals", "start reversals"
    ]):
        parts = ["/resumealerts", "reversals"]
    elif any(k in lower_raw for k in [
        "pause milestone", "pause milestones", "pause tp alerts", "pause tp alert", "pause tp", "mute tp"
    ]):
        parts = ["/pausealerts", "milestones"]
    elif any(k in lower_raw for k in [
        "resume milestone", "resume milestones", "resume tp alerts", "resume tp alert", "resume tp", "unmute tp"
    ]):
        parts = ["/resumealerts", "milestones"]
    elif any(k in lower_raw for k in [
        "pause digest", "pause digests", "pause 2h digest", "pause hourly calls", "mute digests"
    ]):
        parts = ["/pausealerts", "digests"]
    elif any(k in lower_raw for k in [
        "resume digest", "resume digests", "resume 2h digest", "resume hourly calls", "unmute digests"
    ]):
        parts = ["/resumealerts", "digests"]
    elif any(k in lower_raw for k in [
        "pause notification", "pause notifications", "pause alert", "pause alerts",
        "mute notification", "mute notifications", "mute alerts", "stop alerts", "stop notifications"
    ]):
        parts = ["/pausealerts", "all"]
    elif any(k in lower_raw for k in [
        "resume notification", "resume notifications", "resume alert", "resume alerts",
        "unmute notification", "unmute notifications", "unmute alerts", "start alerts", "start notifications"
    ]):
        parts = ["/resumealerts", "all"]
    elif lower_raw in ("notifications", "notification preferences", "alert preferences", "alerts", "notification settings", "alert settings"):
        parts = ["/notifications"]
    elif "how are we doing" in lower_raw or "performance today" in lower_raw or "what is the drawdown" in lower_raw or "show positions" in lower_raw:
        parts = ["/status"]
    else:
        parts = raw_text.split()

    if not parts:
        return
    cmd = parts[0].lower().split("@")[0]
    args = parts[1:]

    import engine

    # Check which bot received this command
    call_cfg = state.get_call_bot_config()
    is_winz_bot = bool(bot_token and bot_token == call_cfg.get("token"))

    # ── 1. Access Code Redemption Handling ────────────────────────────────────
    if cmd in ("/access", "/code", "/invite"):
        if not args:
            _reply(
                chat_id,
                "🔑 *Winz Access Code Activation*\n\n"
                "To unlock Winz Autonomous Trading Desk, please enter your access code:\n"
                "👉 Usage: `/access <YOUR_CODE>` or simply paste your code directly.\n"
                "_Example: `WINZ-9AQU-CMML`_\n\n"
                "ℹ️ Contact the desk administrator to request an invite code.",
                bot_token=_current_bot_token,
            )
            return

        code_arg = args[0].strip()
        ok, msg_text = access_manager.redeem_access_code(chat_id, code_arg)
        if ok:
            if is_winz_bot:
                state.add_call_bot_chat(chat_id)
            _reply(
                chat_id,
                "🎉 *Access Granted! Welcome to Winz Trading Desk.*\n\n"
                "Your access code has been activated successfully.\n"
                "You now have full access to:\n"
                "• 💼 *Personal Trading Desk* ($10,000 Starting Demo Portfolio)\n"
                "• 🔐 *CEX / Bybit Live Automation* (`/connect bybit`)\n"
                "• 📋 *Active Calls & 2-Hour A+ Quality Signals* (`/calls`)\n"
                "• 💎 *Spot Accumulation Setups & Charts* (`/spot`)\n"
                "• 🔬 *Institutional Order Block Thesis* (`/research`)\n\n"
                "🛡️ *Automated Trading is currently OFF by default.*\n"
                "When you are ready for autonomous execution, type `/autotrade on`.\n\n"
                "👉 Type `/help` or `/calls` to explore your trading desk!",
                bot_token=_current_bot_token,
            )
        else:
            _reply(
                chat_id,
                f"❌ *Access Code Failed*\n\n"
                f"{msg_text}\n\n"
                f"Please verify your code or contact the desk administrator to request access.",
                bot_token=_current_bot_token,
            )
        return

    # Direct redemption via /start <CODE> (e.g. t.me/thewinzbot?start=CODE)
    if cmd == "/start" and args:
        code_arg = args[0].strip()
        ok, msg_text = access_manager.redeem_access_code(chat_id, code_arg)
        if ok:
            if is_winz_bot:
                state.add_call_bot_chat(chat_id)
            _reply(
                chat_id,
                "🎉 *Access Granted! Welcome to Winz Trading Desk.*\n\n"
                "Your invite link has been activated successfully!\n\n"
                "🛡️ *Automated Trading is currently OFF by default.*\n"
                "When you are ready for autonomous execution, type `/autotrade on`.\n\n"
                "👉 Type `/help` to view all available commands or `/calls` for live trade setups.",
                bot_token=_current_bot_token,
            )
            return
        else:
            _reply(
                chat_id,
                f"❌ *Invalid Access Code / Invite Link*\n\n"
                f"{msg_text}\n\n"
                f"To activate manually:\n"
                f"👉 Paste your code directly or type `/access <YOUR_CODE>`",
                bot_token=_current_bot_token,
            )
            return

    # ── 2. Access Authorization Gate for Public Users ─────────────────────────
    if not access_manager.is_user_authorized(chat_id):
        # Inspect un-slashed messages to see if user pasted a raw access code or invite link
        if not raw_text.startswith("/"):
            candidate = access_manager.extract_and_normalize_code(raw_text)
            if candidate:
                ok, msg_text = access_manager.redeem_access_code(chat_id, raw_text)
                if ok:
                    if is_winz_bot:
                        state.add_call_bot_chat(chat_id)
                    _reply(
                        chat_id,
                        "🎉 *Access Granted! Welcome to Winz Trading Desk.*\n\n"
                        "Your access code has been activated successfully.\n"
                        "You now have full access to:\n"
                        "• 💼 *Personal Trading Desk* ($10,000 Starting Demo Portfolio)\n"
                        "• 🔐 *CEX / Bybit Live Automation* (`/connect bybit`)\n"
                        "• 📋 *Active Calls & 2-Hour A+ Quality Signals* (`/calls`)\n"
                        "• 💎 *Spot Accumulation Setups & Charts* (`/spot`)\n"
                        "• 🔬 *Institutional Order Block Thesis* (`/research`)\n\n"
                        "🛡️ *Automated Trading is currently OFF by default.*\n"
                        "When you are ready for autonomous execution, type `/autotrade on`.\n\n"
                        "👉 Type `/help` or `/calls` to explore your trading desk!",
                        bot_token=_current_bot_token,
                    )
                    return
                elif candidate.startswith("WINZ") or "-" in candidate or "_" in candidate:
                    _reply(
                        chat_id,
                        f"❌ *Access Code Failed*\n\n"
                        f"{msg_text}\n\n"
                        f"Please verify your code or contact the desk administrator to request a new code.",
                        bot_token=_current_bot_token,
                    )
                    return

        if cmd in ("/start", "/help"):
            _reply(
                chat_id,
                "🔒 *Winz Trading Desk — Access Code Required*\n\n"
                "Welcome to *Winz* — Autonomous Algorithmic Trade Desk & Market Intelligence Hub.\n\n"
                "Winz is currently running in exclusive private access mode. To unlock your personal trading desk, market signals, and Bybit execution:\n\n"
                "👉 *Activate your access:*\n"
                "Simply paste your access code directly here or type:\n"
                "`/access <YOUR_CODE>`\n"
                "_Example: `/access WINZ-9AQU-CMML`_\n\n"
                "ℹ️ *Don't have an access code?* Contact the desk administrator to request an invite code.",
                bot_token=_current_bot_token,
            )
        else:
            _reply(
                chat_id,
                "🔒 *Access Restricted*\n\n"
                "You need an active access code to use Winz Trading Desk.\n\n"
                "👉 Simply paste your access code directly here or type `/access <YOUR_CODE>` to unlock all features.\n"
                "_Example: `WINZ-9AQU-CMML`_",
                bot_token=_current_bot_token,
            )
        return

    # Auto-register this chat into Winz calls distribution list on /start or subscribe
    if is_winz_bot and cmd in ("/start", "/subscribe", "/sub"):
        state.add_call_bot_chat(chat_id)

    # Admin Permission Gate (Master Desk Only)
    if cmd in MASTER_DESK_ONLY_COMMANDS and not is_admin(chat_id):
        log.warning("Non-admin chat_id %s attempted master desk command: %s", chat_id, cmd)
        _reply(
            chat_id,
            "⛔ *Access Restricted: Master Desk Command*\n\n"
            f"The command `{cmd}` controls the master algorithmic trade desk and is reserved for the desk administrator.\n\n"
            "🎯 *Available Personal Trading & Intelligence Commands:*\n"
            "• `/status` — View your personal $10,000 demo portfolio & score\n"
            "• `/positions` — View your personal open trades with live PnL\n"
            "• `/trade <COIN> [BUY/SELL]` — Open a personal paper trade (e.g. `/trade BTC`)\n"
            "• `/tp` — Bank profit on all your winning positions\n"
            "• `/derisk` — Bank winners 100% & trim losers by 50%\n"
            "• `/close <COIN>` — Close a specific personal position\n"
            "• `/closeall` — Close all your open positions\n"
            "• `/resetdemo` — Reset your personal demo balance to $10,000\n"
            "• `/leverage <COIN> <VAL>` — Set your custom leverage\n"
            "• `/existingleverage` — View your active leverage settings\n"
            "• `/avoid <COIN>` — Exclude coins from your setups\n"
            "• `/allow <COIN>` — Re-enable coins\n"
            "• `/avoided` — View your excluded coins\n"
            "• `/calls` — View active triggered setups\n"
            "• `/hourlycalls` — 15 trade desk calls (Scalps + Day + Spot)\n"
            "• `/spot` — 5 Spot accumulation setups with charts\n"
            "• `/research <COIN>` — Institutional thesis & chart markup\n"
            "• `/tweet [COIN]` — 280-char formatted Twitter/X post\n"
            "• `/news [COIN]` — Breaking crypto news & sentiment\n"
            "• `/sentiment` — Fear & Greed Index\n"
            "• `/notifications` — Alert preferences & granular toggles\n"
            "• `/pausealerts` / `/resumealerts` — Pause/resume triggers or reversals\n"
            "• `/subscribe` / `/unsubscribe` — Alert notifications",
            bot_token=_current_bot_token,
        )
        return


    if cmd in ("/start", "/help"):
        if is_winz_bot or not is_admin(chat_id):
            sub_status = "🟢 Active (Receiving 2h digests & live alerts)" if state.is_call_bot_subscribed(chat_id) else "⚪ Inactive (type `/subscribe` to enable)"
            msg = (
                "🤖 *Winz Trading Desk & Market Intelligence Hub*\n\n"
                "Welcome to *Winz*! Your personal institutional crypto companion with full virtual trading desk capabilities.\n\n"
                f"📡 *Alert Status:* {sub_status}\n\n"
                "💼 *Your Personal Trading Desk ($10,000 Demo Portfolio):*\n"
                "• `/status` — 📊 View your equity, floating PnL & win rate\n"
                "• `/positions` — 📈 View active open trades with live PnL\n"
                "• `/trade <COIN> [BUY/SELL]` — 🚀 Open a personal paper trade (e.g. `/trade BTC`)\n"
                "• `/tp` — 🎯 Close all your trades currently in profit\n"
                "• `/derisk` — 🛡️ Bank winners 100% & trim losers 50%\n"
                "• `/close <COIN>` — ✂️ Close a specific personal position\n"
                "• `/closeall` — 🚨 Close all your open positions\n"
                "• `/resetdemo` — 🔄 Reset portfolio back to $10,000 USDT\n\n"
                "🎯 *Trade Setups & Active Signals:*\n"
                "• `/calls` — 📋 View active triggered trades & pending entry bands\n"
                "• `/hourlycalls` — 🚨 View the 15-setup trade desk (6 Scalps + 4 Day + 5 Spot)\n"
                "• `/spot` — 💎 View 5 Spot Accumulation setups with interactive chart buttons\n\n"
                "🔐 *Automated Exchange Trading (Bybit / CEX):*\n"
                "• `/connect bybit <KEY> <SECRET>` — ⚡ Connect your Bybit account for automated execution\n"
                "• `/myexchange` — 💼 View connected account balance, equity & status\n"
                "• `/autotrade <on/off>` — ⚙️ Toggle automated trade execution on/off\n"
                "• `/risk <USDT>` — ⚖️ Set your margin size per trade (e.g. `/risk 50`)\n"
                "• `/disconnect` — 🔌 Wipe your API keys and unlink exchange\n\n"
                "🎯 *Target Growth & Compounding Flight Plan:*\n"
                "• `/setgoal <START> <TARGET> [DAYS] [LEV] [MAX_LOSS%]` — 🚀 Initialize flight plan (e.g. `/setgoal 10 100 30 5 5`)\n"
                "• `/goal` — 📊 View daily progress, required compound rate & pacing\n"
                "• `/goalplan` — 💡 Get highest-conviction setup sized for today's target\n"
                "• `/cancelgoal` — 🛑 Cancel active growth flight plan\n\n"
                "⚙️ *Risk Management & Settings:*\n"
                "• `/leverage <COIN> <VAL>` — ⚡ Set custom leverage (e.g. `/leverage BTC 20`)\n"
                "• `/existingleverage` — 📊 Show your active leverage settings\n"
                "• `/avoid <COINS>` — 🚫 Exclude assets from your trading\n"
                "• `/allow <COINS>` — 🟢 Restore excluded assets\n"
                "• `/avoided` — 📋 Show your avoided assets list\n"
                "• `/pause` / `/resume` — ⏸️ Pause or resume personal execution\n"
                "• `/onlyday` / `/onlyscalp` — 🟡 Trade Day trades only or Scalps only\n"
                "• `/pausescalp` / `/pauseday` — ⏸️ Pause individual execution modes\n"
                "• `/resumescalp` / `/resumeday` — 🟢 Resume individual execution modes\n\n"
                "🔬 *Deep Institutional Research & Charts:*\n"
                "• `/research <COIN>` — Institutional order block thesis & chart markup (e.g. `/research SOL`)\n"
                "• `/research <COIN> spot` — Spot accumulation thesis & macro chart\n"
                "• `/tweet [COIN]` — 🐦 Formatted 280-char post ready to copy to Twitter/X\n\n"
                "🗞️ *Market Fundamentals & Sentiment:*\n"
                "• `/news [COIN]` — Live breaking crypto news & NLP catalyst scoring\n"
                "• `/sentiment` — 🌡️ Crypto Fear & Greed Index + institutional tone\n\n"
                "📢 *Personal Alert Preferences:*\n"
                "• `/notifications` — 🔔 Interactive panel to toggle triggers, reversals, TP & digests\n"
                "• `/pausealerts [cat]` — ⏸️ Pause alerts (`triggers`, `reversals`, `tp`, `digests`, or `all`)\n"
                "• `/resumealerts [cat]` — 🟢 Resume alerts (`triggers`, `reversals`, `tp`, `digests`, or `all`)\n"
                "• `/subscribe` — 🔔 Enable 2h digests & live trade trigger notifications in this chat\n"
                "• `/unsubscribe` — 🔕 Disable automated broadcasts in this chat\n"
                "• `/setchannel` — Broadcast signals to your group or channel"
            )
        else:
            msg = (
                "🤖 *Xira Autonomous Trade Desk Control (Admin Panel)*\n\n"
                "• `/broadcast <MSG>` — 📢 Broadcast announcement to all users & channels\n"
                "• `/signal <CALL>` — 🎯 Broadcast official trade call to all users & channels\n"
                "• `/watchlist` — 🎯 Best setups to watch right now (Shelves, invalidations & macro context)\n"
                "• `/status` — Balance, equity, open positions & win stats\n"
                "• `/positions` — Detailed active positions with live PnL & targets\n"
                "• `/tp` — 🎯 Close all open trades currently in profit\n"
                "• `/derisk` — 🛡️ Close 100% of winning trades & trim 50% of losers\n"
                "• `/hourlyreport` — ⏱️ Hourly analytics breakdown & chart\n"
                "• `/dailyreport` — 📅 Daily (24h) performance & chart\n"
                "• `/weeklyreport` — 📆 Weekly (7d) performance & chart\n"
                "• `/monthlyreport` — 🗓️ Monthly (30d) performance & chart\n"
                "• `/feedback` — 🧠 Daily intelligence feedback: most lost/profit assets & learning\n"
                "• `/hourlycalls` — 🎯 Dispatch 15 hourly manual calls (6 scalps + 4 day + 5 spot)\n"
                "• `/spot` — 💎 Dispatch 5 spot accumulation setups\n"
                "• `/news` / `/sentiment` — 🗞️ Breaking news & Crypto Fear & Greed Index\n"
                "• `/setchannel` — 📢 Register group/channel for automated trade signals\n"
                "• `/setcallbot <TOKEN> <CHAT_ID>` — 📢 Connect external bot for manual calls\n"
                "• `/callbot` — 📋 View current calls bot destination\n"
                "• `/research <COIN>` — 🔬 Institutional research note & chart (e.g. `/research BTC`)\n"
                "• `/tweet [COIN]` — 🐦 Convert research into 280-char Twitter/X post\n"
                "• `/setfeedbackbot <TOKEN> <CHAT_ID>` — 🤖 Connect another bot for daily feedback\n"
                "• `/feedbackbot` — 📋 View current feedback bot destination\n"
                "• `/probation` — 🧪 View 24h quarantined & 50% probation assets\n"
                "• `/setgoal <START> <TARGET> [DAYS] [LEV] [MAX_LOSS%]` — 🚀 Set target growth plan\n"
                "• `/goal` — 📊 Progress tracker & compound pacing\n"
                "• `/goalplan` — 💡 Recommend trade sized for today's target\n"
                "• `/cancelgoal` — 🛑 Cancel growth plan\n"
                "• `/leverage <COIN> <VAL>` — ⚡ Set leverage (e.g. `/leverage BTC 10`)\n"
                "• `/existingleverage` — 📊 Show leverage used for each asset\n"
                "• `/avoid <COINS>` — 🚫 Blacklist assets (e.g. `/avoid DOGE PEPE`)\n"
                "• `/allow <COINS>` — 🟢 Restore assets (e.g. `/allow DOGE`)\n"
                "• `/avoided` — 📋 Show all currently avoided assets\n"
                "• `/drop <COIN>` — ✂️ Close position & immediately add to avoid list\n"
                "• `/scan` — Trigger an immediate market scalp scan\n"
                "• `/onlyday` / `/onlyscalp` — 🟡 Trade day trades only or scalps only\n"
                "• `/pausescalp` / `/pauseday` — ⏸️ Pause scalp or day trade cycle\n"
                "• `/resumescalp` / `/resumeday` — 🟢 Resume scalp or day trade cycle\n"
                "• `/pause` — Pause opening new orders\n"
                "• `/resume` — Resume automatic order execution\n"
                "• `/onspot` — 🟢 Enable Spot order execution\n"
                "• `/offspot` — 🔴 Disable Spot orders (Perps only)\n"
                "• `/cancelorder <COINS>` — 🚫 Cancel resting limit orders\n"
                "• `/cancelallorders` — 🧹 Cancel ALL resting limit orders on Bybit\n"
                "• `/close <TICKER>` — Market close position for ticker\n"
                "• `/closeall` — 🚨 Emergency market close all open positions\n"
                "• `/help` — Show this command list\n\n"
                "Web Dashboard: `http://localhost:8765`"
            )
        _reply(chat_id, msg, bot_token=bot_token)
        return

    elif cmd in ("/status", "/balance"):
        if not is_admin(chat_id):
            u = user_store.get_user(chat_id)
            eq = float(u.get("paper_equity", 10000.0))
            open_pos = u.get("open_positions", {})
            stats = u.get("session", {})
            pause_str = user_store.get_user_execution_mode_status(chat_id)
            ex = user_store.get_user_exchange(chat_id)
            auto_trade_enabled = bool(ex and ex.get("auto_trade"))
            auto_str = "🟢 ON (Autonomous)" if auto_trade_enabled else "🔴 OFF (Type /autotrade on to enable)"

            floating_pnl = 0.0
            from scanner import fetch_live_price
            for pos in open_pos.values():
                t = pos.get("ticker", "BTC")
                side = pos.get("side", "BUY").upper()
                entry = float(pos.get("entry_price", 0.0))
                qty = float(pos.get("qty", 1.0))
                lp = fetch_live_price(t) or entry
                if entry > 0:
                    diff = (lp - entry) if side == "BUY" else (entry - lp)
                    floating_pnl += diff * qty

            net_equity = eq + floating_pnl
            acct_info = ""
            if ex and ex.get("connected"):
                acct_info = f"• *Connected Account*: `Bybit` ({ex.get('api_key_masked')})\n"

            msg = (
                f"💼 *Your Personal Trading Desk Status*\n\n"
                f"{acct_info}"
                f"• *Execution*: {pause_str}\n"
                f"• *Automated Trading*: {auto_str}\n"
                f"• *Demo Equity*: `${eq:,.2f} USDT`\n"
                f"• *Net Equity (with Floating)*: `${net_equity:,.2f} USDT`\n"
                f"• *Open Positions*: `{len(open_pos)}`\n"
                f"• *Session Score*: `{stats.get('wins', 0)}W / {stats.get('losses', 0)}L / {stats.get('breakevens', 0)}BE`\n"
                f"• *Session Realized PnL*: `${stats.get('total_pnl_usdt', 0.0):+,.2f} USDT`\n\n"
                f"_Commands: `/autotrade on/off`, `/positions`, `/trade <COIN>`, `/tp`, `/derisk`, `/close <COIN>`, `/closeall`, `/resetdemo`_"
            )
            _reply(chat_id, msg)
            return

        bal = engine.client.get_wallet_balance("USDT")
        eq = bal.get("equity", 0.0) if not engine.client.is_paper else state.get_equity()
        avail = bal.get("available", 0.0) if not engine.client.is_paper else eq
        open_pos = state.get_open_positions()
        active_count = len(open_pos)
        stats = state.get_session_stats()
        spot_on = state.is_spot_enabled()

        mode_str = cfg.trade_mode.upper()
        pause_str = state.get_execution_mode_status()
        spot_str = "🟢 ON (Dual-Venue)" if spot_on else "🔴 OFF (Perps Only)"

        msg = (
            f"📊 *Xira Trade Desk Status*\n\n"
            f"• *Mode*: `{mode_str}` ({cfg.effective_demo_env})\n"
            f"• *Execution*: {pause_str}\n"
            f"• *Spot Trading*: {spot_str}\n"
            f"• *Total Equity*: `${eq:,.2f} USDT`\n"
            f"• *Available*: `${avail:,.2f} USDT`\n"
            f"• *Open Positions*: `{active_count}`\n"
            f"• *Session Score*: `{stats.get('wins', 0)}W / {stats.get('losses', 0)}L / {stats.get('breakevens', 0)}BE`\n"
            f"• *Session Realized PnL*: `${stats.get('total_pnl_usdt', 0.0):+,.2f} USDT`"
        )
        _reply(chat_id, msg)

    elif cmd == "/positions":
        if not is_admin(chat_id):
            u = user_store.get_user(chat_id)
            open_pos = u.get("open_positions", {})
            if not open_pos:
                _reply(
                    chat_id,
                    "ℹ️ *No open positions in your personal portfolio.*\n\n"
                    "👉 To open a trade, type `/trade <COIN>` (e.g. `/trade BTC` or `/trade SOL SHORT`).\n"
                    "👉 Or view high-conviction setups with `/calls` or `/hourlycalls`."
                )
                return

            from scanner import fetch_live_price
            lines = ["📈 *Your Personal Active Positions:*", ""]
            for pid, p in open_pos.items():
                t = p.get("ticker", "BTC").upper()
                side = p.get("side", "BUY").upper()
                entry = float(p.get("entry_price", 0.0))
                lev = int(p.get("leverage", 10))
                qty = float(p.get("qty", 1.0))
                lp = fetch_live_price(t) or entry

                if entry > 0:
                    raw_pct = ((lp - entry) / entry) if side == "BUY" else ((entry - lp) / entry)
                    pnl_pct = raw_pct * lev * 100.0
                    u_pnl = raw_pct * entry * qty
                else:
                    pnl_pct = 0.0
                    u_pnl = 0.0

                pnl_str = f"${u_pnl:+,.2f} ({pnl_pct:+.2f}%)"
                derisked_tag = " [50% Trimmed]" if p.get("derisked") else ""
                lines.append(
                    f"• *{t}* `{side}` ({lev}x){derisked_tag}\n"
                    f"  Entry: ${entry:,.4f} | Live: ${lp:,.4f}\n"
                    f"  Floating PnL: *{pnl_str}*\n"
                )

            lines.append("\n_Commands: `/tp` (take profit), `/derisk`, `/close <COIN>`, `/closeall`_")
            _reply(chat_id, "\n".join(lines))
            return

        open_pos = state.get_open_positions()
        if not open_pos:
            _reply(chat_id, "ℹ️ No open positions right now.")
            return

        lines = ["📈 *Current Active Positions on Bybit:*", ""]
        for p in open_pos.values():
            t = p.get("ticker", "UNKNOWN")
            side = p.get("side", "")
            mkt = p.get("market", "perp").upper()
            status = p.get("status", "OPEN")
            ep = fmt_dollar(p.get("entry_price"))
            mark = fmt_dollar(p.get("mark_price"))
            u_pnl = p.get("unrealised_pnl")
            pnl_str = f"${u_pnl:+,.2f}" if u_pnl is not None else "—"
            tp1 = fmt_dollar(p.get("tp1"))
            tp2 = fmt_dollar(p.get("tp2"))
            sl = fmt_dollar(p.get("sl"))
            lev = p.get("leverage", 1)

            lines.append(
                f"• *{t}* `{side}` ({mkt} {lev}x) [{status}]\n"
                f"  Entry: {ep} | Mark: {mark}\n"
                f"  Floating PnL: *{pnl_str}*\n"
                f"  TP1: {tp1} | TP2: {tp2} | SL: {sl}\n"
            )

        _reply(chat_id, "\n".join(lines))

    elif cmd in ("/onlyday", "/dayonly"):
        if not is_admin(chat_id):
            user_store.set_user_execution_mode(chat_id, "day")
            _reply(
                chat_id,
                "🟡 *Mode Updated: DAY TRADES ONLY*\n\n"
                "• 15m automated scalps are now PAUSED.\n"
                "• 4h day trade setups and swing signals remain ACTIVE.\n"
                "• Existing positions remain protected by exchange TP & SL.\n"
                "• Type `/resume` anytime to re-enable both scalps and day trades.",
                bot_token=_current_bot_token,
            )
            return

        state.set_execution_mode("day")
        _reply(
            chat_id,
            "🟡 *Master Desk Updated: DAY TRADES ONLY*\n\n"
            "• Automated 15m scalp cycle is now PAUSED.\n"
            "• 4h day trade cycle and swing setups remain ACTIVE.\n"
            "• Open positions remain protected by exchange TP & SL.\n"
            "• Type `/resume` to re-enable scalping.",
            bot_token=_current_bot_token,
        )

    elif cmd in ("/onlyscalp", "/scalponly"):
        if not is_admin(chat_id):
            user_store.set_user_execution_mode(chat_id, "scalp")
            _reply(
                chat_id,
                "🟡 *Mode Updated: SCALPS ONLY*\n\n"
                "• 4h day trade execution is now PAUSED.\n"
                "• 15m automated scalps remain ACTIVE.\n"
                "• Existing positions remain protected by exchange TP & SL.\n"
                "• Type `/resume` anytime to re-enable both scalps and day trades.",
                bot_token=_current_bot_token,
            )
            return

        state.set_execution_mode("scalp")
        _reply(
            chat_id,
            "🟡 *Master Desk Updated: SCALPS ONLY*\n\n"
            "• 4h day trade execution is now PAUSED.\n"
            "• 15m automated scalp cycle remains ACTIVE.\n"
            "• Open positions remain protected by exchange TP & SL.\n"
            "• Type `/resume` to re-enable day trades.",
            bot_token=_current_bot_token,
        )

    elif cmd == "/pausescalp":
        if not is_admin(chat_id):
            user_store.set_user_paused(chat_id, True, mode="scalp")
            _reply(
                chat_id,
                "⏸ *Personal Scalp Trading PAUSED*\n\n"
                "• 15m automated scalp execution is paused.\n"
                "• Day trade execution remains active.\n"
                "• Type `/resumescalp` or `/resume` to restart.",
                bot_token=_current_bot_token,
            )
            return

        state.set_paused(True, mode="scalp")
        _reply(
            chat_id,
            "⏸ *Master Desk Scalp Trading PAUSED*\n\n"
            "• 15m automated scalp scan & order placement is paused.\n"
            "• 4h day trade cycle remains active.\n"
            "• Type `/resumescalp` or `/resume` to reactivate.",
            bot_token=_current_bot_token,
        )

    elif cmd == "/pauseday":
        if not is_admin(chat_id):
            user_store.set_user_paused(chat_id, True, mode="day")
            _reply(
                chat_id,
                "⏸ *Personal Day Trading PAUSED*\n\n"
                "• Day trade execution is paused.\n"
                "• 15m scalp execution remains active.\n"
                "• Type `/resumeday` or `/resume` to restart.",
                bot_token=_current_bot_token,
            )
            return

        state.set_paused(True, mode="day")
        _reply(
            chat_id,
            "⏸ *Master Desk Day Trading PAUSED*\n\n"
            "• 4h day trade cycle is paused.\n"
            "• 15m scalp cycle remains active.\n"
            "• Type `/resumeday` or `/resume` to reactivate.",
            bot_token=_current_bot_token,
        )

    elif cmd == "/resumescalp":
        if not is_admin(chat_id):
            user_store.set_user_paused(chat_id, False, mode="scalp")
            _reply(
                chat_id,
                "🟢 *Personal Scalp Trading RESUMED*\n\n"
                "• 15m automated scalps are now active.",
                bot_token=_current_bot_token,
            )
            return

        state.set_paused(False, mode="scalp")
        _reply(
            chat_id,
            "🟢 *Master Desk Scalp Trading RESUMED*\n\n"
            "• 15m automated scalp scan & execution is now active.",
            bot_token=_current_bot_token,
        )
        if scan_trigger_fn:
            threading.Thread(target=scan_trigger_fn, daemon=True).start()

    elif cmd == "/resumeday":
        if not is_admin(chat_id):
            user_store.set_user_paused(chat_id, False, mode="day")
            _reply(
                chat_id,
                "🟢 *Personal Day Trading RESUMED*\n\n"
                "• 4h day trade execution is now active.",
                bot_token=_current_bot_token,
            )
            return

        state.set_paused(False, mode="day")
        _reply(
            chat_id,
            "🟢 *Master Desk Day Trading RESUMED*\n\n"
            "• 4h day trade execution is now active.",
            bot_token=_current_bot_token,
        )

    elif cmd == "/pause":
        sub_arg = args[0].lower() if args else ""
        if sub_arg in ("scalp", "scalps", "15m", "quick"):
            if not is_admin(chat_id):
                user_store.set_user_paused(chat_id, True, mode="scalp")
                _reply(chat_id, "⏸ *Personal Scalp Trading PAUSED.*\n\nDay trades remain active. Type `/resumescalp` or `/resume` to restart.", bot_token=_current_bot_token)
                return
            state.set_paused(True, mode="scalp")
            _reply(chat_id, "⏸ *Master Desk Scalp Trading PAUSED.*\n\nDay trades remain active. Type `/resumescalp` or `/resume` to reactivate.", bot_token=_current_bot_token)
            return
        elif sub_arg in ("day", "daytrade", "days", "swing", "4h"):
            if not is_admin(chat_id):
                user_store.set_user_paused(chat_id, True, mode="day")
                _reply(chat_id, "⏸ *Personal Day Trading PAUSED.*\n\nScalps remain active. Type `/resumeday` or `/resume` to restart.", bot_token=_current_bot_token)
                return
            state.set_paused(True, mode="day")
            _reply(chat_id, "⏸ *Master Desk Day Trading PAUSED.*\n\nScalps remain active. Type `/resumeday` or `/resume` to reactivate.", bot_token=_current_bot_token)
            return

        # Default: pause all
        if not is_admin(chat_id):
            user_store.set_user_paused(chat_id, True, mode="all")
            _reply(
                chat_id,
                "⏸ *Your personal trading execution is PAUSED.*\n\n"
                "• Your open positions remain active.\n"
                "• Automated signal fills will be ignored.\n"
                "• Type `/resume` anytime to unpause.",
                bot_token=_current_bot_token,
            )
            return

        state.set_paused(True, mode="all")
        _reply(
            chat_id,
            "⏸ *Automated execution PAUSED.*\n\n"
            "• Existing positions remain protected by exchange TP/SL.\n"
            "• You can execute any manual instruction (`/scan`, `/tp`, `/derisk`, `/close`, `/cancelorder`, etc.) while paused.\n"
            "• Automated scanning & new order placement will NOT resume automatically until you explicitly type `/resume`.",
            bot_token=_current_bot_token,
        )

    elif cmd == "/resume":
        sub_arg = args[0].lower() if args else ""
        if sub_arg in ("scalp", "scalps", "15m", "quick"):
            if not is_admin(chat_id):
                user_store.set_user_paused(chat_id, False, mode="scalp")
                _reply(chat_id, "🟢 *Personal Scalp Trading RESUMED.*", bot_token=_current_bot_token)
                return
            state.set_paused(False, mode="scalp")
            _reply(chat_id, "🟢 *Master Desk Scalp Trading RESUMED.*", bot_token=_current_bot_token)
            if scan_trigger_fn:
                threading.Thread(target=scan_trigger_fn, daemon=True).start()
            return
        elif sub_arg in ("day", "daytrade", "days", "swing", "4h"):
            if not is_admin(chat_id):
                user_store.set_user_paused(chat_id, False, mode="day")
                _reply(chat_id, "🟢 *Personal Day Trading RESUMED.*", bot_token=_current_bot_token)
                return
            state.set_paused(False, mode="day")
            _reply(chat_id, "🟢 *Master Desk Day Trading RESUMED.*", bot_token=_current_bot_token)
            return

        # Default: resume all
        if not is_admin(chat_id):
            user_store.set_user_paused(chat_id, False, mode="all")
            _reply(
                chat_id,
                "🟢 *Your personal trading execution is RESUMED.*\n\n"
                "• Personal trade setups and signals are active.\n"
                "• Type `/status` to review your portfolio.",
                bot_token=_current_bot_token,
            )
            return

        state.set_paused(False, mode="all")
        state.reset_global_circuit_breaker()
        _reply(chat_id, "🟢 *Automated execution RESUMED.* (Circuit breaker cleared). Triggering immediate market scan...", bot_token=_current_bot_token)
        if scan_trigger_fn:
            threading.Thread(target=scan_trigger_fn, daemon=True).start()

    elif cmd in ("/resetcircuit", "/unfreeze"):
        state.reset_global_circuit_breaker()
        _reply(chat_id, "🟢 *Global Circuit Breaker RESET.*\n\nTrading cooldown cleared. Immediate scan triggered.", bot_token=_current_bot_token)
        if scan_trigger_fn:
            threading.Thread(target=scan_trigger_fn, daemon=True).start()
        return

    elif cmd in ("/broadcast", "/signal"):
        if not is_admin(chat_id):
            _reply(
                chat_id,
                "⛔ *Access Restricted: Administrator Only*\n\n"
                "Only Master Desk administrators are authorized to broadcast calls and messages to all users.",
                bot_token=_current_bot_token,
            )
            return

        if not args:
            _reply(
                chat_id,
                "📢 *Winz Broadcast Desk*\n\n"
                "Dispatch an instant announcement or official trade call to all subscribed users, groups, and channels.\n\n"
                "• *Usage:* `/broadcast <MESSAGE>` or `/signal <TRADE_CALL>`\n\n"
                "• *Examples:*\n"
                "  `/broadcast 🚀 Market volatility expected ahead of CPI release at 12:30 UTC. Keep stops tight!`\n"
                "  `/signal 🚀 LONG BTCUSDT @ $68,400 | TP: $70,200 | SL: $67,500 | High Conviction Breakout!`",
                bot_token=_current_bot_token,
            )
            return

        broadcast_msg = raw_text.split(None, 1)[1].strip()
        call_cfg = state.get_call_bot_config()
        tok = call_cfg.get("token") or _current_bot_token or cfg.telegram_token

        # Gather deduplicated recipients
        all_destinations = set()
        for c in call_cfg.get("all_chats", []):
            if c:
                all_destinations.add(str(c).strip())
        if call_cfg.get("chat_id"):
            all_destinations.add(str(call_cfg["chat_id"]).strip())
        for u_cid in access_manager.list_authorized_users().keys():
            all_destinations.add(str(u_cid).strip())
        for u_cid in user_store.get_all_users().keys():
            all_destinations.add(str(u_cid).strip())

        # Exclude unsubscribed chats
        unsub = set(str(c).strip() for c in call_cfg.get("unsubscribed_chats", []))
        recipients = [c for c in all_destinations if c and c not in unsub]

        if not recipients:
            _reply(
                chat_id,
                "⚠️ *No active broadcast recipients found.*\n\nUsers and groups are registered when they redeem access codes or use `/subscribe`.",
                bot_token=_current_bot_token,
            )
            return

        is_trade_signal = (cmd == "/signal")
        if is_trade_signal:
            header = "🎯 *WINZ OFFICIAL SIGNAL / CALL* 🎯\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            footer = "\n\n━━━━━━━━━━━━━━━━━━━━━━━\n🛡️ _Dispatched by Winz Master Trading Desk_"
        else:
            header = "📢 *WINZ DESK ANNOUNCEMENT* 📢\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            footer = "\n\n━━━━━━━━━━━━━━━━━━━━━━━\n_Winz Autonomous Trading Desk_"

        formatted_broadcast = f"{header}{broadcast_msg}{footer}"

        _reply(chat_id, f"📡 *Dispatching {cmd} to {len(recipients)} destinations across Telegram and Discord...*", bot_token=_current_bot_token)

        sent_count = 0
        failed_count = 0

        for target_cid in recipients:
            url = f"https://api.telegram.org/bot{tok}/sendMessage"
            payload = {
                "chat_id": target_cid,
                "text": formatted_broadcast,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
                "reply_markup": {
                    "inline_keyboard": [
                        [{"text": "❌ Dismiss", "callback_data": "close_msg"}]
                    ]
                },
            }
            try:
                res = requests.post(url, json=payload, timeout=8)
                if res.status_code == 200 and res.json().get("ok"):
                    sent_count += 1
                else:
                    payload["parse_mode"] = None
                    res_fallback = requests.post(url, json=payload, timeout=8)
                    if res_fallback.status_code == 200 and res_fallback.json().get("ok"):
                        sent_count += 1
                    else:
                        failed_count += 1
            except Exception as exc:
                failed_count += 1
                log.warning("Broadcast send failed for %s: %s", target_cid, exc)

        # Discord dispatch
        discord_sent = False
        try:
            import winz_discord
            winz_discord.broadcast_discord_message(formatted_broadcast)
            discord_sent = True
        except Exception as exc:
            log.debug("Discord broadcast error: %s", exc)

        discord_status_str = " + Discord ✅" if discord_sent else ""
        receipt = (
            f"✅ *Broadcast Dispatched Successfully!*{discord_status_str}\n\n"
            f"• *Type*: `{'Trade Signal' if is_trade_signal else 'Desk Announcement'}`\n"
            f"• *Delivered*: `{sent_count} / {len(recipients)}` Telegram chats\n"
            f"• *Failed*: `{failed_count}`\n\n"
            f"Preview of dispatched text:\n"
            f"_{broadcast_msg}_"
        )
        _reply(chat_id, receipt, bot_token=_current_bot_token)
        return

    elif cmd in ("/onspot", "/spoton"):
        state.set_spot_enabled(True)
        _reply(
            chat_id,
            "🟢 *Spot Trading ENABLED (/onspot)*\n\n"
            "• Bot will execute dual-venue trades (Linear Perps + Spot buys/sells) when valid setups occur.\n"
            "• Use `/offspot` anytime to trade Perpetuals only."
            + _paused_footer()
        )

    elif cmd in ("/offspot", "/spotoff"):
        state.set_spot_enabled(False)
        _reply(
            chat_id,
            "🔴 *Spot Trading DISABLED (/offspot)*\n\n"
            "• Bot is now in *Perpetual Futures Only* mode.\n"
            "• No USDT balance will be spent purchasing spot coins.\n"
            "• Use `/onspot` anytime to re-enable spot execution."
            + _paused_footer()
        )

    elif cmd == "/scan":
        _reply(chat_id, f"🔍 *Triggering scalp scan across Core 24 and Extras...*{_paused_footer()}")
        if scan_trigger_fn:
            threading.Thread(target=scan_trigger_fn, daemon=True).start()
        else:
            _reply(chat_id, f"Scan will execute on next scheduled cycle.{_paused_footer()}")

    elif cmd == "/close":
        if not is_admin(chat_id):
            if not args:
                _reply(chat_id, "⚠️ Specify ticker to close: e.g. `/close BTC`")
                return
            target_ticker = args[0].upper().replace("USDT", "")
            u = user_store.get_user(chat_id)
            open_pos = u.get("open_positions", {})
            matched_pid = None
            for pid, pos in open_pos.items():
                if pos.get("ticker", "").upper().replace("USDT", "") == target_ticker:
                    matched_pid = pid
                    break

            if not matched_pid:
                _reply(chat_id, f"❌ No open personal position found for {target_ticker}.")
                return

            from scanner import fetch_live_price
            lp = fetch_live_price(target_ticker) or float(open_pos[matched_pid].get("entry_price", 0.0))
            closed = user_store.close_user_position(chat_id, matched_pid, lp, reason="USER_CLOSE")
            if closed:
                pnl = closed.get("pnl_usdt", 0.0)
                pnl_pct = closed.get("pnl_pct", 0.0)
                _reply(
                    chat_id,
                    f"✅ *Closed your personal {target_ticker} position:*\n\n"
                    f"• Exit Price: `${lp:,.4f}`\n"
                    f"• Realized PnL: *${pnl:+,.2f} USDT ({pnl_pct:+.2f}%)*\n"
                    f"• Updated Demo Equity: `${user_store.get_user_equity(chat_id):,.2f} USDT`"
                )
            return

        if not args:
            _reply(chat_id, "⚠️ Specify ticker to close: e.g. `/close HYPE`")
            return
        target_ticker = args[0].upper()
        sym = bybit_linear_symbol(target_ticker)
        active = engine.client.get_active_positions()
        matched = [p for p in active if p["symbol"] == sym]
        if not matched:
            _reply(chat_id, f"❌ No active linear position found for {target_ticker} on Bybit.{_paused_footer()}")
            return

        pos = matched[0]
        close_side = "Sell" if pos["side"].lower() == "buy" else "Buy"
        res = engine.client.close_position_market(sym, close_side, pos["size"])
        if res:
            _reply(chat_id, f"✅ *Closed {target_ticker} position on Bybit* ({pos['size']} contracts).{_paused_footer()}")
            engine._sync_with_bybit()
        else:
            _reply(chat_id, f"❌ Failed to close {target_ticker} on Bybit. Check logs.{_paused_footer()}")

    elif cmd in ("/closeall", "/panic"):
        if not is_admin(chat_id):
            from scanner import fetch_live_price
            closed = user_store.close_all_user_positions(chat_id, fetch_price_fn=fetch_live_price, reason="USER_CLOSEALL")
            if not closed:
                _reply(chat_id, "ℹ️ No open positions in your personal portfolio to close.")
                return

            total_pnl = sum(float(c.get("pnl_usdt", 0.0)) for c in closed)
            lines = [
                f"🚨 *Market closed {len(closed)} personal positions.*",
                f"💰 Total PnL: *${total_pnl:+,.2f} USDT*",
                f"💼 Updated Demo Equity: `${user_store.get_user_equity(chat_id):,.2f} USDT`\n"
            ]
            for c in closed:
                lines.append(f"• *{c.get('ticker')}* `{c.get('side')}`: ${c.get('pnl_usdt', 0.0):+,.2f} USDT")
            _reply(chat_id, "\n".join(lines))
            return

        active = engine.client.get_active_positions()
        if not active:
            _reply(chat_id, f"ℹ️ No active linear positions to close on Bybit.{_paused_footer()}")
            return

        closed_count = 0
        for pos in active:
            sym = pos["symbol"]
            close_side = "Sell" if pos["side"].lower() == "buy" else "Buy"
            res = engine.client.close_position_market(sym, close_side, pos["size"])
            if res:
                closed_count += 1

        _reply(chat_id, f"🚨 *EMERGENCY CLOSE ALL:* Market closed {closed_count}/{len(active)} positions on Bybit.{_paused_footer()}")
        engine._sync_with_bybit()

    elif cmd in ("/tp", "/takeprofit", "/closeprofit", "/closeinprofit"):
        if not is_admin(chat_id):
            from scanner import fetch_live_price
            closed = user_store.tp_user_positions(chat_id, fetch_price_fn=fetch_live_price)
            if not closed:
                _reply(chat_id, "ℹ️ None of your personal open positions are currently in profit to close.")
                return

            total_profit = sum(float(c.get("pnl_usdt", 0.0)) for c in closed)
            lines = [
                f"🎯 *Personal Take Profit Executed ({len(closed)} closed)*",
                f"💰 *Total Profit Banked*: `+${total_profit:,.2f} USDT`",
                f"💼 *Updated Equity*: `${user_store.get_user_equity(chat_id):,.2f} USDT`\n"
            ]
            for c in closed:
                lines.append(f"• *{c.get('ticker')}* `{c.get('side')}`: +${c.get('pnl_usdt', 0.0):,.2f} USDT ({c.get('pnl_pct', 0.0):+.2f}%)")
            _reply(chat_id, "\n".join(lines))
            return

        target_tickers = {a.upper().replace("USDT", "") for a in args} if args else set()

        if engine.client.is_paper:
            open_pos = state.get_open_positions()
            if not open_pos:
                _reply(chat_id, "ℹ️ No open paper positions to close.")
                return

            winners = []
            for pid, pos in open_pos.items():
                t = pos.get("ticker", "").upper().replace("USDT", "")
                if target_tickers and t not in target_tickers:
                    continue
                pnl = float(pos.get("unrealised_pnl") or 0.0)
                if pnl > 0:
                    winners.append((pid, pos, pnl))

            if not winners:
                _reply(chat_id, f"ℹ️ No open positions are currently in profit to close.{_paused_footer()}")
                return

            total_profit_banked = 0.0
            closed_list = []
            for pid, pos, pnl in winners:
                mark = float(pos.get("mark_price") or pos.get("entry_price") or 0.0)
                closed = state.close_position(pid, mark, "MANUAL_TP")
                if closed:
                    pnl_val = float(closed.get("pnl_usdt", pnl))
                    total_profit_banked += pnl_val
                    closed_list.append(f"• *{pos.get('ticker')}* `{pos.get('side')}`: +${pnl_val:,.2f} USDT")

            lines = [
                f"🎯 *Take Profit Executed ({len(closed_list)} closed)*",
                f"💰 *Total Profit Banked*: `+${total_profit_banked:,.2f} USDT`\n",
            ]
            lines.extend(closed_list)
            _reply(chat_id, "\n".join(lines) + _paused_footer())
            return

        active = engine.client.get_active_positions(category="linear")
        if not active:
            _reply(chat_id, f"ℹ️ No active linear positions on Bybit.{_paused_footer()}")
            return

        positions_to_check = []
        for p in active:
            sym = p["symbol"]
            ticker = sym.replace("USDT", "")
            if ticker.startswith("1000"):
                ticker = ticker[4:]
            if not target_tickers or ticker in target_tickers:
                positions_to_check.append((p, ticker))

        if not positions_to_check:
            _reply(chat_id, f"ℹ️ No active positions match specified ticker(s): {', '.join(args)}{_paused_footer()}")
            return

        winners = [(p, t) for p, t in positions_to_check if float(p.get("unrealised_pnl") or 0.0) > 0]
        if not winners:
            status_lines = ["ℹ️ *No positions are currently in profit to close.*", "", "📊 *Current Open PnL:*"]
            for p, t in positions_to_check:
                pnl = float(p.get("unrealised_pnl") or 0.0)
                status_lines.append(f"• *{t}* `{p.get('side')}`: ${pnl:+,.2f} USDT")
            status_lines.append("\n_All positions remain protected by exchange Stop Loss._")
            _reply(chat_id, "\n".join(status_lines) + _paused_footer())
            return

        closed_list = []
        failed_list = []
        total_profit_banked = 0.0

        for pos, ticker in winners:
            sym = pos["symbol"]
            side = pos["side"]
            size = float(pos["size"])
            pnl = float(pos.get("unrealised_pnl") or 0.0)
            close_side = "Sell" if side.lower() == "buy" else "Buy"

            res = engine.client.close_position_market(sym, close_side, size)
            if res:
                closed_list.append(f"• *{ticker}* `{side}`: +${pnl:,.2f} USDT ({size} contracts)")
                total_profit_banked += pnl
            else:
                failed_list.append(f"• *{ticker}* `{side}`: Market close order failed")

        engine._sync_with_bybit()
        bal = engine.client.get_wallet_balance("USDT")
        eq = bal.get("equity", 0.0)
        avail = bal.get("available", 0.0)

        lines = [
            f"🎯 *Take Profit Executed ({len(closed_list)}/{len(winners)} in profit closed)*",
            f"💰 *Total Profit Banked*: `+${total_profit_banked:,.2f} USDT`\n",
        ]
        lines.extend(closed_list)
        if failed_list:
            lines.append("\n⚠️ *Failed:*")
            lines.extend(failed_list)

        remaining = [t for p, t in positions_to_check if float(p.get("unrealised_pnl") or 0.0) <= 0]
        if remaining:
            lines.append(f"\n🛡️ *Kept Open ({len(remaining)} in loss/breakeven protected by SL):* `{', '.join(remaining)}`")

        lines.append(f"\n💼 *Account Equity*: `${eq:,.2f} USDT`")
        lines.append(f"🟢 *Available Margin*: `${avail:,.2f} USDT`")

        _reply(chat_id, "\n".join(lines) + _paused_footer())

    elif cmd in ("/derisk", "/harvest"):
        if not is_admin(chat_id):
            from scanner import fetch_live_price
            winners, trimmed = user_store.derisk_user_positions(chat_id, fetch_price_fn=fetch_live_price)
            if not winners and not trimmed:
                _reply(chat_id, "ℹ️ No open positions to derisk in your personal portfolio.")
                return

            lines = ["🛡️ *Personal Portfolio Derisk Summary:*", ""]
            if winners:
                tot_w = sum(float(c.get("pnl_usdt", 0.0)) for c in winners)
                lines.append(f"💰 *Banked in Profit (+${tot_w:,.2f} USDT):*")
                for c in winners:
                    lines.append(f"• *{c.get('ticker')}* `{c.get('side')}`: +${c.get('pnl_usdt', 0.0):,.2f} USDT")
                lines.append("")
            if trimmed:
                lines.append("✂️ *Trimmed 50% Position Size:*")
                for p in trimmed:
                    lines.append(f"• *{p.get('ticker')}* `{p.get('side')}`: Remaining qty {p.get('qty')}")
                lines.append("")

            lines.append(f"💼 *Current Demo Equity*: `${user_store.get_user_equity(chat_id):,.2f} USDT`")
            _reply(chat_id, "\n".join(lines))
            return

        active = engine.client.get_active_positions(category="linear")
        if not active:
            _reply(chat_id, f"ℹ️ No active linear positions on Bybit to derisk.{_paused_footer()}")
            return

        winners_closed = []
        losers_trimmed = []
        unchanged = []

        total_profit_banked = 0.0
        total_loss_trimmed = 0.0

        for pos in active:
            sym = pos["symbol"]
            ticker = sym.replace("USDT", "")
            if ticker.startswith("1000"):
                ticker = ticker[4:]
            side = pos["side"]
            size = float(pos["size"])
            pnl = float(pos.get("unrealised_pnl") or 0.0)
            close_side = "Sell" if side.lower() == "buy" else "Buy"

            if pnl > 0:
                # 100% close in profit
                res = engine.client.close_position_market(sym, close_side, size)
                if res:
                    winners_closed.append(f"• *{ticker}* `{side}`: +${pnl:,.2f} USDT (100% closed)")
                    total_profit_banked += pnl
                else:
                    unchanged.append(f"• *{ticker}* `{side}`: close failed")
            elif pnl < 0:
                # 50% trim in loss
                half_size = engine.client.quantize_qty(sym, size * 0.5, category="linear")
                info = engine.client.get_instrument_info(sym, category="linear")
                if half_size >= info.get("min_qty", 0.001):
                    res = engine.client.close_position_market(sym, close_side, half_size)
                    if res:
                        trimmed_pnl = pnl * (half_size / size) if size > 0 else 0.0
                        total_loss_trimmed += trimmed_pnl
                        losers_trimmed.append(f"• *{ticker}* `{side}`: 50% trimmed ({half_size} contracts, {trimmed_pnl:+,.2f} USDT)")
                    else:
                        unchanged.append(f"• *{ticker}* `{side}`: trim failed")
                else:
                    unchanged.append(f"• *{ticker}* `{side}`: size too small to split (kept open)")
            else:
                unchanged.append(f"• *{ticker}* `{side}`: at breakeven ($0.00)")

        engine._sync_with_bybit()

        lines = ["🛡️ *Derisk Execution Summary:*", ""]
        if winners_closed:
            lines.append(f"💰 *Banked in Profit (Total: +${total_profit_banked:,.2f} USDT):*")
            lines.extend(winners_closed)
            lines.append("")
        if losers_trimmed:
            lines.append(f"✂️ *Trimmed Losses 50% (Loss cut: ${abs(total_loss_trimmed):,.2f} USDT):*")
            lines.extend(losers_trimmed)
            lines.append("*(Remaining 50% remains protected by exchange Stop Loss)*\n")
        if unchanged:
            lines.append("ℹ️ *Unchanged:*")
            lines.extend(unchanged)

        _reply(chat_id, "\n".join(lines) + _paused_footer())

    elif cmd in ("/avoid", "/block"):
        if not is_admin(chat_id):
            if not args:
                _reply(chat_id, "⚠️ Specify tickers to avoid in your personal alerts: e.g. `/avoid DOGE PEPE`")
                return
            avoided = user_store.add_user_avoid(chat_id, args)
            _reply(chat_id, f"🚫 *Added to your personal Avoid List:*\n`{', '.join(avoided)}`")
            return

        if not args:
            _reply(chat_id, f"⚠️ Specify tickers to avoid: e.g. `/avoid DOGE PEPE XLM`{_paused_footer()}")
            return
        avoided = state.add_to_avoid_list(args)
        _reply(chat_id, f"🚫 *Added to Avoid List.* Scanner will ignore:\n`{', '.join(avoided)}`{_paused_footer()}")

    elif cmd in ("/allow", "/unavoid", "/unblock"):
        if not is_admin(chat_id):
            if not args:
                _reply(chat_id, "⚠️ Specify tickers to allow in your personal alerts: e.g. `/allow DOGE PEPE`")
                return
            avoided = user_store.remove_user_avoid(chat_id, args)
            current_str = f"`{', '.join(avoided)}`" if avoided else "_None (all assets allowed)_"
            _reply(chat_id, f"✅ *Removed from your personal Avoid List.* Currently avoided:\n{current_str}")
            return

        if not args:
            _reply(chat_id, f"⚠️ Specify tickers to allow: e.g. `/allow DOGE PEPE`{_paused_footer()}")
            return
        updated = state.remove_from_avoid_list(args)
        current_str = f"`{', '.join(updated)}`" if updated else "_None (all assets allowed)_"
        _reply(chat_id, f"✅ *Removed from Avoid List.* Currently avoided:\n{current_str}{_paused_footer()}")

    elif cmd in ("/avoided", "/blacklist"):
        if not is_admin(chat_id):
            avoided = user_store.get_user_avoid_list(chat_id)
            if not avoided:
                _reply(chat_id, "ℹ️ Your personal avoid list is empty. All assets are eligible for your trades.")
            else:
                _reply(chat_id, f"🚫 *Your Personal Avoided Assets ({len(avoided)}):*\n`{', '.join(avoided)}`\n\n_Use `/allow <TICKER>` to re-enable._")
            return

        current = state.get_avoid_list()
        quar_dict = state.get_quarantine_list()
        prob_dict = state.get_probation_list()
        if not current and not prob_dict:
            _reply(chat_id, "ℹ️ Avoid list is empty. All Core 24 and Extras are actively scanned.")
        else:
            lines = [f"🚫 *Currently Avoided / Filtered Assets ({len(current)}):*"]
            now = datetime.now(timezone.utc)
            for a in current:
                if a in quar_dict:
                    exp_str = quar_dict[a].get("expires_at", "")
                    rem_str = "24h quarantine"
                    if exp_str:
                        try:
                            exp_dt = datetime.fromisoformat(exp_str)
                            diff = exp_dt - now
                            mins = max(0, int(diff.total_seconds() // 60))
                            hrs = mins // 60
                            rem_str = f"24h quarantine ({hrs}h {mins % 60}m left)"
                        except Exception:
                            pass
                    reason = quar_dict[a].get("reason", "")
                    r_str = f" [{reason}]" if reason else ""
                    lines.append(f"• *{a}*: 🔒 {rem_str}{r_str}")
                else:
                    lines.append(f"• *{a}*: 🚫 Manual blacklist")

            if prob_dict:
                lines.append(f"\n🧪 *Assets on 50% Probation ({len(prob_dict)}):*")
                for t, info in prob_dict.items():
                    rem = info.get("trades_remaining", 3)
                    pnl = info.get("probation_pnl", 0.0)
                    lines.append(f"• *{t}*: {rem} trades left | PnL: ${pnl:+,.2f} USDT")

            lines.append("\n_Use `/allow <TICKER>` to resume normal trading, or `/probation` for full health details._")
            _reply(chat_id, "\n".join(lines))

    elif cmd in ("/probation", "/probations", "/quarantine"):
        prob_dict = state.get_probation_list()
        quar_dict = state.get_quarantine_list()

        lines = ["🧪 *Xira Asset Health: Quarantine & Probation Monitor*", ""]
        if not prob_dict and not quar_dict:
            lines.append("✅ *All systems clear!*")
            lines.append("No assets are currently quarantined or on probation.")
            lines.append("All Core 24 and approved Extras trade with *100% position sizing*.")
        else:
            if quar_dict:
                now = datetime.now(timezone.utc)
                lines.append(f"🔒 *Quarantined Assets (24h Lockout - {len(quar_dict)}):*")
                for t, info in sorted(quar_dict.items()):
                    exp_str = info.get("expires_at", "")
                    rem_str = "expiring soon"
                    if exp_str:
                        try:
                            exp_dt = datetime.fromisoformat(exp_str)
                            diff = exp_dt - now
                            mins = max(0, int(diff.total_seconds() // 60))
                            hrs = mins // 60
                            rem_str = f"{hrs}h {mins % 60}m left"
                        except Exception:
                            pass
                    reason = info.get("reason", "")
                    reason_str = f" — _{reason}_" if reason else ""
                    lines.append(f"• *{t}*: 🔒 {rem_str}{reason_str}")
                lines.append("_(Assets auto-transition to 50% probation when 24h expires)_\n")

            if prob_dict:
                lines.append(f"⚠️ *Probation Assets (50% Position Sizing - {len(prob_dict)}):*")
                for t, info in sorted(prob_dict.items()):
                    rem = info.get("trades_remaining", 3)
                    pnl = info.get("probation_pnl", 0.0)
                    pnl_str = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
                    lines.append(f"• *{t}*: `{rem} trade(s) left` | Probation PnL: `{pnl_str} USDT`")
                lines.append("\n_Graduates back to 100% full sizing on net profit after 3 trades. Re-quarantined for 24h on net loss._")
            else:
                lines.append("ℹ️ No assets currently in 50% probation sizing.")

        lines.append("\n_Use `/allow <TICKER>` to manually restore any asset to full 100% trading._")
        _reply(chat_id, "\n".join(lines))

    elif cmd == "/drop":
        if not args:
            _reply(chat_id, f"⚠️ Specify ticker to drop & avoid: e.g. `/drop XLM`{_paused_footer()}")
            return
        target_ticker = args[0].upper()
        sym = bybit_linear_symbol(target_ticker)
        active = engine.client.get_active_positions()
        matched = [p for p in active if p["symbol"] == sym]
        close_msg = ""
        if matched:
            pos = matched[0]
            close_side = "Sell" if pos["side"].lower() == "buy" else "Buy"
            res = engine.client.close_position_market(sym, close_side, pos["size"])
            if res:
                close_msg = f"Closed active {target_ticker} position ({pos['size']} contracts). "
            else:
                close_msg = f"Attempted to close {target_ticker} (check logs). "
            engine._sync_with_bybit()
        else:
            close_msg = f"No active position found for {target_ticker}. "

        state.add_to_avoid_list([target_ticker])
        _reply(chat_id, f"✂️ *Dropped {target_ticker}:* {close_msg}Added to Avoid List.{_paused_footer()}")

    elif cmd == "/leverage":
        if not is_admin(chat_id):
            if len(args) < 2:
                _reply(chat_id, "⚠️ Usage: `/leverage <TICKER> <VALUE>`\nExample: `/leverage BTC 10`")
                return
            ticker = args[0].upper().replace("USDT", "")
            try:
                val = int(args[1])
                if val < 1 or val > 50:
                    _reply(chat_id, "❌ Personal leverage must be an integer between 1 and 50.")
                    return
            except ValueError:
                _reply(chat_id, f"❌ Invalid leverage value: `{args[1]}`. Must be an integer.")
                return

            set_val = user_store.set_user_leverage(chat_id, ticker, val)
            _reply(
                chat_id,
                f"⚡ *Personal leverage for {ticker} set to {set_val}x!*\n\n"
                f"• Future trades on {ticker} will size at `{set_val}x` margin."
            )
            return

        if len(args) < 2:
            _reply(chat_id, f"⚠️ Usage: `/leverage <TICKER> <VALUE>`\nExample: `/leverage BTC 10`{_paused_footer()}")
            return
        ticker = args[0].upper().replace("USDT", "")
        try:
            val = int(args[1])
            if val < 1 or val > 100:
                _reply(chat_id, f"❌ Leverage must be an integer between 1 and 100.{_paused_footer()}")
                return
        except ValueError:
            _reply(chat_id, f"❌ Invalid leverage value: `{args[1]}`. Must be an integer.{_paused_footer()}")
            return

        sym = bybit_linear_symbol(ticker)
        # 1. Update on Bybit exchange
        ex_res = engine.client.set_isolated_margin_and_leverage(sym, val)

        # 2. Persist custom leverage in state
        state.set_custom_leverage(ticker, val)

        # 3. Update any currently open position locally
        open_pos = state.get_open_positions()
        for pid, p in open_pos.items():
            if p.get("ticker") == ticker and p.get("market") == "perp":
                state.update_open_position(pid, {"leverage": val})
                break

        ex_msg = "✅ Updated on Bybit exchange" if ex_res else "⚠️ Note: Applied locally for future orders (exchange update failed or off-market)"
        _reply(
            chat_id,
            f"⚡ *Leverage for {ticker} set to {val}x!*\n\n"
            f"• *Exchange Setting*: {ex_msg}\n"
            f"• *Future Signals*: All new {ticker} orders will size & execute at `{val}x`\n"
            f"• *Margin Mode*: Isolated"
            + _paused_footer()
        )

    elif cmd in ("/existingleverage", "/leverages"):
        if not is_admin(chat_id):
            lev_map = user_store.get_user_leverage(chat_id)
            if args:
                t = args[0].upper().replace("USDT", "")
                lev = lev_map.get(t, 10) if isinstance(lev_map, dict) else 10
                _reply(chat_id, f"📊 *Your Personal Leverage for {t}:* `{lev}x`")
                return
            if not lev_map or not isinstance(lev_map, dict):
                _reply(chat_id, "📊 *Personal Leverage:* All assets currently set to default `10x`.\n\n_Change with `/leverage <TICKER> <VALUE>`_")
                return
            lines = ["📊 *Your Personal Custom Leverages:*", ""]
            for t, l in sorted(lev_map.items()):
                lines.append(f"• *{t}*: `{l}x`")
            lines.append("\n_Change with `/leverage <TICKER> <VALUE>`_")
            _reply(chat_id, "\n".join(lines))
            return

        if args:
            ticker = args[0].upper().replace("USDT", "")
            sym = bybit_linear_symbol(ticker)
            eff_lev = state.get_effective_leverage(ticker)
            custom = state.get_custom_leverage(ticker)
            custom_str = " (User Custom Override)" if custom is not None else " (System Default)"
            
            active = engine.client.get_active_positions()
            matched = [p for p in active if p["symbol"] == sym]
            active_str = f"Active on Bybit ({matched[0]['size']} contracts)" if matched else "No active position"

            _reply(
                chat_id,
                f"📊 *Leverage for {ticker}:*\n\n"
                f"• *Effective Leverage*: `{eff_lev}x`{custom_str}\n"
                f"• *Position State*: {active_str}\n"
                f"• *Margin Mode*: Isolated\n\n"
                f"Change it anytime with: `/leverage {ticker} <VALUE>`"
            )
            return

        active = engine.client.get_active_positions()
        active_map = {p["symbol"].replace("USDT", "").replace("1000", ""): p for p in active}
        custom_map = state.get_custom_leverage()

        lines = ["📊 *Existing Leverage by Asset:*", ""]

        if active_map:
            lines.append("🔥 *Active Positions on Bybit:*")
            for t, p in sorted(active_map.items()):
                lev = p.get("leverage", state.get_effective_leverage(t))
                lines.append(f"• *{t}*: `{lev}x` ({p.get('side')} {p.get('size')} contracts)")
            lines.append("")

        if custom_map:
            lines.append("⚡ *User Custom Overrides:*")
            for t, lev in sorted(custom_map.items()):
                lines.append(f"• *{t}*: `{lev}x` (Custom)")
            lines.append("")

        lines.append("🌐 *Core 24 Settings:*")
        core_chunks = []
        for t in CORE_TICKERS:
            lev = state.get_effective_leverage(t)
            is_cust = " [C]" if t in custom_map else ""
            core_chunks.append(f"{t}: `{lev}x`{is_cust}")
        lines.append(", ".join(core_chunks))
        lines.append("\n_Use `/leverage <TICKER> <VALUE>` to change any asset._")

        _reply(chat_id, "\n".join(lines))

    elif cmd in ("/hourlyreport", "/dailyreport", "/weeklyreport", "/monthlyreport"):
        period = cmd.replace("/", "").replace("report", "")
        _reply(chat_id, f"📊 *Generating {period.capitalize()} Analytics Report & Chart...*")
        import analytics
        threading.Thread(
            target=analytics.send_report,
            args=(chat_id, period),
            daemon=True,
            name=f"report-{period}",
        ).start()

    elif cmd in ("/feedback", "/dailyfeedback", "/learning"):
        _reply(chat_id, "🧠 *Compiling Daily Intelligence & Learning Feedback...*")
        import learning_engine
        threading.Thread(
            target=learning_engine.send_daily_feedback,
            kwargs={"chat_id": chat_id},
            daemon=True,
            name="feedback-thread",
        ).start()

    elif cmd == "/setfeedbackbot":
        if len(args) < 2:
            _reply(chat_id, "⚠️ Usage: `/setfeedbackbot <BOT_TOKEN> <CHAT_ID>`\nExample: `/setfeedbackbot 123456:ABC-DEF -1001234567890`")
            return
        fb_token = args[0]
        fb_chat = args[1]
        state.set_feedback_bot_config(fb_token, fb_chat)
        test_url = f"https://api.telegram.org/bot{fb_token}/sendMessage"
        try:
            r = requests.post(
                test_url,
                json={
                    "chat_id": fb_chat,
                    "text": "✅ *Feedback Connection Established!* Xira will deliver daily intelligence and learning feedback to this destination.",
                    "parse_mode": "Markdown",
                },
                timeout=8,
            )
            if r.status_code == 200 and r.json().get("ok"):
                _reply(chat_id, f"✅ *Feedback Bot Successfully Configured!*\n\n• Token: `{fb_token[:10]}...`\n• Chat ID: `{fb_chat}`\n• Test message delivered to your other bot.")
            else:
                _reply(chat_id, f"⚠️ Config saved, but test message to other bot failed: `{r.text}`. Please check token & chat ID.")
        except Exception as exc:
            _reply(chat_id, f"⚠️ Config saved, but connection error: `{exc}`.")

    elif cmd == "/feedbackbot":
        cfg_fb = state.get_feedback_bot_config()
        masked_tok = (cfg_fb['token'][:8] + "..." + cfg_fb['token'][-4:]) if len(cfg_fb['token']) > 15 else "Primary Bot Token"
        _reply(
            chat_id,
            f"🤖 *Current Daily Feedback Destination:*\n\n"
            f"• *Bot Token*: `{masked_tok}`\n"
            f"• *Destination Chat ID*: `{cfg_fb['chat_id']}`\n\n"
            f"To change or point to another bot:\n`/setfeedbackbot <BOT_TOKEN> <CHAT_ID>`"
        )

    elif cmd == "/setcallbot":
        if len(args) < 2:
            _reply(chat_id, "⚠️ Usage: `/setcallbot <BOT_TOKEN> <CHAT_ID>`\nExample: `/setcallbot 123456:ABC-DEF 6724880386`")
            return
        c_token = args[0]
        c_chat = args[1]
        state.set_call_bot_config(c_token, c_chat)
        test_url = f"https://api.telegram.org/bot{c_token}/sendMessage"
        try:
            r = requests.post(
                test_url,
                json={
                    "chat_id": c_chat,
                    "text": "🎯 *Call Bot Connection Established!* Xira will deliver hourly manual trading calls (6 scalps + 4 day trades with charts) to this destination.",
                    "parse_mode": "Markdown",
                },
                timeout=8,
            )
            if r.status_code == 200 and r.json().get("ok"):
                _reply(chat_id, f"✅ *Call Bot Successfully Configured!*\n\n• Token: `{c_token[:10]}...`\n• Chat ID: `{c_chat}`\n• Test message delivered to your calls bot.")
            else:
                _reply(chat_id, f"⚠️ Config saved, but test message failed: `{r.text}`. Ensure you started the bot first.")
        except Exception as exc:
            _reply(chat_id, f"⚠️ Config saved, but connection error: `{exc}`.")

    elif cmd in ("/setchannel", "/setgroup", "/addchannel", "/addgroup"):
        # Auto-register the current chat (group or channel) as a destination for calls & alerts
        state.add_call_bot_chat(chat_id)
        chat_type_str = "Channel" if str(chat_id).startswith("-100") else "Group / Chat"
        _reply(
            chat_id,
            f"✅ *{chat_type_str} Registered for Winz Calls & Alerts!*\n\n"
            f"• *Chat ID*: `{chat_id}`\n"
            f"• *Status*: Active destination for 15 hourly calls (6 scalps + 4 day + 5 spot) and live milestone alerts (TP1, TP2, SL, +10% gain, -20% drop).\n\n"
            f"_Tip: Make sure @thewinzbot is promoted to Administrator with 'Post Messages' rights._",
            bot_token=_current_bot_token,
        )

    elif cmd in ("/subscribe", "/sub"):
        state.add_call_bot_chat(chat_id)
        chat_type_str = "Group / Channel" if str(chat_id).startswith("-") else "Private DM"
        _reply(
            chat_id,
            f"🔔 *Subscribed to Winz Signals & Alerts! ({chat_type_str})*\n\n"
            "You are now active to receive:\n"
            "• 🚨 *2-Hour Trade Desk Digests* (4 Scalps + 3 Day Trades with interactive charts)\n"
            "• 💎 *Spot Accumulation Desk* (5 high-conviction macro spots)\n"
            "• 🟢 *Live Trade Trigger & Milestone Alerts* (Triggered & Active, TP1, TP2, Trailing Stops, Macro Reversals)\n\n"
            "_Tip: You can use `/calls` or `/research <COIN>` anytime, or type `/unsubscribe` to stop alerts._",
            bot_token=_current_bot_token,
        )

    elif cmd in ("/unsubscribe", "/unsub"):
        state.remove_call_bot_chat(chat_id)
        chat_type_str = "Group / Channel" if str(chat_id).startswith("-") else "Private DM"
        _reply(
            chat_id,
            f"🔕 *Unsubscribed from Broadcasts ({chat_type_str})*\n\n"
            "Automated scheduled digests and milestone alerts have been turned off for this chat.\n\n"
            "• You can still use on-demand interactive tools: `/calls`, `/research <COIN>`, `/news`, `/sentiment`.\n"
            "• To re-enable automated alerts anytime, simply type `/subscribe`.",
            bot_token=_current_bot_token,
        )

    elif cmd in ("/notifications", "/alerts", "/notif", "/notifs"):
        panel_text, panel_markup = _build_notifications_panel(chat_id)
        _reply(chat_id, panel_text, bot_token=_current_bot_token, reply_markup=panel_markup)

    elif cmd in ("/pausealerts", "/pausenotif", "/pausealert"):
        category = args[0].lower() if args else "all"
        if category in ("triggers", "trigger", "entry", "fill"):
            state.set_chat_notification_pref(chat_id, "triggers", False)
            _reply(chat_id, "⏸ *Trade Trigger Alerts PAUSED*\n\nEntry fill and setup invalidation alerts have been paused for this chat.\nOther alerts remain active. Type `/resumealerts triggers` to re-enable.", bot_token=_current_bot_token)
        elif category in ("reversals", "reversal", "warning", "warnings"):
            state.set_chat_notification_pref(chat_id, "reversals", False)
            _reply(chat_id, "⏸ *Market Reversal Alerts PAUSED*\n\nReversal signals, drawdown warnings, and macro alerts have been paused for this chat.\nType `/resumealerts reversals` to re-enable.", bot_token=_current_bot_token)
        elif category in ("milestones", "milestone", "tp", "stops", "stop"):
            state.set_chat_notification_pref(chat_id, "milestones", False)
            _reply(chat_id, "⏸ *Take Profit & Trailing Stop Alerts PAUSED*\n\nTP hits, Break-Even snaps, and profit locks have been paused for this chat.\nType `/resumealerts tp` to re-enable.", bot_token=_current_bot_token)
        elif category in ("digests", "digest", "hourly", "calls"):
            state.set_chat_notification_pref(chat_id, "digests", False)
            _reply(chat_id, "⏸ *2-Hour Desk Digests PAUSED*\n\nScheduled 2-hour trade desk digests have been paused for this chat.\nType `/resumealerts digests` to re-enable.", bot_token=_current_bot_token)
        else:
            state.set_chat_notification_pref(chat_id, "all", False)
            _reply(chat_id, "⏸ *All Notifications PAUSED*\n\nAll automated trade triggers, reversals, milestone alerts, and 2-hour digests have been paused for this chat.\nType `/resumealerts` to resume or `/notifications` to manage preferences.", bot_token=_current_bot_token)

    elif cmd in ("/resumealerts", "/resumenotif", "/resumealert"):
        category = args[0].lower() if args else "all"
        if category in ("triggers", "trigger", "entry", "fill"):
            state.set_chat_notification_pref(chat_id, "triggers", True)
            _reply(chat_id, "🟢 *Trade Trigger Alerts RESUMED*\n\nYou will now receive live entry fill and setup invalidation alerts.", bot_token=_current_bot_token)
        elif category in ("reversals", "reversal", "warning", "warnings"):
            state.set_chat_notification_pref(chat_id, "reversals", True)
            _reply(chat_id, "🟢 *Market Reversal Alerts RESUMED*\n\nYou will now receive market reversal, drawdown warning, and macro trend alerts.", bot_token=_current_bot_token)
        elif category in ("milestones", "milestone", "tp", "stops", "stop"):
            state.set_chat_notification_pref(chat_id, "milestones", True)
            _reply(chat_id, "🟢 *Take Profit & Trailing Stop Alerts RESUMED*\n\nYou will now receive TP hits, Break-Even snaps, and profit lock alerts.", bot_token=_current_bot_token)
        elif category in ("digests", "digest", "hourly", "calls"):
            state.set_chat_notification_pref(chat_id, "digests", True)
            _reply(chat_id, "🟢 *2-Hour Desk Digests RESUMED*\n\nYou will now receive scheduled 2-hour trade digests with interactive charts.", bot_token=_current_bot_token)
        else:
            state.set_chat_notification_pref(chat_id, "all", True)
            _reply(chat_id, "🟢 *All Notifications RESUMED*\n\nAll automated trade triggers, reversals, milestone alerts, and 2-hour digests are active.", bot_token=_current_bot_token)

    elif cmd == "/pausetriggers":
        state.set_chat_notification_pref(chat_id, "triggers", False)
        _reply(chat_id, "⏸ *Trade Trigger Alerts PAUSED*\n\nEntry fill and setup invalidation alerts are silenced.\nType `/resumetriggers` to re-enable.", bot_token=_current_bot_token)

    elif cmd == "/resumetriggers":
        state.set_chat_notification_pref(chat_id, "triggers", True)
        _reply(chat_id, "🟢 *Trade Trigger Alerts RESUMED*\n\nEntry fill and setup invalidation alerts are active.", bot_token=_current_bot_token)

    elif cmd == "/pausereversals":
        state.set_chat_notification_pref(chat_id, "reversals", False)
        _reply(chat_id, "⏸ *Market Reversal Alerts PAUSED*\n\nReversal signals, drawdown warnings, and macro alerts are silenced.\nType `/resumereversals` to re-enable.", bot_token=_current_bot_token)

    elif cmd == "/resumereversals":
        state.set_chat_notification_pref(chat_id, "reversals", True)
        _reply(chat_id, "🟢 *Market Reversal Alerts RESUMED*\n\nReversal signals, drawdown warnings, and macro alerts are active.", bot_token=_current_bot_token)

    elif cmd in ("/news", "/sentiment", "/fng"):
        target_ticker = args[0].upper().replace("USDT", "") if args else "BTC"
        _reply(chat_id, f"🗞 *Gathering Institutional Sentiment & Live Breaking News for ${target_ticker}...*", bot_token=_current_bot_token)
        import news_sentiment
        def _send_news_async(t: str, cid: str, tok: Optional[str]):
            report = news_sentiment.format_sentiment_report(t)
            _reply(cid, report, bot_token=tok)
        threading.Thread(target=_send_news_async, args=(target_ticker, chat_id, _current_bot_token), daemon=True).start()

    elif cmd == "/callbot":
        c_cfg = state.get_call_bot_config()
        masked_tok = (c_cfg['token'][:8] + "..." + c_cfg['token'][-4:]) if len(c_cfg['token']) > 15 else "Primary Bot Token"
        all_chats_str = ", ".join(f"`{c}`" for c in c_cfg.get("all_chats", [])) or f"`{c_cfg['chat_id']}`"
        _reply(
            chat_id,
            f"📢 *Current Winz Calls Destinations:*\n\n"
            f"• *Bot Token*: `{masked_tok}`\n"
            f"• *Configured Chats/Channels*: {all_chats_str}\n\n"
            f"To register this group/channel directly:\nType `/setchannel` in the group/channel\n\n"
            f"To change master destination:\n`/setcallbot <BOT_TOKEN> <CHAT_ID>`\n"
            f"To dispatch 15 calls now:\n`/hourlycalls`"
        )

    elif cmd in ("/trade", "/papertrade", "/buy", "/sell", "/long", "/short"):
        if not args:
            _reply(
                chat_id,
                "⚠️ *Usage:* `/trade <COIN> [BUY/SELL]`\n\n"
                "Examples:\n"
                "• `/trade BTC` (defaults to BUY/LONG)\n"
                "• `/trade SOL SHORT` or `/trade SOL SELL`\n"
                "• `/trade ETH BUY`",
                bot_token=_current_bot_token,
            )
            return

        ticker = args[0].upper().replace("USDT", "")
        side = "BUY"
        if len(args) > 1:
            s_arg = args[1].upper()
            if s_arg in ("SELL", "SHORT"):
                side = "SELL"
            elif s_arg in ("BUY", "LONG"):
                side = "BUY"
        elif cmd in ("/sell", "/short"):
            side = "SELL"

        from scanner import fetch_live_price
        lp = fetch_live_price(ticker)
        if not lp or lp <= 0:
            _reply(chat_id, f"❌ Unable to fetch live price for `{ticker}`. Please check ticker symbol.", bot_token=_current_bot_token)
            return

        ex = user_store.get_user_exchange(chat_id)
        if ex and ex.get("connected"):
            success, msg = exchange_manager.execute_trade_for_user(chat_id, ticker, side, lp)
            if success:
                _reply(
                    chat_id,
                    f"🚀 *Executed Live Trade on Bybit!*\n\n"
                    f"• *Asset*: `{ticker}USDT`\n"
                    f"• *Side*: `{side}`\n"
                    f"• *Entry Price*: `${lp:,.4f}`\n"
                    f"• *Account*: Bybit (`{ex.get('api_key_masked')}`)\n\n"
                    f"Order placed with exchange-native TP & SL protection.\n_Track via `/positions` or `/myexchange`._",
                    bot_token=_current_bot_token,
                )
            else:
                _reply(chat_id, f"❌ Trade execution failed: {msg}", bot_token=_current_bot_token)
            return

        u = user_store.get_user(chat_id)
        lev = int(user_store.get_user_leverage(chat_id, ticker) or 10)
        # Sizing: $1,000 notional margin per demo trade or 10% of equity
        trade_margin = min(1000.0, max(100.0, float(u.get("paper_equity", 10000.0)) * 0.10))
        notional = trade_margin * lev
        qty = round(notional / lp, 6)

        pos = {
            "ticker": ticker,
            "side": side,
            "entry_price": lp,
            "qty": qty,
            "leverage": lev,
            "margin_usdt": trade_margin,
            "source": "MANUAL",
        }
        user_store.open_user_position(chat_id, pos)
        _reply(
            chat_id,
            f"🚀 *Opened Personal Demo Trade!*\n\n"
            f"• *Asset*: `{ticker}`\n"
            f"• *Side*: `{side}`\n"
            f"• *Entry Price*: `${lp:,.4f}`\n"
            f"• *Leverage*: `{lev}x`\n"
            f"• *Size*: `{qty} contracts` (~${notional:,.2f} notional)\n"
            f"• *Margin Used*: `${trade_margin:,.2f} USDT`\n\n"
            f"_Track via `/positions` or take profit with `/tp`._",
            bot_token=_current_bot_token,
        )

    elif cmd in ("/resetdemo", "/reset"):
        user_store.reset_user_demo(chat_id)
        _reply(
            chat_id,
            "🔄 *Personal Demo Portfolio Reset!*\n\n"
            "• Demo Equity restored to: `$10,000.00 USDT`\n"
            "• Open positions cleared.\n"
            "• Session score reset to 0W / 0L / 0BE.\n\n"
            "Type `/status` or `/trade <COIN>` to start fresh!",
            bot_token=_current_bot_token,
        )

    elif cmd in ("/connect", "/link", "/setapi"):
        if message_id:
            _delete_message(chat_id, message_id, bot_token=_current_bot_token)

        if len(args) < 3:
            _reply(
                chat_id,
                "🔐 *Connect Your Personal Exchange (Bybit)*\n\n"
                "Winz can execute trades automatically on your personal Bybit account with exchange-native TP & SL.\n\n"
                "• *Usage:* `/connect bybit <API_KEY> <API_SECRET>`\n"
                "• *Testnet:* `/connect bybit <API_KEY> <API_SECRET> testnet`\n\n"
                "🛡️ *Security Protections:*\n"
                "1. Your message is deleted immediately upon receipt so credentials never stay in chat.\n"
                "2. Keys are encrypted at rest with AES-256 authenticated encryption.\n"
                "3. We only require *Read & Trade* permissions on Contract/Derivatives. Never enable *Withdrawals*.",
                bot_token=_current_bot_token,
            )
            return

        exchange_name = args[0].lower().strip()
        api_key = args[1].strip()
        api_secret = args[2].strip()
        is_testnet = len(args) > 3 and args[3].lower().strip() in ("testnet", "demo")

        if exchange_name not in ("bybit", "binance"):
            _reply(chat_id, "⚠️ Currently supported exchanges: `bybit`. Use `/connect bybit <API_KEY> <API_SECRET>`.", bot_token=_current_bot_token)
            return

        _reply(chat_id, "🔄 *Verifying credentials with Bybit exchange...*", bot_token=_current_bot_token)

        success, msg, equity = exchange_manager.verify_and_connect_bybit(
            chat_id=chat_id,
            api_key=api_key,
            api_secret=api_secret,
            is_testnet=is_testnet,
            risk_per_trade=50.0,
            auto_trade=False,
        )

        if not success:
            _reply(
                chat_id,
                f"❌ *Failed to Connect Bybit Account*\n\n"
                f"Error: `{msg}`\n\n"
                f"⚠️ *Checklist:*\n"
                f"• Ensure your Bybit API Key has **Read-Write** permissions for **Contract / Derivatives (USDT Perpetual)**.\n"
                f"• Check that IP restriction is set to 'No IP restriction' on Bybit API settings.\n"
                f"• Ensure your API Key and Secret are copied accurately.",
                bot_token=_current_bot_token,
            )
            return

        masked = exchange_manager.mask_key(api_key)
        mode_str = "Testnet" if is_testnet else "Live Mainnet"
        _reply(
            chat_id,
            f"✅ *Bybit Exchange Connected Successfully!*\n\n"
            f"• *Account Balance*: `${equity:,.2f} USDT`\n"
            f"• *Linked API Key*: `{masked}`\n"
            f"• *Execution Mode*: `{mode_str}`\n"
            f"• *Automated Trading*: `🔴 OFF (Type /autotrade on to enable)`\n"
            f"• *Risk per Trade*: `$50.00 USDT` margin\n"
            f"• *Security*: `AES-256 Authenticated Fernet Encryption`\n\n"
            f"🛡️ *Safety First:* Automated trading is OFF by default.\n"
            f"When you are ready for Winz to place trades autonomously, type `/autotrade on`.\n\n"
            f"⚙️ *Helpful Controls:*\n"
            f"• `/autotrade on` — Enable automated trade execution\n"
            f"• `/risk <USDT>` — Change margin size per trade (e.g. `/risk 100`)\n"
            f"• `/myexchange` — View real wallet balance & settings\n"
            f"• `/disconnect` — Remove your API keys and unlink",
            bot_token=_current_bot_token,
        )

    elif cmd in ("/disconnect", "/unlink"):
        user_store.disconnect_user_exchange(chat_id)
        _reply(
            chat_id,
            "🔌 *Exchange Disconnected & API Keys Wiped*\n\n"
            "• Your credentials have been permanently removed.\n"
            "• Winz will no longer place trades on your external account.\n"
            "• You have been returned to the default $10,000 Demo Portfolio.\n\n"
            "_To reconnect anytime, type `/connect bybit <API_KEY> <API_SECRET>`._",
            bot_token=_current_bot_token,
        )

    elif cmd in ("/myexchange", "/myaccount", "/exchange"):
        ex = user_store.get_user_exchange(chat_id)
        if not ex or not ex.get("connected"):
            _reply(
                chat_id,
                "ℹ️ *No Exchange Account Connected*\n\n"
                "You are currently using the default **$10,000 Paper Demo Portfolio**.\n\n"
                "👉 To connect your Bybit account for automated trading:\n"
                "Type: `/connect bybit <API_KEY> <API_SECRET>`",
                bot_token=_current_bot_token,
            )
            return

        client = exchange_manager.get_user_bybit_client(chat_id)
        eq_str = "—"
        avail_str = "—"
        if client:
            try:
                bal = client.get_wallet_balance("USDT")
                eq = float(bal.get("equity", 0.0))
                avail = float(bal.get("available", 0.0))
                eq_str = f"${eq:,.2f} USDT"
                avail_str = f"${avail:,.2f} USDT"
            except Exception:
                eq_str = "(Query failed)"

        auto_str = "🟢 ON (Automated)" if ex.get("auto_trade") else "🔴 OFF (Manual 1-Click Only)"
        mode_str = "Testnet" if ex.get("is_testnet") else "Live Mainnet"
        risk_val = float(ex.get("risk_per_trade_usdt", 50.0))
        masked = ex.get("api_key_masked", "****")

        _reply(
            chat_id,
            f"💼 *Your Connected Exchange Account*\n\n"
            f"• *Exchange*: `Bybit` ({mode_str})\n"
            f"• *Linked API Key*: `{masked}`\n"
            f"• *Wallet Equity*: `{eq_str}`\n"
            f"• *Available Margin*: `{avail_str}`\n"
            f"• *Automated Execution*: {auto_str}\n"
            f"• *Risk Sizing*: `${risk_val:,.2f} USDT` margin per trade\n\n"
            f"_Commands: `/risk <AMOUNT>`, `/autotrade on/off`, `/disconnect`_",
            bot_token=_current_bot_token,
        )

    elif cmd in ("/autotrade", "/auto"):
        if not args:
            ex = user_store.get_user_exchange(chat_id)
            cur = "🟢 ON" if ex.get("auto_trade") else "🔴 OFF"
            _reply(
                chat_id,
                f"⚙️ *Automated Trading Status:* {cur}\n\n"
                f"To toggle:\n"
                f"• `/autotrade on` — Automatically execute triggered signals\n"
                f"• `/autotrade off` — Pause auto-execution",
                bot_token=_current_bot_token,
            )
            return

        sub_arg = args[0].lower().strip()
        new_state = sub_arg in ("on", "true", "enable", "yes", "1")
        user_store.set_user_auto_trade(chat_id, new_state)
        if new_state:
            _reply(chat_id, "🟢 *Automated Execution ENABLED.* Winz will automatically place trades on your account when signals trigger.", bot_token=_current_bot_token)
        else:
            _reply(chat_id, "⏸️ *Automated Execution DISABLED.* Signals will not place orders automatically.", bot_token=_current_bot_token)

    elif cmd in ("/risk", "/setrisk"):
        if not args:
            ex = user_store.get_user_exchange(chat_id)
            cur = float(ex.get("risk_per_trade_usdt", 50.0))
            _reply(
                chat_id,
                f"⚖️ *Current Risk per Trade:* `${cur:,.2f} USDT`\n\n"
                f"To adjust your margin size per trade:\n"
                f"Usage: `/risk <AMOUNT>` (e.g. `/risk 100`)",
                bot_token=_current_bot_token,
            )
            return

        try:
            val = float(args[0].replace("$", "").replace(",", ""))
            if val < 5.0 or val > 50000.0:
                _reply(chat_id, "❌ Risk per trade must be between $5 and $50,000 USDT.", bot_token=_current_bot_token)
                return
            set_val = user_store.set_user_risk_per_trade(chat_id, val)
            _reply(
                chat_id,
                f"✅ *Risk Sizing Updated!*\n\n"
                f"• New Margin per Trade: *${set_val:,.2f} USDT*\n"
                f"• Orders will size position quantity to risk exactly `${set_val:,.2f}` margin.",
                bot_token=_current_bot_token,
            )
        except ValueError:
            _reply(chat_id, f"❌ Invalid amount: `{args[0]}`. Must be a numeric value like `/risk 50`.", bot_token=_current_bot_token)

    elif cmd in ("/setgoal", "/goalset"):
        if len(args) < 3:
            _reply(
                chat_id,
                "🎯 *Usage:* `/setgoal <START_USDT> <TARGET_USDT> <DAYS> [LEVERAGE] [MAX_DAILY_LOSS%]`\n\n"
                "• *Example:* `/setgoal 10 100 30` (Takes $10 to $100 in 30 days @ 5x leverage)\n"
                "• *Custom:* `/setgoal 10 100 30 5 5` (With 5x leverage and 5% max daily loss)\n\n"
                "_Winz will calculate your required daily growth rate and recommend adaptive risk-managed trades._",
                bot_token=_current_bot_token,
            )
            return

        try:
            start_cap = float(args[0].replace("$", "").replace(",", ""))
            target_cap = float(args[1].replace("$", "").replace(",", ""))
            days = int(args[2])
            lev = int(args[3]) if len(args) > 3 else 5
            max_loss = float(args[4]) if len(args) > 4 else 5.0

            if start_cap <= 0 or target_cap <= start_cap:
                _reply(chat_id, "❌ Target capital must be greater than starting capital.", bot_token=_current_bot_token)
                return
            if days < 1 or days > 365:
                _reply(chat_id, "❌ Timeframe must be between 1 and 365 days.", bot_token=_current_bot_token)
                return
            if lev < 1 or lev > 25:
                _reply(chat_id, "❌ Goal leverage must be between 1x and 25x.", bot_token=_current_bot_token)
                return
            if max_loss < 1.0 or max_loss > 20.0:
                _reply(chat_id, "❌ Maximum daily loss must be between 1% and 20% of account equity.", bot_token=_current_bot_token)
                return

            import goal_tracker
            goal = user_store.set_user_goal(
                chat_id=chat_id,
                starting_capital=start_cap,
                target_capital=target_cap,
                timeframe_days=days,
                max_daily_loss_pct=max_loss,
                leverage=lev,
            )

            cur_eq = start_cap
            ex = user_store.get_user_exchange(chat_id)
            if ex and ex.get("connected"):
                client = exchange_manager.get_user_bybit_client(chat_id)
                if client:
                    try:
                        bal = client.get_wallet_balance("USDT")
                        cur_eq = float(bal.get("equity", start_cap))
                    except Exception:
                        pass
            else:
                u = user_store.get_user(chat_id)
                cur_eq = float(u.get("paper_equity", start_cap))

            card = goal_tracker.format_goal_progress_card(goal, cur_eq, morning_equity=cur_eq)
            _reply(
                chat_id,
                f"🚀 *Target Growth Flight Plan Initialized!*\n\n{card}",
                bot_token=_current_bot_token,
            )
        except Exception as exc:
            _reply(chat_id, f"❌ Error initializing goal: {exc}", bot_token=_current_bot_token)

    elif cmd in ("/goal", "/progress", "/flightplan"):
        goal = user_store.get_user_goal(chat_id)
        if not goal:
            _reply(
                chat_id,
                "ℹ️ *No Active Target Growth Flight Plan*\n\n"
                "Launch your compounding growth plan with:\n"
                "`/setgoal <START_USDT> <TARGET_USDT> <DAYS> [LEVERAGE] [MAX_DAILY_LOSS%]`\n\n"
                "_Example: `/setgoal 10 100 30` (Takes $10 to $100 in 30 days @ 5x leverage with 5% daily loss cap)_",
                bot_token=_current_bot_token,
            )
            return

        cur_eq = float(goal.get("starting_capital", 10.0))
        ex = user_store.get_user_exchange(chat_id)
        if ex and ex.get("connected"):
            client = exchange_manager.get_user_bybit_client(chat_id)
            if client:
                try:
                    bal = client.get_wallet_balance("USDT")
                    cur_eq = float(bal.get("equity", cur_eq))
                except Exception:
                    pass
        else:
            u = user_store.get_user(chat_id)
            cur_eq = float(u.get("paper_equity", cur_eq))

        user_store.update_goal_morning_equity_if_new_day(chat_id, cur_eq)
        goal = user_store.get_user_goal(chat_id)
        m_eq = float(goal.get("morning_equity", cur_eq))

        import goal_tracker
        card = goal_tracker.format_goal_progress_card(goal, cur_eq, morning_equity=m_eq)
        _reply(chat_id, card, bot_token=_current_bot_token)

    elif cmd in ("/goalplan", "/recommend", "/dailytrade"):
        goal = user_store.get_user_goal(chat_id)
        if not goal:
            _reply(
                chat_id,
                "ℹ️ *No Active Target Goal*\n\n"
                "Set your target goal first with:\n"
                "`/setgoal <START> <TARGET> <DAYS>` (e.g. `/setgoal 10 100 30`)",
                bot_token=_current_bot_token,
            )
            return

        cur_eq = float(goal.get("starting_capital", 10.0))
        ex = user_store.get_user_exchange(chat_id)
        if ex and ex.get("connected"):
            client = exchange_manager.get_user_bybit_client(chat_id)
            if client:
                try:
                    bal = client.get_wallet_balance("USDT")
                    cur_eq = float(bal.get("equity", cur_eq))
                except Exception:
                    pass
        else:
            u = user_store.get_user(chat_id)
            cur_eq = float(u.get("paper_equity", cur_eq))

        user_store.update_goal_morning_equity_if_new_day(chat_id, cur_eq)
        goal = user_store.get_user_goal(chat_id)
        m_eq = float(goal.get("morning_equity", cur_eq))

        import goal_tracker, scanner
        btc_regime = scanner.get_btc_macro_regime()
        candidates = state.get_last_signals()
        if not candidates:
            c, e, _ = scanner.run_scan("scalp")
            candidates = c + e

        rec = goal_tracker.recommend_goal_trade(goal, cur_eq, m_eq, candidates, btc_regime=btc_regime)
        if not rec.get("has_trade"):
            _reply(
                chat_id,
                f"🛡️ *Goal Flight Plan Recommendation: STAND ASIDE*\n\n"
                f"• *Status*: `{rec.get('status')}`\n"
                f"• *Market Regime*: `BTC {btc_regime}`\n"
                f"• *Reason*: {rec.get('message')}\n\n"
                f"_Capital preservation is prioritized over forcing low-quality trades._",
                bot_token=_current_bot_token,
            )
            return

        t = rec["trade"]
        rep_msg = (
            f"🎯 *TODAY'S TARGET-GROWTH TRADE RECOMMENDATION* 🎯\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• *Asset*: `${t['ticker']}` (*{t['side']}* | {t['tf']})\n"
            f"• *Conviction*: `{t['conviction']}% Alignment`\n"
            f"• *Entry Price*: `${t['entry_price']:,.4f}`\n"
            f"• *Entry Band*: `{t['entry_range']}`\n"
            f"• *Target 1 (TP)*: `${t['tp1']:,.4f}` (`+{t['tp_dist_pct']:.1f}%`)\n"
            f"• *Stop Loss (SL)*: `${t['sl']:,.4f}` (`-{t['sl_dist_pct']:.1f}%`)\n"
            f"• *Risk:Reward*: `1:{t['rr']:.1f}`\n\n"
            f"⚖️ *Goal Sizing & Risk Calibration*:\n"
            f"• *Leverage*: `{t['leverage']}x Isolated`\n"
            f"• *Recommended Margin*: `${t['recommended_margin']:.2f} USDT`\n"
            f"• *Notional Position Size*: `${t['recommended_notional']:.2f} USDT`\n"
            f"• *Potential Gain (at TP)*: `+${t['est_gain_usdt']:.2f}` (`+{t['est_return_on_equity_pct']:.1f}%` on equity)\n"
            f"• *Max Downside (at SL)*: `-${t['est_loss_at_sl']:.2f}` (Safe within 5% daily cap)\n\n"
            f"🧠 *Institutional Thesis*:\n"
            f"{t['reason']}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👉 _If auto-trade is enabled, this trade executes automatically when the entry band fills._"
        )
        _reply(chat_id, rep_msg, bot_token=_current_bot_token)

    elif cmd in ("/cancelgoal", "/stopgoal"):
        res = user_store.cancel_user_goal(chat_id)
        if res:
            _reply(chat_id, "🛑 *Target Growth Goal CANCELLED.* Your trading parameters have returned to default.", bot_token=_current_bot_token)
        else:
            _reply(chat_id, "ℹ️ You do not currently have an active target goal.", bot_token=_current_bot_token)

    elif cmd in ("/calls", "/activecalls", "/tracked"):
        summary_text, inline_keyboard = format_active_calls_summary()
        markup = {"inline_keyboard": inline_keyboard} if inline_keyboard else None
        _reply(chat_id, summary_text, bot_token=_current_bot_token, reply_markup=markup)

    elif cmd in ("/hourlycalls", "/sendcalls"):
        _reply(chat_id, "🚀 *Generating and Dispatching 2-Hour Calls Digest (4 Scalps + 3 Day Trades with Interactive Buttons)...*")
        import market_research
        threading.Thread(
            target=market_research.dispatch_hourly_calls,
            kwargs={"force": True},
            daemon=True,
            name="hourly-calls-manual",
        ).start()

    elif cmd in ("/spot", "/spotcalls"):
        _reply(chat_id, "💎 *Compiling and Dispatching 5 Spot Accumulation Setups (1D Macro Timeframe)...*", bot_token=_current_bot_token)
        import market_research
        threading.Thread(
            target=market_research.dispatch_spot_calls,
            kwargs={"bot_token": _current_bot_token, "chat_id": chat_id},
            daemon=True,
            name="spot-calls-manual",
        ).start()

    elif cmd in ("/research", "/call", "/thesis"):
        import market_research

        req_mode = "both"
        target_ticker = "BTC"

        if args:
            first_arg = args[0].strip().lower()
            if first_arg in ("day and scalp", "day & scalp", "both", "scalp and day", "scalp & day"):
                req_mode = "both"
                if len(args) > 1:
                    target_ticker = args[1].upper().replace("USDT", "")
            elif first_arg in ("scalp", "15m"):
                req_mode = "scalp"
                if len(args) > 1:
                    target_ticker = args[1].upper().replace("USDT", "")
            elif first_arg in ("day", "4h"):
                req_mode = "day"
                if len(args) > 1:
                    target_ticker = args[1].upper().replace("USDT", "")
            elif first_arg in ("spot", "1d", "daily"):
                req_mode = "spot"
                if len(args) > 1:
                    target_ticker = args[1].upper().replace("USDT", "")
            else:
                target_ticker = args[0].upper().replace("USDT", "")
                if len(args) > 1:
                    second_arg = args[1].strip().lower()
                    if second_arg in ("day and scalp", "both"):
                        req_mode = "both"
                    elif second_arg in ("scalp", "15m"):
                        req_mode = "scalp"
                    elif second_arg in ("day", "4h"):
                        req_mode = "day"
                    elif second_arg in ("spot", "1d", "daily"):
                        req_mode = "spot"

        mode_desc = "Spot (1D)" if req_mode == "spot" else ("Day & Scalp" if req_mode == "both" else ("Scalp (15m)" if req_mode == "scalp" else "Day (4h)"))
        _reply(chat_id, f"🔬 *Compiling {mode_desc} Institutional Research & Charts for {target_ticker}...*")

        def _do_research(t: str, mode: str, cid: str):
            tok = _current_bot_token or cfg.telegram_token
            # 1. Spot setup (1D)
            if mode == "spot":
                sig_spot = market_research.build_spot_signal(t)
                state.set_last_researched_signal(sig_spot)
                market_research.send_research_with_chart(sig_spot, bot_token=tok, chat_id=cid, tf_minutes=1440)
                return

            # 2. Scalp setup (15m)
            if mode in ("both", "scalp"):
                sig_scalp = market_research.build_signal_for_timeframe(t, tf_minutes=15)
                state.set_last_researched_signal(sig_scalp)
                market_research.send_research_with_chart(sig_scalp, bot_token=tok, chat_id=cid, tf_minutes=15)

            # 3. Day setup (4h)
            if mode in ("both", "day"):
                sig_day = market_research.build_signal_for_timeframe(t, tf_minutes=240)
                state.set_last_researched_signal(sig_day)
                market_research.send_research_with_chart(sig_day, bot_token=tok, chat_id=cid, tf_minutes=240)

        threading.Thread(target=_do_research, args=(target_ticker, req_mode, chat_id), daemon=True, name=f"research-{target_ticker}").start()

    elif cmd in ("/tweet", "/twitter"):
        import market_research
        target_ticker = args[0].upper().replace("USDT", "") if args else ""
        sig = None
        if target_ticker:
            sig = market_research.build_signal_for_timeframe(target_ticker, tf_minutes=15)
        else:
            sig = state.get_last_researched_signal() or state.get_last_signal("BTC")
            if not sig:
                sig = market_research.build_signal_for_timeframe("BTC", tf_minutes=15)

        tok = _current_bot_token or cfg.telegram_token
        market_research.send_twitter_post(sig, bot_token=tok, chat_id=chat_id)

    elif cmd in ("/cancelorder", "/cancel"):
        if not args:
            _reply(chat_id, f"⚠️ Usage: `/cancelorder <ASSET1> <ASSET2> ...`\nExample: `/cancelorder BTC ETH`{_paused_footer()}")
            return

        results = []
        total_canceled = 0

        for raw_t in args:
            ticker = raw_t.upper().replace("USDT", "")
            lin_sym = bybit_linear_symbol(ticker)
            spot_sym = bybit_spot_symbol(ticker)

            lin_ids = engine.client.cancel_all_orders(category="linear", symbol=lin_sym)
            spot_ids = engine.client.cancel_all_orders(category="spot", symbol=spot_sym)
            state.remove_working_orders(ticker)

            count = len(lin_ids) + len(spot_ids)
            total_canceled += count
            if count > 0:
                results.append(f"• *{ticker}*: Cancelled {count} resting order(s)")
            else:
                results.append(f"• *{ticker}*: No resting orders found")

        engine._sync_with_bybit()
        bal = engine.client.get_wallet_balance("USDT")
        avail = bal.get("available", 0.0)

        lines = [
            f"🚫 *Cancel Order Result (Total: {total_canceled} orders cancelled):*",
            "",
        ]
        lines.extend(results)
        lines.append(f"\n💼 *Available Margin*: `${avail:,.2f} USDT`")
        _reply(chat_id, "\n".join(lines) + _paused_footer())

    elif cmd in ("/cancelallorders", "/cancelall"):
        _reply(chat_id, f"🧹 *Cancelling all resting orders across Bybit...*{_paused_footer()}")
        lin_ids = engine.client.cancel_all_orders(category="linear")
        spot_ids = engine.client.cancel_all_orders(category="spot")
        cleared_local = state.remove_working_orders()

        total = len(lin_ids) + len(spot_ids)
        engine._sync_with_bybit()
        bal = engine.client.get_wallet_balance("USDT")
        avail = bal.get("available", 0.0)

        _reply(
            chat_id,
            f"🧹 *Cancelled All Resting Orders on Bybit!*\n\n"
            f"• *Linear Perpetual Orders Cancelled*: `{len(lin_ids)}`\n"
            f"• *Spot Orders Cancelled*: `{len(spot_ids)}`\n"
            f"• *Total Orders Cancelled*: `{total}`\n"
            f"• *Local Working State Cleared*: `{cleared_local}`\n"
            f"• *Available Margin Unlocked*: `${avail:,.2f} USDT`\n\n"
            f"*(Active open positions remain protected by exchange TP/SL)*"
            + _paused_footer()
        )

    elif cmd in ("/watchlist", "/setups", "/scanwatch"):
        _reply(chat_id, "🔍 *Scanning market for institutional setups, key shelves & macro context...*")
        def _do_watchlist_scan():
            import scanner
            from config import CORE_TICKERS
            btc_regime = scanner.get_btc_macro_regime()
            
            actionable = []
            watchlist = []
            
            for t in CORE_TICKERS:
                try:
                    s = scanner.analyze_ticker(t, "scalp")
                    if not s:
                        continue
                    if s.get("side") in ("BUY", "SELL"):
                        actionable.append(s)
                    else:
                        watchlist.append(s)
                except Exception:
                    pass
            
            lines = [
                "🎯 *Market Scan: Best Setups & Smart Watchlist*",
                f"• *BTC Macro Regime*: `{btc_regime}`",
                "• *Execution Filter*: `Strict 4H Trend Confluence + Regime Lock Active`",
                ""
            ]
            
            if actionable:
                lines.append(f"🔥 *Actionable High-Conviction Setups ({len(actionable)}):*")
                for s in actionable:
                    t = s["ticker"]
                    side = s["side"]
                    conv = s.get("conviction", 0.0)
                    rr = s.get("rr", 0.0)
                    e_low = fmt_dollar(s.get("entry_low"))
                    e_high = fmt_dollar(s.get("entry_high"))
                    mark = fmt_dollar(s.get("live_price"))
                    sl = fmt_dollar(s.get("sl"))
                    tp1 = fmt_dollar(s.get("tp1"))
                    lines.append(
                        f"• *{t}* `{side}` (Conviction: `{conv:.0f}%` | R:R: `1:{rr:.2f}`)\n"
                        f"  Entry: `{e_low} – {e_high}` | Mark: `{mark}`\n"
                        f"  Stop: `{sl}` | TP1: `{tp1}`\n"
                    )
            else:
                lines.append("🛡️ *Actionable Entries*: `None currently` (Counter-trend dip buying blocked by BTC Bear Regime).\n")
            
            lines.append("👀 *Curated Watchlist (Key Shelves & Invalidation Levels):*")
            # Pull top 4 key assets
            focus_assets = ["BTC", "SOL", "ETH", "SUI"]
            for t in focus_assets:
                df = scanner.fetch_ohlcv(t, tf_minutes=15, limit=60)
                if df is not None:
                    struct = scanner.detect_shelves_and_edges(df)
                    live_p = fmt_dollar(struct["live_price"])
                    dem_l, dem_h = struct["demand_shelf"]
                    sup_l, sup_h = struct["supply_shelf"]
                    trend_4h = scanner.get_4h_trend(t)
                    lines.append(
                        f"• *{t}* (`{live_p}` | 4H: `{trend_4h}`)\n"
                        f"  Demand Support: `{fmt_dollar(dem_l)} – {fmt_dollar(dem_h)}`\n"
                        f"  Supply Resistance: `{fmt_dollar(sup_l)} – {fmt_dollar(sup_h)}`\n"
                    )
            
            lines.append("_Less noise. Better context. Disciplined execution._")
            _reply(chat_id, "\n".join(lines) + _paused_footer())
        
        threading.Thread(target=_do_watchlist_scan, daemon=True, name="tg-watchlist-scan").start()

    elif cmd in ("/gencode", "/createcode", "/newcode"):
        if not is_admin(chat_id):
            return
        custom_code = None
        max_uses = 0
        label = "Admin Generated"
        if args:
            first = args[0].strip()
            if first.isdigit():
                max_uses = int(first)
                if len(args) > 1:
                    label = " ".join(args[1:])
            else:
                custom_code = first
                if len(args) > 1 and args[1].isdigit():
                    max_uses = int(args[1])
                    if len(args) > 2:
                        label = " ".join(args[2:])
                elif len(args) > 1:
                    label = " ".join(args[1:])

        ok, resp_msg, rec = access_manager.create_access_code(
            code=custom_code,
            max_uses=max_uses,
            label=label,
            created_by=chat_id,
        )
        if ok:
            lim_str = f"{rec['max_uses']} redemptions" if rec['max_uses'] > 0 else "Unlimited"
            _reply(
                chat_id,
                f"🔑 *New Winz Access Code Generated!*\n\n"
                f"• *Code:* `{rec['code']}`\n"
                f"• *Usage Limit:* `{lim_str}`\n"
                f"• *Label:* `{rec['label']}`\n"
                f"• *Created At:* `{rec['created_at'][:19].replace('T', ' ')} UTC`\n\n"
                f"Share with user:\n"
                f"👉 `/access {rec['code']}`\n"
                f"Direct Link: `https://t.me/thewinzbot?start={rec['code']}`",
                bot_token=_current_bot_token,
            )
        else:
            _reply(chat_id, f"❌ {resp_msg}", bot_token=_current_bot_token)

    elif cmd in ("/codes", "/listcodes"):
        if not is_admin(chat_id):
            return
        all_codes = access_manager.list_access_codes()
        if not all_codes:
            _reply(chat_id, "ℹ️ No access codes configured yet.", bot_token=_current_bot_token)
            return
        lines = [
            "📋 *Winz Configured Access Codes*",
            f"Total Codes: `{len(all_codes)}`",
            "",
        ]
        for c_name, c_info in all_codes.items():
            status_icon = "🟢 Active" if c_info.get("is_active", True) else "🔴 Revoked"
            max_u = c_info.get("max_uses", 0)
            used_u = c_info.get("used_count", 0)
            usage_str = f"{used_u}/{max_u}" if max_u > 0 else f"{used_u}/∞"
            lbl = c_info.get("label", "Standard")
            lines.append(f"• `{c_name}` — {status_icon} | Uses: `{usage_str}` | _{lbl}_")
        _reply(chat_id, "\n".join(lines), bot_token=_current_bot_token)

    elif cmd in ("/revokecode",):
        if not is_admin(chat_id):
            return
        if not args:
            _reply(chat_id, "Usage: `/revokecode <CODE>`", bot_token=_current_bot_token)
            return
        ok, resp_msg = access_manager.revoke_access_code(args[0])
        _reply(chat_id, f"✅ {resp_msg}" if ok else f"❌ {resp_msg}", bot_token=_current_bot_token)

    elif cmd in ("/grant",):
        if not is_admin(chat_id):
            return
        if not args:
            _reply(chat_id, "Usage: `/grant <CHAT_ID> [LABEL]`", bot_token=_current_bot_token)
            return
        target_cid = args[0].strip()
        lbl = " ".join(args[1:]) if len(args) > 1 else "Direct Admin Grant"
        access_manager.grant_user_access(target_cid, label=lbl)
        _reply(chat_id, f"✅ Access granted directly to `{target_cid}`.", bot_token=_current_bot_token)

    elif cmd in ("/revokeuser",):
        if not is_admin(chat_id):
            return
        if not args:
            _reply(chat_id, "Usage: `/revokeuser <CHAT_ID>`", bot_token=_current_bot_token)
            return
        target_cid = args[0].strip()
        access_manager.revoke_user_access(target_cid)
        _reply(chat_id, f"🚫 Access revoked for user `{target_cid}`.", bot_token=_current_bot_token)

    elif cmd in ("/accessusers", "/users"):
        if not is_admin(chat_id):
            return
        users = access_manager.list_authorized_users()
        lines = [
            "👥 *Winz Authorized Users List*",
            f"Total Users: `{len(users)}`",
            "",
        ]
        if not users:
            lines.append("ℹ️ No authorized users in database yet.")
        else:
            for u_cid, u_info in users.items():
                st = "🟢 Active" if u_info.get("is_authorized", True) else "🔴 Revoked"
                c_used = u_info.get("code", "N/A")
                lines.append(f"• `{u_cid}`: {st} | Code: `{c_used}`")
        _reply(chat_id, "\n".join(lines), bot_token=_current_bot_token)

    else:
        _reply(chat_id, f"Unknown command: `{cmd}`. Type `/help` for available commands.")



def _poll_single_bot(token: str, allowed_chat_id: Optional[str] = None, scan_trigger_fn=None) -> None:
    last_update_id = 0
    call_cfg = state.get_call_bot_config()
    c_tok = call_cfg.get("token")

    try:
        init_url = f"https://api.telegram.org/bot{token}/getUpdates"
        init_r = requests.get(init_url, timeout=10)
        if init_r.status_code == 200:
            results = init_r.json().get("result", [])
            if results:
                last_update_id = results[-1]["update_id"]
    except Exception:
        pass

    while _bot_running:
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 15}
            resp = requests.get(url, params=params, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                for update in data.get("result", []):
                    last_update_id = update["update_id"]

                    # 1. Handle bot membership updates (my_chat_member) when added to a group/channel
                    my_cm = update.get("my_chat_member")
                    if my_cm:
                        new_status = my_cm.get("new_chat_member", {}).get("status")
                        if new_status in ("member", "administrator"):
                            g_chat_id = str(my_cm.get("chat", {}).get("id"))
                            if token == c_tok:
                                state.add_call_bot_chat(g_chat_id)
                                log.info("Winz bot added to group/channel %s (status=%s). Auto-registered destination.", g_chat_id, new_status)
                                _reply(
                                    g_chat_id,
                                    f"🚨 *Winz 2-Hour A+ Quality Desk Connected!*\n\n"
                                    f"• *Chat ID*: `{g_chat_id}`\n"
                                    f"• *Status*: Active destination for top A+ trade calls & live alerts.\n\n"
                                    f"_Tip: Ensure @thewinzbot is promoted to Admin with Post Messages rights._",
                                    bot_token=token,
                                )

                    # 2. Handle button callbacks (InlineKeyboard taps)
                    cb_query = update.get("callback_query")
                    if cb_query:
                        cb_id = cb_query.get("id")
                        cb_data = cb_query.get("data", "")
                        from_user = cb_query.get("from", {})
                        cb_msg = cb_query.get("message") or {}
                        cb_chat_id = str(cb_msg.get("chat", {}).get("id") or from_user.get("id"))
                        cb_msg_id = cb_msg.get("message_id")

                        # Handle message dismissal / close IMMEDIATELY before authorization check
                        if cb_data in ("close_msg", "dismiss_msg", "delete_msg"):
                            try:
                                ack_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                                requests.post(ack_url, json={"callback_query_id": cb_id}, timeout=4)
                            except Exception:
                                pass
                            if cb_msg_id:
                                try:
                                    del_url = f"https://api.telegram.org/bot{token}/deleteMessage"
                                    r_del = requests.post(del_url, json={"chat_id": cb_chat_id, "message_id": cb_msg_id}, timeout=5)
                                    if not (r_del.status_code == 200 and r_del.json().get("ok")):
                                        edit_url = f"https://api.telegram.org/bot{token}/editMessageText"
                                        requests.post(edit_url, json={
                                            "chat_id": cb_chat_id,
                                            "message_id": cb_msg_id,
                                            "text": "🗑️ _Message closed._",
                                            "parse_mode": "Markdown",
                                        }, timeout=5)
                                except Exception as exc:
                                    log.debug("Failed to delete closed message: %s", exc)
                            continue

                        if not access_manager.is_user_authorized(cb_chat_id):
                            try:
                                ack_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                                requests.post(
                                    ack_url,
                                    json={
                                        "callback_query_id": cb_id,
                                        "text": "🔒 Access code required! Type /access <CODE> in chat to unlock.",
                                        "show_alert": True,
                                    },
                                    timeout=6,
                                )
                            except Exception:
                                pass
                            continue

                        # Handle interactive notification preference toggles
                        if cb_data.startswith("toggle_notif_"):
                            action = cb_data.replace("toggle_notif_", "")
                            if action == "pause_all":
                                state.set_chat_notification_pref(cb_chat_id, "all", False)
                                ack_text = "⏸ All notifications paused"
                            elif action == "resume_all":
                                state.set_chat_notification_pref(cb_chat_id, "all", True)
                                ack_text = "🟢 All notifications resumed"
                            else:
                                curr = state.get_chat_notification_prefs(cb_chat_id).get(action, True)
                                state.set_chat_notification_pref(cb_chat_id, action, not curr)
                                ack_text = f"{action.capitalize()}: {'ON 🟢' if not curr else 'OFF ⏸'}"

                            try:
                                ack_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                                requests.post(ack_url, json={"callback_query_id": cb_id, "text": ack_text}, timeout=4)
                            except Exception:
                                pass

                            panel_text, panel_markup = _build_notifications_panel(cb_chat_id)
                            try:
                                edit_url = f"https://api.telegram.org/bot{token}/editMessageText"
                                requests.post(edit_url, json={
                                    "chat_id": cb_chat_id,
                                    "message_id": cb_msg_id,
                                    "text": panel_text,
                                    "parse_mode": "Markdown",
                                    "reply_markup": panel_markup,
                                }, timeout=6)
                            except Exception as exc:
                                log.debug("Failed to update notification panel: %s", exc)
                            continue

                        # Acknowledge callback query to Telegram
                        try:
                            ack_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                            requests.post(ack_url, json={"callback_query_id": cb_id}, timeout=6)
                        except Exception:
                            pass

                        if cb_data.startswith("call_view_"):
                            parts = cb_data.split("_")
                            if len(parts) >= 4:
                                ticker = parts[2]
                                tf_str = parts[3]
                                is_spot = tf_str.lower() in ("spot", "1d")
                                tf_min = 1440 if is_spot else (240 if tf_str == "4h" else 15)
                                badge = "💎 Spot" if is_spot else tf_str
                                _reply(cb_chat_id, f"🔬 *Compiling live chart & analysis for ${ticker} ({badge})...*", bot_token=token)

                                def _async_call_view(t: str, tf_m: int, c_id: str, b_tok: str, spot_mode: bool):
                                    import market_research
                                    if spot_mode:
                                        sig = market_research.build_spot_signal(t)
                                    else:
                                        sig = market_research.build_signal_for_timeframe(t, tf_minutes=tf_m)
                                    market_research.send_research_with_chart(sig, bot_token=b_tok, chat_id=c_id, tf_minutes=tf_m)

                                threading.Thread(
                                    target=_async_call_view,
                                    args=(ticker, tf_min, cb_chat_id, token, is_spot),
                                    daemon=True,
                                    name=f"cb-{ticker}",
                                ).start()
                        continue

                    # 3. Handle messages & channel posts
                    msg = update.get("message") or update.get("edited_message") or update.get("channel_post") or update.get("edited_channel_post")
                    if not msg:
                        continue

                    chat_id = str(msg.get("chat", {}).get("id"))
                    text = msg.get("text", "")

                    if allowed_chat_id and chat_id != str(allowed_chat_id):
                        log.warning("Ignoring message from unauthorized chat_id: %s on bot %s", chat_id, token[:10])
                        continue

                    # Auto-register group if command or message received on Winz bot
                    if token == c_tok and (chat_id.startswith("-") or chat_id.startswith("-100")):
                        state.add_call_bot_chat(chat_id)

                    raw_text = text.strip()
                    lower_text = raw_text.lower()
                    is_dm = not (chat_id.startswith("-") or chat_id.startswith("-100"))
                    is_unauthorized = not access_manager.is_user_authorized(chat_id)
                    is_cmd = (
                        raw_text.startswith("/")
                        or (is_dm and is_unauthorized)
                        or "convert to twitter" in lower_text
                        or "tweet" in lower_text
                        or ("research" in lower_text and "(" in lower_text and ")" in lower_text)
                        or "scan the market" in lower_text
                        or "best setups" in lower_text
                        or "watchlist" in lower_text
                    )
                    if is_cmd:
                        msg_id = msg.get("message_id")
                        log.info("Received command on bot %s: %s from %s", token[:10], raw_text, chat_id)
                        handle_command(raw_text, chat_id, scan_trigger_fn=scan_trigger_fn, bot_token=token, message_id=msg_id)
        except Exception as exc:
            log.debug("Telegram polling exception for %s: %s", token[:10], exc)

        time.sleep(1)


def _poll_updates_loop(scan_trigger_fn=None) -> None:
    log.info("Telegram interactive command listeners starting")

    polled_tokens = set()

    # 1. Primary bot listener
    if cfg.telegram_token:
        polled_tokens.add(cfg.telegram_token)
        t_prim = threading.Thread(
            target=_poll_single_bot,
            kwargs={
                "token": cfg.telegram_token,
                "allowed_chat_id": str(cfg.telegram_chat_id) if cfg.telegram_chat_id else None,
                "scan_trigger_fn": scan_trigger_fn,
            },
            daemon=True,
            name="tg-poller-primary",
        )
        t_prim.start()

    # 2. Feedback bot listener (if distinct)
    try:
        fb_cfg = state.get_feedback_bot_config()
        fb_tok = fb_cfg.get("token")
        fb_cid = fb_cfg.get("chat_id")
        if fb_tok and fb_tok not in polled_tokens:
            polled_tokens.add(fb_tok)
            t_fb = threading.Thread(
                target=_poll_single_bot,
                kwargs={
                    "token": fb_tok,
                    "allowed_chat_id": str(fb_cid) if fb_cid else None,
                    "scan_trigger_fn": scan_trigger_fn,
                },
                daemon=True,
                name="tg-poller-feedback",
            )
            t_fb.start()
    except Exception as exc:
        log.warning("Could not launch feedback bot polling listener: %s", exc)

    # 3. Dedicated Calls Bot listener (Winz - @thewinzbot)
    try:
        call_cfg = state.get_call_bot_config()
        c_tok = call_cfg.get("token")
        c_cid = call_cfg.get("chat_id")
        if c_tok and c_tok not in polled_tokens:
            polled_tokens.add(c_tok)
            t_call = threading.Thread(
                target=_poll_single_bot,
                kwargs={
                    "token": c_tok,
                    "allowed_chat_id": None, # Allow interactive button taps & public commands on Winz
                    "scan_trigger_fn": scan_trigger_fn,
                },
                daemon=True,
                name="tg-poller-callbot",
            )
            t_call.start()
    except Exception as exc:
        log.warning("Could not launch call bot polling listener: %s", exc)

    while _bot_running:
        time.sleep(2)


BOT_COMMANDS = [
    {"command": "watchlist", "description": "🎯 Best setups to watch now & smart shelves"},
    {"command": "status", "description": "Balance, equity & open positions"},
    {"command": "positions", "description": "Active Bybit positions & targets"},
    {"command": "tp", "description": "🎯 Close all open trades in profit"},
    {"command": "derisk", "description": "Close winning trades & trim losers 50%"},
    {"command": "dailyreport", "description": "📅 Daily 24h analytics report & chart"},
    {"command": "hourlyreport", "description": "⏱️ Hourly analytics breakdown & chart"},
    {"command": "weeklyreport", "description": "📆 Weekly 7d analytics report & chart"},
    {"command": "monthlyreport", "description": "🗓️ Monthly 30d analytics report & chart"},
    {"command": "calls", "description": "📋 View active triggered trades & pending setups"},
    {"command": "hourlycalls", "description": "🎯 Dispatch 15 hourly manual calls (6 scalp + 4 day + 5 spot)"},
    {"command": "spot", "description": "💎 Dispatch 5 spot accumulation setups with buttons"},
    {"command": "news", "description": "🗞️ Live breaking crypto news & macro sentiment"},
    {"command": "sentiment", "description": "🌡️ Crypto Fear & Greed Index + tone analysis"},
    {"command": "setchannel", "description": "📢 Register current group/channel for Winz calls"},
    {"command": "setcallbot", "description": "📢 Connect external bot for manual calls"},
    {"command": "callbot", "description": "View current calls bot destination"},
    {"command": "feedback", "description": "🧠 Daily feedback, worst/best assets & learning"},
    {"command": "research", "description": "🔬 Deep thesis & research note (e.g. /research BTC)"},
    {"command": "tweet", "description": "🐦 Convert research setup into 280-char Twitter post"},
    {"command": "feedbackbot", "description": "View current feedback bot destination"},
    {"command": "setfeedbackbot", "description": "Connect another bot for daily feedback"},
    {"command": "probation", "description": "🧪 Quarantined & 50% probation assets"},
    {"command": "leverage", "description": "Set leverage (e.g. /leverage BTC 10)"},
    {"command": "existingleverage", "description": "Show leverage used for each asset"},
    {"command": "scan", "description": "Trigger immediate scalp scan"},
    {"command": "avoid", "description": "Blacklist assets from trading"},
    {"command": "allow", "description": "Restore asset to active trading"},
    {"command": "avoided", "description": "List currently avoided assets"},
    {"command": "drop", "description": "Close position & blacklist coin"},
    {"command": "broadcast", "description": "📢 Broadcast announcement/update to all users"},
    {"command": "signal", "description": "🎯 Broadcast official trade call to all users"},
    {"command": "onlyday", "description": "🟡 Trade day trades only (pause scalps)"},
    {"command": "onlyscalp", "description": "🟡 Trade scalps only (pause day trades)"},
    {"command": "pausescalp", "description": "⏸ Pause automated scalp cycle"},
    {"command": "pauseday", "description": "⏸ Pause day trade cycle"},
    {"command": "resumescalp", "description": "🟢 Resume automated scalp cycle"},
    {"command": "resumeday", "description": "🟢 Resume day trade cycle"},
    {"command": "pause", "description": "Pause automated order execution"},
    {"command": "resume", "description": "Resume automated execution"},
    {"command": "onspot", "description": "🟢 Enable Spot trade execution"},
    {"command": "offspot", "description": "🔴 Disable Spot trades (Perps only)"},
    {"command": "cancelorder", "description": "Cancel resting orders for asset(s)"},
    {"command": "cancelallorders", "description": "Cancel all resting orders on Bybit"},
    {"command": "close", "description": "Market close a specific ticker"},
    {"command": "closeall", "description": "Emergency close all positions"},
    {"command": "gencode", "description": "🔑 Generate new access code"},
    {"command": "codes", "description": "📋 List access codes & usage stats"},
    {"command": "revokecode", "description": "🚫 Revoke an access code"},
    {"command": "grant", "description": "🟢 Directly authorize a user/group"},
    {"command": "revokeuser", "description": "🔴 Revoke access for a user"},
    {"command": "users", "description": "👥 List all authorized users"},
    {"command": "help", "description": "Show command guide and help"},
]


WINZ_PUBLIC_COMMANDS = [
    {"command": "access", "description": "🔑 Activate or verify your access code"},
    {"command": "calls", "description": "📋 View active triggered trades & pending setups"},
    {"command": "hourlycalls", "description": "🎯 View 15 trade desk calls (6 scalp + 4 day + 5 spot)"},
    {"command": "spot", "description": "💎 View 5 spot accumulation setups with buttons"},
    {"command": "status", "description": "📊 View your personal $10,000 demo portfolio"},
    {"command": "positions", "description": "📈 View your personal active positions"},
    {"command": "trade", "description": "🚀 Open a personal paper trade (e.g. /trade BTC)"},
    {"command": "tp", "description": "🎯 Bank profits on all winning positions"},
    {"command": "derisk", "description": "🛡️ Bank winners 100% & trim losers 50%"},
    {"command": "close", "description": "✂️ Close a specific personal position"},
    {"command": "closeall", "description": "🚨 Close all your personal positions"},
    {"command": "resetdemo", "description": "🔄 Reset demo equity to $10,000"},
    {"command": "leverage", "description": "⚡ Set custom leverage for a coin"},
    {"command": "existingleverage", "description": "📊 View active leverage settings"},
    {"command": "avoid", "description": "🚫 Blacklist coins from your trades"},
    {"command": "allow", "description": "🟢 Restore blacklisted coins"},
    {"command": "avoided", "description": "📋 Show your avoided coins"},
    {"command": "onlyday", "description": "🟡 Trade day trades only (pause scalps)"},
    {"command": "onlyscalp", "description": "🟡 Trade scalps only (pause day trades)"},
    {"command": "pausescalp", "description": "⏸ Pause personal scalp execution"},
    {"command": "pauseday", "description": "⏸ Pause personal day trade execution"},
    {"command": "resumescalp", "description": "🟢 Resume personal scalp execution"},
    {"command": "resumeday", "description": "🟢 Resume personal day trade execution"},
    {"command": "pause", "description": "⏸️ Pause your personal trading"},
    {"command": "resume", "description": "🟢 Resume your personal trading"},
    {"command": "research", "description": "🔬 Deep thesis & chart markup (e.g. /research BTC)"},
    {"command": "tweet", "description": "🐦 Convert research into 280-char Twitter/X post"},
    {"command": "news", "description": "🗞️ Live breaking crypto news & macro sentiment"},
    {"command": "sentiment", "description": "🌡️ Crypto Fear & Greed Index + tone analysis"},
    {"command": "subscribe", "description": "🔔 Enable automated digests & milestone alerts in this chat"},
    {"command": "unsubscribe", "description": "🔕 Disable automated broadcast alerts in this chat"},
    {"command": "connect", "description": "🔐 Connect Bybit account (/connect bybit <KEY> <SECRET>)"},
    {"command": "myexchange", "description": "💼 View connected exchange balance & status"},
    {"command": "autotrade", "description": "⚙️ Toggle automated trade execution on/off"},
    {"command": "risk", "description": "⚖️ Set margin size per trade (e.g. /risk 50)"},
    {"command": "disconnect", "description": "🔌 Disconnect exchange and wipe API keys"},
    {"command": "help", "description": "Show command guide and help"},
]


def register_bot_commands() -> bool:
    """
    Register bot commands with Telegram so typing '/' pops up the autocomplete command menu.
    Registers public commands on Winz bot and full administrative commands on master bots.
    """
    call_cfg = state.get_call_bot_config()
    c_tok = call_cfg.get("token")

    # 1. Register Winz Bot commands (Public default + Admin scope)
    if c_tok:
        url = f"https://api.telegram.org/bot{c_tok}/setMyCommands"
        try:
            # Default public scope
            requests.post(url, json={"commands": WINZ_PUBLIC_COMMANDS, "scope": {"type": "default"}}, timeout=10)
            # Admin chat scope (if admin chat configured)
            if cfg.telegram_chat_id:
                try:
                    admin_chat_int = int(cfg.telegram_chat_id)
                    requests.post(url, json={"commands": BOT_COMMANDS, "scope": {"type": "chat", "chat_id": admin_chat_int}}, timeout=10)
                except Exception:
                    pass
            menu_btn_url = f"https://api.telegram.org/bot{c_tok}/setChatMenuButton"
            requests.post(menu_btn_url, json={"menu_button": {"type": "commands"}}, timeout=10)
            log.info("Registered %d public commands on Winz bot %s...", len(WINZ_PUBLIC_COMMANDS), c_tok[:10])
        except Exception as exc:
            log.warning("Telegram setMyCommands error for Winz bot %s: %s", c_tok[:10], exc)

    # 2. Register master admin bot commands
    other_tokens = []
    if cfg.telegram_token and cfg.telegram_token != c_tok:
        other_tokens.append(cfg.telegram_token)
    try:
        fb_cfg = state.get_feedback_bot_config()
        fb_tok = fb_cfg.get("token")
        if fb_tok and fb_tok not in other_tokens and fb_tok != c_tok:
            other_tokens.append(fb_tok)
    except Exception:
        pass

    for token in other_tokens:
        url = f"https://api.telegram.org/bot{token}/setMyCommands"
        try:
            requests.post(url, json={"commands": BOT_COMMANDS}, timeout=10)
            menu_btn_url = f"https://api.telegram.org/bot{token}/setChatMenuButton"
            requests.post(menu_btn_url, json={"menu_button": {"type": "commands"}}, timeout=10)
            log.info("Registered %d admin commands on bot %s...", len(BOT_COMMANDS), token[:10])
        except Exception as exc:
            log.warning("Telegram setMyCommands error for bot %s: %s", token[:10], exc)

    return True


def start_telegram_listener(scan_trigger_fn=None) -> None:
    global _bot_running
    if not cfg.telegram_token:
        log.info("Telegram token not set, interactive listener disabled")
        return

    # Register popup command menu with Telegram Bot API
    register_bot_commands()

    if not _bot_running:
        _bot_running = True
        t = threading.Thread(
            target=_poll_updates_loop,
            args=(scan_trigger_fn,),
            daemon=True,
            name="tg-listener",
        )
        t.start()


def stop_telegram_listener() -> None:
    global _bot_running
    _bot_running = False
