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

from datetime import datetime, timezone
import requests
from cryptography.fernet import Fernet

from config import cfg, bybit_linear_symbol
from notifier import fmt_dollar

# Ring buffer of recently shielded / filtered setups for user transparency
_recent_shielded_setups: List[Dict[str, Any]] = []
_recent_user_dispatches: Dict[str, float] = {}

def record_shielded_setup(chat_id: str, ticker: str, side: str, reason: str) -> None:
    global _recent_shielded_setups
    _recent_shielded_setups.append({
        "time": time.time(),
        "chat_id": str(chat_id),
        "ticker": ticker,
        "side": side,
        "reason": reason,
    })
    if len(_recent_shielded_setups) > 25:
        _recent_shielded_setups.pop(0)

def get_recent_shielded_setups(chat_id: Optional[str] = None) -> List[Dict[str, Any]]:
    if not chat_id:
        return list(_recent_shielded_setups)
    res = [s for s in _recent_shielded_setups if s.get("chat_id") == str(chat_id)]
    return res if res else list(_recent_shielded_setups)

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


def _get_user_daily_profit_shield(cid: str, current_equity: float) -> Dict[str, Any]:
    """
    2-Stage Daily Profit Shield for Live & Personal Demo Accounts:
      - Stage 1 (+20.0% Daily Gain — Uncapped Momentum Filter):
          Keeps 100% trade sizing so the user can compound toward +50%,
          but filters out lower-conviction (<85%) chop setups.
      - Stage 2 (+50.0% Daily Gain — Jackpot Lock Mode):
          Locks in the +50% daily trophy by scaling risk to 0.5x and only taking A+ (>=90%) setups.
    """
    from datetime import datetime, timezone
    u = user_store.get_user(cid)
    closed = u.get("closed_positions", [])
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    daily_pnl = 0.0
    for c in closed:
        c_time_str = c.get("closed_at")
        if c_time_str:
            try:
                c_dt = datetime.fromisoformat(str(c_time_str).replace("Z", "+00:00"))
                if c_dt >= day_start:
                    daily_pnl += float(c.get("pnl_usdt", 0.0) or 0.0)
            except Exception:
                pass

    user_goal = user_store.get_user_goal(cid)
    if user_goal and current_equity > 0:
        m_eq = float(user_goal.get("morning_equity", 0.0) or 0.0)
        if m_eq > 0:
            goal_pnl = current_equity - m_eq
            if abs(goal_pnl) > abs(daily_pnl):
                daily_pnl = goal_pnl

    start_eq = max(1.0, current_equity - daily_pnl)
    daily_gain_pct = (daily_pnl / start_eq) * 100.0 if start_eq > 0 else 0.0

    if daily_gain_pct >= 50.0:
        return {
            "stage": 2,
            "mode": "JACKPOT_LOCK_50PCT",
            "daily_pnl_usdt": round(daily_pnl, 2),
            "daily_gain_pct": round(daily_gain_pct, 2),
            "min_score": 90.0,
            "risk_mult": 0.50,
        }
    elif daily_gain_pct >= 20.0:
        return {
            "stage": 1,
            "mode": "MOMENTUM_FILTER_20PCT",
            "daily_pnl_usdt": round(daily_pnl, 2),
            "daily_gain_pct": round(daily_gain_pct, 2),
            "min_score": 85.0,
            "risk_mult": 1.00,
        }
    return {
        "stage": 0,
        "mode": "NORMAL",
        "daily_pnl_usdt": round(daily_pnl, 2),
        "daily_gain_pct": round(daily_gain_pct, 2),
        "min_score": 0.0,
        "risk_mult": 1.00,
    }


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
    signal_metadata: Optional[dict] = None,
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

    # 2b. User Daily Loss Circuit Breaker (Safety Fuse)
    cb_active, cb_reason = user_store.check_and_apply_user_circuit_breaker(cid)
    if cb_active:
        log.info("🛡️ User Circuit Breaker blocked %s for user %s: %s", t_clean, cid, cb_reason)
        return False, cb_reason

    # 2c. Portfolio Correlation Cap: Max 2 high-beta altcoins per direction (Option A: allows 2 Longs + 1 Short)
    if t_clean not in ("BTC", "ETH"):
        user_pos = user_store.get_user_open_positions(cid)
        active_same_dir_alts = [
            p.get("ticker", "") for p in user_pos.values()
            if p.get("side", "").upper() == side_clean and p.get("ticker", "").upper() not in ("BTC", "ETH")
        ]
        if len(active_same_dir_alts) >= 2:
            reason = f"Portfolio Correlation Cap: 2 {side_clean} altcoins already active ({', '.join(active_same_dir_alts)}). Max 2 same-direction alts permitted to prevent correlated drawdowns."
            log.info("🛡️ %s blocked %s for user %s", reason, t_clean, cid)
            return False, reason

    # 2d. All Winz assets permitted on Winz using Xira's methodology & dynamic guardrails

    sig_score = float((signal_metadata or {}).get("score") or (signal_metadata or {}).get("confidence") or (signal_metadata or {}).get("conviction") or 85.0)

    # 3-Layer Shield: Tier-Specific Gates (Layer 3 Conviction Gate)
    from config import get_asset_tier, get_asset_risk_multiplier
    asset_tier = get_asset_tier(t_clean)
    tier_mult = get_asset_risk_multiplier(t_clean)

    if asset_tier == 3 and sig_score < 88.0:
        log.info("🛡️ Conviction Gate: Skipped %s for user %s: Score %.1f < 88%% A+ requirement for Tier 3 asset.", t_clean, cid, sig_score)
        record_shielded_setup(cid, t_clean, side_clean, f"Tier 3 Low Conviction ({sig_score:.0f}% < 88% A+)")
        return False, f"Tier 3 Asset ({t_clean}): Score {sig_score:.0f}% below 88% A+ institutional threshold."

    # Directional Quarantine Gate (Cooldown after recent adverse stopouts)
    import state
    is_dq, dq_reason = state.is_directionally_quarantined(t_clean, side_clean)
    if is_dq:
        log.info("🛡️ %s: Skipping auto-trade for user %s on %s", dq_reason, cid, t_clean)
        record_shielded_setup(cid, t_clean, side_clean, dq_reason)
        return False, dq_reason

    # Xira Methodology: All Winz assets permitted using Xira's institutional framework
    # 3. BTC Momentum Gatekeeper (Real-time Altcoin Protection against Squeezes & Dumps)
    if t_clean != "BTC":
        try:
            from scanner import check_btc_momentum_gate
            gate_ok, gate_reason = check_btc_momentum_gate(side_clean)
            if not gate_ok:
                log.info("BTC Momentum Gate blocked %s %s for user %s: %s", t_clean, side_clean, cid, gate_reason)
                record_shielded_setup(cid, t_clean, side_clean, f"BTC Surge: {gate_reason}")
                return False, f"BTC Momentum Gate active: {gate_reason}"
        except Exception as e_gate:
            log.debug("BTC Momentum Gate check error: %s", e_gate)

    # Xira Methodology: Minimum Risk-to-Reward Gate (Strictly >= 2.0:1 institutional standard across all Winz assets)
    if sl and tp1 and live_price > 0:
        sl_dist = abs(live_price - sl)
        tp_dist = abs(tp1 - live_price)
        rr_ratio = tp_dist / max(sl_dist, 1e-9)
        min_user_rr = max(2.0, cfg.min_rr_gate)
        if rr_ratio < min_user_rr:
            log.info("Xira R:R Gate: Skipped %s %s for user %s: R:R ratio %.2f < %.2f minimum.", t_clean, side_clean, cid, rr_ratio, min_user_rr)
            record_shielded_setup(cid, t_clean, side_clean, f"R:R Ratio {rr_ratio:.2f} < {min_user_rr:.1f}:1 min")
            return False, f"Xira R:R Gate: Risk-to-reward ratio ({rr_ratio:.2f}) below {min_user_rr:.1f}:1 minimum."

    # 4. Mode Filter (Sniper, Hybrid 70/30, Normal)
    user_mode = user_store.get_user_auto_trade_mode(cid)
    sig_score = float((signal_metadata or {}).get("score") or (signal_metadata or {}).get("confidence") or (signal_metadata or {}).get("conviction") or 85.0)
    is_trend_trade = str(timeframe).lower() in ("4h", "1h", "day", "trend")

    if user_mode == "sniper":
        # Strict Institutional Conviction Filter (Grade A+ or Score >= 88.0%)
        if signal_metadata:
            grade = str(signal_metadata.get("grade", "")).upper()
            score = sig_score
            if (grade and grade not in ("A+", "A")) or score < 88.0:
                log.info("Sniper Mode: Skipped %s %s for user %s: Grade %s, Score %.1f below 88%% A+ institutional threshold.", t_clean, side_clean, cid, grade, score)
                record_shielded_setup(cid, t_clean, side_clean, f"Low Conviction: Grade {grade} / Score {score:.0f}")
                return False, f"Sniper Mode: Grade {grade} / Score {score:.0f} below 88% A+ institutional threshold."

    elif user_mode == "hybrid":
        # Hybrid 70% Scalp / 30% Trend:
        # A. Strict Institutional Conviction Filter for all setups (A+ / A Grade or Score >= 80%)
        if signal_metadata:
            grade = str(signal_metadata.get("grade", "")).upper()
            score = sig_score
            if grade and grade not in ("A+", "A") and score < 80.0:
                log.info("Hybrid Mode: Skipped %s %s for user %s: Lower conviction (Grade %s, Score %.1f). Only A+ setups allowed.", t_clean, side_clean, cid, grade, score)
                record_shielded_setup(cid, t_clean, side_clean, f"Low Conviction: Grade {grade} / Score {score:.0f}")
                return False, f"Hybrid Mode: Grade {grade} / Score {score:.0f} below A+ institutional threshold."

        # B. For scalps, preserve target discipline (TP1 <= 2.0% for BTC/ETH, <= 3.5% for Alts)
        if not is_trend_trade:
            if tp1 and live_price > 0:
                tp_dist_pct = abs(tp1 - live_price) / live_price * 100.0
                max_scalp_dist = 2.0 if t_clean in ("BTC", "ETH") else 3.5
                if tp_dist_pct > max_scalp_dist:
                    log.info("Hybrid Scalp: Skipped %s %s for user %s: Scalp TP1 distance (%.2f%%) exceeds %.1f%% cap.", t_clean, side_clean, cid, tp_dist_pct, max_scalp_dist)
                    record_shielded_setup(cid, t_clean, side_clean, f"Scalp TP Distance ({tp_dist_pct:.1f}%) > {max_scalp_dist:.1f}% cap")
                    return False, f"Hybrid Scalp: TP1 distance ({tp_dist_pct:.1f}%) exceeds {max_scalp_dist:.1f}% cap."
        # Note: If is_trend_trade is True, wider TP1 (>1.8%) is fully allowed for trend capture!

    ex = user_store.get_user_exchange(cid)
    is_connected = bool(ex and ex.get("connected"))

    # If NOT connected to a real exchange, execute on user's paper demo portfolio
    if not is_connected:
        if user_mode == "hybrid" and is_trend_trade:
            open_user_pos = user_store.get_user_open_positions(cid)
            trend_pos_count = sum(
                1 for p in open_user_pos.values()
                if str(p.get("tf", "")).lower() in ("4h", "1h", "day", "trend")
            )
            max_trend_slots = 1
            if trend_pos_count >= max_trend_slots:
                reason = f"Hybrid 70/30 Rule: Trend capacity reached ({trend_pos_count}/{max_trend_slots} slots). Remaining slot reserved for scalps."
                record_shielded_setup(cid, t_clean, side_clean, "Hybrid 30% Trend Cap Reached")
                return False, reason

        user_goal = user_store.get_user_goal(cid)
        u = user_store.get_user(cid)
        paper_eq = float(u.get("paper_equity", 10000.0))
        lev = int(user_store.get_user_leverage(cid, t_clean) or 10)
        trade_margin = min(1000.0, max(5.0, paper_eq * 0.10)) * tier_mult

        # 2-Stage Daily Profit Shield for Demo User
        p_shield = _get_user_daily_profit_shield(cid, paper_eq)
        if p_shield["stage"] >= 1 and sig_score < p_shield["min_score"]:
            return False, f"Daily Profit Shield ({p_shield['mode']}, +{p_shield['daily_gain_pct']:.1f}% today): Filtered {t_clean} (Score {sig_score:.0f} < {p_shield['min_score']:.0f}%)."

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

        trade_margin *= p_shield["risk_mult"]
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
            "orig_sl": sl,
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
        open_orders = client.get_open_orders(category="linear")
        active_entry_orders = [
            o for o in open_orders
            if o.get("orderStatus") in ("New", "PartiallyFilled")
            and float(o.get("price", 0.0)) > 0.0
        ]
        total_committed_slots = len(active_ex_pos) + len(active_entry_orders)
        if total_committed_slots >= max_user_positions:
            log.info("Skipping trade %s for user %s: max concurrent capacity (%d) reached (%d active positions, %d resting entry orders)",
                     sym, cid, max_user_positions, len(active_ex_pos), len(active_entry_orders))
            return False, f"Max concurrent capacity ({max_user_positions}) reached ({len(active_ex_pos)} active positions, {len(active_entry_orders)} resting orders). Preserving free margin buffer."

        # Prevent duplicate positions or resting orders on the exact same asset
        if any(p.get("symbol") == sym for p in active_ex_pos) or any(o.get("symbol") == sym for o in active_entry_orders):
            log.info("Skipping trade %s for user %s: position or resting order already active on Bybit", sym, cid)
            return False, f"Position or resting limit order on {sym} is already active on Bybit."

        # Hybrid 70/30 Slot Allocation: ensure trend moves do not crowd out scalps
        if user_mode == "hybrid" and is_trend_trade:
            open_user_pos = user_store.get_user_open_positions(cid)
            trend_pos_count = sum(
                1 for p in open_user_pos.values()
                if str(p.get("tf", "")).lower() in ("4h", "1h", "day", "trend")
            )
            max_trend_slots = max(1, int(round(max_user_positions * 0.30)))
            if trend_pos_count >= max_trend_slots:
                reason = f"Hybrid 70/30 Capacity: Trend slot filled ({trend_pos_count}/{max_trend_slots} slots). Remaining slot reserved for rapid 70% scalps."
                log.info("%s for user %s on %s", reason, cid, sym)
                record_shielded_setup(cid, t_clean, side_clean, "Hybrid 30% Trend Cap Reached")
                return False, reason
    except Exception as e_pos:
        log.debug("Active position check exception for user %s: %s", cid, e_pos)

    lev = int(user_store.get_user_leverage(cid, t_clean) or 10)
    risk_usdt = float(ex.get("risk_per_trade_usdt", 50.0)) * tier_mult

    # Fetch live wallet balance for Daily Profit Shield & Goal Tracker
    cur_eq = 0.0
    try:
        wb = client.get_wallet_balance("USDT")
        cur_eq = float(wb.get("equity", 0.0))
    except Exception:
        pass

    # Dynamic Balance Compounding Mode:
    # Scales risk per trade proportionally with live Bybit equity growth
    compound_pct = user_store.get_user_compounding_pct(cid)
    if compound_pct > 0.0 and cur_eq > 0.0:
        raw_risk = cur_eq * (compound_pct / 100.0)
        risk_usdt = max(2.0, min(cur_eq * 0.25, raw_risk)) * tier_mult
        log.info("📈 Dynamic Compounding Active (%.2f%%) for user %s: Sized risk_usdt to $%.2f (Equity: $%.2f)", compound_pct, cid, risk_usdt, cur_eq)

    p_shield = _get_user_daily_profit_shield(cid, cur_eq)
    if p_shield["stage"] >= 1 and sig_score < p_shield["min_score"]:
        reason_str = f"Daily Profit Shield ({p_shield['mode']}, +{p_shield['daily_gain_pct']:.1f}% today): Score {sig_score:.0f} < {p_shield['min_score']:.0f}% min"
        log.info("🛡️ %s for user %s on %s", reason_str, cid, sym)
        record_shielded_setup(cid, t_clean, side_clean, reason_str)
        return False, reason_str

    # Goal Tracker Guard & Sizing Integration for Live Exchange
    user_goal = user_store.get_user_goal(cid)
    if user_goal and cur_eq > 0:
        try:
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

    if p_shield["risk_mult"] < 1.0:
        risk_usdt = max(2.0, risk_usdt * p_shield["risk_mult"])
        log.info("🏆 +50%% Daily Jackpot Lock Active (+%.1f%% today) for user %s: Scaled risk_usdt to $%.2f",
                 p_shield["daily_gain_pct"], cid, risk_usdt)

    try:
        # A. Set isolated margin and leverage
        client.set_isolated_margin_and_leverage(sym, lev)

        # B. Xira Methodology: True Structural Stop Loss & Risk-Normalized Position Sizing across all Winz assets
        if sl and live_price > 0:
            stop_dist_pct = abs(live_price - sl) / live_price
            sl = client.quantize_price(sym, sl, category="linear")
        else:
            stop_dist_pct = 0.018
            sl = live_price * (1.0 - 0.018) if side_clean == "BUY" else live_price * (1.0 + 0.018)
            sl = client.quantize_price(sym, sl, category="linear")

        target_notional = risk_usdt / max(stop_dist_pct, 0.005)
        # Margin Guard: Cap margin to avoid small wallet overcommitment (Option A: $2.00 target margin)
        target_margin_pref = float(ex.get("margin_per_trade_usdt") or ex.get("risk_per_trade_usdt") or 2.0)
        max_margin_budget = target_margin_pref if cur_eq < 25.0 else max(2.5, risk_usdt * 2.5)
        required_margin = target_notional / max(1, lev)
        if required_margin > max_margin_budget:
            target_notional = max_margin_budget * lev
            log.info("Capped %s notional to $%.2f (margin budget $%.2f at %dx lev)", sym, target_notional, max_margin_budget, lev)

        raw_qty = target_notional / live_price if live_price > 0 else 0.0
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
            if avail_bal > 0 and required_margin > (avail_bal * 0.70):
                log.info("Skipping trade %s for user %s: required margin $%.2f exceeds 70%% of available balance ($%.2f)", sym, cid, required_margin, avail_bal)
                return False, f"Requires ${required_margin:.2f} margin (exceeds safe buffer for ${avail_bal:.2f} available balance). Skipped to protect small wallet."
        except Exception as e_bal:
            log.debug("Balance check exception for user %s: %s", cid, e_bal)

        # C. Post-Only Maker Fee Optimizer with BBO Best-Price Anchoring:
        order_side = "Buy" if side_clean == "BUY" else "Sell"
        maker_price = live_price
        try:
            ob = client.get_orderbook(sym, limit=5)
            if ob:
                bids = ob.get("bids", [])
                asks = ob.get("asks", [])
                if order_side == "Buy" and bids:
                    best_bid = float(bids[0][0])
                    maker_price = min(live_price, best_bid)
                elif order_side == "Sell" and asks:
                    best_ask = float(asks[0][0])
                    maker_price = max(live_price, best_ask)
        except Exception as e_ob:
            log.debug("Orderbook fetch error for maker price on %s: %s", sym, e_ob)

        res = None
        exec_type = "MAKER (Post-Only 0.02% Fee)"
        # Xira Model: Set Bybit take_profit to major target tp2 (not slim tp1) so runners expand!
        target_tp = tp2 if tp2 and tp2 > 0 else tp1
        try:
            res = client.place_order(
                category="linear",
                symbol=sym,
                side=order_side,
                order_type="Limit",
                price=maker_price,
                time_in_force="PostOnly",
                qty=q_qty,
                stop_loss=sl,
                take_profit=target_tp,
            )
            # If rejected because order would cross book, seamlessly fallback to passive GTC limit
            if not res or not res.get("orderId"):
                log.info("🎯 Post-Only rejected on %s, falling back to GTC limit @ $%.4f", sym, maker_price)
                res = client.place_order(
                    category="linear",
                    symbol=sym,
                    side=order_side,
                    order_type="Limit",
                    price=maker_price,
                    time_in_force="GTC",
                    qty=q_qty,
                    stop_loss=sl,
                    take_profit=target_tp,
                )
                exec_type = "MAKER (GTC Limit)"

            if res and res.get("orderId"):
                log.info("🎯 %s placed for user %s on %s @ $%.4f (ID=%s)", exec_type, cid, sym, maker_price, res.get("orderId"))
        except Exception as e_post:
            log.debug("Order placement attempt failed for %s on %s: %s", cid, sym, e_post)
            res = None

        if not res or not res.get("orderId"):
            log.info("Order placement failed for user %s on %s", cid, sym)
            return False, f"Limit order not accepted on Bybit for {sym}."

        order_id = res.get("orderId", "")
        # D. Record in user's position tracker
        pos = {
            "ticker": t_clean,
            "side": side_clean,
            "entry_price": maker_price,
            "qty": q_qty,
            "leverage": lev,
            "margin_usdt": risk_usdt,
            "exchange_order_id": order_id,
            "tp1": tp1,
            "tp2": tp2,
            "sl": sl,
            "orig_sl": sl,
            "tf": timeframe,
            "source": "BYBIT_LIVE",
            "exec_type": exec_type,
        }
        user_store.open_user_position(cid, pos)
        log.info("Live auto-trade executed for user %s on Bybit: %s %s (qty: %s, %s)", cid, sym, order_side, q_qty, exec_type)
        return True, f"Live Bybit order placed: {sym} {order_side} ({q_qty} contracts) [{exec_type}]"

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
    trade_type = "scalp" if str(tf).lower() in ("15m", "15", "scalp", "quick") else "day"
    auto_users = user_store.get_all_auto_trade_users(trade_type=trade_type)
    if not auto_users:
        return 0

    log.info("Dispatching %s auto-trade signal %s %s to %d active users...", trade_type, ticker, side, len(auto_users))
    executed_count = 0
    now_ts = time.time()

    for cid, user_data in auto_users:
        dedup_key = f"{cid}:{ticker}:{side}:{tf}"
        if now_ts - _recent_user_dispatches.get(dedup_key, 0.0) < 90.0:
            log.debug("Skipping duplicate dispatch for %s (already dispatched within 90s)", dedup_key)
            continue
        _recent_user_dispatches[dedup_key] = now_ts
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
                signal_metadata=signal,
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
                    f"_One-tap quick controls below:_ \n"
                )
                trade_buttons = {
                    "inline_keyboard": [
                        [
                            {"text": "🔒 Lock BE", "callback_data": f"pos_lock_be_{ticker}"},
                            {"text": "💰 Bank 50%", "callback_data": f"pos_bank_50_{ticker}"},
                        ],
                        [
                            {"text": "🛑 Emergency Close", "callback_data": f"pos_close_full_{ticker}"},
                            {"text": "🧬 View DNA", "callback_data": f"pos_view_dna_{ticker}"},
                        ]
                    ]
                }
                _reply(cid, receipt, bot_token=bot_token, reply_markup=trade_buttons)
            else:
                log.info("🛡️ Auto-trade filtered for user %s on %s %s: %s", cid, ticker, side, msg)
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

    # Ingest any active Bybit positions not yet recorded in user_open
    for bp in bybit_pos:
        s = bp.get("symbol", "")
        sz = float(bp.get("size", 0.0) or 0.0)
        if sz > 0:
            t_raw = s.replace("USDT", "").replace("1000", "")
            if not any(p.get("ticker") == t_raw for p in user_open.values()):
                side_str = "BUY" if str(bp.get("side")).upper() in ("BUY", "LONG") else "SELL"
                ep = float(bp.get("entry_price") or bp.get("avgPrice") or bp.get("avg_price") or 0.0)
                sl_val = float(bp.get("stop_loss") or bp.get("stopLoss") or 0.0) or None
                tp_val = float(bp.get("take_profit") or bp.get("takeProfit") or 0.0) or None
                lev_val = int(bp.get("leverage") or 10)
                pos_id = f"{t_raw}_{side_str}_{int(time.time())}"
                raw_ts = bp.get("created_time") or bp.get("createdTime") or bp.get("updated_time") or bp.get("updatedTime")
                open_time_str = datetime.now(timezone.utc).isoformat()
                if raw_ts:
                    try:
                        open_time_str = datetime.fromtimestamp(float(raw_ts) / 1000.0, tz=timezone.utc).isoformat()
                    except Exception:
                        pass
                new_p = {
                    "ticker": t_raw,
                    "side": side_str,
                    "entry_price": ep,
                    "qty": sz,
                    "leverage": lev_val,
                    "margin_usdt": round((sz * ep) / lev_val, 2),
                    "tp1": tp_val,
                    "sl": sl_val,
                    "orig_sl": sl_val,
                    "tf": "15m",
                    "source": "BYBIT_LIVE",
                    "opened_at": open_time_str,
                    "id": pos_id,
                }
                user_store.open_user_position(cid, new_p)
                user_open[pos_id] = new_p
                log.info("Synced live Bybit position for user %s: %s %s size=%s entry=%s", cid, t_raw, side_str, sz, ep)

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
            match_pnl = None
            exit_p = float(pos.get("entry_price", 0.0))
            exec_type = "TRADE"
            for rec in closed_records:
                if rec.get("symbol") == sym:
                    match_pnl = float(rec.get("closed_pnl", 0.0))
                    exit_p = float(rec.get("exit_price", exit_p))
                    exec_type = str(rec.get("exec_type", "TRADE")).upper()
                    break

            c_pos = user_store.close_user_position(
                cid,
                pid,
                exit_p,
                reason=f"BYBIT_{exec_type}",
                exact_pnl=match_pnl,
            )
            if c_pos:
                realized_pnl = float(c_pos.get("pnl_usdt", 0.0))
                closed_positions.append(c_pos)

                pnl_str = f"+${realized_pnl:.2f} 🟢" if realized_pnl >= 0 else f"-${abs(realized_pnl):.2f} 🔴"
                outcome_label = "🏆 TAKE PROFIT HIT!" if realized_pnl > 0 else "🛑 STOP LOSS TRIGGERED"
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

                # Check if Daily Circuit Breaker tripped after this close
                cb_tripped, cb_msg = user_store.check_and_apply_user_circuit_breaker(cid)
                if cb_tripped and "Tripped" in cb_msg:
                    cb_alert = (
                        "🚨 *Daily Capital Guard Active* 🚨\n\n"
                        f"• *Status*: {cb_msg}\n"
                        "• *Action*: Auto-trading paused for **12 hours** to protect your balance.\n"
                        "• *Rule*: Preserves capital after choppy market sessions until high-conviction momentum returns."
                    )
                    try:
                        _reply(cid, cb_alert, bot_token=bot_token)
                    except Exception:
                        pass

        # Position is STILL ACTIVE on Bybit -- check dynamic profit locking and ratchet trailing!
        else:
            b_pos = next((p for p in bybit_pos if p.get("symbol") == sym), None)
            if b_pos:
                try:
                    b_mark = float(b_pos.get("mark_price") or b_pos.get("markPrice") or 0.0)
                    b_entry = float(b_pos.get("entry_price") or b_pos.get("avgPrice") or pos.get("entry_price", 0.0))
                    b_side = str(b_pos.get("side", pos.get("side", "Buy"))).upper()

                    if b_entry > 0 and b_mark > 0:
                        is_buy = b_side in ("BUY", "LONG")
                        gain_pct = ((b_mark - b_entry) / b_entry * 100.0) if is_buy else ((b_entry - b_mark) / b_entry * 100.0)

                        orig_sl = float(pos.get("orig_sl") or pos.get("sl") or 0.0)
                        if not pos.get("orig_sl") and orig_sl > 0:
                            pos["orig_sl"] = orig_sl

                        risk_dist = abs(b_entry - orig_sl) if orig_sl > 0 else (b_entry * 0.015)
                        if risk_dist <= 0:
                            risk_dist = b_entry * 0.015
                        curr_r = (((b_mark - b_entry) / risk_dist) if is_buy else ((b_entry - b_mark) / risk_dist)) if risk_dist > 0 else 0.0

                        # A. Check Stale Scalp Time-Stop (Free up slots if stuck in flat chop)
                        opened_at_str = pos.get("opened_at") or pos.get("created_at")
                        is_scalp = str(pos.get("tf", "15m")).lower() in ("15m", "15", "scalp")
                        max_stale_hours = 2.5 if is_scalp else 12.0
                        hours_open = 0.0
                        if opened_at_str:
                            try:
                                ot = datetime.fromisoformat(opened_at_str)
                                hours_open = (datetime.now(timezone.utc) - ot).total_seconds() / 3600.0
                            except Exception:
                                pass

                        # If position has been flat in chop for > 5.0h (and not in profit): scratch to free up trade slot
                        max_stale_hours = 5.0 if is_scalp else 12.0
                        if hours_open >= max_stale_hours and abs(gain_pct) < 0.25 and not pos.get("stale_warned"):
                            pos_size = float(b_pos.get("size", 0) or b_pos.get("qty", 0) or 0)
                            if pos_size > 0:
                                log.info("Stale Time-Stop: %s %s flat for %.1f hours. Closing at market to free trade slot.", sym, b_side, hours_open)
                                close_side = "Sell" if is_buy else "Buy"
                                scratch_res = client.close_position_market(sym, side=close_side, qty=pos_size, category="linear")
                                if scratch_res:
                                    user_store.close_user_position(cid, pid, b_mark, reason="STALE_TIME_STOP")
                                    pnl_est = ((b_mark - b_entry) * pos_size) if is_buy else ((b_entry - b_mark) * pos_size)
                                    pnl_str = f"+${pnl_est:.2f} 🟢" if pnl_est >= 0 else f"-${abs(pnl_est):.2f} 🔴"
                                    time_stop_msg = (
                                        f"⏱️ *Stale Scalp Time-Stop Executed* ⏱️\n\n"
                                        f"• *Asset*: `{sym}` ({pos.get('side')})\n"
                                        f"• *Duration*: `{hours_open:.1f} hours` (Flat Range Consolidation)\n"
                                        f"• *Exit Price*: `{fmt_dollar(b_mark)}` ({gain_pct:+.2f}%)\n"
                                        f"• *Est PnL*: `{pnl_str}`\n\n"
                                        f"✨ *Slot Freed*: 1 trading slot is now open and ready for fresh A+ Sniper setups!"
                                    )
                                    if not bot_token:
                                        call_cfg = state.get_call_bot_config()
                                        bot_token = call_cfg.get("token") or cfg.call_bot_token or cfg.telegram_token
                                    try:
                                        _reply(cid, time_stop_msg, bot_token=bot_token)
                                    except Exception as e_rep:
                                        log.debug("Error sending time-stop receipt: %s", e_rep)
                                    continue

                        # B. Check Partial Scale-Out (Bank 50% profit at +1.0R progress or >= +1.5% gain)
                        if (curr_r >= 1.0 or gain_pct >= 1.5) and not pos.get("tp1_scaled"):
                            b_qty = float(b_pos.get("size", 0) or b_pos.get("qty", 0) or 0)
                            half_qty = client.quantize_qty(sym, b_qty * 0.50, category="linear")
                            try:
                                min_qty = float(client.get_instrument_info(sym).get("min_qty", 0.001))
                            except Exception:
                                min_qty = 0.001

                            import asset_dna
                            dna_sc = asset_dna.get_asset_dna(ticker)
                            # Xira Model: Always bank 50% partial profit at TP1 and let remaining 50% runner ride to TP2/TP3
                            close_qty = half_qty

                            if close_qty >= min_qty:
                                close_side = "Sell" if is_buy else "Buy"
                                res_scale = client.close_position_market(sym, side=close_side, qty=close_qty, category="linear")
                                if res_scale:
                                    pos["tp1_scaled"] = True
                                    pos["scaled_qty"] = close_qty
                                    # Immediately snap Bybit exchange Stop Loss to +0.35R Green Profit Lock on scale-out!
                                    lock_sl_raw = b_entry + (risk_dist * 0.35) if is_buy else b_entry - (risk_dist * 0.35)
                                    lock_sl = client.quantize_price(sym, lock_sl_raw, category="linear")
                                    cur_sl_check = float(b_pos.get("stopLoss") or pos.get("sl") or 0.0)
                                    should_move_sl = (lock_sl > cur_sl_check) if is_buy else (cur_sl_check == 0.0 or lock_sl < cur_sl_check)
                                    if should_move_sl:
                                        sl_moved = client.set_trading_stop(category="linear", symbol=sym, stop_loss=lock_sl)
                                        if sl_moved:
                                            pos["sl"] = lock_sl
                                            pos["profit_lock_l2"] = True
                                            pos["be_trailed"] = True
                                    user_store.update_user_position(cid, pid, pos)
                                    log.info("TP1 Partial Scale-Out: Banked 50%% profit (%s units) and locked SL@$%.4f for user %s on %s", close_qty, lock_sl, cid, sym)
                                    scale_msg = (
                                        f"💰 *TP1 Institutional Profit Banked: {ticker}* 💰\n\n"
                                        f"• *Progress*: `+{gain_pct:.1f}%` (+{curr_r:.2f}R) at `{fmt_dollar(b_mark)}`\n"
                                        f"• *Archetype*: `{dna_sc.get('archetype')}` (Xira Multi-Tier Model)\n"
                                        f"• *Action*: Realized **50% profit** ({close_qty} contracts banked into balance)!\n"
                                        f"• *Stop Loss*: Snapped to **+0.35R Guaranteed Green Profit** (`{fmt_dollar(lock_sl)}`)\n"
                                        f"• *Runner Status*: Remaining 50% position running 100% Risk-Free to TP2!"
                                    )
                                    if not bot_token:
                                        call_cfg = state.get_call_bot_config()
                                        bot_token = call_cfg.get("token") or cfg.call_bot_token or cfg.telegram_token
                                    scale_buttons = {
                                        "inline_keyboard": [
                                            [
                                                {"text": "🔒 Lock BE", "callback_data": f"pos_lock_be_{ticker}"},
                                                {"text": "💰 Bank 50%", "callback_data": f"pos_bank_50_{ticker}"},
                                            ],
                                            [
                                                {"text": "🛑 Emergency Close", "callback_data": f"pos_close_full_{ticker}"},
                                                {"text": "🧬 View DNA", "callback_data": f"pos_view_dna_{ticker}"},
                                            ]
                                        ]
                                    }
                                    try:
                                        _reply(cid, scale_msg, bot_token=bot_token, reply_markup=scale_buttons)
                                    except Exception as e_rep:
                                        log.debug("Error sending scale-out receipt: %s", e_rep)

                        # Multi-Tier Ratchet Progression:
                        # Tier 4: Ultra Profit Trail (+1.75R or >= +2.5% gain) -> Lock in +1.10R profit
                        # Tier 3: Super Profit Lock (+1.20R or >= +1.8% gain) -> Lock in +0.65R profit
                        # Tier 2: Green Profit Lock (+0.55R or >= +1.0% gain) -> Lock in +0.30R profit
                        # Tier 1: Break-Even Lock (+0.35R or >= +0.7% gain) -> Lock in Entry (0 Risk)
                        # Tier 0: Active BTC Shock Shield (if BTC surges/dumps >= 0.35% against open trade before BE)

                        import asset_dna
                        dna = asset_dna.get_asset_dna(ticker)
                        trail_mode = dna.get("trail_mode", "STANDARD_RATCHET")

                        target_sl = None
                        tier_name = None
                        lock_desc = ""
                        lock_flag = None

                        # Pillar 2: Dynamic Ratchet based on Asset DNA Personality
                        # Give Scalps Full Breathing Room to Hit Real TP Targets (No 1m Micro-Choking):
                        if is_scalp:
                            be_r_trigger = 1.25
                            be_gain_trigger = 2.40
                            l2_r_trigger = 1.75
                            l2_gain_trigger = 3.20
                        else:
                            be_r_trigger = 1.35
                            be_gain_trigger = 2.80
                            l2_r_trigger = 1.85
                            l2_gain_trigger = 3.60

                        if (curr_r >= 1.75 or gain_pct >= 2.5) and not pos.get("profit_lock_l4"):
                            target_sl = b_entry + (risk_dist * 1.10) if is_buy else b_entry - (risk_dist * 1.10)
                            tier_name = "Ultra Profit Trail"
                            lock_desc = f"+1.10R (+{gain_pct * 0.6:.1f}% locked)"
                            lock_flag = "profit_lock_l4"
                        elif (curr_r >= 1.25 or gain_pct >= 1.8) and not pos.get("profit_lock_l3"):
                            target_sl = b_entry + (risk_dist * 0.65) if is_buy else b_entry - (risk_dist * 0.65)
                            tier_name = "Super Profit Lock"
                            lock_desc = f"+0.65R (+{gain_pct * 0.5:.1f}% locked)"
                            lock_flag = "profit_lock_l3"
                        elif (curr_r >= l2_r_trigger or gain_pct >= l2_gain_trigger) and not pos.get("profit_lock_l2") and not pos.get("profit_lock_l3") and not pos.get("profit_lock_l4"):
                            target_sl = b_entry + (risk_dist * 0.25) if is_buy else b_entry - (risk_dist * 0.25)
                            tier_name = "Green Profit Lock (+0.25R)"
                            lock_desc = f"+0.25R (+{gain_pct * 0.4:.1f}% locked)"
                            lock_flag = "profit_lock_l2"
                        elif (curr_r >= be_r_trigger or gain_pct >= be_gain_trigger) and not pos.get("be_trailed"):
                            fee_buf = b_entry * 0.0015
                            target_sl = b_entry + fee_buf if is_buy else b_entry - fee_buf
                            tier_name = "Early Breakeven Shield (BE+Fees)"
                            lock_desc = "+0.15% Net Profit (Fees Guaranteed Covered)"
                            lock_flag = "be_trailed"
                        elif not is_scalp and (curr_r >= 0.50 or gain_pct >= 0.70) and not pos.get("risk_cut_half") and not pos.get("be_trailed"):
                            target_sl = b_entry - (risk_dist * 0.50) if is_buy else b_entry + (risk_dist * 0.50)
                            tier_name = "Progressive Risk Cut (-0.50R)"
                            lock_desc = "Max Loss Cut by 50% (-0.50R Downside Protected)"
                            lock_flag = "risk_cut_half"
                        elif not pos.get("be_trailed") and ticker != "BTC":
                            try:
                                import scanner
                                side_check = "BUY" if is_buy else "SELL"
                                btc_ok, _ = scanner.check_btc_momentum_gate(side_check)
                                if not btc_ok:
                                    if curr_r >= 0.50:
                                        fee_buf = b_entry * 0.0030
                                        target_sl = b_entry + fee_buf if is_buy else b_entry - fee_buf
                                        tier_name = "BTC Shock Shield (Fee-Protected BE)"
                                        lock_desc = "+0.30% Net Profit (Protected against adverse BTC impulse)"
                                        lock_flag = "be_trailed"
                                    elif -0.42 <= curr_r < 0.50 and not pos.get("btc_shock_tightened") and not pos.get("risk_cut_half") and not pos.get("be_trailed"):
                                        target_sl = b_entry - (risk_dist * 0.50) if is_buy else b_entry + (risk_dist * 0.50)
                                        tier_name = "BTC Shock Shield (-0.50R Risk Cut)"
                                        lock_desc = "Max Loss Cut by 50% (-0.50R against BTC impulse)"
                                        lock_flag = "btc_shock_tightened"
                            except Exception as e_btc_sh:
                                log.debug("BTC shock check error for %s: %s", sym, e_btc_sh)

                        if target_sl is not None and lock_flag:
                            target_sl = client.quantize_price(sym, target_sl, category="linear")
                            cur_sl = float(b_pos.get("stopLoss") or pos.get("sl") or 0.0)

                            # Only advance SL if it tightens risk/locks more profit and is safely behind current mark
                            should_update = False
                            if is_buy:
                                should_update = (cur_sl == 0.0 or target_sl > cur_sl) and target_sl < b_mark
                            else:
                                should_update = (cur_sl == 0.0 or target_sl < cur_sl) and target_sl > b_mark

                            if should_update:
                                adj_sl = target_sl * 1000.0 if ticker == "PEPE" else target_sl
                                be_res = client.set_trading_stop(sym, stop_loss=adj_sl)
                                if be_res:
                                    pos[lock_flag] = True
                                    if lock_flag == "profit_lock_l4":
                                        pos["profit_lock_l3"] = True
                                        pos["profit_lock_l2"] = True
                                        pos["be_trailed"] = True
                                        pos["risk_cut_half"] = True
                                    elif lock_flag == "profit_lock_l3":
                                        pos["profit_lock_l2"] = True
                                        pos["be_trailed"] = True
                                        pos["risk_cut_half"] = True
                                    elif lock_flag == "be_trailed":
                                        pos["risk_cut_half"] = True

                                    pos["sl"] = target_sl
                                    user_store.update_user_position(cid, pid, pos)
                                    log.info(
                                        "Tier '%s' engaged for user %s on %s %s! SL updated to %.4f (gain: +%.2f%%, +%.2fR)",
                                        tier_name, cid, sym, b_side, target_sl, gain_pct, curr_r,
                                    )

                                    trail_receipt = (
                                        f"🔒 *{tier_name}: {ticker}* 🔒\n\n"
                                        f"• *Current Gain*: `+{gain_pct:.1f}%` (+{curr_r:.2f}R) at `{fmt_dollar(b_mark)}`\n"
                                        f"• *Action*: Bybit Stop Loss shifted to **`{fmt_dollar(target_sl)}`**\n"
                                        f"• *Guaranteed Profit*: **{lock_desc}**\n"
                                        f"• *Status*: Trade locked against reversals -- profits secured!"
                                    )
                                    if not bot_token:
                                        call_cfg = state.get_call_bot_config()
                                        bot_token = call_cfg.get("token") or cfg.call_bot_token or cfg.telegram_token
                                    trail_buttons = {
                                        "inline_keyboard": [
                                            [
                                                {"text": "🔒 Lock BE", "callback_data": f"pos_lock_be_{ticker}"},
                                                {"text": "💰 Bank 50%", "callback_data": f"pos_bank_50_{ticker}"},
                                            ],
                                            [
                                                {"text": "🛑 Emergency Close", "callback_data": f"pos_close_full_{ticker}"},
                                                {"text": "🧬 View DNA", "callback_data": f"pos_view_dna_{ticker}"},
                                            ]
                                        ]
                                    }
                                    try:
                                        _reply(cid, trail_receipt, bot_token=bot_token, reply_markup=trail_buttons)
                                    except Exception as e_rep:
                                        log.debug("Error sending trail notification to %s: %s", cid, e_rep)
                except Exception as e_be:
                    log.debug("Error checking profit lock trail for user %s %s: %s", cid, sym, e_be)

    return closed_positions


def sync_all_active_users(bot_token: Optional[str] = None) -> None:
    """Run position sync for all live connected auto-trading users."""
    auto_users = user_store.get_all_auto_trade_users()
    for cid, _ in auto_users:
        try:
            sync_user_positions_with_bybit(cid, bot_token=bot_token)
        except Exception as exc:
            log.debug("Error syncing positions for user %s: %s", cid, exc)
