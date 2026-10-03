"""
state.py — JSON-backed persistent state store.

Tracks:
  - Open paper positions (demo mode)
  - Paper equity (demo mode)
  - Live position refs (live mode — Binance order IDs)
  - Last signal per ticker
  - Session metrics (win/loss/breakeven)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from config import cfg

log = logging.getLogger(__name__)

_STATE_FILE = Path(__file__).parent / "state.json"
_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_raw() -> Dict[str, Any]:
    if _STATE_FILE.exists():
        try:
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            log.error("State load error: %s — starting fresh", exc)
    return _default_state()


def _default_state() -> Dict[str, Any]:
    return {
        "mode": cfg.trade_mode,
        "paper_equity": cfg.paper_equity,
        "paper_equity_start": cfg.paper_equity,
        "open_positions": {},      # key: "TICKER_SIDE_TYPE" → position dict
        "closed_positions": [],
        "last_signals": {},        # key: ticker → last signal dict
        "session": {
            "wins": 0,
            "losses": 0,
            "breakevens": 0,
            "total_pnl_usdt": 0.0,
        },
        "avoid_list": [],
        "quarantine": {},
        "probation": {},
        "custom_leverage": {},
        "spot_enabled": False,
        "created_at": _now(),
        "updated_at": _now(),
    }


def _save(state: Dict[str, Any]) -> None:
    state["updated_at"] = _now()
    content = json.dumps(state, indent=2)
    # Retry loop to handle Windows file locking gracefully
    for attempt in range(6):
        tmp = _STATE_FILE.with_suffix(f".tmp_{os.getpid()}_{attempt}")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
            tmp.replace(_STATE_FILE)
            return
        except OSError:
            if tmp.exists():
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
            time.sleep(0.04)

    # Fallback direct write
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as exc:
        log.warning("State direct write fallback: %s", exc)


# ── Public API ────────────────────────────────────────────────────────────────

def get_state() -> Dict[str, Any]:
    with _lock:
        return _load_raw()


def get_equity() -> float:
    with _lock:
        return _load_raw()["paper_equity"]


def set_equity(new_equity: float) -> None:
    with _lock:
        s = _load_raw()
        s["paper_equity"] = round(new_equity, 4)
        _save(s)


def is_paused() -> bool:
    with _lock:
        return bool(_load_raw().get("is_paused", False))


def set_paused(paused: bool) -> None:
    with _lock:
        s = _load_raw()
        s["is_paused"] = paused
        _save(s)


def save_signal(sig: dict) -> None:
    """Store the last signal for a ticker."""
    with _lock:
        s = _load_raw()
        s["last_signals"][sig["ticker"]] = sig
        _save(s)


def get_last_signal(ticker: str) -> Optional[dict]:
    with _lock:
        return _load_raw()["last_signals"].get(ticker)


def open_position(pos: dict) -> None:
    """
    Register a new open position.
    pos = {
        id: str,          # unique key e.g. "BTC_BUY_scalp_<ts>"
        ticker: str,
        side: str,        # 'BUY' | 'SELL'
        market: str,      # 'perp' | 'spot'
        trade_type: str,
        entry_price: float,
        qty: float,
        tp1: float,
        tp2: float,
        sl: float,
        leverage: int,
        binance_order_id: str | None,
        opened_at: str,
    }
    """
    with _lock:
        s = _load_raw()
        s["open_positions"][pos["id"]] = pos
        _save(s)


def close_position(pos_id: str, exit_price: float, reason: str, exact_pnl: Optional[float] = None) -> Optional[dict]:
    """
    Close position by ID. Returns closed position dict with PnL.
    """
    with _lock:
        s = _load_raw()
        pos = s["open_positions"].pop(pos_id, None)
        if pos is None:
            log.warning("close_position: unknown id %s", pos_id)
            return None

        entry = pos["entry_price"]
        qty = pos["qty"]
        leverage = pos.get("leverage", 1)

        if exact_pnl is not None:
            pnl_usdt = exact_pnl
            notional = entry * qty
            margin = notional / max(1, leverage) if leverage > 0 else notional
            pnl_pct = (pnl_usdt / margin) if margin > 0 else 0.0
        else:
            if pos["side"] == "BUY":
                pnl_pct = (exit_price - entry) / entry * leverage
            else:
                pnl_pct = (entry - exit_price) / entry * leverage
            pnl_usdt = pnl_pct * (entry * qty)

        pos["exit_price"] = exit_price
        pos["exit_reason"] = reason
        pos["closed_at"] = _now()
        pos["pnl_pct"] = round(pnl_pct * 100, 3)
        pos["pnl_usdt"] = round(pnl_usdt, 4)

        s["closed_positions"].append(pos)

        # Update equity (demo mode)
        if cfg.trade_mode == "demo":
            s["paper_equity"] = round(s["paper_equity"] + pnl_usdt, 4)

        # Session stats
        if pnl_usdt > 0:
            s["session"]["wins"] += 1
        elif pnl_usdt < 0:
            s["session"]["losses"] += 1
        else:
            s["session"]["breakevens"] += 1
        s["session"]["total_pnl_usdt"] = round(
            s["session"]["total_pnl_usdt"] + pnl_usdt, 4
        )

        _save(s)
        return pos


def update_open_position(pos_id: str, updates: dict) -> None:
    with _lock:
        s = _load_raw()
        if pos_id in s["open_positions"]:
            s["open_positions"][pos_id].update(updates)
            _save(s)


def remove_open_position(pos_id: str) -> Optional[dict]:
    with _lock:
        s = _load_raw()
        pos = s["open_positions"].pop(pos_id, None)
        if pos:
            _save(s)
        return pos


def record_closed_position(closed: dict) -> None:
    with _lock:
        s = _load_raw()
        order_id = closed.get("order_id")
        cid = closed.get("id")
        for existing in s["closed_positions"]:
            if order_id and existing.get("order_id") == order_id:
                return
            if cid and existing.get("id") == cid:
                return

        s["closed_positions"].append(closed)
        pnl = closed.get("pnl_usdt", 0.0)
        if pnl > 0:
            s["session"]["wins"] += 1
        elif pnl < 0:
            s["session"]["losses"] += 1
        else:
            s["session"]["breakevens"] += 1
        s["session"]["total_pnl_usdt"] = round(s["session"]["total_pnl_usdt"] + pnl, 4)
        _save(s)


def get_closed_positions() -> List[dict]:
    with _lock:
        return list(_load_raw().get("closed_positions", []))


def get_open_positions() -> Dict[str, dict]:
    with _lock:
        return _load_raw()["open_positions"]


def get_session_stats() -> dict:
    with _lock:
        return _load_raw()["session"]


def reset_state(keep_equity: bool = False) -> None:
    """Reset state to defaults. Use for fresh session or mode switch."""
    with _lock:
        fresh = _default_state()
        if keep_equity:
            existing = _load_raw()
            fresh["paper_equity"] = existing.get("paper_equity", cfg.paper_equity)
        _save(fresh)
        log.info("State reset. Equity: %.2f", fresh["paper_equity"])


def quarantine_asset(ticker: str, hours: float = 24.0, reason: str = "") -> dict:
    """Quarantine an asset for N hours. Adds to avoid_list with expiration timestamp."""
    with _lock:
        s = _load_raw()
        t = ticker.strip().upper()
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=hours)

        if "quarantine" not in s:
            s["quarantine"] = {}
        if "probation" in s and t in s["probation"]:
            del s["probation"][t]

        s["quarantine"][t] = {
            "ticker": t,
            "quarantined_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "hours": hours,
            "reason": reason,
        }
        current_avoid = set(s.get("avoid_list", []))
        current_avoid.add(t)
        s["avoid_list"] = sorted(list(current_avoid))
        _save(s)
        return s["quarantine"][t]


def check_and_update_quarantines() -> None:
    """Check if any 24h quarantine expired and transition the asset to 50% probation."""
    with _lock:
        s = _load_raw()
        quarantine = s.get("quarantine", {})
        probation = s.get("probation", {})
        avoid_list = set(s.get("avoid_list", []))
        now = datetime.now(timezone.utc)
        changed = False

        expired_tickers = []
        for t, data in list(quarantine.items()):
            exp_str = data.get("expires_at")
            if exp_str:
                try:
                    exp_dt = datetime.fromisoformat(exp_str)
                    if now >= exp_dt:
                        expired_tickers.append(t)
                except Exception:
                    pass

        for t in expired_tickers:
            del quarantine[t]
            avoid_list.discard(t)
            probation[t] = {
                "ticker": t,
                "started_at": now.isoformat(),
                "trades_remaining": 3,
                "probation_pnl": 0.0,
            }
            changed = True
            log.info("24h Quarantine expired for %s. Moved to 50%% probation for 3 trades.", t)

        if changed:
            s["quarantine"] = quarantine
            s["probation"] = probation
            s["avoid_list"] = sorted(list(avoid_list))
            _save(s)


def get_quarantine_list() -> Dict[str, dict]:
    check_and_update_quarantines()
    with _lock:
        return dict(_load_raw().get("quarantine", {}))


def get_avoid_list() -> List[str]:
    check_and_update_quarantines()
    with _lock:
        return list(_load_raw().get("avoid_list", []))


def add_to_avoid_list(tickers: List[str]) -> List[str]:
    with _lock:
        s = _load_raw()
        current = set(s.get("avoid_list", []))
        for t in tickers:
            clean = t.strip().upper()
            if clean:
                current.add(clean)
        s["avoid_list"] = sorted(list(current))
        _save(s)
        return s["avoid_list"]


def remove_from_avoid_list(tickers: List[str]) -> List[str]:
    with _lock:
        s = _load_raw()
        current = set(s.get("avoid_list", []))
        quarantine = s.get("quarantine", {})
        probation = s.get("probation", {})
        for t in tickers:
            clean = t.strip().upper()
            current.discard(clean)
            quarantine.pop(clean, None)
            probation.pop(clean, None)
        s["avoid_list"] = sorted(list(current))
        s["quarantine"] = quarantine
        s["probation"] = probation
        _save(s)
        return s["avoid_list"]


def is_on_probation(ticker: str) -> bool:
    check_and_update_quarantines()
    with _lock:
        return ticker.upper() in _load_raw().get("probation", {})


def get_probation_list() -> Dict[str, dict]:
    check_and_update_quarantines()
    with _lock:
        return dict(_load_raw().get("probation", {}))


def record_probation_trade(ticker: str, pnl: float) -> Optional[str]:
    """
    Called when a trade completes for an asset on probation.
    Returns: 'GRADUATED', 'RE_QUARANTINED', or 'CONTINUING'
    """
    with _lock:
        s = _load_raw()
        t = ticker.upper()
        prob = s.get("probation", {})
        if t not in prob:
            return None

        p_info = prob[t]
        p_info["trades_remaining"] = max(0, p_info.get("trades_remaining", 3) - 1)
        p_info["probation_pnl"] = round(p_info.get("probation_pnl", 0.0) + pnl, 4)

        if p_info["trades_remaining"] <= 0:
            pnl_final = p_info["probation_pnl"]
            del prob[t]
            if pnl_final >= 0:
                _save(s)
                log.info("Probation GRADUATED for %s: net PnL $%.2f", t, pnl_final)
                return "GRADUATED"
            else:
                _save(s)
                quarantine_asset(t, hours=24.0, reason=f"Failed probation (PnL: ${pnl_final:,.2f})")
                log.info("Probation FAILED for %s: Re-quarantined for 24h", t)
                return "RE_QUARANTINED"
        else:
            _save(s)
            return "CONTINUING"


def check_and_trigger_consecutive_loss_quarantine(ticker: str) -> Optional[dict]:
    """
    Real-time circuit breaker:
    If ticker has 2 consecutive closed losses, immediately quarantine for 24h.
    """
    with _lock:
        s = _load_raw()
        t_clean = ticker.upper()
        if t_clean in s.get("quarantine", {}):
            return None

        closed = s.get("closed_positions", [])
        ticker_closed = [
            c for c in reversed(closed)
            if c.get("ticker", "").upper() == t_clean or c.get("symbol", "").replace("USDT", "").replace("1000", "") == t_clean
        ]
        if len(ticker_closed) >= 2:
            pnl1 = float(ticker_closed[0].get("pnl_usdt", 0.0) or ticker_closed[0].get("closed_pnl", 0.0))
            pnl2 = float(ticker_closed[1].get("pnl_usdt", 0.0) or ticker_closed[1].get("closed_pnl", 0.0))
            if pnl1 < -1.0 and pnl2 < -1.0:
                log.warning("Real-Time Circuit Breaker: %s had 2 consecutive losses ($%.2f, $%.2f). Auto-quarantining for 24h.", t_clean, pnl2, pnl1)
                return quarantine_asset(t_clean, hours=24.0, reason=f"2 consecutive stop-outs (${pnl2:,.2f}, ${pnl1:,.2f})")
    return None


def get_custom_leverage(ticker: Optional[str] = None) -> Union[Dict[str, int], Optional[int]]:
    with _lock:
        levs = _load_raw().get("custom_leverage", {})
        if ticker:
            return levs.get(ticker.upper())
        return dict(levs)


def set_custom_leverage(ticker: str, leverage: int) -> int:
    with _lock:
        s = _load_raw()
        if "custom_leverage" not in s:
            s["custom_leverage"] = {}
        val = int(leverage)
        s["custom_leverage"][ticker.upper()] = val
        _save(s)
        return val


def get_effective_leverage(ticker: str, scalp: bool = True) -> int:
    custom = get_custom_leverage(ticker)
    if custom is not None:
        return int(custom)
    from config import get_leverage
    return get_leverage(ticker, scalp=scalp)


def remove_working_orders(ticker: Optional[str] = None) -> int:
    """Remove unfilled resting (WORKING) orders from state."""
    with _lock:
        s = _load_raw()
        open_pos = s.get("open_positions", {})
        to_delete = []
        for pid, p in open_pos.items():
            if p.get("status") == "WORKING":
                if ticker is None or p.get("ticker", "").upper() == ticker.upper():
                    to_delete.append(pid)
        for pid in to_delete:
            del open_pos[pid]
        if to_delete:
            _save(s)
        return len(to_delete)


def is_spot_enabled() -> bool:
    with _lock:
        return bool(_load_raw().get("spot_enabled", False))


def set_spot_enabled(enabled: bool) -> bool:
    with _lock:
        s = _load_raw()
        s["spot_enabled"] = bool(enabled)
        _save(s)
        return s["spot_enabled"]


def get_feedback_bot_config() -> Dict[str, str]:
    with _lock:
        s = _load_raw()
        cfg_custom = s.get("feedback_bot", {})
        token = cfg_custom.get("token") or cfg.feedback_bot_token or cfg.telegram_token
        chat_id = cfg_custom.get("chat_id") or cfg.feedback_chat_id or cfg.telegram_chat_id
        return {"token": token, "chat_id": str(chat_id)}


def set_feedback_bot_config(token: str, chat_id: str) -> Dict[str, str]:
    with _lock:
        s = _load_raw()
        s["feedback_bot"] = {
            "token": token.strip(),
            "chat_id": str(chat_id).strip(),
            "updated_at": _now(),
        }
        _save(s)
        return s["feedback_bot"]


def get_call_bot_config() -> Dict[str, str]:
    with _lock:
        s = _load_raw()
        cfg_custom = s.get("call_bot", {})
        token = cfg_custom.get("token") or cfg.call_bot_token or cfg.feedback_bot_token or cfg.telegram_token
        chat_id = cfg_custom.get("chat_id") or cfg.call_bot_chat_id or cfg.feedback_chat_id or cfg.telegram_chat_id
        return {"token": token, "chat_id": str(chat_id)}


def set_call_bot_config(token: str, chat_id: str) -> Dict[str, str]:
    with _lock:
        s = _load_raw()
        s["call_bot"] = {
            "token": token.strip(),
            "chat_id": str(chat_id).strip(),
            "updated_at": _now(),
        }
        _save(s)
        return s["call_bot"]


def record_learning_event(event: dict) -> None:
    with _lock:
        s = _load_raw()
        if "learning_events" not in s:
            s["learning_events"] = []
        event["timestamp"] = _now()
        s["learning_events"].append(event)
        if len(s["learning_events"]) > 50:
            s["learning_events"] = s["learning_events"][-50:]
        _save(s)


def get_learning_history() -> List[dict]:
    with _lock:
        return list(_load_raw().get("learning_events", []))


def set_last_researched_signal(sig: dict) -> None:
    with _lock:
        s = _load_raw()
        s["last_researched_signal"] = sig
        _save(s)


def get_last_researched_signal() -> Optional[dict]:
    with _lock:
        return _load_raw().get("last_researched_signal")

