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
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import cfg

log = logging.getLogger(__name__)

_STATE_FILE = Path(__file__).parent / "state.json"
_lock = threading.Lock()


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
        "created_at": _now(),
        "updated_at": _now(),
    }


def _save(state: Dict[str, Any]) -> None:
    state["updated_at"] = _now()
    tmp = _STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp.replace(_STATE_FILE)


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


def close_position(pos_id: str, exit_price: float, reason: str) -> Optional[dict]:
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
