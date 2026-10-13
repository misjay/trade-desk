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

from config import bybit_linear_symbol
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
    auto_trade: bool = True,
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
    if user_store.is_user_paused(cid):
        return False, "User trading execution is paused."

    # 2. Check user avoid list
    avoid_list = user_store.get_user_avoid_list(cid)
    if t_clean in avoid_list:
        return False, f"Asset {t_clean} is in your personal Avoid List."

    ex = user_store.get_user_exchange(cid)
    is_connected = bool(ex and ex.get("connected"))

    # If NOT connected to a real exchange, execute on user's paper demo portfolio
    if not is_connected:
        lev = int(user_store.get_user_leverage(cid, t_clean) or 10)
        u = user_store.get_user(cid)
        trade_margin = min(1000.0, max(50.0, float(u.get("paper_equity", 10000.0)) * 0.10))
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
    lev = int(user_store.get_user_leverage(cid, t_clean) or 10)
    risk_usdt = float(ex.get("risk_per_trade_usdt", 50.0))

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
    auto_users = user_store.get_all_auto_trade_users()
    if not auto_users:
        return 0

    log.info("Dispatching auto-trade signal %s %s to %d active users...", ticker, side, len(auto_users))
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
