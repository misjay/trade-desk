"""
scanner.py — Fetches OHLCV from Binance and generates trade signals.

Signal schema:
    {
        ticker:       str,
        side:         'BUY' | 'SELL' | 'WAIT',
        trade_type:   'scalp' | 'day',
        tf:           str,           # '15m' | '1h' | '4h'
        entry_low:    float | None,
        entry_high:   float | None,
        tp1:          float | None,
        tp2:          float | None,
        sl:           float | None,
        rr:           float | None,
        structure:    str,
        reason:       str,
        live_price:   float,
        tv_url:       str,
        timestamp:    str,           # ISO-8601
    }
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

from config import (
    ALL_TICKERS,
    CORE_TICKERS,
    EXTRA_TICKERS,
    MIN_LIQUIDITY_USDT,
    SWING_LOOKBACK,
    DEMAND_ZONE_PCT,
    VOLUME_SPIKE_MULT,
    TREND_LOOKBACK,
    MIN_RR,
    cfg,
    get_hard_cap,
    symbol,
    tv_url,
)

log = logging.getLogger(__name__)

# ── Bybit V5 REST base URLs ───────────────────────────────────────────────────
_BYBIT_MAINNET = "https://api.bybit.com"
_BYBIT_TESTNET = "https://api-testnet.bybit.com"

# Kline interval strings accepted by Bybit V5
_TF_MAP = {15: "15", 60: "60", 240: "240"}


def _bybit_base() -> str:
    return _BYBIT_MAINNET if cfg.is_live else _BYBIT_TESTNET


# ── HTTP helpers ─────────────────────────────────────────────────────────────
def _get(url: str, params: dict, retries: int = 3) -> Optional[dict | list]:
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            log.warning("HTTP attempt %d failed for %s: %s", attempt + 1, url, exc)
            time.sleep(1.5 ** attempt)
    return None


def fetch_ohlcv(ticker: str, tf_minutes: int, limit: int = 200) -> Optional[pd.DataFrame]:
    """Fetch OHLCV candles from Bybit V5 (linear perps, fallback to spot)."""
    interval = _TF_MAP.get(tf_minutes, "15")
    sym = symbol(ticker)
    url = f"{_bybit_base()}/v5/market/kline"

    raw = _get(url, {"category": "linear", "symbol": sym, "interval": interval, "limit": limit})
    if not raw or not isinstance(raw, dict) or raw.get("retCode") != 0 or not raw.get("result", {}).get("list"):
        # Fallback to spot
        raw = _get(url, {"category": "spot", "symbol": sym, "interval": interval, "limit": limit})

    if not raw or not isinstance(raw, dict) or raw.get("retCode") != 0:
        return None

    kline_list = raw.get("result", {}).get("list", [])
    if not kline_list:
        return None

    # Bybit returns candles in descending order (newest first) — reverse to ascending
    kline_list = list(reversed(kline_list))

    # Item format: [startTime, openPrice, highPrice, lowPrice, closePrice, volume, turnover]
    records = []
    for k in kline_list:
        records.append({
            "open_time": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "quote_vol": float(k[6]) if len(k) > 6 else 0.0,
        })

    df = pd.DataFrame(records)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df.set_index("open_time", inplace=True)
    return df


def fetch_24h_quote_vol(ticker: str) -> float:
    """Return 24h turnover (quote volume in USDT) from Bybit V5."""
    sym = symbol(ticker)
    url = f"{_bybit_base()}/v5/market/tickers"

    raw = _get(url, {"category": "linear", "symbol": sym})
    if raw and isinstance(raw, dict) and raw.get("retCode") == 0:
        t_list = raw.get("result", {}).get("list", [])
        if t_list:
            return float(t_list[0].get("turnover24h", 0))

    raw = _get(url, {"category": "spot", "symbol": sym})
    if raw and isinstance(raw, dict) and raw.get("retCode") == 0:
        t_list = raw.get("result", {}).get("list", [])
        if t_list:
            return float(t_list[0].get("turnover24h", 0))

    return 0.0


def fetch_live_price(ticker: str) -> Optional[float]:
    """Return live price from Bybit V5 tickers."""
    sym = symbol(ticker)
    url = f"{_bybit_base()}/v5/market/tickers"

    raw = _get(url, {"category": "linear", "symbol": sym})
    if raw and isinstance(raw, dict) and raw.get("retCode") == 0:
        t_list = raw.get("result", {}).get("list", [])
        if t_list and "lastPrice" in t_list[0]:
            return float(t_list[0]["lastPrice"])

    raw = _get(url, {"category": "spot", "symbol": sym})
    if raw and isinstance(raw, dict) and raw.get("retCode") == 0:
        t_list = raw.get("result", {}).get("list", [])
        if t_list and "lastPrice" in t_list[0]:
            return float(t_list[0]["lastPrice"])

    return None


# ── Structure detection helpers ───────────────────────────────────────────────
def _find_swing_highs(df: pd.DataFrame, n: int = SWING_LOOKBACK) -> List[int]:
    """Indices of swing high candles (highest high in ±n window)."""
    highs = df["high"].values
    swings = []
    for i in range(n, len(highs) - n):
        window = highs[i - n: i + n + 1]
        if highs[i] == max(window):
            swings.append(i)
    return swings


def _find_swing_lows(df: pd.DataFrame, n: int = SWING_LOOKBACK) -> List[int]:
    """Indices of swing low candles."""
    lows = df["low"].values
    swings = []
    for i in range(n, len(lows) - n):
        window = lows[i - n: i + n + 1]
        if lows[i] == min(window):
            swings.append(i)
    return swings


def _trend(df: pd.DataFrame, n: int = TREND_LOOKBACK) -> str:
    """
    Classify recent trend using last n swing highs/lows.
    Returns: 'bullish_hh' | 'bearish_ll' | 'neutral'
    """
    closes = df["close"].values[-n * 2:]
    # Simple: compare recent highs and lows
    segment = len(closes) // 2
    early = closes[:segment]
    late = closes[segment:]
    if late.mean() > early.mean() * 1.005:
        return "bullish_hh"
    if late.mean() < early.mean() * 0.995:
        return "bearish_ll"
    return "neutral"


def _volume_is_spiking(df: pd.DataFrame, n: int = 20) -> bool:
    """True if last candle volume > N× average of prior n candles."""
    if len(df) < n + 1:
        return False
    avg = df["volume"].iloc[-(n + 1):-1].mean()
    last = df["volume"].iloc[-1]
    return float(last) > avg * VOLUME_SPIKE_MULT


def _demand_zone(df: pd.DataFrame) -> Optional[Tuple[float, float]]:
    """
    Find the most recent demand zone:
    - A swing low candle followed by a strong up-move.
    - Zone = candle low to candle high.
    Returns (zone_low, zone_high) or None.
    """
    swing_idx = _find_swing_lows(df)
    if not swing_idx:
        return None
    # Take the most recent swing low
    idx = swing_idx[-1]
    zone_low = float(df["low"].iloc[idx])
    zone_high = float(df["high"].iloc[idx])
    # Widen slightly by DEMAND_ZONE_PCT
    pad = zone_low * DEMAND_ZONE_PCT
    return zone_low - pad, zone_high + pad


def _supply_zone(df: pd.DataFrame) -> Optional[Tuple[float, float]]:
    """Find the most recent supply zone from a swing high."""
    swing_idx = _find_swing_highs(df)
    if not swing_idx:
        return None
    idx = swing_idx[-1]
    zone_low = float(df["low"].iloc[idx])
    zone_high = float(df["high"].iloc[idx])
    pad = zone_high * DEMAND_ZONE_PCT
    return zone_low - pad, zone_high + pad


def _rr(entry: float, sl: float, tp: float) -> float:
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    return round(reward / risk, 2) if risk > 0 else 0.0


# ── BTC floor tracker ─────────────────────────────────────────────────────────
_btc_session_floor: Optional[float] = None
_btc_broke_floor_at: Optional[float] = None   # unix timestamp
_btc_hard_reclaim_at: Optional[float] = None  # unix timestamp
_BTC_FLOOR_SUPPRESS_SECS = 3600               # 1 hour suppression


def update_btc_floor(df_btc: pd.DataFrame) -> None:
    """Track BTC session floor (first candle open of current session)."""
    global _btc_session_floor
    if df_btc is not None and len(df_btc) > 0:
        _btc_session_floor = float(df_btc["low"].iloc[:5].min())  # first 5 candles as floor proxy


def btc_broke_floor(live_price: float) -> bool:
    global _btc_broke_floor_at
    if _btc_session_floor is None:
        return False
    if live_price < _btc_session_floor * 0.999:
        if _btc_broke_floor_at is None:
            _btc_broke_floor_at = time.time()
        return True
    _btc_broke_floor_at = None
    return False


def btc_hard_reclaim(live_price: float) -> bool:
    global _btc_hard_reclaim_at
    if _btc_session_floor is None:
        return False
    if live_price > _btc_session_floor * 1.001:
        if _btc_hard_reclaim_at is None:
            _btc_hard_reclaim_at = time.time()
        return True
    _btc_hard_reclaim_at = None
    return False


def alt_longs_suppressed() -> bool:
    """True if BTC broke session floor < 1 hour ago → suppress new alt longs."""
    if _btc_broke_floor_at is None:
        return False
    return (time.time() - _btc_broke_floor_at) < _BTC_FLOOR_SUPPRESS_SECS


def alt_shorts_suppressed() -> bool:
    """True if BTC hard reclaimed < 1 hour ago → suppress new alt shorts."""
    if _btc_hard_reclaim_at is None:
        return False
    return (time.time() - _btc_hard_reclaim_at) < _BTC_FLOOR_SUPPRESS_SECS


# ── Signal generation ─────────────────────────────────────────────────────────
def _make_wait(
    ticker: str,
    live_price: float,
    tf: str,
    structure: str,
    reason: str,
    trade_type: str,
) -> dict:
    return {
        "ticker": ticker,
        "side": "WAIT",
        "trade_type": trade_type,
        "tf": tf,
        "entry_low": None,
        "entry_high": None,
        "tp1": None,
        "tp2": None,
        "sl": None,
        "rr": None,
        "structure": structure,
        "reason": reason,
        "live_price": live_price,
        "tv_url": tv_url(ticker),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def analyze_ticker(ticker: str, trade_type: str = "scalp") -> dict:
    """
    Run full structure analysis on a single ticker.
    Returns a signal dict.
    """
    tf_minutes = cfg.scalp_tf if trade_type == "scalp" else (cfg.day_tf * 60 if cfg.day_tf >= 1 else cfg.day_tf)
    tf_str = _TF_MAP.get(tf_minutes, "15m")

    # For day: use 1h (60) and 4h (240) to confirm
    # We'll use the primary day TF = 4h for structure, 1h for entry
    if trade_type == "day":
        df_primary = fetch_ohlcv(ticker, 240, limit=150)  # 4h for structure
        df_entry = fetch_ohlcv(ticker, 60, limit=100)    # 1h for entry refinement
        tf_str = "4h"
    else:
        df_primary = fetch_ohlcv(ticker, 15, limit=200)
        df_entry = df_primary
        tf_str = "15m"

    # Live price
    live_price = fetch_live_price(ticker)
    if live_price is None:
        return _make_wait(ticker, 0.0, tf_str, "No data", "Cannot fetch price", trade_type)
    if df_primary is None or len(df_primary) < 50:
        return _make_wait(ticker, live_price, tf_str, "Insufficient data", "Too few candles", trade_type)

    # Liquidity gate for extras
    if ticker in EXTRA_TICKERS:
        vol = fetch_24h_quote_vol(ticker)
        if vol < MIN_LIQUIDITY_USDT:
            return _make_wait(
                ticker, live_price, tf_str,
                f"24h vol ${vol/1e6:.1f}M < min ${MIN_LIQUIDITY_USDT/1e6:.0f}M",
                "Illiquid — below liquidity threshold",
                trade_type,
            )

    trend = _trend(df_primary)
    vol_spiking = _volume_is_spiking(df_primary)

    demand = _demand_zone(df_entry if df_entry is not None else df_primary)
    supply = _supply_zone(df_entry if df_entry is not None else df_primary)

    # ── BUY logic ───────────────────────────────────────────────────────────
    # BUY = pullback into demand OR failed-breakdown reclaim
    # Do NOT buy if trend is bearish_ll with volume spike
    if demand is not None and not (trend == "bearish_ll" and vol_spiking):
        zone_low, zone_high = demand

        # Price must be at or near the demand zone (within 2% above zone top)
        in_or_near_demand = zone_low <= live_price <= zone_high * 1.02

        if in_or_near_demand and not alt_longs_suppressed():
            # Calculate levels
            entry_low = round(zone_low, _price_decimals(zone_low))
            entry_high = round(zone_high, _price_decimals(zone_high))
            sl = round(zone_low * 0.995, _price_decimals(zone_low))  # 0.5% below demand
            range_size = entry_high - entry_low
            tp1 = round(entry_high + range_size * 2, _price_decimals(entry_high))
            tp2 = round(entry_high + range_size * 4, _price_decimals(entry_high))

            entry_mid = (entry_low + entry_high) / 2
            rr1 = _rr(entry_mid, sl, tp1)

            if rr1 >= MIN_RR:
                structure_line = (
                    f"Demand zone ${entry_low:.4g}–${entry_high:.4g} | "
                    f"Trend: {trend} | Vol spike: {vol_spiking}"
                )
                reason = (
                    f"Price pulling into demand ${entry_low:.4g}–${entry_high:.4g}. "
                    f"Trend: {trend}. R:R {rr1:.1f}."
                )
                return {
                    "ticker": ticker,
                    "side": "BUY",
                    "trade_type": trade_type,
                    "tf": tf_str,
                    "entry_low": entry_low,
                    "entry_high": entry_high,
                    "tp1": tp1,
                    "tp2": tp2,
                    "sl": sl,
                    "rr": rr1,
                    "structure": structure_line,
                    "reason": reason,
                    "live_price": live_price,
                    "tv_url": tv_url(ticker),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }

    # ── SELL logic ──────────────────────────────────────────────────────────
    # SELL = supply rejection, failed breakout, or +10-30% that loses breakout
    # Do NOT short if trend is bullish_hh with volume spike
    if supply is not None and not (trend == "bullish_hh" and vol_spiking):
        zone_low, zone_high = supply

        in_or_near_supply = zone_low * 0.98 <= live_price <= zone_high

        if in_or_near_supply and not alt_shorts_suppressed():
            entry_low = round(zone_low, _price_decimals(zone_low))
            entry_high = round(zone_high, _price_decimals(zone_high))
            sl = round(zone_high * 1.005, _price_decimals(zone_high))
            range_size = entry_high - entry_low
            tp1 = round(entry_low - range_size * 2, _price_decimals(entry_low))
            tp2 = round(entry_low - range_size * 4, _price_decimals(entry_low))

            entry_mid = (entry_low + entry_high) / 2
            rr1 = _rr(entry_mid, sl, tp1)

            if rr1 >= MIN_RR:
                structure_line = (
                    f"Supply zone ${entry_low:.4g}–${entry_high:.4g} | "
                    f"Trend: {trend} | Vol spike: {vol_spiking}"
                )
                reason = (
                    f"Price rejecting supply ${entry_low:.4g}–${entry_high:.4g}. "
                    f"Trend: {trend}. R:R {rr1:.1f}."
                )
                return {
                    "ticker": ticker,
                    "side": "SELL",
                    "trade_type": trade_type,
                    "tf": tf_str,
                    "entry_low": entry_low,
                    "entry_high": entry_high,
                    "tp1": tp1,
                    "tp2": tp2,
                    "sl": sl,
                    "rr": rr1,
                    "structure": structure_line,
                    "reason": reason,
                    "live_price": live_price,
                    "tv_url": tv_url(ticker),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }

    # ── WAIT ────────────────────────────────────────────────────────────────
    wait_reason_parts = []
    if alt_longs_suppressed():
        wait_reason_parts.append("BTC broke session floor — alt longs suppressed")
    if alt_shorts_suppressed():
        wait_reason_parts.append("BTC hard reclaim — alt shorts suppressed")
    if trend == "bearish_ll" and vol_spiking:
        wait_reason_parts.append("Falling LL with volume — no long")
    if trend == "bullish_hh" and vol_spiking:
        wait_reason_parts.append("Rising HH with volume — no short")
    if not wait_reason_parts:
        wait_reason_parts.append("Price mid-range — no precise entry range available")

    structure_line = f"Trend: {trend} | Live: ${live_price:.4g} | Vol spike: {vol_spiking}"
    return _make_wait(ticker, live_price, tf_str, structure_line, "; ".join(wait_reason_parts), trade_type)


def _price_decimals(price: float) -> int:
    """Return appropriate decimal places based on price magnitude."""
    if price >= 10000:
        return 0
    if price >= 1000:
        return 1
    if price >= 100:
        return 2
    if price >= 1:
        return 3
    if price >= 0.01:
        return 5
    return 8


def run_scan(trade_type: str = "scalp") -> List[dict]:
    """
    Run a full scan across all tickers (Core 24 + Extras).
    Each ticker analyzed independently — never bundled.
    Returns list of signal dicts in mandated desk order.
    """
    log.info("Starting %s scan for %d tickers", trade_type, len(ALL_TICKERS))
    signals = []

    # First update BTC floor
    btc_df = fetch_ohlcv("BTC", 15 if trade_type == "scalp" else 240, limit=100)
    if btc_df is not None:
        update_btc_floor(btc_df)
        btc_live = fetch_live_price("BTC")
        if btc_live:
            btc_broke_floor(btc_live)
            btc_hard_reclaim(btc_live)

    for ticker in ALL_TICKERS:
        log.info("Analyzing %s [%s]", ticker, trade_type)
        try:
            sig = analyze_ticker(ticker, trade_type)
            signals.append(sig)
        except Exception as exc:
            log.error("Error analyzing %s: %s", ticker, exc, exc_info=True)
            signals.append(_make_wait(ticker, 0.0, "15m", "Error", str(exc), trade_type))

    log.info("Scan complete: %d BUY, %d SELL, %d WAIT",
             sum(1 for s in signals if s["side"] == "BUY"),
             sum(1 for s in signals if s["side"] == "SELL"),
             sum(1 for s in signals if s["side"] == "WAIT"))
    return signals
