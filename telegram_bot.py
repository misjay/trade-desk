"""
telegram_bot.py — Interactive two-way Telegram command handler for Xira.
Allows user to check status, view positions, scan, pause/resume, and close positions.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import requests

from config import cfg, bybit_linear_symbol
from notifier import send_text, fmt_dollar
import state

log = logging.getLogger(__name__)

_bot_running = False
_last_update_id = 0


def _reply(chat_id: str, text: str) -> None:
    if not cfg.telegram_token:
        return
    url = f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        requests.post(url, json=payload, timeout=8)
    except Exception as exc:
        log.warning("Telegram reply failed: %s", exc)


def handle_command(cmd_text: str, chat_id: str, scan_trigger_fn=None) -> None:
    parts = cmd_text.strip().split()
    if not parts:
        return
    cmd = parts[0].lower().split("@")[0]
    args = parts[1:]

    import engine

    if cmd in ("/start", "/help"):
        msg = (
            "🤖 *Xira Autonomous Trade Desk Control*\n\n"
            "• `/status` — Balance, equity, open positions & win stats\n"
            "• `/positions` — Detailed active positions with live PnL & targets\n"
            "• `/derisk` — 🛡️ Close 100% of winning trades & trim 50% of losers\n"
            "• `/avoid <COINS>` — 🚫 Blacklist assets (e.g. `/avoid DOGE PEPE`)\n"
            "• `/allow <COINS>` — 🟢 Restore assets (e.g. `/allow DOGE`)\n"
            "• `/avoided` — 📋 Show all currently avoided assets\n"
            "• `/drop <COIN>` — ✂️ Close position & immediately add to avoid list\n"
            "• `/scan` — Trigger an immediate market scalp scan\n"
            "• `/pause` — Pause opening new orders\n"
            "• `/resume` — Resume automatic order execution\n"
            "• `/close <TICKER>` — Market close position for ticker (e.g. `/close HYPE`)\n"
            "• `/closeall` — 🚨 Emergency market close all open positions\n"
            "• `/help` — Show this command list\n\n"
            "Web Dashboard: `http://localhost:8765`"
        )
        _reply(chat_id, msg)

    elif cmd in ("/status", "/balance"):
        bal = engine.client.get_wallet_balance("USDT")
        eq = bal.get("equity", 0.0) if not engine.client.is_paper else state.get_equity()
        avail = bal.get("available", 0.0) if not engine.client.is_paper else eq
        open_pos = state.get_open_positions()
        active_count = len(open_pos)
        stats = state.get_session_stats()
        paused = state.is_paused()

        mode_str = cfg.trade_mode.upper()
        pause_str = "⏸ PAUSED" if paused else "🟢 ACTIVE"

        msg = (
            f"📊 *Xira Trade Desk Status*\n\n"
            f"• *Mode*: `{mode_str}` ({cfg.effective_demo_env})\n"
            f"• *Execution*: {pause_str}\n"
            f"• *Total Equity*: `${eq:,.2f} USDT`\n"
            f"• *Available*: `${avail:,.2f} USDT`\n"
            f"• *Open Positions*: `{active_count}`\n"
            f"• *Session Score*: `{stats.get('wins', 0)}W / {stats.get('losses', 0)}L / {stats.get('breakevens', 0)}BE`\n"
            f"• *Session Realized PnL*: `${stats.get('total_pnl_usdt', 0.0):+,.2f} USDT`"
        )
        _reply(chat_id, msg)

    elif cmd == "/positions":
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

    elif cmd == "/pause":
        state.set_paused(True)
        _reply(chat_id, "⏸ *Automated execution PAUSED.* Existing positions remain protected by exchange TP/SL.")

    elif cmd == "/resume":
        state.set_paused(False)
        _reply(chat_id, "🟢 *Automated execution RESUMED.* Scanning and placing orders.")

    elif cmd == "/scan":
        _reply(chat_id, "🔍 *Triggering scalp scan across Core 24 and Extras...*")
        if scan_trigger_fn:
            threading.Thread(target=scan_trigger_fn, daemon=True).start()
        else:
            _reply(chat_id, "Scan will execute on next scheduled cycle.")

    elif cmd == "/close":
        if not args:
            _reply(chat_id, "⚠️ Specify ticker to close: e.g. `/close HYPE`")
            return
        target_ticker = args[0].upper()
        sym = bybit_linear_symbol(target_ticker)
        active = engine.client.get_active_positions()
        matched = [p for p in active if p["symbol"] == sym]
        if not matched:
            _reply(chat_id, f"❌ No active linear position found for {target_ticker} on Bybit.")
            return

        pos = matched[0]
        close_side = "Sell" if pos["side"].lower() == "buy" else "Buy"
        res = engine.client.close_position_market(sym, close_side, pos["size"])
        if res:
            _reply(chat_id, f"✅ *Closed {target_ticker} position on Bybit* ({pos['size']} contracts).")
            engine._sync_with_bybit()
        else:
            _reply(chat_id, f"❌ Failed to close {target_ticker} on Bybit. Check logs.")

    elif cmd in ("/closeall", "/panic"):
        active = engine.client.get_active_positions()
        if not active:
            _reply(chat_id, "ℹ️ No active linear positions to close on Bybit.")
            return

        closed_count = 0
        for pos in active:
            sym = pos["symbol"]
            close_side = "Sell" if pos["side"].lower() == "buy" else "Buy"
            res = engine.client.close_position_market(sym, close_side, pos["size"])
            if res:
                closed_count += 1

        _reply(chat_id, f"🚨 *EMERGENCY CLOSE ALL:* Market closed {closed_count}/{len(active)} positions on Bybit.")
        engine._sync_with_bybit()

    elif cmd in ("/derisk", "/harvest"):
        active = engine.client.get_active_positions(category="linear")
        if not active:
            _reply(chat_id, "ℹ️ No active linear positions on Bybit to derisk.")
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

        _reply(chat_id, "\n".join(lines))

    elif cmd in ("/avoid", "/block"):
        if not args:
            _reply(chat_id, "⚠️ Specify tickers to avoid: e.g. `/avoid DOGE PEPE XLM`")
            return
        avoided = state.add_to_avoid_list(args)
        _reply(chat_id, f"🚫 *Added to Avoid List.* Scanner will ignore:\n`{', '.join(avoided)}`")

    elif cmd in ("/allow", "/unavoid", "/unblock"):
        if not args:
            _reply(chat_id, "⚠️ Specify tickers to allow: e.g. `/allow DOGE PEPE`")
            return
        updated = state.remove_from_avoid_list(args)
        current_str = f"`{', '.join(updated)}`" if updated else "_None (all assets allowed)_"
        _reply(chat_id, f"✅ *Removed from Avoid List.* Currently avoided:\n{current_str}")

    elif cmd in ("/avoided", "/blacklist"):
        current = state.get_avoid_list()
        if not current:
            _reply(chat_id, "ℹ️ Avoid list is empty. All Core 24 and Extras are actively scanned.")
        else:
            _reply(chat_id, f"🚫 *Currently Avoided Assets ({len(current)}):*\n`{', '.join(current)}`\n\nUse `/allow <TICKER>` to resume trading them.")

    elif cmd == "/drop":
        if not args:
            _reply(chat_id, "⚠️ Specify ticker to drop & avoid: e.g. `/drop XLM`")
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
        _reply(chat_id, f"✂️ *Dropped {target_ticker}:* {close_msg}Added to Avoid List.")

    else:
        _reply(chat_id, f"Unknown command: `{cmd}`. Type `/help` for available commands.")


def _poll_updates_loop(scan_trigger_fn=None) -> None:
    global _last_update_id, _bot_running
    log.info("Telegram interactive command listener started")

    # Get initial update offset so old historical messages aren't re-executed
    try:
        init_url = f"https://api.telegram.org/bot{cfg.telegram_token}/getUpdates"
        init_r = requests.get(init_url, timeout=10)
        if init_r.status_code == 200:
            results = init_r.json().get("result", [])
            if results:
                _last_update_id = results[-1]["update_id"]
    except Exception:
        pass

    while _bot_running:
        if not cfg.telegram_token:
            time.sleep(10)
            continue

        try:
            url = f"https://api.telegram.org/bot{cfg.telegram_token}/getUpdates"
            params = {"offset": _last_update_id + 1, "timeout": 15}
            resp = requests.get(url, params=params, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                for update in data.get("result", []):
                    _last_update_id = update["update_id"]
                    msg = update.get("message") or update.get("edited_message")
                    if not msg:
                        continue

                    chat_id = str(msg.get("chat", {}).get("id"))
                    text = msg.get("text", "")

                    if cfg.telegram_chat_id and chat_id != str(cfg.telegram_chat_id):
                        log.warning("Ignoring message from unauthorized chat_id: %s", chat_id)
                        continue

                    if text.startswith("/"):
                        log.info("Received Telegram command: %s from %s", text, chat_id)
                        handle_command(text, chat_id, scan_trigger_fn=scan_trigger_fn)
        except Exception as exc:
            log.debug("Telegram polling exception: %s", exc)

        time.sleep(1)


BOT_COMMANDS = [
    {"command": "status", "description": "Balance, equity & open positions"},
    {"command": "positions", "description": "Active Bybit positions & targets"},
    {"command": "derisk", "description": "Close winning trades & trim losers 50%"},
    {"command": "scan", "description": "Trigger immediate scalp scan"},
    {"command": "avoid", "description": "Blacklist assets from trading"},
    {"command": "allow", "description": "Restore asset to active trading"},
    {"command": "avoided", "description": "List currently avoided assets"},
    {"command": "drop", "description": "Close position & blacklist coin"},
    {"command": "pause", "description": "Pause automated order execution"},
    {"command": "resume", "description": "Resume automated execution"},
    {"command": "close", "description": "Market close a specific ticker"},
    {"command": "closeall", "description": "Emergency close all positions"},
    {"command": "help", "description": "Show command guide and help"},
]


def register_bot_commands() -> bool:
    """
    Register bot commands with Telegram so typing '/' pops up the autocomplete command menu.
    """
    if not cfg.telegram_token:
        return False
    url = f"https://api.telegram.org/bot{cfg.telegram_token}/setMyCommands"
    try:
        r = requests.post(url, json={"commands": BOT_COMMANDS}, timeout=10)
        if r.status_code == 200 and r.json().get("ok"):
            log.info("Registered %d Telegram commands with setMyCommands", len(BOT_COMMANDS))
            menu_btn_url = f"https://api.telegram.org/bot{cfg.telegram_token}/setChatMenuButton"
            requests.post(menu_btn_url, json={"menu_button": {"type": "commands"}}, timeout=10)
            return True
        else:
            log.warning("Telegram setMyCommands failed: %s", r.text)
    except Exception as exc:
        log.warning("Telegram setMyCommands error: %s", exc)
    return False


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
