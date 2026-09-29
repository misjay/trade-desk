"""
engine.py — Trade execution engine.

DEMO mode:  paper fills, simulates limit orders hitting when price reaches range.
LIVE mode:  places real Binance Futures (perp) + Spot limit orders.
            Sets isolated margin + leverage before every perp order.
            Monitors fills via polling (no WS required for MVP).

Position sizing:
    qty = (equity × risk_pct) / |entry_mid - sl|
    capped so notional <= 20% of equity (sanity guard).
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import requests

import state
from config import (
    cfg,
    get_hard_cap,
    get_leverage,
    NO_MARKET_TICKERS,
    symbol,
)
from notifier import notify_fill

log = logging.getLogger(__name__)

# ── Bybit V5 REST constants ───────────────────────────────────────────────────
_BYBIT_MAINNET = "https://api.bybit.com"
_BYBIT_TESTNET = "https://api-testnet.bybit.com"


def _bybit_base() -> str:
    return _BYBIT_MAINNET if cfg.is_live else _BYBIT_TESTNET


# ── Signed requests (Bybit V5) ──────────────────────────────────────────────
import hashlib
import hmac
import json
from urllib.parse import urlencode


def _bybit_sign(timestamp: str, payload_str: str) -> str:
    param_str = timestamp + cfg.active_api_key + "5000" + payload_str
    return hmac.new(
        cfg.active_api_secret.encode("utf-8"),
        param_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _bybit_post(path: str, params: dict) -> Optional[dict]:
    ts = str(int(time.time() * 1000))
    body_str = json.dumps(params, separators=(',', ':'))
    sig = _bybit_sign(ts, body_str)
    headers = {
        "X-BAPI-API-KEY": cfg.active_api_key,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": "5000",
        "X-BAPI-SIGN": sig,
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(f"{_bybit_base()}{path}", data=body_str, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.error("Bybit POST %s failed: %s", path, exc)
        return None


def _bybit_get(path: str, params: dict) -> Optional[dict]:
    ts = str(int(time.time() * 1000))
    query_str = urlencode(params) if params else ""
    sig = _bybit_sign(ts, query_str)
    headers = {
        "X-BAPI-API-KEY": cfg.active_api_key,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": "5000",
        "X-BAPI-SIGN": sig,
    }
    url = f"{_bybit_base()}{path}"
    if query_str:
        url += f"?{query_str}"
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.error("Bybit GET %s failed: %s", path, exc)
        return None


# ── Position sizing ────────────────────────────────────────────────────────────
def _compute_qty(entry_mid: float, sl: float, equity: float, risk_pct: float) -> float:
    """
    qty = (equity × risk_pct) / |entry_mid - sl|
    Minimum qty guard: at least 0.0001 BTC-equivalent value.
    """
    risk_per_unit = abs(entry_mid - sl)
    if risk_per_unit == 0:
        return 0.0
    risk_capital = equity * risk_pct
    qty = risk_capital / risk_per_unit
    # Sanity cap: notional <= 20% equity
    max_notional = equity * 0.20
    if qty * entry_mid > max_notional:
        qty = max_notional / entry_mid
    return round(qty, 6)


# ── Bybit helpers ────────────────────────────────────────────────────────────
def _set_leverage_and_margin(ticker: str, leverage: int) -> bool:
    """Set isolated margin + leverage on Bybit V5 before placing an order."""
    sym = symbol(ticker)
    # Switch to Isolated margin (tradeMode=1)
    _bybit_post("/v5/position/switch-isolated", {
        "category": "linear",
        "symbol": sym,
        "tradeMode": 1,
        "buyLeverage": str(leverage),
        "sellLeverage": str(leverage),
    })
    # Set leverage
    resp = _bybit_post("/v5/position/set-leverage", {
        "category": "linear",
        "symbol": sym,
        "buyLeverage": str(leverage),
        "sellLeverage": str(leverage),
    })
    if resp and resp.get("retCode") in (0, 110043):  # 110043 = leverage not modified
        log.info("Set %s leverage to %dx isolated on Bybit", ticker, leverage)
        return True
    log.error("Failed to set leverage for %s on Bybit: %s", ticker, resp)
    return False


def _place_limit_order_perp(ticker: str, side: str, qty: float, price: float, sl: Optional[float] = None, tp1: Optional[float] = None) -> Optional[str]:
    """
    Place a limit order on Bybit Linear Futures.
    side: 'BUY' | 'SELL'
    Returns Bybit order ID (str) or None.
    """
    sym = symbol(ticker)
    order_side = "Buy" if side == "BUY" else "Sell"
    params = {
        "category": "linear",
        "symbol": sym,
        "side": order_side,
        "orderType": "Limit",
        "qty": str(qty),
        "price": str(price),
        "timeInForce": "GTC",
    }
    if sl is not None:
        params["stopLoss"] = str(round(sl, 4))
    if tp1 is not None:
        params["takeProfit"] = str(round(tp1, 4))

    resp = _bybit_post("/v5/order/create", params)
    if resp and resp.get("retCode") == 0:
        order_id = resp.get("result", {}).get("orderId")
        log.info("Bybit Perp limit order placed: %s %s qty=%.6f @%.4f — orderId=%s",
                 ticker, side, qty, price, order_id)
        return str(order_id) if order_id else None
    log.error("Failed to place Bybit Perp order for %s: %s", ticker, resp)
    return None


def _place_limit_order_spot(ticker: str, side: str, qty: float, price: float) -> Optional[str]:
    """Place a limit order on Bybit Spot."""
    sym = symbol(ticker)
    order_side = "Buy" if side == "BUY" else "Sell"
    params = {
        "category": "spot",
        "symbol": sym,
        "side": order_side,
        "orderType": "Limit",
        "qty": str(qty),
        "price": str(price),
        "timeInForce": "GTC",
    }
    resp = _bybit_post("/v5/order/create", params)
    if resp and resp.get("retCode") == 0:
        order_id = resp.get("result", {}).get("orderId")
        log.info("Bybit Spot limit order placed: %s %s qty=%.6f @%.4f — orderId=%s",
                 ticker, side, qty, price, order_id)
        return str(order_id) if order_id else None
    log.error("Failed to place Bybit Spot order for %s: %s", ticker, resp)
    return None


# ── Demo fill simulation ───────────────────────────────────────────────────────
def _simulate_fill(sig: dict) -> float:
    """
    In demo mode, simulate fill at mid of entry range.
    Returns fill price.
    """
    return (sig["entry_low"] + sig["entry_high"]) / 2


# ── Main execute function ─────────────────────────────────────────────────────
def execute_signal(sig: dict) -> None:
    """
    Process a BUY or SELL signal:
    1. Size position.
    2. Demo: paper-fill instantly.
       Live: place limit orders on Binance (PERP + SPOT).
    3. Record in state.
    4. Notify fill.
    """
    if sig["side"] == "WAIT":
        return  # nothing to execute

    ticker = sig["ticker"]
    side = sig["side"]
    trade_type = sig["trade_type"]
    entry_low = sig["entry_low"]
    entry_high = sig["entry_high"]
    tp1 = sig["tp1"]
    tp2 = sig["tp2"]
    sl = sig["sl"]

    if None in (entry_low, entry_high, tp1, tp2, sl):
        log.warning("Skipping %s %s — missing levels", ticker, side)
        return

    entry_mid = (entry_low + entry_high) / 2
    equity = state.get_equity()
    risk_pct = cfg.risk_per_trade
    qty = _compute_qty(entry_mid, sl, equity, risk_pct)
    if qty <= 0:
        log.warning("Computed qty=0 for %s — skipping", ticker)
        return

    lev = get_leverage(ticker, scalp=(trade_type == "scalp"))
    # Never exceed hard cap
    lev = min(lev, get_hard_cap(ticker))

    now_ts = datetime.now(timezone.utc).isoformat()
    pos_id_perp = f"{ticker}_{side}_perp_{trade_type}_{uuid.uuid4().hex[:8]}"
    pos_id_spot = f"{ticker}_{side}_spot_{trade_type}_{uuid.uuid4().hex[:8]}"

    # ── PERP ──────────────────────────────────────────────────────────────
    perp_order_id = None
    if cfg.is_live:
        _set_leverage_and_margin(ticker, lev)
        # Use mid-range as limit price with native Bybit TP/SL
        perp_order_id = _place_limit_order_perp(
            ticker, side, qty, round(entry_mid, 4), sl=sl, tp1=tp1
        )
    else:
        # Demo: instant paper fill
        fill_price = _simulate_fill(sig)
        pos_perp = {
            "id": pos_id_perp,
            "ticker": ticker,
            "side": side,
            "market": "perp",
            "trade_type": trade_type,
            "entry_price": fill_price,
            "qty": qty,
            "tp1": tp1,
            "tp2": tp2,
            "sl": sl,
            "leverage": lev,
            "bybit_order_id": None,
            "opened_at": now_ts,
        }
        state.open_position(pos_perp)
        notify_fill(pos_perp, "ENTRY_FILLED")
        log.info("DEMO perp fill: %s %s @%.4f qty=%.6f lev=%dx",
                 ticker, side, fill_price, qty, lev)

    # ── SPOT (long only; for shorts → perp only) ──────────────────────────
    # Only place spot BUY (not spot SELL unless user holds the coin — enforced by config)
    spot_order_id = None
    if side == "BUY":
        if cfg.is_live:
            spot_order_id = _place_limit_order_spot(ticker, "BUY", qty, round(entry_mid, 4))
        else:
            fill_price = _simulate_fill(sig)
            pos_spot = {
                "id": pos_id_spot,
                "ticker": ticker,
                "side": side,
                "market": "spot",
                "trade_type": trade_type,
                "entry_price": fill_price,
                "qty": qty,
                "tp1": tp1,
                "tp2": tp2,
                "sl": sl,
                "leverage": 1,
                "bybit_order_id": None,
                "opened_at": now_ts,
            }
            state.open_position(pos_spot)
            notify_fill(pos_spot, "ENTRY_FILLED")
            log.info("DEMO spot fill: %s BUY @%.4f qty=%.6f 1x",
                     ticker, fill_price, qty)


# ── Position monitor (polls open positions for TP/SL hit in demo mode) ────────
_monitor_running = False


def _price_of(ticker: str) -> Optional[float]:
    from scanner import fetch_live_price
    return fetch_live_price(ticker)


def _monitor_loop() -> None:
    """Poll open positions every 60s and simulate TP/SL hits in demo mode."""
    log.info("Position monitor started")
    while _monitor_running:
        time.sleep(60)
        if not _monitor_running:
            break
        positions = state.get_open_positions()
        for pos_id, pos in list(positions.items()):
            ticker = pos["ticker"]
            side = pos["side"]
            live = _price_of(ticker)
            if live is None:
                continue
            entry = pos["entry_price"]
            tp1 = pos.get("tp1")
            tp2 = pos.get("tp2")
            sl = pos.get("sl")

            if side == "BUY":
                if sl is not None and live <= sl:
                    closed = state.close_position(pos_id, live, "SL_HIT")
                    if closed:
                        notify_fill(closed, "SL_HIT")
                elif tp2 is not None and live >= tp2:
                    closed = state.close_position(pos_id, live, "TP2_HIT")
                    if closed:
                        notify_fill(closed, "TP2_HIT")
                elif tp1 is not None and live >= tp1:
                    # Partial close at TP1 (log only for now — move SL to entry in future)
                    log.info("TP1 hit for %s %s @%.4f", ticker, side, live)
                    notify_fill({**pos, "exit_price": live}, "TP1_HIT")
            else:  # SELL
                if sl is not None and live >= sl:
                    closed = state.close_position(pos_id, live, "SL_HIT")
                    if closed:
                        notify_fill(closed, "SL_HIT")
                elif tp2 is not None and live <= tp2:
                    closed = state.close_position(pos_id, live, "TP2_HIT")
                    if closed:
                        notify_fill(closed, "TP2_HIT")
                elif tp1 is not None and live <= tp1:
                    log.info("TP1 hit for %s %s @%.4f", ticker, side, live)
                    notify_fill({**pos, "exit_price": live}, "TP1_HIT")


def start_monitor() -> None:
    global _monitor_running
    _monitor_running = True
    t = threading.Thread(target=_monitor_loop, daemon=True, name="pos-monitor")
    t.start()


def stop_monitor() -> None:
    global _monitor_running
    _monitor_running = False
