"""
exchange_manager.py — Multi-Tenant CEX Connection & Automated Execution Engine.

Allows public users to link their personal Bybit (or CEX) accounts to Winz:
  - AES-256 (Fernet) authenticated encryption of API credentials at rest
  - Secure verification of API keys against live exchange endpoints
  - User-configurable risk sizing (USDT margin per trade) and leverage
  - Automated trade execution when high-conviction setups trigger
  - Zero-knowledge isolation from the Master Trading Desk
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import requests
from cryptography.fernet import Fernet

from config import cfg, bybit_linear_symbol
from notifier import fmt_dollar
import user_store

log = logging.getLogger(__name__)

# Master key file location for encrypting user CEX credentials
_SECRET_KEY_FILE = Path(__file__).parent / ".exchange_key.secret"


def _get_or_create_master_key() -> bytes:
    """Retrieve or generate the server-side master Fernet encryption key."""
    env_key = os.getenv("EXCHANGE_ENCRYPTION_KEY")
    if env_key:
        try:
            return env_key.strip().encode("utf-8")
        except Exception:
            pass

    if _SECRET_KEY_FILE.exists():
        try:
            with open(_SECRET_KEY_FILE, "rb") as f:
                k = f.read().strip()
                if len(k) == 44:  # Valid Fernet urlsafe base64 length
                    return k
        except Exception as exc:
            log.warning("Could not read secret key file: %s", exc)

    # Generate new Fernet key and save
    new_key = Fernet.generate_key()
    try:
        with open(_SECRET_KEY_FILE, "wb") as f:
            f.write(new_key)
        # On POSIX, restrict permissions
        try:
            os.chmod(_SECRET_KEY_FILE, 0o600)
        except Exception:
            pass
    except Exception as exc:
        log.error("Could not persist secret key file: %s", exc)
    return new_key


_fernet_instance: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    global _fernet_instance
    if _fernet_instance is None:
        key = _get_or_create_master_key()
        _fernet_instance = Fernet(key)
    return _fernet_instance


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a raw secret string into an authenticated Fernet token."""
    if not plaintext:
        return ""
    f = _get_fernet()
    return f.encrypt(plaintext.strip().encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt an authenticated Fernet token back into raw string."""
    if not ciphertext:
        return ""
    f = _get_fernet()
    try:
        return f.decrypt(ciphertext.strip().encode("utf-8")).decode("utf-8")
    except Exception as exc:
        log.error("Decryption failed: %s", exc)
        return ""


def mask_key(key: str) -> str:
    """Mask an API key for safe public display in Telegram (e.g. 'aB3d...89xZ')."""
    k = key.strip()
    if len(k) <= 8:
        return "****"
    return f"{k[:4]}...{k[-4:]}"


# ── Exchange Verification & Client Factory ─────────────────────────────────────

def verify_and_connect_bybit(
    chat_id: Union[str, int],
    api_key: str,
    api_secret: str,
    is_testnet: bool = False,
    risk_per_trade: float = 50.0,
    auto_trade: bool = False,
) -> Tuple[bool, str, float]:
    """
    Verify user's Bybit API credentials by querying account balance.
    If valid, encrypts and stores credentials in the user profile.
    Returns (success, message, wallet_equity).
    """
    from bybit_client import BybitClient

    k_clean = api_key.strip()
    s_clean = api_secret.strip()
    if not k_clean or not s_clean:
        return False, "API key and API secret cannot be empty.", 0.0

    mode = "demo" if is_testnet else "live"
    demo_env = "testnet" if is_testnet else "paper"

    test_client = BybitClient(
        api_key=k_clean,
        api_secret=s_clean,
        mode=mode,
        demo_env=demo_env,
    )

    try:
        bal = test_client.get_wallet_balance("USDT")
        equity = float(bal.get("equity", 0.0))
        avail = float(bal.get("available", 0.0))

        # Check if exchange returned valid data
        if equity < 0:
            return False, "Bybit returned an error or rejected API credentials.", 0.0

        # Credentials are valid — encrypt and store
        enc_k = encrypt_secret(k_clean)
        enc_s = encrypt_secret(s_clean)
        masked_k = mask_key(k_clean)

        user_store.set_user_exchange(
            chat_id=chat_id,
            exchange_name="bybit",
            api_key_enc=enc_k,
            api_secret_enc=enc_s,
            api_key_masked=masked_k,
            is_testnet=is_testnet,
            auto_trade=auto_trade,
            risk_per_trade=risk_per_trade,
        )

        log.info("User %s successfully connected Bybit account (%s, equity: $%.2f)", chat_id, masked_k, equity)
        return True, "Connected successfully", equity

    except Exception as exc:
        log.warning("Bybit credential verification failed for user %s: %s", chat_id, exc)
        return False, f"Connection failed: {exc}", 0.0


def get_user_bybit_client(chat_id: Union[str, int]) -> Optional[Any]:
    """
    Construct an authenticated BybitClient instance for a specific user.
    Uses their isolated credentials. Never touches Master Desk keys.
    """
    from bybit_client import BybitClient

    ex = user_store.get_user_exchange(chat_id)
    if not ex or not ex.get("connected"):
        return None

    if ex.get("name") != "bybit":
        return None

    raw_key = decrypt_secret(ex.get("encrypted_api_key", ""))
    raw_secret = decrypt_secret(ex.get("encrypted_api_secret", ""))
    if not raw_key or not raw_secret:
        return None

    is_testnet = bool(ex.get("is_testnet", False))
    return BybitClient(
        api_key=raw_key,
        api_secret=raw_secret,
        mode="demo" if is_testnet else "live",
        demo_env="testnet" if is_testnet else "paper",
    )


# ── Automated & 1-Click Trade Execution ────────────────────────────────────────

def execute_trade_for_user(
    chat_id: Union[str, int],
    ticker: str,
    side: str,
    live_price: float,
    sl: Optional[float] = None,
    tp1: Optional[float] = None,
    tp2: Optional[float] = None,
    timeframe: str = "15m",
) -> Tuple[bool, str]:
    """
    Execute a trade on the user's personal exchange account (or demo if not connected).
    Returns (success, message).
    """
    cid = str(chat_id).strip()
    t_clean = ticker.upper().replace("USDT", "")
    side_clean = side.upper()

    # 1. Check user pause status
    if user_store.is_user_paused_for_type(cid, timeframe):
        return False, f"User trading execution is paused for {timeframe}."

    # 2. Check user avoid list
    avoid_list = user_store.get_user_avoid_list(cid)
    if t_clean in avoid_list:
        return False, f"Asset {t_clean} is in your personal Avoid List."

    ex = user_store.get_user_exchange(cid)
    is_connected = bool(ex and ex.get("connected"))

    # If NOT connected to a real exchange, execute on user's paper demo portfolio
    if not is_connected:
        user_goal = user_store.get_user_goal(cid)
        u = user_store.get_user(cid)
        paper_eq = float(u.get("paper_equity", 10000.0))
        lev = int(user_store.get_user_leverage(cid, t_clean) or 10)
        trade_margin = min(1000.0, max(5.0, paper_eq * 0.10))

        if user_goal:
            user_store.update_goal_morning_equity_if_new_day(cid, paper_eq)
            user_goal = user_store.get_user_goal(cid)
            m_eq = float(user_goal.get("morning_equity", paper_eq))
            max_loss_pct = float(user_goal.get("max_daily_loss_pct", 5.0))
            today_pnl = paper_eq - m_eq
            if m_eq > 0 and (today_pnl / m_eq * 100.0) <= -max_loss_pct:
                log.info("Goal Guard: Demo user %s reached %s%% daily loss limit ($%.2f). Skipping trade.", cid, max_loss_pct, today_pnl)
                return False, f"Goal Guard Active: Daily loss cap ({max_loss_pct}%) reached today. Demo trading paused to preserve equity."

            lev = int(user_goal.get("leverage", lev))
            from goal_tracker import size_goal_trade
            sl_dist = abs(live_price - sl) / live_price * 100.0 if (live_price > 0 and sl) else 1.2
            sizing = size_goal_trade(paper_eq, m_eq, sl_dist, leverage=lev, max_daily_loss_pct=max_loss_pct, today_realized_loss=min(0.0, today_pnl))
            if sizing.get("allowed"):
                trade_margin = float(sizing.get("margin_usdt", trade_margin))

        notional = trade_margin * lev
        qty = round(notional / live_price, 6) if live_price > 0 else 1.0

        pos = {
            "ticker": t_clean,
            "side": side_clean,
            "entry_price": live_price,
            "qty": qty,
            "leverage": lev,
            "margin_usdt": trade_margin,
            "tp1": tp1,
            "tp2": tp2,
            "sl": sl,
            "tf": timeframe,
            "source": "AUTO_SIGNAL",
        }
        user_store.open_user_position(cid, pos)
        return True, f"Executed on Demo Paper Portfolio ({t_clean} {side_clean} {lev}x)"

    # User IS connected to Bybit — execute live on exchange
    client = get_user_bybit_client(cid)
    if not client:
        return False, "Could not initialize Bybit client with saved credentials."

    sym = bybit_linear_symbol(t_clean)

    # First, reconcile any closed positions on Bybit so slots are accurately freed up
    try:
        sync_user_positions_with_bybit(cid)
    except Exception as e_sync:
        log.debug("Auto-sync prior to order execution exception: %s", e_sync)

    # 1. Enforce max concurrent positions on exchange (default 2 to protect wallet margin)
    max_user_positions = int(ex.get("max_positions", 2))
    try:
        active_ex_pos = client.get_active_positions(category="linear")
        if len(active_ex_pos) >= max_user_positions:
            log.info("Skipping trade %s for user %s: max concurrent positions (%d) reached", sym, cid, max_user_positions)
            return False, f"Max concurrent positions ({max_user_positions}) reached. Preserving free margin buffer."

        # Prevent duplicate positions on the exact same asset
        if any(p.get("symbol") == sym for p in active_ex_pos):
            log.info("Skipping trade %s for user %s: position already active on Bybit", sym, cid)
            return False, f"Position on {sym} is already open on Bybit."
    except Exception as e_pos:
        log.debug("Active position check exception for user %s: %s", cid, e_pos)

    lev = int(user_store.get_user_leverage(cid, t_clean) or 10)
    risk_usdt = float(ex.get("risk_per_trade_usdt", 50.0))

    # Goal Tracker Guard & Sizing Integration for Live Exchange
    user_goal = user_store.get_user_goal(cid)
    if user_goal:
        try:
            wb = client.get_wallet_balance("USDT")
            cur_eq = float(wb.get("equity", 0.0))
            user_store.update_goal_morning_equity_if_new_day(cid, cur_eq)
            user_goal = user_store.get_user_goal(cid)
            m_eq = float(user_goal.get("morning_equity", cur_eq))
            max_loss_pct = float(user_goal.get("max_daily_loss_pct", 5.0))
            today_pnl = cur_eq - m_eq
            if m_eq > 0 and (today_pnl / m_eq * 100.0) <= -max_loss_pct:
                log.info("Goal Guard: Live user %s reached %s%% daily loss limit ($%.2f). Skipping trade %s.", cid, max_loss_pct, today_pnl, sym)
                return False, f"Goal Guard Active: Daily loss cap ({max_loss_pct}%) reached today. New trades paused to protect capital."

            lev = int(user_goal.get("leverage", lev))
            from goal_tracker import size_goal_trade
            sl_dist = abs(live_price - sl) / live_price * 100.0 if (live_price > 0 and sl) else 1.2
            sizing = size_goal_trade(cur_eq, m_eq, sl_dist, leverage=lev, max_daily_loss_pct=max_loss_pct, today_realized_loss=min(0.0, today_pnl))
            if sizing.get("allowed"):
                risk_usdt = float(sizing.get("margin_usdt", risk_usdt))
        except Exception as e_goal:
            log.debug("Goal check exception in exchange_manager for %s: %s", cid, e_goal)

    try:
        # A. Set isolated margin and leverage
        client.set_isolated_margin_and_leverage(sym, lev)

        # B. Calculate order quantity
        notional = risk_usdt * lev
        raw_qty = notional / live_price if live_price > 0 else 0.0
        q_qty = client.quantize_qty(sym, raw_qty, category="linear")

        info = client.get_instrument_info(sym, category="linear")
        min_qty = float(info.get("min_qty", 0.001))
        if q_qty < min_qty:
            q_qty = min_qty

        # Safety Guard for Small Wallets: prevent over-leveraging
        required_margin = (q_qty * live_price) / lev
        try:
            wb = client.get_wallet_balance("USDT")
            avail_bal = float(wb.get("available", 0.0))
            if avail_bal > 0 and required_margin > (avail_bal * 0.60):
                log.info("Skipping trade %s for user %s: required margin $%.2f exceeds 60%% of available balance ($%.2f)", sym, cid, required_margin, avail_bal)
                return False, f"Requires ${required_margin:.2f} margin (exceeds safe buffer for ${avail_bal:.2f} available balance). Skipped to protect small wallet."
        except Exception as e_bal:
            log.debug("Balance check exception for user %s: %s", cid, e_bal)

        # C. Place Market Order with exchange-native TP1 and SL
        order_side = "Buy" if side_clean == "BUY" else "Sell"
        res = client.place_order(
            category="linear",
            symbol=sym,
            side=order_side,
            order_type="Market",
            qty=q_qty,
            stop_loss=sl,
            take_profit=tp1,
        )

        if not res:
            return False, f"Exchange order placement failed on {sym}."

        order_id = res.get("orderId", "")
        # D. Record in user's position tracker
        pos = {
            "ticker": t_clean,
            "side": side_clean,
            "entry_price": live_price,
            "qty": q_qty,
            "leverage": lev,
            "margin_usdt": risk_usdt,
            "exchange_order_id": order_id,
            "tp1": tp1,
            "tp2": tp2,
            "sl": sl,
            "tf": timeframe,
            "source": "BYBIT_LIVE",
        }
        user_store.open_user_position(cid, pos)
        log.info("Live auto-trade executed for user %s on Bybit: %s %s (qty: %s)", cid, sym, order_side, q_qty)
        return True, f"Live Bybit order placed: {sym} {order_side} ({q_qty} contracts)"

    except Exception as exc:
        log.error("Error executing trade for user %s on Bybit: %s", cid, exc)
        return False, f"Execution exception: {exc}"


def dispatch_triggered_trade_to_users(
    signal: dict,
    bot_token: Optional[str] = None,
) -> int:
    """
    Called by call_tracker or scanner when an entry band fills.
    Executes trades for all subscribed auto-trading users in parallel.
    Returns the count of executed user orders.
    """
    ticker = signal.get("ticker", "").upper().replace("USDT", "")
    side = signal.get("side", "BUY").upper()
    lp = float(signal.get("trigger_price") or signal.get("live_price") or 0.0)
    sl = float(signal.get("sl") or 0.0)
    tp1 = float(signal.get("tp1") or 0.0)
    tp2 = float(signal.get("tp2") or 0.0)
    tf = signal.get("tf", "15m")

    if not ticker or lp <= 0:
        return 0

    from telegram_bot import _reply
    trade_type = "scalp" if str(tf).lower() in ("15m", "15") else "day"
    auto_users = user_store.get_all_auto_trade_users(trade_type=trade_type)
    if not auto_users:
        return 0

    log.info("Dispatching %s auto-trade signal %s %s to %d active users...", trade_type, ticker, side, len(auto_users))
    executed_count = 0

    for cid, user_data in auto_users:
        try:
            success, msg = execute_trade_for_user(
                chat_id=cid,
                ticker=ticker,
                side=side,
                live_price=lp,
                sl=sl if sl > 0 else None,
                tp1=tp1 if tp1 > 0 else None,
                tp2=tp2 if tp2 > 0 else None,
                timeframe=tf,
            )
            if success:
                executed_count += 1
                ex = user_data.get("exchange", {})
                is_live = bool(ex and ex.get("connected"))
                venue_str = f"Bybit ({ex.get('api_key_masked')})" if is_live else "Winz Demo ($10k Portfolio)"
                receipt = (
                    f"🚀 *Winz Auto-Trade Executed!* 🚀\n\n"
                    f"• *Venue*: `{venue_str}`\n"
                    f"• *Asset*: `{ticker}USDT`\n"
                    f"• *Side*: `{side}`\n"
                    f"• *Entry Price*: `{fmt_dollar(lp)}`\n"
                    f"• *Target 1 (TP1)*: `{fmt_dollar(tp1)}`\n"
                    f"• *Stop Loss (SL)*: `{fmt_dollar(sl)}`\n\n"
                    f"🛡️ *Status:* Trade is open and protected by exchange-native TP & SL.\n"
                    f"_View with `/positions` or take profit with `/tp`._"
                )
                _reply(cid, receipt, bot_token=bot_token)
        except Exception as exc:
            log.warning("Failed auto-trade dispatch for user %s: %s", cid, exc)

    return executed_count
 
 
def sync_user_positions_with_bybit(chat_id: Union[str, int], bot_token: Optional[str] = None) -> List[dict]:
    """
    Reconciles user_store open_positions with live Bybit positions.
    Detects if a trade closed on Bybit (TP, SL, manual), updates user_store,
    notifies the user via Telegram, and frees up the concurrent trade slot!
    """
    cid = str(chat_id).strip()
    client = get_user_bybit_client(cid)
    if not client:
        return []

    try:
        bybit_pos = client.get_active_positions(category="linear")
        active_syms = {p["symbol"] for p in bybit_pos if float(p.get("size", 0.0)) > 0}
    except Exception as e:
        log.debug("Could not fetch active Bybit positions for user %s: %s", cid, e)
        return []

    u = user_store.get_user(cid)
    user_open = u.get("open_positions", {})
    if not user_open:
        return []

    closed_positions = []
    from telegram_bot import _reply
    import state

    # Fetch recent closed PnL from Bybit to record exact realized gain/loss
    try:
        closed_records = client.get_closed_pnl(category="linear", limit=10) or []
    except Exception:
        closed_records = []

    for pid, pos in list(user_open.items()):
        ticker = pos.get("ticker", "").upper().replace("USDT", "")
        sym = bybit_linear_symbol(ticker)

        # If position is no longer active on Bybit, it was closed!
        if sym not in active_syms:
            log.info("Live position %s (%s) closed on Bybit for user %s. Finalizing state...", pid, sym, cid)
            
            # Find matching closed PnL record
            match_pnl = 0.0
            exit_p = float(pos.get("entry_price", 0.0))
            exec_type = "TRADE"
            for rec in closed_records:
                if rec.get("symbol") == sym:
                    match_pnl = float(rec.get("closed_pnl", 0.0))
                    exit_p = float(rec.get("exit_price", exit_p))
                    exec_type = str(rec.get("exec_type", "TRADE")).upper()
                    break

            c_pos = user_store.close_user_position(cid, pid, exit_p, reason=f"BYBIT_{exec_type}")
            if c_pos:
                c_pos["pnl_usdt"] = match_pnl
                closed_positions.append(c_pos)

                pnl_str = f"+${match_pnl:.2f} 🟢" if match_pnl >= 0 else f"-${abs(match_pnl):.2f} 🔴"
                outcome_label = "🏆 TAKE PROFIT HIT!" if match_pnl > 0 else "🛑 STOP LOSS TRIGGERED"
                receipt = (
                    f"{outcome_label} (Live Bybit)\n\n"
                    f"• *Asset*: `{sym}` ({pos.get('side', 'TRADE')})\n"
                    f"• *Entry Price*: `{fmt_dollar(pos.get('entry_price', 0.0))}`\n"
                    f"• *Exit Price*: `{fmt_dollar(exit_p)}`\n"
                    f"• *Realized PnL*: `{pnl_str}`\n\n"
                    f"✨ *Slot Freed*: 1 trading slot is now open and ready for the next setup!"
                )
                if not bot_token:
                    call_cfg = state.get_call_bot_config()
                    bot_token = call_cfg.get("token") or cfg.call_bot_token or cfg.telegram_token
                try:
                    _reply(cid, receipt, bot_token=bot_token)
                except Exception as e_rep:
                    log.debug("Error sending position close receipt to %s: %s", cid, e_rep)

    return closed_positions


def sync_all_active_users(bot_token: Optional[str] = None) -> None:
    """Run position sync for all live connected auto-trading users."""
    auto_users = user_store.get_all_auto_trade_users()
    for cid, _ in auto_users:
        try:
            sync_user_positions_with_bybit(cid, bot_token=bot_token)
        except Exception as exc:
            log.debug("Error syncing positions for user %s: %s", cid, exc)
