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
from typing import Dict, List, Optional, Tuple

import requests

from config import cfg, bybit_linear_symbol, bybit_spot_symbol
from scanner import fmt_dollar
import state

log = logging.getLogger(__name__)

_tracker_running = False
_last_known_btc_regime: Optional[str] = None
_last_macro_reversal_alert_time: float = 0.0
_triggered_alerts_cache: Dict[str, float] = {}


def detect_trade_reversal(
    call: dict,
    live_price: float,
    gain_pct: float,
    drop_pct: float,
    entry_mid: float,
) -> Optional[Tuple[str, List[str]]]:
    """
    Evaluate structural, momentum, and macro signals for an active trade.
    Returns (reason_string, list_of_actionable_advice_strings) if a reversal is triggered, else None.
    """
    ticker = call.get("ticker", "").upper()
    side = call.get("side", "BUY").upper()
    tf = call.get("tf", "15m")

    # 1. Profit Retracement (Severe Peak Retracement)
    peak_gain = float(call.get("peak_gain_pct", 0.0))
    if peak_gain >= 1.8 and gain_pct <= (peak_gain * 0.40) and gain_pct < 1.0:
        reason = f"Profit Fade: Trade pulled back from +{peak_gain:.1f}% peak to {gain_pct:+.1f}%"
        if gain_pct > 0.3:
            advice = [
                f"👉 *Lock In Remaining Profit:* Bank remaining {gain_pct:+.1f}% profit at market price.",
                f"👉 *Trail Stop-Loss to Breakeven:* Move stop to entry ({fmt_dollar(entry_mid)}) immediately to eliminate downside risk.",
                "👉 *Market Note:* Momentum has stalled; do not let a winning setup become a stop-loss hit.",
            ]
        else:
            advice = [
                f"👉 *Exit at Breakeven / Market:* Price has given back gains and is threatening entry.",
                "👉 *Preserve Capital:* Close setup at current mark or move stop to entry level.",
                "👉 *Invalidate Setup:* Momentum reversed against the setup; cancel remaining targets.",
            ]
        return reason, advice

    # 2. Macro BTC Momentum Reversal
    try:
        from scanner import check_btc_momentum_gate
        gate_ok, gate_reason = check_btc_momentum_gate(side)
        if not gate_ok:
            if side == "SELL":
                reason = f"Macro BTC Short Squeeze: {gate_reason}"
                advice = [
                    "👉 *Short Squeeze Alert:* Bitcoin is expanding upward, pulling altcoins higher against our short.",
                    f"👉 *Action:* Tighten Stop-Loss to Entry Breakeven ({fmt_dollar(entry_mid)}) or scale out 50%-75% of position at market.",
                    "👉 *Discipline:* Do NOT average down into expanding upward momentum.",
                ]
            else:  # BUY
                reason = f"Macro BTC Flush: {gate_reason}"
                advice = [
                    "👉 *Liquidation Flush Alert:* Bitcoin is cascading downward, breaking support levels across alts.",
                    "👉 *Action:* Close Long position at market or tighten stop immediately to avoid knife-catching.",
                    "👉 *Preserve Cash:* Wait for Bitcoin stabilization before re-entering longs.",
                ]
            return reason, advice
    except Exception as exc:
        log.debug("Error checking BTC momentum gate in reversal check: %s", exc)

    # 3. 15m Candle Rejection with Volume Spike
    try:
        import scanner
        df = scanner.fetch_ohlcv(ticker, tf_minutes=15, limit=6)
        if df is not None and len(df) >= 3:
            c_open = float(df["open"].iloc[-2])
            c_close = float(df["close"].iloc[-2])
            c_high = float(df["high"].iloc[-2])
            c_low = float(df["low"].iloc[-2])
            vol_latest = float(df["volume"].iloc[-2])
            vol_avg = float(df["volume"].iloc[-5:-2].mean()) if len(df) >= 5 else 1.0
            vol_spike = (vol_latest / vol_avg) if vol_avg > 0 else 1.0
            body = abs(c_close - c_open)

            if vol_spike >= 2.2:
                if side == "BUY" and (c_close < c_open or (c_high - max(c_open, c_close)) >= 1.5 * body):
                    reason = f"Bearish Supply Rejection: Reversal wick with {vol_spike:.1f}x volume surge"
                    advice = [
                        "👉 *Institutional Distribution:* Heavy seller volume rejecting upside expansion.",
                        f"👉 *Action:* Lock in partial gains now and tighten Stop-Loss toward {fmt_dollar(live_price)}.",
                        "👉 *Caution:* Invalidate setup if local support shelf fails.",
                    ]
                    return reason, advice
                elif side == "SELL" and (c_close > c_open or (min(c_open, c_close) - c_low) >= 1.5 * body):
                    reason = f"Bullish Demand Absorption: Lower absorption wick with {vol_spike:.1f}x volume surge"
                    advice = [
                        "👉 *Buyer Absorption:* Aggressive buying absorbing orders off support.",
                        f"👉 *Action:* Lock in partial short gains now and trail Stop-Loss to Breakeven.",
                        "👉 *Caution:* Invalidate setup if local supply ceiling is reclaimed.",
                    ]
                    return reason, advice
    except Exception as exc:
        log.debug("Error checking 15m candle structure for %s: %s", ticker, exc)

    return None


_recent_sent_hashes: Dict[str, float] = {}


def _send_alert(msg: str, category: str = "milestones") -> None:
    """Send alert to Telegram calls destination and Discord without duplicates, honoring chat preferences."""
    global _recent_sent_hashes
    now_ts = time.time()
    
    # Clean cache older than 60s
    _recent_sent_hashes = {k: v for k, v in _recent_sent_hashes.items() if now_ts - v < 60.0}
    
    # Hash check to block exact duplicate alert across multiple rapid ticks
    msg_hash = hash(msg.strip())
    if msg_hash in _recent_sent_hashes and (now_ts - _recent_sent_hashes[msg_hash]) < 30.0:
        log.debug("Suppressing duplicate alert dispatched within 30s: %s", msg[:40])
        return
    _recent_sent_hashes[msg_hash] = now_ts

    call_cfg = state.get_call_bot_config()
    tok = call_cfg.get("token") or cfg.call_bot_token or cfg.telegram_token
    raw_chats = call_cfg.get("all_chats")
    if not raw_chats:
        cid = call_cfg.get("chat_id") or cfg.call_bot_chat_id or cfg.telegram_chat_id
        raw_chats = [cid] if cid else []

    # Strict Deduplication: preserve order, unique string IDs only
    seen_chats = set()
    unique_chats: List[str] = []
    for c in raw_chats:
        c_str = str(c).strip()
        if c_str and c_str not in seen_chats:
            seen_chats.add(c_str)
            unique_chats.append(c_str)

    if tok and unique_chats:
        url = f"https://api.telegram.org/bot{tok}/sendMessage"
        for target_chat in unique_chats:
            # Check if this chat has enabled this alert category
            if not state.is_chat_alert_enabled(target_chat, category):
                log.debug("Chat %s has paused alert category '%s'. Skipping alert.", target_chat, category)
                continue

            try:
                requests.post(url, json={
                    "chat_id": target_chat,
                    "text": msg,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                    "reply_markup": {
                        "inline_keyboard": [
                            [{"text": "❌ Close Alert", "callback_data": "close_msg"}]
                        ]
                    },
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

        # 4. Check Entry Fill Status or Pre-Entry Setup Invalidation
        if not entry_filled:
            # Check Stale Setup TTL Expiration (Scalp > 2h, Day > 12h)
            max_ttl_seconds = 7200 if str(tf).lower() in ("15m", "15", "scalp") else 43200
            if age_seconds >= max_ttl_seconds:
                updates["is_closed"] = True
                updates["status"] = "EXPIRED"
                updates["invalidated_at"] = now_utc.isoformat()
                log.info("Tracked unfilled setup %s %s expired after %d hours", ticker, side, int(age_seconds // 3600))
                state.update_tracked_call(cid, updates)
                continue

            # Check if price invalidated the setup BEFORE filling entry (e.g. blew past SL before ever entering band)
            pre_entry_invalidated = (live_price <= sl) if side == "BUY" else (live_price >= sl)
            if pre_entry_invalidated:
                updates["is_closed"] = True
                updates["status"] = "INVALIDATED"
                updates["invalidated_at"] = now_utc.isoformat()
                log.info("Tracked setup %s %s invalidated prior to entry fill at mark $%.4f (SL=$%.4f)", ticker, side, live_price, sl)
                _send_alert(
                    f"⚠️ *{type_prefix}SETUP INVALIDATED (UNFILLED)* ⚠️\n\n"
                    f"• *Asset*: `${ticker}` ({tf}){age_str}\n"
                    f"• *Side*: `{side}`\n"
                    f"• *Planned Entry Band*: `{fmt_dollar(entry_low)} – {fmt_dollar(entry_high)}`\n"
                    f"• *Invalidation Level*: `{fmt_dollar(sl)}`\n"
                    f"• *Current Mark*: `{fmt_dollar(live_price)}`\n\n"
                    f"🚫 *Outcome:* Price blew past structure before entry was filled. Setup cancelled and removed from active tracking.",
                    category="triggers"
                )
                state.update_tracked_call(cid, updates)
                continue

            if side == "BUY" and live_price <= (entry_high * 1.0005):
                entry_hit = True
            elif side == "SELL" and live_price >= (entry_low * 0.9995):
                entry_hit = True
            else:
                entry_hit = False

            if not entry_hit:
                # Still waiting for price to retest entry band
                continue

            # Pre-Trigger BTC Momentum Gatekeeper: cancel unfilled altcoin call if BTC is surging/dumping against it
            if ticker.upper() != "BTC":
                try:
                    from scanner import check_btc_momentum_gate
                    gate_ok, gate_reason = check_btc_momentum_gate(side)
                    if not gate_ok:
                        updates["is_closed"] = True
                        updates["status"] = "INVALIDATED_BTC_SHOCK"
                        updates["invalidated_at"] = now_utc.isoformat()
                        log.info("Tracked unfilled setup %s %s cancelled at trigger due to BTC Shock Shield: %s", ticker, side, gate_reason)
                        _send_alert(
                            f"🛡️ *{type_prefix}SETUP CANCELLED (BTC SHOCK SHIELD)* 🛡️\n\n"
                            f"• *Asset*: `${ticker}` ({tf} {side}){age_str}\n"
                            f"• *Entry Band*: `{fmt_dollar(entry_low)} – {fmt_dollar(entry_high)}`\n"
                            f"• *Current Mark*: `{fmt_dollar(live_price)}`\n"
                            f"• *Reason*: `{gate_reason}`\n\n"
                            f"🚫 *Action:* Unfilled order cancelled prior to entry to protect against adverse BTC impulse.",
                            category="triggers"
                        )
                        state.update_tracked_call(cid, updates)
                        continue
                except Exception as e_gate:
                    log.debug("Call tracker BTC gate check error: %s", e_gate)

            entry_filled = True
            updates["entry_filled"] = True
            updates["status"] = "ACTIVE"
            updates["trigger_price"] = live_price
            updates["triggered_at"] = now_utc.isoformat()
            if sl and not call.get("initial_sl"):
                updates["initial_sl"] = sl
                call["initial_sl"] = sl
            log.info("Tracked call %s %s entry filled at mark $%.4f (zone $%.4f–$%.4f)", cid, side, live_price, entry_low, entry_high)

            # Notify that trade has triggered and is now active
            base_key = f"{ticker}_{tf}_{round(entry_mid, 4)}"
            now_ts = time.time()
            if not call.get("trigger_alert_sent") and (base_key not in _triggered_alerts_cache or now_ts - _triggered_alerts_cache[base_key] > 7200):
                _triggered_alerts_cache[base_key] = now_ts
                updates["trigger_alert_sent"] = True
                tp1_p = call.get("tp1_pct", 2.5)
                tp2_p = call.get("tp2_pct", 5.0)
                sl_p = call.get("sl_pct", 1.2)
                _send_alert(
                    f"🟢 *{type_prefix}TRADE TRIGGERED & ACTIVE!* 🟢\n\n"
                    f"• *Asset*: `${ticker}` ({tf} {side}){age_str}\n"
                    f"• *Trigger / Fill Price*: `{fmt_dollar(live_price)}`\n"
                    f"• *Entry Band*: `{fmt_dollar(entry_low)} – {fmt_dollar(entry_high)}`\n"
                    f"• *Target 1 (TP1)*: `+{tp1_p:.1f}%` ({fmt_dollar(tp1)})\n"
                    f"• *Target 2 (TP2)*: `+{tp2_p:.1f}%` ({fmt_dollar(tp2)})\n"
                    f"• *Stop Loss (SL)*: `-{sl_p:.1f}%` ({fmt_dollar(sl)})\n\n"
                    f"⚡ *Action:* Entry band reached. Position is now ACTIVE and under 24/7 target tracking.",
                    category="triggers"
                )

                # Dispatch automated trade execution to subscribed users
                try:
                    import exchange_manager
                    call_cfg = state.get_call_bot_config()
                    c_tok = call_cfg.get("token") or cfg.call_bot_token or cfg.telegram_token
                    sig_payload = dict(call)
                    sig_payload.update(updates)
                    sig_payload["live_price"] = live_price
                    sig_payload["confidence"] = call.get("confidence", 85)
                    sig_payload["grade"] = call.get("grade", "A+")
                    sig_payload["score"] = call.get("score", 85)
                    threading.Thread(
                        target=exchange_manager.dispatch_triggered_trade_to_users,
                        args=(sig_payload,),
                        kwargs={"bot_token": c_tok},
                        daemon=True,
                    ).start()
                except Exception as exc:
                    log.warning("Auto-trade dispatch exception: %s", exc)

        # ── 5. Calculate percentage gain / loss from mid entry ────────────────
        if side == "BUY":
            gain_pct = ((live_price - entry_mid) / entry_mid) * 100.0
            drop_pct = ((entry_mid - live_price) / entry_mid) * 100.0
        else: # SELL
            gain_pct = ((entry_mid - live_price) / entry_mid) * 100.0
            drop_pct = ((live_price - entry_mid) / entry_mid) * 100.0

        curr_peak = max(float(call.get("peak_gain_pct", 0.0)), gain_pct)
        if curr_peak > float(call.get("peak_gain_pct", 0.0)):
            updates["peak_gain_pct"] = round(curr_peak, 2)

        # ── 6. Check Early Profit Protection & Ratchet Profit Locking ─────
        be_trailed = call.get("be_trailed", False)
        orig_sl = float(call.get("initial_sl") or call.get("orig_sl") or sl or 0.0)
        if orig_sl > 0 and abs(entry_mid - orig_sl) > 0 and not call.get("initial_sl"):
            updates["initial_sl"] = orig_sl
            call["initial_sl"] = orig_sl
        risk_dist = abs(entry_mid - orig_sl) if orig_sl and abs(entry_mid - orig_sl) > 0 else (entry_mid * 0.015)
        curr_r = gain_pct / ((risk_dist / entry_mid) * 100.0) if risk_dist > 0 else 0.0

        # Tier 0: Active BTC Shock Shield for Open Altcoin Calls (pre-Breakeven)
        if ticker.upper() != "BTC" and not be_trailed and not call.get("tp0_hit") and not call.get("tp1_hit") and not call.get("btc_shock_shielded"):
            try:
                from scanner import check_btc_momentum_gate
                btc_ok, _ = check_btc_momentum_gate(side)
                if not btc_ok:
                    if curr_r >= 0.08:
                        updates["be_trailed"] = True
                        updates["btc_shock_shielded"] = True
                        updates["sl"] = entry_mid
                        call["be_trailed"] = True
                        call["btc_shock_shielded"] = True
                        call["sl"] = entry_mid
                        sl = entry_mid
                        be_trailed = True
                        log.info("BTC Shock Shield snapped tracked call %s (%s) SL to Break-Even ($%.4f)", ticker, side, entry_mid)
                    elif -0.42 <= curr_r < 0.08 and risk_dist > 0:
                        tight_sl = round(entry_mid - (risk_dist * 0.50) if side == "BUY" else entry_mid + (risk_dist * 0.50), 8)
                        is_tighter = (tight_sl > sl) if side == "BUY" else (tight_sl < sl)
                        if is_tighter:
                            updates["btc_shock_shielded"] = True
                            updates["sl"] = tight_sl
                            call["btc_shock_shielded"] = True
                            call["sl"] = tight_sl
                            sl = tight_sl
                            log.info("BTC Shock Shield tightened tracked call %s (%s) SL by 50%% to $%.4f (-0.50R)", ticker, side, tight_sl)
            except Exception as e_btc_call:
                log.debug("BTC Shock Shield check in call_tracker error: %s", e_btc_call)

        # Level 4 Ratchet: at +1.75R, lock +0.80R guaranteed profit
        if curr_r >= 1.75 and not call.get("ratchet_l4"):
            lock_p = round(entry_mid + (risk_dist * 0.80) if side == "BUY" else entry_mid - (risk_dist * 0.80), 8)
            updates["ratchet_l4"] = True
            updates["ratchet_l3"] = True
            updates["be_trailed"] = True
            updates["locked_sl"] = lock_p
            updates["sl"] = lock_p
            call["ratchet_l4"] = True
            call["locked_sl"] = lock_p
            call["sl"] = lock_p
            _send_alert(
                f"🔒 *{type_prefix}SUPER PROFIT LOCK ACTIVE: ${ticker}* 🔒\n\n"
                f"• *Current Gain*: `+{gain_pct:.1f}%` (+{curr_r:.2f}R @ `{fmt_dollar(live_price)}`)\n"
                f"• *Action*: Trailing Stop shifted to **+0.80R** guaranteed profit (`{fmt_dollar(lock_p)}`)\n"
                f"• *Status*: Trade locked in solid green profit! Downside risk eliminated."
            )
        # Level 3 Ratchet: at +1.10R, lock +0.35R guaranteed profit
        elif curr_r >= 1.10 and not call.get("ratchet_l3"):
            lock_p = round(entry_mid + (risk_dist * 0.35) if side == "BUY" else entry_mid - (risk_dist * 0.35), 8)
            updates["ratchet_l3"] = True
            updates["be_trailed"] = True
            updates["locked_sl"] = lock_p
            updates["sl"] = lock_p
            call["ratchet_l3"] = True
            call["locked_sl"] = lock_p
            call["sl"] = lock_p
            _send_alert(
                f"🔒 *{type_prefix}GUARANTEED PROFIT LOCK ACTIVE: ${ticker}* 🔒\n\n"
                f"• *Current Gain*: `+{gain_pct:.1f}%` (+{curr_r:.2f}R @ `{fmt_dollar(live_price)}`)\n"
                f"• *Action*: Trailing Stop shifted to **+0.35R** profit (`{fmt_dollar(lock_p)}`)\n"
                f"• *Status*: Adverse reversal profit lock engaged. Guaranteed green exit."
            )
        # Level 2 Ratchet (Break-Even): at +1.25R progress or >= +2.5% profit, snap SL to BE
        elif not be_trailed and not call.get("tp1_hit") and (curr_r >= 1.25 or gain_pct >= 2.5):
            updates["be_trailed"] = True
            updates["sl"] = entry_mid
            call["be_trailed"] = True
            call["sl"] = entry_mid
            _send_alert(
                f"🛡️ *{type_prefix}CAPITAL PROTECTION ACTIVE: ${ticker}* 🛡️\n\n"
                f"• *Current Gain*: `+{gain_pct:.1f}%` (+{curr_r:.2f}R @ `{fmt_dollar(live_price)}`)\n"
                f"• *Action*: Stop Loss trailed to Entry Breakeven (`{fmt_dollar(entry_mid)}`)\n"
                f"• *Status*: Trade is now **100% RISK-FREE** running toward TP1 & TP2!"
            )

        # ── 6b. Check Stop Exits (Locked Profit / Breakeven / Original SL) ─
        locked_sl = call.get("locked_sl")
        if locked_sl is not None:
            lock_exit = (live_price <= float(locked_sl)) if side == "BUY" else (live_price >= float(locked_sl))
            if lock_exit:
                updates["sl_hit"] = True
                updates["is_closed"] = True
                updates["status"] = "CLOSED_PROFIT_LOCK"
                _send_alert(
                    f"🔒 *{type_prefix}PROFIT LOCK STOP TRIGGERED* 🔒\n\n"
                    f"• *Asset*: `${ticker}` ({tf} {side}){age_str}\n"
                    f"• *Exit Price*: `{fmt_dollar(live_price)}` (+{gain_pct:.1f}%)\n\n"
                    f"💡 *Outcome:* Favorable profit was protected by trailing stop. Position closed in guaranteed profit! Capital and gains secured."
                )
                state.update_tracked_call(cid, updates)
                continue

        tp0_hit = call.get("tp0_hit", False)
        tp1_hit = call.get("tp1_hit", False)
        if tp1_hit or tp0_hit or be_trailed:
            # Trailing Stop to Entry Breakeven Exit
            be_triggered = (live_price <= entry_mid) if side == "BUY" else (live_price >= entry_mid)
            if be_triggered:
                updates["sl_hit"] = True
                updates["is_closed"] = True
                updates["status"] = "CLOSED_BREAKEVEN"
                profit_note = "TP0 (50%) profit was secured in cash earlier" if tp0_hit else ("TP1 (50%) profit was secured earlier" if tp1_hit else "Trade floated into profit and trailed stop protected capital")
                _send_alert(
                    f"🛡️ *{type_prefix}BREAKEVEN STOP TRIGGERED* 🛡️\n\n"
                    f"• *Asset*: `${ticker}` ({tf} {side}){age_str}\n"
                    f"• *Exit Price*: `{fmt_dollar(live_price)}`\n\n"
                    f"💡 *Outcome:* {profit_note}. Remaining position exited at Breakeven ($0.00 loss). Net trade ended green/risk-free and capital is fully preserved!"
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
                    f"🛑 *{type_prefix}STOP LOSS HIT (ACTIVE TRADE CLOSED)* 🛑\n\n"
                    f"• *Asset*: `${ticker}` ({tf} {side}){age_str}\n"
                    f"• *Entry Fill*: `{fmt_dollar(entry_mid)}`\n"
                    f"• *Stop Loss Level*: `{fmt_dollar(sl)}`\n"
                    f"• *Exit Price*: `{fmt_dollar(live_price)}` (-{drop_pct:.1f}%)\n\n"
                    f"⚠️ *Outcome:* Active position stopped out at predefined risk limit (-1R). Position closed and tracking finalized."
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

        # ── 8b. Check Reversal Warnings & Provide Best Advice ────────────────
        last_rev_time = float(call.get("reversal_time", 0.0))
        now_ts = time.time()
        if not call.get("reversal_alert_sent") or (now_ts - last_rev_time > 7200):
            rev_data = detect_trade_reversal(
                call={**call, "peak_gain_pct": curr_peak},
                live_price=live_price,
                gain_pct=gain_pct,
                drop_pct=drop_pct,
                entry_mid=entry_mid,
            )
            if rev_data:
                rev_reason, rev_advice = rev_data
                updates["reversal_alert_sent"] = True
                updates["reversal_reason"] = rev_reason
                updates["reversal_time"] = now_ts
                pnl_display = f"+{gain_pct:.1f}% 🟢" if gain_pct >= 0 else f"-{drop_pct:.1f}% 🔴"
                advice_text = "\n".join(rev_advice)
                _send_alert(
                    f"⚠️ *{type_prefix}MARKET REVERSAL ALERT: ${ticker} ({tf} {side})* ⚠️\n\n"
                    f"• *Active Setup*: `{side}` triggered @ `{fmt_dollar(entry_mid)}`{age_str}\n"
                    f"• *Current Price*: `{fmt_dollar(live_price)}` (PnL: {pnl_display})\n"
                    f"• *Reversal Driver*: {rev_reason}\n\n"
                    f"💡 *Best Actionable Advice:*\n"
                    f"{advice_text}",
                    category="reversals"
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
                f"⚠️ High volatility drawdown step reached. Verify structure before adding risk.",
                category="reversals"
            )

        if updates:
            state.update_tracked_call(cid, updates)

    # 11. Purge all closed / completed / expired calls from active state
    removed = state.clear_closed_tracked_calls()
    if removed > 0:
        log.info("Call tracker purged %d closed/expired calls from memory", removed)

    # ── 12. Desk-Wide Macro BTC Trend Shift Monitor ─────────────────────────
    global _last_known_btc_regime, _last_macro_reversal_alert_time
    try:
        from scanner import get_btc_macro_regime
        current_btc_regime = get_btc_macro_regime()
        now_t = time.time()
        if _last_known_btc_regime is not None and current_btc_regime != _last_known_btc_regime:
            if now_t - _last_macro_reversal_alert_time > 1800:
                _last_macro_reversal_alert_time = now_t
                _send_alert(
                    f"🌊 *WINZ MACRO REGIME SHIFT ALERT* 🌊\n\n"
                    f"• *Bitcoin Regime Shift*: `{_last_known_btc_regime}` ➔ `{current_btc_regime}`\n"
                    f"• *Context*: Macro trend structure has shifted. Altcoin beta will adjust accordingly.\n\n"
                    f"💡 *Best Professional Advice for Traders:*\n"
                    f"1. Type `/calls` to review all running trades and their current PnL.\n"
                    f"2. Move Stop-Loss on profitable trades to Entry Breakeven.\n"
                    f"3. Avoid new counter-trend entries until fresh 15m/4h structural consolidation confirms.",
                    category="reversals"
                )
        _last_known_btc_regime = current_btc_regime
    except Exception as exc:
        log.debug("Macro BTC regime monitor error: %s", exc)


def _tracker_loop() -> None:
    log.info("Call Tracker background monitor started (polling every 15s)")
    while _tracker_running:
        try:
            check_tracked_calls_once()
        except Exception as exc:
            log.debug("Call tracker loop error: %s", exc)
        try:
            import exchange_manager
            exchange_manager.sync_all_active_users()
        except Exception as exc:
            log.debug("User exchange sync error: %s", exc)
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
