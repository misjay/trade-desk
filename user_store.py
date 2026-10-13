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
from datetime import datetime, timezone
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
    return bool(get_user(chat_id).get("is_paused", False))


def set_user_paused(chat_id: Union[str, int], paused: bool) -> None:
    u = get_user(chat_id)
    u["is_paused"] = paused
    _save_user(chat_id, u)


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

    if entry > 0:
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
