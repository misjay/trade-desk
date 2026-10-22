"""
user_store.py — Multi-Tenant User State & Personal Portfolio Engine for Winz.

Provides dedicated isolated environments for every public user:
  - Personal virtual paper equity ($10,000 starting demo)
  - Personal open and closed positions
  - Personal win/loss session analytics
  - Personal pause / resume controls
  - Personal avoid / blacklist assets
  - Personal leverage overrides

Guarantees complete isolation between public users and the Master Trade Desk.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

log = logging.getLogger(__name__)

_USERS_FILE = Path(__file__).parent / "users_state.json"
_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_all() -> Dict[str, Any]:
    if _USERS_FILE.exists():
        try:
            with open(_USERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            log.error("Error loading user store: %s — starting fresh", exc)
    return {}


def _save_all(data: Dict[str, Any]) -> None:
    content = json.dumps(data, indent=2)
    for attempt in range(6):
        tmp = _USERS_FILE.with_suffix(f".tmp_{os.getpid()}_{attempt}")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
            tmp.replace(_USERS_FILE)
            return
        except OSError:
            if tmp.exists():
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
            time.sleep(0.04)

    try:
        with open(_USERS_FILE, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as exc:
        log.warning("User store direct write fallback error: %s", exc)


def _default_user_state(chat_id: str) -> Dict[str, Any]:
    cid = str(chat_id).strip()
    return {
        "user_id": cid,
        "paper_equity": 10000.0,
        "paper_equity_start": 10000.0,
        "high_water_mark": 10000.0,
        "is_paused": False,
        "is_scalp_paused": False,
        "is_day_paused": False,
        "open_positions": {},      # key: pos_id -> dict
        "closed_positions": [],
        "session": {
            "wins": 0,
            "losses": 0,
            "breakevens": 0,
            "total_pnl_usdt": 0.0,
        },
        "avoid_list": [],
        "custom_leverage": {},
        "has_access": False,
        "access_code": None,
        "access_granted_at": None,
        "exchange": {
            "connected": False,
            "name": "bybit",
            "api_key_masked": "",
            "encrypted_api_key": "",
            "encrypted_api_secret": "",
            "is_testnet": False,
            "auto_trade": False,
            "risk_per_trade_usdt": 50.0,
            "connected_at": None,
        },
        "created_at": _now(),
        "updated_at": _now(),
    }


def get_user(chat_id: Union[str, int]) -> Dict[str, Any]:
    """Retrieve user state dict. Initializes with $10,000 demo if new."""
    cid = str(chat_id).strip()
    with _lock:
        all_users = _load_all()
        if cid not in all_users:
            all_users[cid] = _default_user_state(cid)
            _save_all(all_users)
        return all_users[cid]


def _save_user(chat_id: Union[str, int], user_data: Dict[str, Any]) -> None:
    cid = str(chat_id).strip()
    user_data["updated_at"] = _now()
    with _lock:
        all_users = _load_all()
        all_users[cid] = user_data
        _save_all(all_users)


# ── Equity & Pause ────────────────────────────────────────────────────────────

def get_user_equity(chat_id: Union[str, int]) -> float:
    return float(get_user(chat_id).get("paper_equity", 10000.0))


def set_user_equity(chat_id: Union[str, int], equity: float) -> None:
    u = get_user(chat_id)
    u["paper_equity"] = round(equity, 4)
    _save_user(chat_id, u)


def is_user_paused(chat_id: Union[str, int]) -> bool:
    u = get_user(chat_id)
    return bool(u.get("is_paused", False) or (u.get("is_scalp_paused", False) and u.get("is_day_paused", False)))


def is_user_scalp_paused(chat_id: Union[str, int]) -> bool:
    u = get_user(chat_id)
    return bool(u.get("is_paused", False) or u.get("is_scalp_paused", False))


def is_user_day_paused(chat_id: Union[str, int]) -> bool:
    u = get_user(chat_id)
    return bool(u.get("is_paused", False) or u.get("is_day_paused", False))


def is_user_paused_for_type(chat_id: Union[str, int], trade_type: str = "scalp") -> bool:
    tt = str(trade_type).lower()
    if tt in ("scalp", "15m", "15", "quick"):
        return is_user_scalp_paused(chat_id)
    return is_user_day_paused(chat_id)


def set_user_paused(chat_id: Union[str, int], paused: bool, mode: str = "all") -> None:
    u = get_user(chat_id)
    m = str(mode).lower()
    if m in ("scalp", "scalping"):
        u["is_scalp_paused"] = bool(paused)
        if not paused and not u.get("is_day_paused"):
            u["is_paused"] = False
        elif paused and u.get("is_day_paused"):
            u["is_paused"] = True
        else:
            u["is_paused"] = False
    elif m in ("day", "daytrade", "swing", "4h"):
        u["is_day_paused"] = bool(paused)
        if not paused and not u.get("is_scalp_paused"):
            u["is_paused"] = False
        elif paused and u.get("is_scalp_paused"):
            u["is_paused"] = True
        else:
            u["is_paused"] = False
    else:
        u["is_paused"] = bool(paused)
        u["is_scalp_paused"] = bool(paused)
        u["is_day_paused"] = bool(paused)
    _save_user(chat_id, u)


def set_user_execution_mode(chat_id: Union[str, int], mode: str) -> Tuple[bool, str]:
    u = get_user(chat_id)
    m = str(mode).lower()
    if m in ("day_only", "onlyday", "dayonly", "only_day", "day", "daytrade", "daytrades", "swing"):
        u["is_scalp_paused"] = True
        u["is_day_paused"] = False
        u["is_paused"] = False
        _save_user(chat_id, u)
        return True, "🟡 *DAY TRADES ONLY Mode Activated.*\nYour 15m scalps are PAUSED. 4H day trades are ACTIVE."
    elif m in ("scalp_only", "onlyscalp", "scalponly", "only_scalp", "scalp", "scalps", "scalping", "quick"):
        u["is_scalp_paused"] = False
        u["is_day_paused"] = True
        u["is_paused"] = False
        _save_user(chat_id, u)
        return True, "🟡 *SCALP ONLY Mode Activated.*\nYour 4H day trades are PAUSED. 15m scalps are ACTIVE."
    elif m in ("all", "resume", "both", "resume_all"):
        u["is_scalp_paused"] = False
        u["is_day_paused"] = False
        u["is_paused"] = False
        _save_user(chat_id, u)
        return True, "🟢 *ALL TRADING ACTIVE.*\nBoth 15m scalps and 4H day trades are RUNNING on your account."
    elif m in ("pause", "pause_all", "freeze"):
        u["is_scalp_paused"] = True
        u["is_day_paused"] = True
        u["is_paused"] = True
        _save_user(chat_id, u)
        return True, "⏸ *ALL TRADING PAUSED.*\nBoth scalps and day trades are FROZEN on your account."
    else:
        return False, f"Unknown mode `{mode}`."


def get_user_execution_mode_status(chat_id: Union[str, int]) -> str:
    u = get_user(chat_id)
    paused = u.get("is_paused", False)
    sc_p = u.get("is_scalp_paused", False)
    day_p = u.get("is_day_paused", False)
    if paused or (sc_p and day_p):
        return "⏸ ALL PAUSED (Both Scalps & Day Trades Frozen)"
    elif sc_p:
        return "🟡 DAY TRADES ONLY (15m Scalps Paused, 4H Day Active)"
    elif day_p:
        return "🟡 SCALPS ONLY (4H Day Paused, 15m Scalps Active)"
    else:
        return "🟢 ALL ACTIVE (Both Scalps & Day Trades Running)"


# ── Positions ─────────────────────────────────────────────────────────────────

def get_user_open_positions(chat_id: Union[str, int]) -> Dict[str, dict]:
    return dict(get_user(chat_id).get("open_positions", {}))


def get_user_closed_positions(chat_id: Union[str, int]) -> List[dict]:
    return list(get_user(chat_id).get("closed_positions", []))


def get_user_session_stats(chat_id: Union[str, int]) -> dict:
    return dict(get_user(chat_id).get("session", {}))


def open_user_position(chat_id: Union[str, int], pos: dict) -> dict:
    """Register an open position in the user's isolated portfolio."""
    u = get_user(chat_id)
    pid = pos.get("id") or f"{pos.get('ticker', 'COIN')}_{pos.get('side', 'BUY')}_{int(time.time())}"
    pos["id"] = pid
    pos["opened_at"] = pos.get("opened_at", _now())
    pos["status"] = "OPEN"
    pos.setdefault("leverage", u.get("custom_leverage", {}).get(pos.get("ticker", ""), 10))

    u["open_positions"][pid] = pos
    _save_user(chat_id, u)
    log.info("User %s opened personal position: %s %s", chat_id, pos.get("ticker"), pos.get("side"))
    return pos


def close_user_position(
    chat_id: Union[str, int],
    pos_id: str,
    exit_price: float,
    reason: str = "MANUAL",
    exact_pnl: Optional[float] = None,
) -> Optional[dict]:
    """Close a specific position in the user's portfolio and credit/debit PnL."""
    u = get_user(chat_id)
    pos = u["open_positions"].pop(pos_id, None)
    if not pos:
        return None

    entry = float(pos.get("entry_price", exit_price))
    qty = float(pos.get("qty", 1.0))
    side = pos.get("side", "BUY").upper()
    leverage = int(pos.get("leverage", 10))

    if exact_pnl is not None:
        pnl_usdt = float(exact_pnl)
        notional = entry * qty
        margin = float(pos.get("margin_usdt", 0.0)) or (notional / max(leverage, 1))
        pnl_pct = (pnl_usdt / margin) if margin > 0 else 0.0
    elif entry > 0:
        raw_pct = ((exit_price - entry) / entry) if side == "BUY" else ((entry - exit_price) / entry)
        pnl_pct = raw_pct * leverage
        notional = entry * qty
        margin = notional / max(leverage, 1)
        pnl_usdt = raw_pct * notional
    else:
        pnl_pct = 0.0
        pnl_usdt = 0.0

    pos["exit_price"] = exit_price
    pos["exit_reason"] = reason
    pos["closed_at"] = _now()
    pos["pnl_pct"] = round(pnl_pct * 100.0, 2)
    pos["pnl_usdt"] = round(pnl_usdt, 4)
    pos["status"] = "CLOSED"

    u["closed_positions"].append(pos)
    u["paper_equity"] = round(u["paper_equity"] + pnl_usdt, 4)

    sess = u.setdefault("session", {"wins": 0, "losses": 0, "breakevens": 0, "total_pnl_usdt": 0.0})
    if pnl_usdt > 0.01:
        sess["wins"] += 1
    elif pnl_usdt < -0.01:
        sess["losses"] += 1
    else:
        sess["breakevens"] += 1
    sess["total_pnl_usdt"] = round(sess["total_pnl_usdt"] + pnl_usdt, 4)

    _save_user(chat_id, u)
    return pos


def update_user_position(chat_id: Union[str, int], pos_id: str, updates: dict) -> bool:
    """Update fields of an active open position in user_store."""
    u = get_user(chat_id)
    if pos_id in u.get("open_positions", {}):
        u["open_positions"][pos_id].update(updates)
        _save_user(chat_id, u)
        return True
    return False


def close_all_user_positions(
    chat_id: Union[str, int],
    fetch_price_fn: Callable[[str], float],
    reason: str = "CLOSEALL",
) -> List[dict]:
    """Close all open positions in the user's personal portfolio."""
    u = get_user(chat_id)
    closed = []
    for pid in list(u.get("open_positions", {}).keys()):
        pos = u["open_positions"][pid]
        ticker = pos.get("ticker", "BTC")
        lp = fetch_price_fn(ticker) or float(pos.get("entry_price", 0.0))
        c_pos = close_user_position(chat_id, pid, lp, reason=reason)
        if c_pos:
            closed.append(c_pos)
    return closed


def tp_user_positions(
    chat_id: Union[str, int],
    fetch_price_fn: Callable[[str], float],
) -> List[dict]:
    """Close all positions currently in profit for this user."""
    u = get_user(chat_id)
    closed = []
    for pid, pos in list(u.get("open_positions", {}).items()):
        ticker = pos.get("ticker", "BTC")
        entry = float(pos.get("entry_price", 0.0))
        side = pos.get("side", "BUY").upper()
        lp = fetch_price_fn(ticker) or entry

        is_profit = (lp > entry) if side == "BUY" else (lp < entry)
        if is_profit:
            c = close_user_position(chat_id, pid, lp, reason="TAKE_PROFIT")
            if c:
                closed.append(c)
    return closed


def derisk_user_positions(
    chat_id: Union[str, int],
    fetch_price_fn: Callable[[str], float],
) -> Tuple[List[dict], List[dict]]:
    """Close winners 100% and trim losers by 50% for this user."""
    u = get_user(chat_id)
    closed_winners = []
    trimmed_losers = []

    for pid, pos in list(u.get("open_positions", {}).items()):
        ticker = pos.get("ticker", "BTC")
        entry = float(pos.get("entry_price", 0.0))
        side = pos.get("side", "BUY").upper()
        lp = fetch_price_fn(ticker) or entry

        is_winner = (lp > entry) if side == "BUY" else (lp < entry)
        if is_winner:
            c = close_user_position(chat_id, pid, lp, reason="DERISK_WINNER")
            if c:
                closed_winners.append(c)
        else:
            # Trim 50% of position size
            old_qty = float(pos.get("qty", 1.0))
            if old_qty > 0.0001:
                half_qty = old_qty * 0.5
                pos["qty"] = round(half_qty, 6)
                pos["derisked"] = True
                trimmed_losers.append(pos)
                _save_user(chat_id, u)

    return closed_winners, trimmed_losers


# ── Avoid List & Leverage ─────────────────────────────────────────────────────

def get_user_avoid_list(chat_id: Union[str, int]) -> List[str]:
    return list(get_user(chat_id).get("avoid_list", []))


def add_user_avoid(chat_id: Union[str, int], tickers: List[str]) -> List[str]:
    u = get_user(chat_id)
    avoid_set = set(u.get("avoid_list", []))
    for t in tickers:
        t_clean = t.strip().upper().replace("USDT", "")
        if t_clean:
            avoid_set.add(t_clean)
    u["avoid_list"] = sorted(list(avoid_set))
    _save_user(chat_id, u)
    return u["avoid_list"]


def remove_user_avoid(chat_id: Union[str, int], tickers: List[str]) -> List[str]:
    u = get_user(chat_id)
    avoid_set = set(u.get("avoid_list", []))
    for t in tickers:
        t_clean = t.strip().upper().replace("USDT", "")
        avoid_set.discard(t_clean)
    u["avoid_list"] = sorted(list(avoid_set))
    _save_user(chat_id, u)
    return u["avoid_list"]


def get_user_leverage(chat_id: Union[str, int], ticker: str = "") -> Union[int, Dict[str, int]]:
    u = get_user(chat_id)
    lev_map = u.get("custom_leverage", {})
    if ticker:
        return int(lev_map.get(ticker.upper(), 10))
    return dict(lev_map)


def set_user_leverage(chat_id: Union[str, int], ticker: str, lev: int) -> int:
    u = get_user(chat_id)
    t = ticker.strip().upper().replace("USDT", "")
    val = max(1, min(int(lev), 50))
    u.setdefault("custom_leverage", {})[t] = val
    _save_user(chat_id, u)
    return val


def reset_user_demo(chat_id: Union[str, int]) -> dict:
    """Reset the user's paper demo equity back to $10,000 and clear positions."""
    fresh = _default_user_state(str(chat_id))
    _save_user(chat_id, fresh)
    log.info("Reset user %s demo portfolio to default $10,000", chat_id)
    return fresh


# ── Exchange Configuration & Auto-Trade ──────────────────────────────────────

def set_user_exchange(
    chat_id: Union[str, int],
    exchange_name: str,
    api_key_enc: str,
    api_secret_enc: str,
    api_key_masked: str,
    is_testnet: bool = False,
    auto_trade: bool = False,
    risk_per_trade: float = 50.0,
) -> dict:
    """Save encrypted exchange credentials into user's profile."""
    u = get_user(chat_id)
    u["exchange"] = {
        "connected": True,
        "name": exchange_name.lower().strip(),
        "api_key_masked": api_key_masked,
        "encrypted_api_key": api_key_enc,
        "encrypted_api_secret": api_secret_enc,
        "is_testnet": is_testnet,
        "auto_trade": auto_trade,
        "risk_per_trade_usdt": max(5.0, float(risk_per_trade)),
        "connected_at": _now(),
    }
    _save_user(chat_id, u)
    return u["exchange"]


def get_user_exchange(chat_id: Union[str, int]) -> dict:
    """Retrieve user's exchange configuration dictionary."""
    return dict(get_user(chat_id).get("exchange", {
        "connected": False,
        "name": "bybit",
        "api_key_masked": "",
        "auto_trade": False,
        "risk_per_trade_usdt": 50.0,
    }))


def disconnect_user_exchange(chat_id: Union[str, int]) -> bool:
    """Securely disconnect exchange and wipe all encrypted keys."""
    u = get_user(chat_id)
    u["exchange"] = {
        "connected": False,
        "name": "bybit",
        "api_key_masked": "",
        "encrypted_api_key": "",
        "encrypted_api_secret": "",
        "is_testnet": False,
        "auto_trade": False,
        "risk_per_trade_usdt": 50.0,
        "disconnected_at": _now(),
    }
    _save_user(chat_id, u)
    log.info("User %s disconnected their exchange and wiped API keys", chat_id)
    return True


def set_user_auto_trade(chat_id: Union[str, int], enabled: bool) -> bool:
    """Enable or disable automated signal execution for user."""
    u = get_user(chat_id)
    ex = u.setdefault("exchange", {})
    ex["auto_trade"] = bool(enabled)
    _save_user(chat_id, u)
    return ex["auto_trade"]


def set_user_risk_per_trade(chat_id: Union[str, int], risk_usdt: float) -> float:
    """Set the fixed USDT margin allocated per automated trade."""
    u = get_user(chat_id)
    ex = u.setdefault("exchange", {})
    val = max(1.0, min(10000.0, float(risk_usdt)))
    ex["risk_per_trade_usdt"] = val
    _save_user(chat_id, u)
    return val


def get_user_compounding_pct(chat_id: Union[str, int]) -> float:
    """Retrieve user's compounding percentage (0.0 if disabled)."""
    u = get_user(chat_id)
    ex = u.get("exchange", {})
    return float(ex.get("compounding_pct", 0.0) or 0.0)


def set_user_compounding_pct(chat_id: Union[str, int], pct: float) -> float:
    """
    Set dynamic balance compounding percentage (e.g. 1.0 = 1.0% of balance per trade).
    Setting to 0.0 disables compounding and falls back to fixed margin.
    """
    u = get_user(chat_id)
    ex = u.setdefault("exchange", {})
    val = round(max(0.0, min(10.0, float(pct))), 2)
    ex["compounding_pct"] = val
    _save_user(chat_id, u)
    log.info("User %s compounding set to %.2f%%", chat_id, val)
    return val


def get_all_auto_trade_users(trade_type: str = "scalp") -> List[Tuple[str, dict]]:
    """Return all authorized users who have explicitly enabled auto_trade and are not paused for this trade_type."""
    import access_manager
    with _lock:
        all_users = _load_all()
        active = []
        for cid, u in all_users.items():
            # Must be authorized to use Winz desk
            if not access_manager.is_user_authorized(cid):
                continue
            # Must not be paused for this trade type
            if is_user_paused_for_type(cid, trade_type):
                continue
            ex = u.get("exchange", {})
            # Must have explicitly set auto_trade to True
            if ex.get("auto_trade") is True:
                active.append((cid, u))
        return active


def get_all_users() -> Dict[str, dict]:
    """Return dict of all registered users."""
    with _lock:
        return dict(_load_all())


# ── Access Authorization ───────────────────────────────────────────────────────

def is_user_has_access(chat_id: Union[str, int]) -> bool:
    """Return True if user has been granted access in their profile."""
    return bool(get_user(chat_id).get("has_access", False))


def set_user_has_access(chat_id: Union[str, int], has_access: bool, code: Optional[str] = None) -> None:
    """Update user access authorization state."""
    u = get_user(chat_id)
    u["has_access"] = bool(has_access)
    if has_access:
        u["access_code"] = code
        u["access_granted_at"] = _now()
    else:
        u["access_revoked_at"] = _now()
    _save_user(chat_id, u)


# ── Target Growth & Goal Engine ──────────────────────────────────────────────

def set_user_goal(
    chat_id: Union[str, int],
    starting_capital: float,
    target_capital: float,
    timeframe_days: int = 30,
    max_daily_loss_pct: float = 5.0,
    leverage: int = 5,
    target_daily_return_pct: Optional[float] = None,
) -> dict:
    """Initialize or update a user's target growth goal."""
    u = get_user(chat_id)
    now_dt = datetime.now(timezone.utc)
    
    # Calculate initial daily return if not provided
    from goal_tracker import calculate_compound_daily_rate
    calc_rate = calculate_compound_daily_rate(starting_capital, target_capital, timeframe_days)
    daily_rate = float(target_daily_return_pct) if target_daily_return_pct else calc_rate

    goal = {
        "active": True,
        "starting_capital": round(float(starting_capital), 2),
        "target_capital": round(float(target_capital), 2),
        "timeframe_days": int(timeframe_days),
        "max_daily_loss_pct": round(float(max_daily_loss_pct), 1),
        "leverage": max(1, min(25, int(leverage))),
        "target_daily_return_pct": daily_rate,
        "start_date": now_dt.date().isoformat(),
        "created_at": now_dt.isoformat(),
        "morning_equity": round(float(starting_capital), 2),
        "morning_date": now_dt.date().isoformat(),
        "daily_history": [],
    }
    u["goal"] = goal
    _save_user(chat_id, u)
    log.info("User %s initialized growth goal: $%s -> $%s in %d days", chat_id, starting_capital, target_capital, timeframe_days)
    return goal


def get_user_goal(chat_id: Union[str, int]) -> Optional[dict]:
    """Retrieve user's goal if active, else None."""
    u = get_user(chat_id)
    goal = u.get("goal")
    if goal and goal.get("active"):
        return dict(goal)
    return None


def cancel_user_goal(chat_id: Union[str, int]) -> bool:
    """Deactivate user's target goal."""
    u = get_user(chat_id)
    if "goal" in u:
        u["goal"]["active"] = False
        u["goal"]["cancelled_at"] = _now()
        _save_user(chat_id, u)
        return True
    return False


def update_goal_morning_equity_if_new_day(chat_id: Union[str, int], current_equity: float) -> dict:
    """Updates the morning reference equity at 00:00 UTC rollover for daily 5% loss tracking."""
    u = get_user(chat_id)
    goal = u.get("goal")
    if not goal or not goal.get("active"):
        return {}

    today_str = datetime.now(timezone.utc).date().isoformat()
    if goal.get("morning_date") != today_str:
        # Record previous day snapshot into history
        prev_m = float(goal.get("morning_equity", current_equity))
        day_pnl = current_equity - prev_m
        goal.setdefault("daily_history", []).append({
            "date": goal.get("morning_date", today_str),
            "start_equity": prev_m,
            "end_equity": current_equity,
            "pnl": round(day_pnl, 2),
            "pnl_pct": round((day_pnl / prev_m) * 100.0, 2) if prev_m > 0 else 0.0,
        })
        # Reset morning baseline
        goal["morning_equity"] = round(current_equity, 2)
        goal["morning_date"] = today_str
        _save_user(chat_id, u)

    return goal



def get_user_auto_trade_mode(chat_id: Union[str, int]) -> str:
    """Get the auto-trade filtering mode: 'hybrid' (70% scalp / 30% trend), 'sniper', or 'normal'."""
    u = get_user(chat_id)
    return str(u.get("auto_trade_mode", "hybrid")).lower()


def set_user_auto_trade_mode(chat_id: Union[str, int], mode: str) -> str:
    """Set the auto-trade filtering mode to 'hybrid', 'sniper', or 'normal'."""
    u = get_user(chat_id)
    m = str(mode).strip().lower()
    if m in ("normal", "std", "standard", "off"):
        m_clean = "normal"
    elif m in ("sniper", "tight"):
        m_clean = "sniper"
    else:
        m_clean = "hybrid"
    u["auto_trade_mode"] = m_clean
    _save_user(chat_id, u)
    return m_clean


def is_user_circuit_breaker_active(chat_id: Union[str, int]) -> Tuple[bool, str]:
    """Check if the user is currently under an active circuit breaker cooldown."""
    cid = str(chat_id).strip()
    u = get_user(cid)
    paused_until_str = u.get("circuit_breaker_until")
    if paused_until_str:
        try:
            p_until = datetime.fromisoformat(paused_until_str.replace("Z", "+00:00"))
            now_dt = datetime.now(timezone.utc)
            if now_dt < p_until:
                remaining_m = int((p_until - now_dt).total_seconds() / 60)
                return True, f"Daily Loss Circuit Breaker Active: Cooldown for another {remaining_m} minutes."
            else:
                u["circuit_breaker_until"] = None
                u["circuit_breaker_reset_at"] = now_dt.isoformat()
                _save_user(cid, u)
        except Exception:
            pass
    return False, ""


def check_and_apply_user_circuit_breaker(chat_id: Union[str, int]) -> Tuple[bool, str]:
    """
    User Account Daily Loss Circuit Breaker:
    Checks user's closed trades in the past 24 hours (or since last circuit breaker reset).
    If the last 2 consecutive closed trades were losses OR net loss >= $3.00:
    Triggers a 12-hour cooling pause to protect balance.
    Returns: (is_paused, reason)
    """
    cid = str(chat_id).strip()
    u = get_user(cid)

    # 1. Check if already active
    is_active, active_reason = is_user_circuit_breaker_active(cid)
    if is_active:
        return True, active_reason

    closed = u.get("closed_positions", [])
    if len(closed) < 2:
        return False, ""

    # 2. Check 24-hour window, or since last circuit breaker reset
    now_dt = datetime.now(timezone.utc)
    cutoff = now_dt - timedelta(hours=24)
    last_reset_str = u.get("circuit_breaker_reset_at")
    if last_reset_str:
        try:
            reset_dt = datetime.fromisoformat(last_reset_str.replace("Z", "+00:00"))
            if reset_dt > cutoff:
                cutoff = reset_dt
        except Exception:
            pass

    recent_closed = []
    total_loss_24h = 0.0

    for p in closed:
        c_at = p.get("closed_at")
        if c_at:
            try:
                dt = datetime.fromisoformat(c_at.replace("Z", "+00:00"))
                if dt >= cutoff:
                    pnl = float(p.get("pnl_usdt", 0.0) or p.get("pnl", 0.0) or 0.0)
                    total_loss_24h += pnl
                    recent_closed.append(p)
            except Exception:
                pass

    if len(recent_closed) < 2:
        return False, ""

    # Check last 2 closed trades in this active cycle
    last_two = recent_closed[-2:]
    all_losses = True
    for p in last_two:
        pnl = float(p.get("pnl_usdt", 0.0) or p.get("pnl", 0.0) or 0.0)
        if pnl >= 0.0:
            all_losses = False
            break

    should_trip = False
    trip_reason = ""
    if all_losses:
        should_trip = True
        trip_reason = "2 consecutive losses recorded"
    elif total_loss_24h <= -3.00:
        should_trip = True
        trip_reason = f"Cycle net loss reached ${abs(total_loss_24h):.2f} (>= $3.00 limit)"

    if should_trip:
        cooldown_until = (now_dt + timedelta(hours=12)).isoformat()
        u["circuit_breaker_until"] = cooldown_until
        u["circuit_breaker_reset_at"] = (now_dt + timedelta(hours=12)).isoformat()
        _save_user(cid, u)
        log.warning("🚨 Daily Circuit Breaker TRIPPED for user %s: %s. Paused for 12 hours.", cid, trip_reason)
        return True, f"Daily Loss Circuit Breaker Tripped ({trip_reason}). Auto-trading paused for 12 hours to protect capital."

    return False, ""


def reset_user_circuit_breaker(chat_id: Union[str, int]) -> bool:
    """Manually or programmatically reset user's circuit breaker and advance the evaluation window."""
    cid = str(chat_id).strip()
    u = get_user(cid)
    now_dt = datetime.now(timezone.utc)
    u["circuit_breaker_until"] = None
    u["circuit_breaker_reset_at"] = now_dt.isoformat()
    _save_user(cid, u)
    log.info("Reset circuit breaker for user %s at %s", cid, now_dt.isoformat())
    return True
