"""
telegram_bot.py — Interactive two-way Telegram command handler for Xira.
Allows user to check status, view positions, scan, pause/resume, and close positions.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from config import cfg, bybit_linear_symbol, bybit_spot_symbol, CORE_TICKERS
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


def _paused_footer() -> str:
    """Returns a footer reminder if automated execution is currently paused."""
    if state.is_paused():
        return "\n\n_(⏸ Execution remains PAUSED. Will NOT resume automatically until you type `/resume`.)_"
    return ""


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
            "• `/tp` — 🎯 Close all open trades currently in profit\n"
            "• `/derisk` — 🛡️ Close 100% of winning trades & trim 50% of losers\n"
            "• `/hourlyreport` — ⏱️ Hourly analytics breakdown & chart\n"
            "• `/dailyreport` — 📅 Daily (24h) performance & chart\n"
            "• `/weeklyreport` — 📆 Weekly (7d) performance & chart\n"
            "• `/monthlyreport` — 🗓️ Monthly (30d) performance & chart\n"
            "• `/feedback` — 🧠 Daily intelligence feedback: most lost/profit assets & learning\n"
            "• `/research <COIN>` — 🔬 Institutional research note: thesis, what to watch, verdict\n"
            "• `/setfeedbackbot <TOKEN> <CHAT_ID>` — 🤖 Connect another bot for daily feedback\n"
            "• `/feedbackbot` — 📋 View current feedback bot destination\n"
            "• `/probation` — 🧪 View 24h quarantined & 50% probation assets\n"
            "• `/leverage <COIN> <VAL>` — ⚡ Set leverage (e.g. `/leverage BTC 10`)\n"
            "• `/existingleverage` — 📊 Show leverage used for each asset\n"
            "• `/avoid <COINS>` — 🚫 Blacklist assets (e.g. `/avoid DOGE PEPE`)\n"
            "• `/allow <COINS>` — 🟢 Restore assets (e.g. `/allow DOGE`)\n"
            "• `/avoided` — 📋 Show all currently avoided assets\n"
            "• `/drop <COIN>` — ✂️ Close position & immediately add to avoid list\n"
            "• `/scan` — Trigger an immediate market scalp scan\n"
            "• `/pause` — Pause opening new orders\n"
            "• `/resume` — Resume automatic order execution\n"
            "• `/onspot` — 🟢 Enable Spot order execution\n"
            "• `/offspot` — 🔴 Disable Spot orders (Perps only)\n"
            "• `/cancelorder <COINS>` — 🚫 Cancel resting limit orders (e.g. `/cancelorder BTC`)\n"
            "• `/cancelallorders` — 🧹 Cancel ALL resting limit orders on Bybit\n"
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
        spot_on = state.is_spot_enabled()

        mode_str = cfg.trade_mode.upper()
        pause_str = "⏸ PAUSED" if paused else "🟢 ACTIVE"
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
        _reply(
            chat_id,
            "⏸ *Automated execution PAUSED.*\n\n"
            "• Existing positions remain protected by exchange TP/SL.\n"
            "• You can execute any manual instruction (`/scan`, `/tp`, `/derisk`, `/close`, `/cancelorder`, etc.) while paused.\n"
            "• Automated scanning & new order placement will NOT resume automatically until you explicitly type `/resume`."
        )

    elif cmd == "/resume":
        state.set_paused(False)
        _reply(chat_id, "🟢 *Automated execution RESUMED.* Triggering immediate market scan and order placement now...")
        if scan_trigger_fn:
            threading.Thread(target=scan_trigger_fn, daemon=True).start()

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
        if not args:
            _reply(chat_id, f"⚠️ Specify tickers to avoid: e.g. `/avoid DOGE PEPE XLM`{_paused_footer()}")
            return
        avoided = state.add_to_avoid_list(args)
        _reply(chat_id, f"🚫 *Added to Avoid List.* Scanner will ignore:\n`{', '.join(avoided)}`{_paused_footer()}")

    elif cmd in ("/allow", "/unavoid", "/unblock"):
        if not args:
            _reply(chat_id, f"⚠️ Specify tickers to allow: e.g. `/allow DOGE PEPE`{_paused_footer()}")
            return
        updated = state.remove_from_avoid_list(args)
        current_str = f"`{', '.join(updated)}`" if updated else "_None (all assets allowed)_"
        _reply(chat_id, f"✅ *Removed from Avoid List.* Currently avoided:\n{current_str}{_paused_footer()}")

    elif cmd in ("/avoided", "/blacklist"):
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

    elif cmd in ("/research", "/call", "/thesis"):
        target_ticker = args[0].upper().replace("USDT", "") if args else "BTC"
        _reply(chat_id, f"🔬 *Compiling Institutional Market Research for {target_ticker}...*")
        import market_research

        def _do_research(t: str):
            sig = state.get_last_signal(t)
            if not sig:
                from scanner import fetch_live_price
                lp = fetch_live_price(t) or 100.0
                sig = {
                    "ticker": t,
                    "side": "BUY",
                    "trade_type": "scalp",
                    "tf": "15m",
                    "entry_low": lp * 0.995,
                    "entry_high": lp * 1.002,
                    "tp1": lp * 1.025,
                    "tp2": lp * 1.050,
                    "sl": lp * 0.985,
                    "rr": 2.0,
                    "structure": f"Demand zone near ${lp:,.2f}",
                    "reason": "Institutional order block retest",
                    "live_price": lp,
                }
            note = market_research.generate_market_research(sig)
            _reply(chat_id, note + _paused_footer())
            market_research.send_call_research_to_feedback_bot(sig)

        threading.Thread(target=_do_research, args=(target_ticker,), daemon=True, name=f"research-{target_ticker}").start()

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
    {"command": "tp", "description": "🎯 Close all open trades in profit"},
    {"command": "derisk", "description": "Close winning trades & trim losers 50%"},
    {"command": "dailyreport", "description": "📅 Daily 24h analytics report & chart"},
    {"command": "hourlyreport", "description": "⏱️ Hourly analytics breakdown & chart"},
    {"command": "weeklyreport", "description": "📆 Weekly 7d analytics report & chart"},
    {"command": "monthlyreport", "description": "🗓️ Monthly 30d analytics report & chart"},
    {"command": "feedback", "description": "🧠 Daily feedback, worst/best assets & learning"},
    {"command": "research", "description": "🔬 Deep thesis & research note (e.g. /research BTC)"},
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
    {"command": "pause", "description": "Pause automated order execution"},
    {"command": "resume", "description": "Resume automated execution"},
    {"command": "onspot", "description": "🟢 Enable Spot trade execution"},
    {"command": "offspot", "description": "🔴 Disable Spot trades (Perps only)"},
    {"command": "cancelorder", "description": "Cancel resting orders for asset(s)"},
    {"command": "cancelallorders", "description": "Cancel all resting orders on Bybit"},
    {"command": "close", "description": "Market close a specific ticker"},
    {"command": "closeall", "description": "Emergency close all positions"},
    {"command": "help", "description": "Show command guide and help"},
]


def register_bot_commands() -> bool:
    """
    Register bot commands with Telegram so typing '/' pops up the autocomplete command menu.
    Registers on both primary bot and secondary feedback bot (if configured).
    """
    tokens = []
    if cfg.telegram_token:
        tokens.append(cfg.telegram_token)
    try:
        fb_cfg = state.get_feedback_bot_config()
        fb_tok = fb_cfg.get("token")
        if fb_tok and fb_tok not in tokens:
            tokens.append(fb_tok)
    except Exception:
        pass

    if not tokens:
        return False

    all_success = True
    for token in tokens:
        url = f"https://api.telegram.org/bot{token}/setMyCommands"
        try:
            r = requests.post(url, json={"commands": BOT_COMMANDS}, timeout=10)
            if r.status_code == 200 and r.json().get("ok"):
                log.info("Registered %d Telegram commands with setMyCommands on %s...", len(BOT_COMMANDS), token[:10])
                menu_btn_url = f"https://api.telegram.org/bot{token}/setChatMenuButton"
                requests.post(menu_btn_url, json={"menu_button": {"type": "commands"}}, timeout=10)
            else:
                log.warning("Telegram setMyCommands failed for %s...: %s", token[:10], r.text)
                all_success = False
        except Exception as exc:
            log.warning("Telegram setMyCommands error for %s...: %s", token[:10], exc)
            all_success = False
    return all_success


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
