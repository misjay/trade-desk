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
    check_macro_news_blackout,
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
    # Macro Trend Gate: Block counter-trend trades against BTC regime
    try:
        btc_regime = get_btc_macro_regime()
        if side_upper == "SELL" and btc_regime in ("BULL_TREND", "STRONG_BULL"):
            return False, f"BTC macro regime is {btc_regime}. Altcoin Shorts prohibited against bullish market beta."
        elif side_upper == "BUY" and btc_regime in ("BEAR_TREND", "STRONG_BEAR"):
            return False, f"BTC macro regime is {btc_regime}. Altcoin Longs prohibited against bearish market beta."
    except Exception as e_reg:
        log.debug("BTC macro regime check error: %s", e_reg)

    if side_upper == "BUY" and pct_chg <= -0.0045:
        return False, f"BTC dumping ({pct_chg*100:+.2f}% over 30m). Altcoin Longs frozen to prevent knife-catching."
    elif side_upper == "SELL" and pct_chg >= 0.0045:
        return False, f"BTC pumping ({pct_chg*100:+.2f}% over 30m). Altcoin Shorts frozen to prevent short-squeezing."

    return True, f"BTC momentum neutral ({pct_chg*100:+.2f}%)"


# ── RSI Calculation ────────────────────────────────────────────────────────
def compute_rsi(df: pd.DataFrame, period: int = 14) -> float:
    """Compute Relative Strength Index (RSI) over specified period."""
    if df is None or len(df) < period + 1:
        return 50.0
    try:
        closes = df["close"]
        diff = closes.diff()
        gain = diff.clip(lower=0)
        loss = -diff.clip(upper=0)
        avg_gain = gain.rolling(window=period).mean()
        avg_loss = loss.rolling(window=period).mean()
        rs = avg_gain / avg_loss.replace(0, 1e-9)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        val = rsi.iloc[-1]
        return float(val) if not np.isnan(val) else 50.0
    except Exception:
        return 50.0


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

    # High-Impact Macro Economic Event Blackout Check (CPI, FOMC, NFP)
    macro_active, macro_reason = check_macro_news_blackout()
    if macro_active:
        return _make_wait(
            ticker, live, tf_str,
            "Macro Event Blackout",
            macro_reason,
            trade_type,
        )

    # Scalp Quarantine: Restrict chronic lag or violent noise assets from 15m automated scalps
    from config import SCALP_QUARANTINE_TICKERS
    if trade_type == "scalp" and ticker.upper() in SCALP_QUARANTINE_TICKERS:
        return _make_wait(
            ticker, live, tf_str,
            f"Asset in Scalp Quarantine ({ticker.upper()})",
            f"Asset Quarantine: {ticker.upper()} is temporarily restricted from 15m automated scalps due to high wick noise or chronic relative weakness. Day trade mode only.",
            trade_type,
        )

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

    # Institutional Edge: ATR Volatility Stop Buffer with Asset DNA Intelligence
    import asset_dna
    atr = compute_atr(df, 14)
    from config import get_asset_tier, get_asset_risk_multiplier
    asset_tier = get_asset_tier(ticker)
    min_buf = asset_dna.get_stop_buffer_floor(ticker, live)
    max_buf = live * 0.0125 if trade_type == "scalp" else live * 0.025
    sl_buffer = max(min_buf, min(max_buf, cfg.atr_multiplier * atr)) if (cfg.enable_atr_stops and atr > 0) else min_buf

    # 3-Layer Shield: Volatility Gate (Layer 2)
    # Require 15m ATR to be >= 0.80% for Tier 3 assets to prevent paying spread & fees on flat chop
    atr_pct = (atr / live * 100.0) if (live > 0 and atr > 0) else 1.0
    if asset_tier == 3 and atr_pct < 0.80 and trade_type == "scalp":
        log.info("🛡️ Volatility Gate blocked %s: 15m ATR (%.2f%%) < 0.80%% min for Tier 3 asset. Skipping dead chop.", ticker, atr_pct)
        return _make_wait(
            ticker, live, tf_str,
            f"Low volatility chop (ATR {atr_pct:.2f}% < 0.80% min)",
            "3-Layer Shield: Minimum volatility gate active. Skipping trade to prevent fee and spread decay during flat chop.",
            trade_type,
        )

    # Weekend Liquidity Shield: Filter low-volume weekend traps (Saturday 18:00 UTC through Sunday 22:00 UTC)
    now_utc = datetime.now(timezone.utc)
    is_weekend_window = (now_utc.weekday() == 5 and now_utc.hour >= 18) or (now_utc.weekday() == 6 and now_utc.hour < 22)
    if is_weekend_window and asset_tier == 3:
        log.info("🛡️ Weekend Liquidity Shield: Skipping Tier 3 high-chop asset %s during low-volume weekend window.", ticker)
        return _make_wait(
            ticker, live, tf_str,
            "Weekend Liquidity Shield Active",
            "Weekend Trap Filter: Tier 3 high-chop assets skipped during low-volume weekend hours.",
            trade_type,
        )

    # Institutional Edge: Order Flow Depth Imbalance & Open Interest
    sym = bybit_linear_symbol(ticker)
    ob = _client.get_orderbook(sym, category="linear", limit=25)
    depth_info = _client.compute_depth_imbalance(ob) if (cfg.enable_orderflow_imbalance and ob) else {"ratio": 0.5, "state": "BALANCED"}
    oi_info = _client.get_open_interest_delta(sym) if cfg.enable_open_interest_filter else {"delta_pct": 0.0, "is_liquidation_flush": False, "is_fakeout_risk": False}

    # Institutional Edge: Funding Rate Carry Bias
    tick_data = _client.get_ticker(sym, category="linear") if cfg.enable_funding_bias else None
    funding_rate = float(tick_data.get("funding_rate", 0.0)) if tick_data else 0.0

    # Institutional Edge: 14-period RSI Momentum Filter
    rsi = compute_rsi(df, 14)

    # Check for BUY:
    # Pullback to demand shelf that held or failed-breakdown reclaim
    # Price must be at lower edge of range (range_pos <= 0.22) or inside demand shelf
    dist_to_demand = (live - dem_high) / live if live > 0 else 1.0
    dist_to_supply = (sup_low - live) / live if live > 0 else 1.0

    if (dist_to_demand <= 0.008 and pos <= 0.25) or (dem_low <= live <= dem_high * 1.002):
        # Directional Quarantine Gate with Structural Release
        is_dq, dq_reason = state.is_directionally_quarantined(ticker, "BUY", current_rsi=rsi)
        if is_dq:
            log.info("🛡️ %s: Skipping BUY on %s.", dq_reason, ticker)
            return _make_wait(ticker, live, tf_str, "Directional Quarantine Active", dq_reason, trade_type)

        # Funding Rate & OI Squeeze Sentinel (Long Crowding):
        if funding_rate >= 0.0005:  # +0.05% or higher per 8h
            log.info("🛡️ Squeeze Sentinel blocked BUY on %s: Overcrowded positive funding (+%.4f%%)", ticker, funding_rate * 100.0)
            return _make_wait(
                ticker, live, tf_str,
                f"Long Squeeze Risk (Funding +{funding_rate*100:.3f}%)",
                f"Derivatives Squeeze Sentinel: Longs are overleveraged (+{funding_rate*100:.3f}% funding). High probability of long-liquidation cascade.",
                trade_type,
            )
        if oi_info.get("is_fakeout_risk"):
            log.info("🛡️ Squeeze Sentinel blocked BUY on %s: Open Interest fakeout detected", ticker)
            return _make_wait(
                ticker, live, tf_str,
                "OI Fakeout Divergence",
                "Derivatives Squeeze Sentinel: Open Interest dropping during price expansion indicating trapped retail fakeout.",
                trade_type,
            )

        # Pillar 3: Pullback Entry Filter (Buy the dip, don't buy the rip)
        if cfg.enable_pullback_entry_filter and (pos > 0.52 or rsi > 62.0):
            log.info("Pullback filter blocked BUY on %s: Overbought extension (pos=%.2f, RSI=%.1f)", ticker, pos, rsi)
            return _make_wait(
                ticker, live, tf_str,
                f"Overbought expansion (RSI {rsi:.1f}, pos {pos:.2f})",
                "Pillar 3 Gate: Price extended in upper half of range. Avoid buying near local highs; wait for demand shelf retest.",
                trade_type,
            )

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

        # Freqtrade & BTC Macro Regime Filter: Block counter-trend Long in macro bear market (Day trades only)
        is_bear_locked = (trade_type == "day" and (ticker_regime == "STRONG_BEAR" or btc_regime == "STRONG_BEAR"))
        if cfg.enable_btc_regime_lock and btc_regime == "STRONG_BEAR" and ticker.upper() != "BTC" and trade_type == "day":
            log.info("BTC Regime Lock blocked Day BUY on %s: BTC is in STRONG_BEAR flush. Altcoin Longs frozen.", ticker)
            return _make_wait(
                ticker, live, tf_str,
                f"Demand shelf test during BTC STRONG_BEAR",
                "BTC Macro Regime Lock: BTC is in STRONG_BEAR. Altcoin Day Longs frozen to prevent knife-catching.",
                trade_type,
            )

        if cfg.enable_market_regime_filter and is_bear_locked:
            log.info("Regime filter blocked Day BUY on %s: Market in STRONG_BEAR (ticker=%s, btc=%s)", ticker, ticker_regime, btc_regime)
            return _make_wait(
                ticker, live, tf_str,
                f"Demand shelf test in STRONG_BEAR",
                "Freqtrade regime filter: Counter-trend Day LONG blocked in STRONG_BEAR flush. Shorts only for swing trades.",
                trade_type,
            )

        # Higher-timeframe (4h) confluence filter
        trend_4h = get_4h_trend(ticker) if trade_type == "scalp" else None
        if cfg.enable_strict_4h_confluence and trend_4h and trend_4h != "BULLISH":
            log.info("Strict 4H Trend Confluence blocked BUY on %s: 4H Trend is %s. Counter-trend 15m Long blocked.", ticker, trend_4h)
            return _make_wait(
                ticker, live, tf_str,
                f"15m demand test against {trend_4h} 4H trend",
                f"Strict 4H Macro Confluence: 15m scalp BUY rejected because 4H trend is {trend_4h}. Only trades aligned with 4H macro trend are permitted.",
                trade_type,
            )

        # Asset DNA Gate: Tokenomics Unlocks (Pillar 4) & Multi-Month Macro Resistance (Pillar 3)
        dna_ok, dna_reason = asset_dna.check_asset_dna_safety_gates(ticker, "BUY", live, _client)
        if not dna_ok:
            log.info("🛡️ Asset DNA Gate blocked BUY on %s: %s", ticker, dna_reason)
            return _make_wait(
                ticker, live, tf_str,
                "Asset DNA Safety Block",
                dna_reason,
                trade_type,
            )

        # Binance Spot Lead-Lag Order Flow Sentinel
        import binance_lead
        b_lead_ok, b_lead_reason, b_lead_delta = binance_lead.check_binance_spot_lead(ticker, live, "BUY")
        if not b_lead_ok:
            log.info("🛡️ Binance Spot Sentinel blocked BUY on %s: %s", ticker, b_lead_reason)
            return _make_wait(
                ticker, live, tf_str,
                "Binance Spot Divergence",
                b_lead_reason,
                trade_type,
            )

        # Precise BUY call with ATR adaptive stop
        entry_low = dem_low
        entry_high = dem_high
        sl = round(entry_low - sl_buffer, 8)
        # Enforce minimum stop loss distance of min_buf to prevent noise stopouts
        if (entry_high - sl) < min_buf:
            sl = round(entry_high - min_buf, 8)

        risk = entry_high - sl
        if risk > 0:
            if trade_type == "scalp":
                # Institutional Expansion Targets (Xira Model): Strict 2.2R TP1, 3.8R TP2, 5.5R TP3
                # Stops slim micro-scalps; ensures winners substantially outweigh standard stops
                tp1 = round(entry_high + (risk * 2.2), 8)
                tp2 = round(max(struct["session_high"], entry_high + (risk * 3.8)), 8)
                tp3 = round(entry_high + (risk * 5.5), 8)
                req_rr = max(2.0, cfg.min_rr_gate)
            else:
                tp1 = round(max(struct["mid_range"], entry_high + (risk * 2.5)), 8)
                tp2_target = max(sup_high, struct["session_high"])
                tp2 = round(max(tp2_target, tp1 + (risk * 1.5)), 8)
                tp3 = round(max(struct["session_high"], tp2 + (risk * 2.5)), 8)
                req_rr = max(2.0, cfg.min_rr_gate)

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
            confluence_tag = ""
            conviction = 86
            if trend_4h:
                if trend_4h == "BULLISH":
                    confluence_tag = " [4H Trend: BULLISH Confluence]"
                    conviction += 4
                else:
                    confluence_tag = " [4H Counter-Trend]"
                    req_rr = max(req_rr, 1.3)
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

            # Binance Spot Whale Lead bonus
            lead_tag = ""
            if "Whale Front-Run" in b_lead_reason:
                lead_tag = f" [Binance Spot Whale Lead: +{b_lead_delta:.2f}%]"
                conviction += 4

            conviction = max(75, min(99, conviction))

            # 3-Layer Shield: Conviction Gate (Layer 3)
            # Tier 3 high-chop assets require strictly >= 88% A+ conviction
            if asset_tier == 3 and conviction < 88.0:
                log.info("🛡️ Conviction Gate blocked BUY on %s: Conviction (%d%%) < 88%% A+ requirement for Tier 3 asset.", ticker, conviction)
                return _make_wait(
                    ticker, live, tf_str,
                    f"Sub-88% setup on Tier 3 asset ({conviction}%)",
                    f"3-Layer Shield: Tier 3 high-chop asset conviction ({conviction}%) is below the mandatory 88% A+ institutional threshold.",
                    trade_type,
                )

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
                    structure=f"Pullback into demand shelf {fmt_dollar(entry_low)}–{fmt_dollar(entry_high)} (session low support){confluence_tag}{regime_tag}{of_tag}{oi_tag}{lead_tag}",
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
        # 1. Directional Quarantine Gate with Structural Release
        is_dq, dq_reason = state.is_directionally_quarantined(ticker, "SELL", current_rsi=rsi)
        if is_dq:
            log.info("🛡️ %s: Skipping SELL on %s.", dq_reason, ticker)
            return _make_wait(ticker, live, tf_str, "Directional Quarantine Active", dq_reason, trade_type)

        # 2. Momentum Exemption Gate: In crypto, RSI >= 68 is aggressive bullish momentum, NOT resistance exhaustion!
        if rsi >= cfg.max_short_rsi_cap:
            log.info("🛡️ Momentum Exemption blocked SELL on %s: High RSI (%.1f >= %.1f). Shorting into breakout momentum is prohibited.", ticker, rsi, cfg.max_short_rsi_cap)
            return _make_wait(
                ticker, live, tf_str,
                f"Breakout momentum (RSI {rsi:.1f} >= {cfg.max_short_rsi_cap:.1f})",
                f"Momentum Exemption Gate: 15m RSI is {rsi:.1f} (>= {cfg.max_short_rsi_cap:.1f}). Aggressive buyer demand present; shorting against momentum breakout is forbidden.",
                trade_type,
            )

        # 3. Macro Trend Alignment Gate: Block altcoin shorts when BTC is in macro bull trend
        if cfg.enable_btc_regime_lock and ticker.upper() != "BTC":
            btc_trend_4h = get_4h_trend("BTC")
            if btc_trend_4h == "BULLISH" or btc_regime in ("STRONG_BULL", "BULLISH", "EXPANSION"):
                log.info("🛡️ Macro Trend Alignment blocked SELL on %s: BTC 4H Trend is %s, Regime is %s. Counter-trend short blocked.", ticker, btc_trend_4h, btc_regime)
                return _make_wait(
                    ticker, live, tf_str,
                    f"BTC Macro Bull Trend ({btc_trend_4h} / {btc_regime})",
                    f"Macro Alignment Gate: Altcoin shorts strictly prohibited while Bitcoin is in a Bullish macro trend ({btc_trend_4h}).",
                    trade_type,
                )

        # Funding Rate & OI Squeeze Sentinel (Short Crowding):
        if funding_rate <= -0.0003:  # -0.03% or lower per 8h
            log.info("🛡️ Squeeze Sentinel blocked SELL on %s: Overcrowded short funding (%.4f%%)", ticker, funding_rate * 100.0)
            return _make_wait(
                ticker, live, tf_str,
                f"Short Squeeze Risk (Funding {funding_rate*100:.3f}%)",
                f"Derivatives Squeeze Sentinel: Shorts are overcrowded with negative {funding_rate*100:.3f}% funding. High risk of explosive short-squeeze bounce.",
                trade_type,
            )
        if oi_info.get("is_fakeout_risk"):
            log.info("🛡️ Squeeze Sentinel blocked SELL on %s: Open Interest fakeout detected", ticker)
            return _make_wait(
                ticker, live, tf_str,
                "OI Fakeout Divergence",
                "Derivatives Squeeze Sentinel: Open Interest dropping during breakdown indicating trapped retail fakeout.",
                trade_type,
            )

        # Pillar 3: Pullback Entry Filter (Sell the rip, don't short the dip)
        if cfg.enable_pullback_entry_filter and (pos < 0.48 or rsi < 38.0):
            log.info("Pullback filter blocked SELL on %s: Oversold exhaustion (pos=%.2f, RSI=%.1f)", ticker, pos, rsi)
            return _make_wait(
                ticker, live, tf_str,
                f"Oversold exhaustion (RSI {rsi:.1f}, pos {pos:.2f})",
                "Pillar 3 Gate: Price extended in lower half of range. Avoid shorting local bottoms; wait for relief bounce into supply shelf.",
                trade_type,
            )

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

        # Freqtrade & BTC Macro Regime Filter: Block counter-trend Short in macro bull market (Day trades only)
        is_bull_locked = (trade_type == "day" and (ticker_regime == "STRONG_BULL" or btc_regime == "STRONG_BULL"))
        if cfg.enable_btc_regime_lock and btc_regime == "STRONG_BULL" and ticker.upper() != "BTC" and trade_type == "day":
            log.info("BTC Regime Lock blocked Day SELL on %s: BTC is in STRONG_BULL expansion. Altcoin Shorts frozen.", ticker)
            return _make_wait(
                ticker, live, tf_str,
                f"Supply shelf test during BTC STRONG_BULL",
                "BTC Macro Regime Lock: BTC is in STRONG_BULL. Altcoin Day Shorts frozen to prevent short-squeezes.",
                trade_type,
            )

        if cfg.enable_market_regime_filter and is_bull_locked:
            log.info("Regime filter blocked Day SELL on %s: Market in STRONG_BULL (ticker=%s, btc=%s)", ticker, ticker_regime, btc_regime)
            return _make_wait(
                ticker, live, tf_str,
                f"Supply shelf test in STRONG_BULL",
                "Freqtrade regime filter: Counter-trend Day SHORT blocked in STRONG_BULL expansion. Longs only for swing trades.",
                trade_type,
            )

        # Higher-timeframe (4h) confluence filter
        trend_4h = get_4h_trend(ticker) if trade_type == "scalp" else None
        if cfg.enable_strict_4h_confluence and trend_4h and trend_4h != "BEARISH":
            log.info("Strict 4H Trend Confluence blocked SELL on %s: 4H Trend is %s. Counter-trend 15m Short blocked.", ticker, trend_4h)
            return _make_wait(
                ticker, live, tf_str,
                f"15m supply test against {trend_4h} 4H trend",
                f"Strict 4H Macro Confluence: 15m scalp SELL rejected because 4H trend is {trend_4h}. Only trades aligned with 4H macro trend are permitted.",
                trade_type,
            )

        # Asset DNA Gate: Multi-Month Macro Support Floor (Pillar 3)
        dna_ok, dna_reason = asset_dna.check_asset_dna_safety_gates(ticker, "SELL", live, _client)
        if not dna_ok:
            log.info("🛡️ Asset DNA Gate blocked SELL on %s: %s", ticker, dna_reason)
            return _make_wait(
                ticker, live, tf_str,
                "Asset DNA Safety Block",
                dna_reason,
                trade_type,
            )

        # Binance Spot Lead-Lag Order Flow Sentinel
        import binance_lead
        b_lead_ok, b_lead_reason, b_lead_delta = binance_lead.check_binance_spot_lead(ticker, live, "SELL")
        if not b_lead_ok:
            log.info("🛡️ Binance Spot Sentinel blocked SELL on %s: %s", ticker, b_lead_reason)
            return _make_wait(
                ticker, live, tf_str,
                "Binance Spot Divergence",
                b_lead_reason,
                trade_type,
            )

        # Precise SELL call with ATR adaptive stop
        entry_low = sup_low
        entry_high = sup_high
        sl = round(entry_high + sl_buffer, 8)
        # Enforce minimum stop loss distance of min_buf to prevent noise stopouts
        if (sl - entry_low) < min_buf:
            sl = round(entry_low + min_buf, 8)

        risk = sl - entry_low
        if risk > 0:
            if trade_type == "scalp":
                # Institutional Expansion Targets (Xira Model): Strict 2.2R TP1, 3.8R TP2, 5.5R TP3
                # Stops slim micro-scalps; ensures winners substantially outweigh standard stops
                tp1 = round(entry_low - (risk * 2.2), 8)
                tp2 = round(min(struct["session_low"], entry_low - (risk * 3.8)), 8)
                tp3 = round(entry_low - (risk * 5.5), 8)
                req_rr = max(2.0, cfg.min_rr_gate)
            else:
                tp1 = round(min(struct["mid_range"], entry_low - (risk * 2.5)), 8)
                tp2_target = min(dem_low, struct["session_low"])
                tp2 = round(min(tp2_target, tp1 - (risk * 1.5)), 8)
                tp3 = round(min(struct["session_low"], tp2 - (risk * 2.5)), 8)
                req_rr = max(2.0, cfg.min_rr_gate)

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
            confluence_tag = ""
            conviction = 86
            if trend_4h:
                if trend_4h == "BEARISH":
                    confluence_tag = " [4H Trend: BEARISH Confluence]"
                    conviction += 4
                else:
                    confluence_tag = " [4H Counter-Trend]"
                    req_rr = max(req_rr, 1.3)
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

            # Binance Spot Whale Lead bonus
            lead_tag = ""
            if "Whale Front-Run" in b_lead_reason:
                lead_tag = f" [Binance Spot Whale Lead: {b_lead_delta:.2f}%]"
                conviction += 4

            conviction = max(75, min(99, conviction))

            # 3-Layer Shield: Conviction Gate (Layer 3)
            # Tier 3 high-chop assets require strictly >= 88% A+ conviction
            if asset_tier == 3 and conviction < 88.0:
                log.info("🛡️ Conviction Gate blocked SELL on %s: Conviction (%d%%) < 88%% A+ requirement for Tier 3 asset.", ticker, conviction)
                return _make_wait(
                    ticker, live, tf_str,
                    f"Sub-88% setup on Tier 3 asset ({conviction}%)",
                    f"3-Layer Shield: Tier 3 high-chop asset conviction ({conviction}%) is below the mandatory 88% A+ institutional threshold.",
                    trade_type,
                )

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
                    structure=f"Rejection at supply shelf {fmt_dollar(entry_low)}–{fmt_dollar(entry_high)} (session high cap){confluence_tag}{regime_tag}{of_tag}{oi_tag}{lead_tag}",
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
    log.info("Running market scan (%s) across Core Universe and Extras...", trade_type)
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
        f"Order desk active: {len(buys)} BUY, {len(sells)} SELL, {len(waits)} WAIT across active universe.",
        f"Extras liquid (>= $50M 24h turnover): {', '.join(s['ticker'] for s in extra_signals) if extra_signals else 'NONE'}.",
        "Strict 0.5% balance risk per position with post-only limit execution inside printed bands.",
    ]

    return core_signals, extra_signals, tape
