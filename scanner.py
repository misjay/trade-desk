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
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

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
    tf_str = "15" if tf_minutes == 15 else ("60" if tf_minutes == 60 else ("D" if tf_minutes >= 1440 else "240"))
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


# ── Freqtrade / FreqAI Market Regime Filter ────────────────────────────────
_btc_regime_cache: Tuple[float, str] = (0.0, "CHOP_RANGE")


def detect_market_regime(ticker: str) -> Dict[str, Any]:
    """
    Freqtrade/FreqAI multi-timeframe market regime classifier.
    Analyzes 4H candles (EMA20 and EMA50 relationship, trend slope):
      - 'STRONG_BULL': close > EMA20 > EMA50. Parabolic / expansion bull trend. Counter-trend shorts forbidden.
      - 'STRONG_BEAR': close < EMA20 < EMA50. Parabolic / expansion bear flush. Counter-trend longs forbidden.
      - 'CHOP_RANGE': Mixed moving averages or consolidation. Two-sided mean-reversion permitted.
    """
    try:
        df_4h = fetch_ohlcv(ticker, tf_minutes=240, limit=50)
        if df_4h is None or len(df_4h) < 15:
            return {"regime": "CHOP_RANGE", "ema20": 0.0, "ema50": 0.0, "description": "Insufficient 4H data"}

        closes = df_4h["close"]
        ema20 = float(closes.ewm(span=20, adjust=False).mean().iloc[-1])
        span50 = min(50, len(closes))
        ema50 = float(closes.ewm(span=span50, adjust=False).mean().iloc[-1])
        latest_close = float(closes.iloc[-1])

        if latest_close >= ema20 and ema20 >= ema50:
            regime = "STRONG_BULL"
            desc = f"4H Macro Bull Expansion (Close {fmt_dollar(latest_close)} > EMA20 > EMA50)"
        elif latest_close <= ema20 and ema20 <= ema50:
            regime = "STRONG_BEAR"
            desc = f"4H Macro Bear Downtrend (Close {fmt_dollar(latest_close)} < EMA20 < EMA50)"
        else:
            regime = "CHOP_RANGE"
            desc = "4H Mean-Reversion Range (EMA Compression)"

        return {
            "regime": regime,
            "ema20": ema20,
            "ema50": ema50,
            "latest_close": latest_close,
            "description": desc,
        }
    except Exception as exc:
        log.debug("4H market regime detection failed for %s: %s", ticker, exc)
        return {"regime": "CHOP_RANGE", "ema20": 0.0, "ema50": 0.0, "description": str(exc)}


def get_btc_macro_regime() -> str:
    """Returns BTC 4H macro regime cached for 120 seconds to govern market-wide beta."""
    global _btc_regime_cache
    now = time.time()
    if now - _btc_regime_cache[0] < 120:
        return _btc_regime_cache[1]
    res = detect_market_regime("BTC")
    regime = res.get("regime", "CHOP_RANGE")
    _btc_regime_cache = (now, regime)
    return regime


# ── BTC Momentum Gatekeeper (Correlated Altcoin Protection) ─────────────────
_btc_momentum_cache: Tuple[float, float, str] = (0.0, 0.0, "NEUTRAL")  # (ts, pct_chg, bias)


def check_btc_momentum_gate(target_side: str) -> Tuple[bool, str]:
    """
    BTC Momentum Gatekeeper:
    Altcoins correlate >80% with BTC during sharp expansions.
    If BTC is actively dumping (>0.45% drop over the last 30m / 2x15m candles),
    block altcoin LONG calls (don't catch alt falling knives during a BTC flush).
    If BTC is actively pumping violently (>0.45% rally over 30m),
    block altcoin SHORT calls (don't short into market-wide expansion beta).
    Cached for 60 seconds to avoid API spam.
    Returns (allowed: bool, reason: str).
    """
    if not cfg.enable_btc_momentum_gate:
        return True, "BTC momentum gate disabled"

    global _btc_momentum_cache
    now = time.time()
    if now - _btc_momentum_cache[0] < 60:
        pct_chg = _btc_momentum_cache[1]
    else:
        try:
            df_btc = fetch_ohlcv("BTC", tf_minutes=15, limit=5)
            if df_btc is not None and len(df_btc) >= 3:
                # 30m delta: close of latest candle vs close of 2 candles ago
                p_current = float(df_btc["close"].iloc[-1])
                p_past = float(df_btc["close"].iloc[-3])
                pct_chg = (p_current - p_past) / p_past if p_past > 0 else 0.0
            else:
                pct_chg = 0.0
        except Exception as exc:
            log.debug("BTC momentum calculation failed: %s", exc)
            pct_chg = 0.0

        bias = "FLUSH_DOWN" if pct_chg <= -0.0045 else ("EXPANSION_UP" if pct_chg >= 0.0045 else "NEUTRAL")
        _btc_momentum_cache = (now, pct_chg, bias)

    side_upper = target_side.upper()
    if side_upper == "BUY" and pct_chg <= -0.0045:
        return False, f"BTC dumping ({pct_chg*100:+.2f}% over 30m). Altcoin Longs frozen to prevent knife-catching."
    elif side_upper == "SELL" and pct_chg >= 0.0045:
        return False, f"BTC pumping ({pct_chg*100:+.2f}% over 30m). Altcoin Shorts frozen to prevent short-squeezing."

    return True, f"BTC momentum neutral ({pct_chg*100:+.2f}%)"


# ── ATR Volatility Calculation ─────────────────────────────────────────────
def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Compute Average True Range (ATR) over specified period."""
    if df is None or len(df) < period + 1:
        return 0.0
    highs = df["high"]
    lows = df["low"]
    closes = df["close"]
    prev_closes = closes.shift(1)

    tr1 = highs - lows
    tr2 = (highs - prev_closes).abs()
    tr3 = (lows - prev_closes).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean().iloc[-1]
    return float(atr) if not np.isnan(atr) else float(tr.iloc[-1])


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
        "latest_open": float(latest_open),
        "latest_high": float(latest_high),
        "latest_low": float(latest_low),
        "latest_close": float(latest_close),
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

    # Freqtrade / FreqAI Market Regime
    regime_info = detect_market_regime(ticker) if cfg.enable_market_regime_filter else {"regime": "CHOP_RANGE"}
    ticker_regime = regime_info.get("regime", "CHOP_RANGE")
    btc_regime = get_btc_macro_regime() if cfg.enable_market_regime_filter else "CHOP_RANGE"

    # Institutional Edge: ATR Volatility Stop Buffer
    atr = compute_atr(df, 14)
    min_buf = live * 0.0035
    max_buf = live * 0.022
    sl_buffer = max(min_buf, min(max_buf, cfg.atr_multiplier * atr)) if (cfg.enable_atr_stops and atr > 0) else (live * 0.006)

    # Institutional Edge: Order Flow Depth Imbalance & Open Interest
    sym = bybit_linear_symbol(ticker)
    ob = _client.get_orderbook(sym, category="linear", limit=25)
    depth_info = _client.compute_depth_imbalance(ob) if (cfg.enable_orderflow_imbalance and ob) else {"ratio": 0.5, "state": "BALANCED"}
    oi_info = _client.get_open_interest_delta(sym) if cfg.enable_open_interest_filter else {"delta_pct": 0.0, "is_liquidation_flush": False, "is_fakeout_risk": False}

    # Institutional Edge: Funding Rate Carry Bias
    tick_data = _client.get_ticker(sym, category="linear") if cfg.enable_funding_bias else None
    funding_rate = float(tick_data.get("funding_rate", 0.0)) if tick_data else 0.0

    # Check for BUY:
    # Pullback to demand shelf that held or failed-breakdown reclaim
    # Price must be at lower edge of range (range_pos <= 0.22) or inside demand shelf
    dist_to_demand = (live - dem_high) / live if live > 0 else 1.0
    dist_to_supply = (sup_low - live) / live if live > 0 else 1.0

    if (dist_to_demand <= 0.008 and pos <= 0.25) or (dem_low <= live <= dem_high * 1.002):
        # BTC Momentum Gatekeeper: Freeze altcoin longs if BTC is aggressively dumping
        if ticker.upper() != "BTC" and cfg.enable_btc_momentum_gate:
            gate_ok, gate_reason = check_btc_momentum_gate("BUY")
            if not gate_ok:
                log.info("BTC Momentum Gate blocked BUY on %s: %s", ticker, gate_reason)
                return _make_wait(
                    ticker, live, tf_str,
                    "Demand shelf test during BTC dump",
                    f"BTC Momentum Gate: {gate_reason}",
                    trade_type,
                )

        # Shelf Wick Rejection Confirmation:
        # If the candle sliced through support with a solid body closing below dem_low, it's a breakdown.
        # Demand shelf must show bottom wick absorption (lower wick >= 15% range or close >= dem_low).
        if cfg.enable_wick_confirmation:
            c_open = struct.get("latest_open", live)
            c_high = struct.get("latest_high", live)
            c_low = struct.get("latest_low", live)
            c_close = struct.get("latest_close", live)
            c_range = c_high - c_low
            if c_range > 0:
                lower_wick = min(c_open, c_close) - c_low
                lower_wick_ratio = lower_wick / c_range
                is_solid_breakdown_red = (c_close < c_open) and (c_close < dem_low * 0.9985) and (lower_wick_ratio < 0.15)
                if is_solid_breakdown_red:
                    log.info("Wick confirmation blocked BUY on %s: Solid red breakdown candle (lower wick %.1f%%)", ticker, lower_wick_ratio * 100)
                    return _make_wait(
                        ticker, live, tf_str,
                        "Breakdown candle slicing through demand",
                        "Wick rejection check: No demand absorption wick detected at shelf. Avoid catching falling knife.",
                        trade_type,
                    )

        # Freqtrade Regime Filter: Block counter-trend Long in macro bear market
        if cfg.enable_market_regime_filter and (ticker_regime == "STRONG_BEAR" or btc_regime == "STRONG_BEAR"):
            log.info("Regime filter blocked BUY on %s: Market in STRONG_BEAR (ticker=%s, btc=%s)", ticker, ticker_regime, btc_regime)
            return _make_wait(
                ticker, live, tf_str,
                f"Demand shelf test in STRONG_BEAR",
                "Freqtrade regime filter: Counter-trend LONG blocked in STRONG_BEAR flush. Shorts only.",
                trade_type,
            )

        # Precise BUY call with ATR adaptive stop
        entry_low = dem_low
        entry_high = dem_high
        sl = round(entry_low - sl_buffer, 8)

        risk = entry_high - sl
        if risk > 0:
            tp1 = round(max(struct["mid_range"], entry_high + (risk * 1.5)), 8)
            tp2_target = max(sup_high, struct["session_high"])
            tp2 = round(max(tp2_target, tp1 + (risk * 1.0)), 8)
            tp3 = round(max(struct["session_high"], tp2 + (risk * 1.5)), 8)

            # Precision Edge: Order Book Liquidity Wall Front-Running
            if cfg.enable_wall_frontrun and ob:
                wall_tp1 = _client.find_liquidity_wall(ob, "BUY", tp1)
                if wall_tp1 and wall_tp1 > entry_high:
                    tp1 = round(wall_tp1, 8)
                wall_tp2 = _client.find_liquidity_wall(ob, "BUY", tp2)
                if wall_tp2 and wall_tp2 > tp1:
                    tp2 = round(wall_tp2, 8)

            reward = tp1 - entry_high
            rr = reward / risk

            # Higher-timeframe (4h) confluence filter
            trend_4h = get_4h_trend(ticker) if trade_type == "scalp" else None
            req_rr = MIN_RR
            confluence_tag = ""
            conviction = 86
            if trend_4h:
                if trend_4h == "BULLISH":
                    confluence_tag = " [4H Trend: BULLISH Confluence]"
                    conviction += 4
                else:
                    confluence_tag = " [4H Counter-Trend]"
                    req_rr = 2.0
                    conviction -= 8

            regime_tag = f" [Regime: {ticker_regime}]"

            # Order Flow & OI Confluence bonuses
            of_tag = ""
            if depth_info.get("state") == "BID_HEAVY":
                of_tag = " [Order Flow: Bid Wall Absorption]"
                conviction += 4
            oi_tag = ""
            if oi_info.get("is_liquidation_flush"):
                oi_tag = " [OI: Liquidation Flush Reversal]"
                conviction += 4
            elif oi_info.get("is_fakeout_risk"):
                conviction -= 5

            # Funding Rate Carry Bias
            funding_tag = ""
            if cfg.enable_funding_bias:
                if funding_rate <= -0.0004:
                    # Shorts heavily paying longs: positive carry bonus!
                    funding_tag = f" [Carry: Favorable Yield {funding_rate*100:.3f}%]"
                    conviction += 4
                elif funding_rate >= 0.0006:
                    # Longs heavily paying shorts: expensive carry drag
                    funding_tag = f" [Carry: Negative Drag {funding_rate*100:.3f}%]"
                    conviction -= 4

            conviction = max(75, min(99, conviction))

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
                    structure=f"Pullback into demand shelf {fmt_dollar(entry_low)}–{fmt_dollar(entry_high)} (session low support){confluence_tag}{regime_tag}{of_tag}{oi_tag}",
                    reason=f"Tested demand shelf and held. Target session mid {fmt_dollar(tp1)}. Risk:Reward 1:{rr:.2f}.",
                    live_price=live,
                    leverage=lev,
                    tp3=tp3,
                    conviction=conviction,
                    orderflow_info=depth_info,
                    oi_info=oi_info,
                )

    # Check for SELL:
    # Rejection at supply shelf or failed breakout
    # Price must be at upper edge of range (range_pos >= 0.78) or inside supply shelf
    if (dist_to_supply <= 0.008 and pos >= 0.75) or (sup_low * 0.998 <= live <= sup_high):
        # BTC Momentum Gatekeeper: Freeze altcoin shorts if BTC is aggressively pumping
        if ticker.upper() != "BTC" and cfg.enable_btc_momentum_gate:
            gate_ok, gate_reason = check_btc_momentum_gate("SELL")
            if not gate_ok:
                log.info("BTC Momentum Gate blocked SELL on %s: %s", ticker, gate_reason)
                return _make_wait(
                    ticker, live, tf_str,
                    "Supply shelf test during BTC pump",
                    f"BTC Momentum Gate: {gate_reason}",
                    trade_type,
                )

        # Shelf Wick Rejection Confirmation:
        # If the candle sliced upward through resistance with a solid body closing above sup_high, it's a breakout.
        # Supply shelf rejection must show top wick rejection (upper wick >= 15% range or close <= sup_high).
        if cfg.enable_wick_confirmation:
            c_open = struct.get("latest_open", live)
            c_high = struct.get("latest_high", live)
            c_low = struct.get("latest_low", live)
            c_close = struct.get("latest_close", live)
            c_range = c_high - c_low
            if c_range > 0:
                upper_wick = c_high - max(c_open, c_close)
                upper_wick_ratio = upper_wick / c_range
                is_solid_breakout_green = (c_close > c_open) and (c_close > sup_high * 1.0015) and (upper_wick_ratio < 0.15)
                if is_solid_breakout_green:
                    log.info("Wick confirmation blocked SELL on %s: Solid green breakout candle (upper wick %.1f%%)", ticker, upper_wick_ratio * 100)
                    return _make_wait(
                        ticker, live, tf_str,
                        "Breakout candle blasting through supply",
                        "Wick rejection check: No overhead rejection wick detected at shelf. Avoid shorting breakout expansion.",
                        trade_type,
                    )

        # Freqtrade Regime Filter: Block counter-trend Short in macro bull market
        if cfg.enable_market_regime_filter and (ticker_regime == "STRONG_BULL" or btc_regime == "STRONG_BULL"):
            log.info("Regime filter blocked SELL on %s: Market in STRONG_BULL (ticker=%s, btc=%s)", ticker, ticker_regime, btc_regime)
            return _make_wait(
                ticker, live, tf_str,
                f"Supply shelf test in STRONG_BULL",
                "Freqtrade regime filter: Counter-trend SHORT blocked in STRONG_BULL expansion. Longs only.",
                trade_type,
            )

        # Precise SELL call with ATR adaptive stop
        entry_low = sup_low
        entry_high = sup_high
        sl = round(entry_high + sl_buffer, 8)

        risk = sl - entry_low
        if risk > 0:
            tp1 = round(min(struct["mid_range"], entry_low - (risk * 1.5)), 8)
            tp2_target = min(dem_low, struct["session_low"])
            tp2 = round(min(tp2_target, tp1 - (risk * 1.0)), 8)
            tp3 = round(min(struct["session_low"], tp2 - (risk * 1.5)), 8)

            # Precision Edge: Order Book Liquidity Wall Front-Running
            if cfg.enable_wall_frontrun and ob:
                wall_tp1 = _client.find_liquidity_wall(ob, "SELL", tp1)
                if wall_tp1 and wall_tp1 < entry_low:
                    tp1 = round(wall_tp1, 8)
                wall_tp2 = _client.find_liquidity_wall(ob, "SELL", tp2)
                if wall_tp2 and wall_tp2 < tp1:
                    tp2 = round(wall_tp2, 8)

            reward = entry_low - tp1
            rr = reward / risk

            # Higher-timeframe (4h) confluence filter
            trend_4h = get_4h_trend(ticker) if trade_type == "scalp" else None
            req_rr = MIN_RR
            confluence_tag = ""
            conviction = 86
            if trend_4h:
                if trend_4h == "BEARISH":
                    confluence_tag = " [4H Trend: BEARISH Confluence]"
                    conviction += 4
                else:
                    confluence_tag = " [4H Counter-Trend]"
                    req_rr = 2.0
                    conviction -= 8

            regime_tag = f" [Regime: {ticker_regime}]"

            # Order Flow Confluence bonuses
            of_tag = ""
            if depth_info.get("state") == "ASK_HEAVY":
                of_tag = " [Order Flow: Ask Wall Cap]"
                conviction += 4
            oi_tag = ""
            if oi_info.get("is_fakeout_risk"):
                oi_tag = " [OI: Fakeout Exhaustion]"
                conviction += 4

            # Funding Rate Carry Bias
            funding_tag = ""
            if cfg.enable_funding_bias:
                if funding_rate >= 0.0006:
                    # Longs heavily paying shorts: short gets positive carry yield!
                    funding_tag = f" [Carry: Favorable Short Yield {funding_rate*100:.3f}%]"
                    conviction += 4
                elif funding_rate <= -0.0004:
                    # Shorts heavily paying longs: short suffers negative carry drag
                    funding_tag = f" [Carry: Negative Short Drag {funding_rate*100:.3f}%]"
                    conviction -= 4

            conviction = max(75, min(99, conviction))

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
                    structure=f"Rejection at supply shelf {fmt_dollar(entry_low)}–{fmt_dollar(entry_high)} (session high cap){confluence_tag}{regime_tag}{of_tag}{oi_tag}",
                    reason=f"Failed breakout / resistance rejection. Target session mid {fmt_dollar(tp1)}. Risk:Reward 1:{rr:.2f}.",
                    live_price=live,
                    leverage=lev,
                    tp3=tp3,
                    conviction=conviction,
                    orderflow_info=depth_info,
                    oi_info=oi_info,
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
        "tp3": None,
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
    tp3: Optional[float] = None,
    conviction: Optional[int] = None,
    orderflow_info: Optional[dict] = None,
    oi_info: Optional[dict] = None,
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
        "tp3": tp3,
        "conviction": conviction or 88,
        "sl": sl,
        "rr": round(rr, 2),
        "structure": structure,
        "reason": reason,
        "live_price": live_price,
        "leverage": leverage,
        "orderflow": orderflow_info or {},
        "open_interest": oi_info or {},
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
