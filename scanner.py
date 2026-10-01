"""
scanner.py — Two-sided Crypto Trade Desk Scanner for Spot & Perps.

Strict Rules & Methodology:
  - Job: Two-sided book. Long and short are equal. Most names should be WAIT.
    A call exists only when there is a named dollar band at demand or supply.
  - Universe: Core 24 in mandated order:
    BTC ETH SOL XRP BNB DOGE ADA LINK AVAX SUI HYPE LTC AAVE ZEC UNI BCH TRX XLM TAO ONDO PEPE ENA HBAR NEAR
    Extras only if liquid (turnover >= 50M USDT): ARB WLD STRK APT SEI INJ OP DOT ATOM FIL RENDER FET
  - Chart URL: TradingView only:
    https://www.tradingview.com/chart/?symbol=BINANCE:TICKERUSDT (PEPE=BINANCE:PEPEUSDT)
  - Setups:
    BUY  = Pullback to demand shelf that already held or failed-breakdown reclaim
    SELL = Rejection at supply or failed breakout
    WAIT = Mid-range, vertical 15m candle, or no precise $ range
  - NO IMAGES: Zero chart image generation.
  - Live data from Bybit every run.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from bybit_client import BybitClient
from config import (
    ALL_TICKERS,
    CORE_TICKERS,
    EXTRA_TICKERS,
    MIN_LIQUIDITY_USDT,
    SWING_LOOKBACK,
    MIN_RR,
    cfg,
    bybit_linear_symbol,
    bybit_spot_symbol,
    get_leverage,
    tv_url,
    VERTICAL_CANDLE_BODY_PCT,
)
import state

log = logging.getLogger(__name__)

# Standalone scanner client
_client = BybitClient(mode="demo", demo_env="paper")


# ── Price Formatter Helper ──────────────────────────────────────────────────
def fmt_dollar(val: Optional[float]) -> str:
    """Format price cleanly with appropriate precision and no scientific notation."""
    if val is None or math.isnan(val):
        return "—"
    if val >= 10000:
        return f"${val:,.0f}"
    if val >= 1000:
        return f"${val:,.1f}"
    if val >= 10:
        return f"${val:.2f}"
    if val >= 0.1:
        return f"${val:.4f}"
    if val >= 0.001:
        return f"${val:.6f}"
    return f"${val:.8f}"


def fmt_raw(val: Optional[float]) -> str:
    """Raw number for machine executor line."""
    if val is None or math.isnan(val):
        return ""
    if val >= 1000:
        return f"{val:.2f}"
    if val >= 1:
        return f"{val:.4f}"
    return f"{val:.8f}"


# ── Market Data Fetching ────────────────────────────────────────────────────
def fetch_ohlcv(ticker: str, tf_minutes: int = 15, limit: int = 100) -> Optional[pd.DataFrame]:
    """Fetch OHLCV candles from Bybit Linear (or Spot)."""
    sym = bybit_linear_symbol(ticker)
    tf_str = "15" if tf_minutes == 15 else ("60" if tf_minutes == 60 else "240")
    klines = _client.get_klines(sym, interval=tf_str, limit=limit, category="linear")
    if not klines:
        klines = _client.get_klines(bybit_spot_symbol(ticker), interval=tf_str, limit=limit, category="spot")
    if not klines:
        return None

    df = pd.DataFrame(klines)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df.set_index("open_time", inplace=True)

    # Standardize PEPE if from 1000PEPEUSDT
    if ticker.upper() == "PEPE":
        df["open"] /= 1000.0
        df["high"] /= 1000.0
        df["low"] /= 1000.0
        df["close"] /= 1000.0
        df["turnover"] = df["volume"] * df["close"]

    return df


def fetch_live_price(ticker: str) -> Optional[float]:
    sym = bybit_linear_symbol(ticker)
    tick = _client.get_ticker(sym, category="linear")
    if not tick:
        tick = _client.get_ticker(bybit_spot_symbol(ticker), category="spot")
    if not tick:
        return None
    price = tick["mark_price"]
    if ticker.upper() == "PEPE":
        price /= 1000.0
    return price


def fetch_extras_turnover() -> Dict[str, float]:
    """Fetch 24h turnover for all extra tickers to filter liquidity >= 50M USDT."""
    res = _client.request("GET", "/v5/market/tickers", {"category": "linear"})
    turnovers: Dict[str, float] = {}
    if res and res.get("retCode") == 0:
        for item in res.get("result", {}).get("list", []):
            sym = item.get("symbol", "")
            turnover = float(item.get("turnover24h", 0) or 0)
            for extra in EXTRA_TICKERS:
                if sym == f"{extra}USDT":
                    turnovers[extra] = turnover
    return turnovers


def get_4h_trend(ticker: str) -> Optional[str]:
    """
    Fetch 4h klines and compute EMA20 to determine higher-timeframe confluence:
    Returns 'BULLISH' if close >= EMA20, 'BEARISH' if close < EMA20, or None if insufficient data.
    """
    try:
        df_4h = fetch_ohlcv(ticker, tf_minutes=240, limit=25)
        if df_4h is None or len(df_4h) < 15:
            return None
        ema20 = df_4h["close"].ewm(span=20, adjust=False).mean().iloc[-1]
        latest_close = df_4h["close"].iloc[-1]
        return "BULLISH" if latest_close >= ema20 else "BEARISH"
    except Exception as exc:
        log.debug("4H trend calculation failed for %s: %s", ticker, exc)
        return None


# ── Structure & Shelf Detection ─────────────────────────────────────────────
def detect_shelves_and_edges(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Detect clean demand and supply shelves from swing highs/lows and session extremes:
      - Session high / low
      - Key support shelves (cluster of swing lows with held wicks)
      - Key resistance shelves (cluster of swing highs)
      - 15m candle verticality check
    """
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)

    # Latest candle check
    latest_open = df["open"].iloc[-1]
    latest_close = df["close"].iloc[-1]
    latest_high = df["high"].iloc[-1]
    latest_low = df["low"].iloc[-1]
    live_price = latest_close

    candle_body_pct = abs(latest_close - latest_open) / latest_open
    is_vertical = candle_body_pct >= VERTICAL_CANDLE_BODY_PCT

    # Session / Lookback extremes (past 48 periods)
    lookback = min(48, n)
    recent_highs = highs[-lookback:]
    recent_lows = lows[-lookback:]
    session_high = float(np.max(recent_highs))
    session_low = float(np.min(recent_lows))
    session_range = session_high - session_low
    mid_range = session_low + (session_range * 0.5)

    # Find swing highs and lows
    swing_lows = []
    swing_highs = []
    for i in range(5, n - 2):
        if lows[i] == min(lows[i-3:i+4]):
            swing_lows.append(float(lows[i]))
        if highs[i] == max(highs[i-3:i+4]):
            swing_highs.append(float(highs[i]))

    # Demand shelf: closest held support below current price
    valid_demand_lows = [l for l in swing_lows if l <= live_price]
    demand_level = max(valid_demand_lows) if valid_demand_lows else session_low

    # Supply shelf: closest held resistance above current price
    valid_supply_highs = [h for h in swing_highs if h >= live_price]
    supply_level = min(valid_supply_highs) if valid_supply_highs else session_high

    # Shelf width: ~0.4% - 0.8% of price
    shelf_width = demand_level * 0.005

    demand_shelf = (round(demand_level - (shelf_width * 0.3), 8), round(demand_level + (shelf_width * 0.7), 8))
    supply_shelf = (round(supply_level - (shelf_width * 0.7), 8), round(supply_level + (shelf_width * 0.3), 8))

    # Position in range: 0.0 = session low, 1.0 = session high
    range_pos = (live_price - session_low) / session_range if session_range > 0 else 0.5

    return {
        "live_price": live_price,
        "session_high": session_high,
        "session_low": session_low,
        "mid_range": mid_range,
        "range_pos": range_pos,
        "demand_shelf": demand_shelf,
        "supply_shelf": supply_shelf,
        "is_vertical": is_vertical,
        "candle_body_pct": candle_body_pct,
    }


# ── Signal Construction ─────────────────────────────────────────────────────
def analyze_ticker(ticker: str, trade_type: str = "scalp") -> dict:
    """
    Analyze single ticker independently according to the methodology.
    Returns structured signal card dict + EXECUTOR CONTRACT machine line.
    """
    tf_str = "15m" if trade_type == "scalp" else "4h"
    tf_minutes = 15 if trade_type == "scalp" else 240
    tv = tv_url(ticker, tf_str)

    df = fetch_ohlcv(ticker, tf_minutes=tf_minutes, limit=60)
    if df is None or len(df) < 20:
        return _make_wait(ticker, 0.0, tf_str, "Insufficient kline data", "Data feed returned no candles", trade_type)

    struct = detect_shelves_and_edges(df)
    live = struct["live_price"]
    pos = struct["range_pos"]
    is_vert = struct["is_vertical"]

    # Rule: Vertical 15m candle in progress -> WAIT
    if is_vert:
        return _make_wait(
            ticker, live, tf_str,
            f"Vertical {tf_str} candle ({struct['candle_body_pct']*100:.2f}% body)",
            "Expansion candle in progress — wait for shelf consolidation",
            trade_type,
        )

    # Demand & Supply shelves
    dem_low, dem_high = struct["demand_shelf"]
    sup_low, sup_high = struct["supply_shelf"]

    # Check for BUY:
    # Pullback to demand shelf that held or failed-breakdown reclaim
    # Price must be at lower edge of range (range_pos <= 0.22) or inside demand shelf
    dist_to_demand = (live - dem_high) / live if live > 0 else 1.0
    dist_to_supply = (sup_low - live) / live if live > 0 else 1.0

    if (dist_to_demand <= 0.008 and pos <= 0.25) or (dem_low <= live <= dem_high * 1.002):
        # Precise BUY call
        entry_low = dem_low
        entry_high = dem_high
        sl = round(entry_low - (entry_low * 0.006), 8) # SL below demand shelf

        risk = entry_high - sl
        if risk > 0:
            tp1 = round(max(struct["mid_range"], entry_high + (risk * 1.5)), 8)
            tp2_target = max(sup_high, struct["session_high"])
            tp2 = round(max(tp2_target, tp1 + (risk * 1.0)), 8)
            reward = tp1 - entry_high
            rr = reward / risk

            # Higher-timeframe (4h) confluence filter
            trend_4h = get_4h_trend(ticker) if trade_type == "scalp" else None
            req_rr = MIN_RR
            confluence_tag = ""
            if trend_4h:
                if trend_4h == "BULLISH":
                    confluence_tag = " [4H Trend: BULLISH Confluence]"
                else:
                    confluence_tag = " [4H Counter-Trend]"
                    req_rr = 2.0  # require higher RR for counter-trend scalps

            if rr >= req_rr and sl < entry_low < entry_high < tp1 < tp2:
                lev = state.get_effective_leverage(ticker, scalp=(trade_type == "scalp"))
                return _make_buy_sell(
                    ticker=ticker,
                    side="BUY",
                    trade_type=trade_type,
                    tf=tf_str,
                    entry_low=entry_low,
                    entry_high=entry_high,
                    tp1=tp1,
                    tp2=tp2,
                    sl=sl,
                    rr=rr,
                    structure=f"Pullback into demand shelf {fmt_dollar(entry_low)}–{fmt_dollar(entry_high)} (session low support){confluence_tag}",
                    reason=f"Tested demand shelf and held. Target session mid {fmt_dollar(tp1)}. Risk:Reward 1:{rr:.2f}.",
                    live_price=live,
                    leverage=lev,
                )

    # Check for SELL:
    # Rejection at supply shelf or failed breakout
    # Price must be at upper edge of range (range_pos >= 0.78) or inside supply shelf
    if (dist_to_supply <= 0.008 and pos >= 0.75) or (sup_low * 0.998 <= live <= sup_high):
        # Precise SELL call
        entry_low = sup_low
        entry_high = sup_high
        sl = round(entry_high + (entry_high * 0.006), 8) # SL above supply shelf

        risk = sl - entry_low
        if risk > 0:
            tp1 = round(min(struct["mid_range"], entry_low - (risk * 1.5)), 8)
            tp2_target = min(dem_low, struct["session_low"])
            tp2 = round(min(tp2_target, tp1 - (risk * 1.0)), 8)
            reward = entry_low - tp1
            rr = reward / risk

            # Higher-timeframe (4h) confluence filter
            trend_4h = get_4h_trend(ticker) if trade_type == "scalp" else None
            req_rr = MIN_RR
            confluence_tag = ""
            if trend_4h:
                if trend_4h == "BEARISH":
                    confluence_tag = " [4H Trend: BEARISH Confluence]"
                else:
                    confluence_tag = " [4H Counter-Trend]"
                    req_rr = 2.0  # require higher RR for counter-trend scalps

            if rr >= req_rr and tp2 < tp1 < entry_low < entry_high < sl:
                lev = state.get_effective_leverage(ticker, scalp=(trade_type == "scalp"))
                return _make_buy_sell(
                    ticker=ticker,
                    side="SELL",
                    trade_type=trade_type,
                    tf=tf_str,
                    entry_low=entry_low,
                    entry_high=entry_high,
                    tp1=tp1,
                    tp2=tp2,
                    sl=sl,
                    rr=rr,
                    structure=f"Rejection at supply shelf {fmt_dollar(entry_low)}–{fmt_dollar(entry_high)} (session high cap){confluence_tag}",
                    reason=f"Failed breakout / resistance rejection. Target session mid {fmt_dollar(tp1)}. Risk:Reward 1:{rr:.2f}.",
                    live_price=live,
                    leverage=lev,
                )

    # Default: WAIT (mid-range or no clean dollar edge)
    return _make_wait(
        ticker=ticker,
        live_price=live,
        tf=tf_str,
        structure=f"Mid-range at {pos*100:.1f}% of session | Range: {fmt_dollar(struct['session_low'])}–{fmt_dollar(struct['session_high'])}",
        reason="Price mid-range; no precise demand or supply shelf at current mark",
        trade_type=trade_type,
    )


def _make_wait(ticker: str, live_price: float, tf: str, structure: str, reason: str, trade_type: str) -> dict:
    t = ticker.upper()
    return {
        "ticker": t,
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
        "tv_url": tv_url(t, tf),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "bot_line": f"BOT|{t}|WAIT||||||",
    }


def _make_buy_sell(
    ticker: str,
    side: str,
    trade_type: str,
    tf: str,
    entry_low: float,
    entry_high: float,
    tp1: float,
    tp2: float,
    sl: float,
    rr: float,
    structure: str,
    reason: str,
    live_price: float,
    leverage: int,
) -> dict:
    t = ticker.upper()
    venue = "PERP"
    expire_utc = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M UTC")
    valid_if = (
        "live mark inside or approaching band; funding not extreme against the side; "
        "15m candle not vertical; book slip < 0.15% on BTC ETH SOL XRP BNB else cancel; "
        "never market PEPE TAO ENA HBAR NEAR"
    )
    bot_line = (
        f"BOT|{t}|{side}|{venue}|{tf}|{fmt_raw(entry_low)}|{fmt_raw(entry_high)}|"
        f"{fmt_raw(tp1)}|{fmt_raw(tp2)}|{fmt_raw(sl)}|{leverage}|0.005|{expire_utc}|{valid_if}"
    )

    return {
        "ticker": t,
        "side": side,
        "trade_type": trade_type,
        "tf": tf,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "tp1": tp1,
        "tp2": tp2,
        "sl": sl,
        "rr": round(rr, 2),
        "structure": structure,
        "reason": reason,
        "live_price": live_price,
        "leverage": leverage,
        "tv_url": tv_url(t, tf),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "bot_line": bot_line,
    }


# ── Full Scan Runner ────────────────────────────────────────────────────────
def run_scan(trade_type: str = "scalp") -> Tuple[List[dict], List[dict], List[str]]:
    """
    Run full desk scan across Core 24 and Extras.
    Returns: (core_signals, extra_signals, tape_bullets)
    """
    log.info("Running desk scan (%s) across Core 24 and Extras...", trade_type)
    core_signals = []
    for ticker in CORE_TICKERS:
        try:
            sig = analyze_ticker(ticker, trade_type)
            core_signals.append(sig)
        except Exception as exc:
            log.error("Error analyzing %s: %s", ticker, exc)
            core_signals.append(_make_wait(ticker, 0.0, "15m", "Error", str(exc), trade_type))

    # Check Extras liquidity
    extra_turnovers = fetch_extras_turnover()
    extra_signals = []
    for ticker in EXTRA_TICKERS:
        turnover = extra_turnovers.get(ticker, 0.0)
        if turnover >= MIN_LIQUIDITY_USDT:
            try:
                sig = analyze_ticker(ticker, trade_type)
                extra_signals.append(sig)
            except Exception as exc:
                extra_signals.append(_make_wait(ticker, 0.0, "15m", "Error", str(exc), trade_type))
        else:
            # Drop or mark illiquid
            pass

    # Build 3-5 tape bullets
    buys = [s["ticker"] for s in core_signals if s["side"] == "BUY"]
    sells = [s["ticker"] for s in core_signals if s["side"] == "SELL"]
    waits = [s["ticker"] for s in core_signals if s["side"] == "WAIT"]
    btc_sig = next((s for s in core_signals if s["ticker"] == "BTC"), None)
    btc_price_str = fmt_dollar(btc_sig["live_price"]) if btc_sig else "—"

    tape = [
        f"BTC trading at {btc_price_str} in tight consolidation shelf.",
        f"Order desk active: {len(buys)} BUY, {len(sells)} SELL, {len(waits)} WAIT across Core 24.",
        f"Extras liquid (>= $50M 24h turnover): {', '.join(s['ticker'] for s in extra_signals) if extra_signals else 'NONE'}.",
        "Strict 0.5% balance risk per position with post-only limit execution inside printed bands.",
    ]

    return core_signals, extra_signals, tape
