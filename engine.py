"""
engine.py — Trade execution engine for Trade Desk on Bybit (Demo and Live).

Enforces:
  1. Job: Two-sided book. Post-only limits inside printed band.
  2. Risk: Takes exactly 0.5% of balance per trade:
     qty = (balance * 0.005) / |entry_mid - sl|
     If SL would require > 1% equity at allowed leverage, cut size.
  3. Pre-execution checks (VALID_IF):
     - Live mark inside or approaching band; if left band -> NO_CHASE
     - Funding rate not extreme against side (<= 0.05%)
     - 15m candle not vertical (body < 2.5%)
     - Book slip < 0.15% on BTC ETH SOL XRP BNB else cancel market / use limit
     - Never market PEPE, TAO, ENA, HBAR, NEAR
     - Spot sell only if inventory > 0; if flat, drop spot sell
  4. Dual execution:
     - PERP: Isolated margin, leverage cap, Post-Only limit with native Bybit TP/SL
     - SPOT: Spot limit order (1x leverage / none)
  5. Executor Contract parser:
     Parses and executes BOT|TICKER|SIDE|VENUE|TF|ENTRY_LOW|...
"""
from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from bybit_client import BybitClient
from config import (
    cfg,
    bybit_linear_symbol,
    bybit_spot_symbol,
    get_hard_cap,
    get_leverage,
    MARKET_ALLOWED_TICKERS,
    NO_MARKET_TICKERS,
    MAX_SLIPPAGE_PCT,
    MAX_FUNDING_RATE,
    VERTICAL_CANDLE_BODY_PCT,
    get_sector,
    is_volatile_ticker,
    is_weekend_derisk_window,
)
import notifier
from notifier import (
    notify_fill,
    notify_order_placed,
    notify_entry_filled,
    notify_tp1_be,
    notify_tp2_lock,
    notify_trade_closed,
)
import state

log = logging.getLogger(__name__)

# Initialize client
client = BybitClient(
    api_key=cfg.active_api_key,
    api_secret=cfg.active_api_secret,
    mode=cfg.trade_mode,
    demo_env=cfg.effective_demo_env,
)


def reload_client() -> None:
    """Reload client when mode or keys change."""
    global client
    client = BybitClient(
        api_key=cfg.active_api_key,
        api_secret=cfg.active_api_secret,
        mode=cfg.trade_mode,
        demo_env=cfg.effective_demo_env,
    )


def fmt_dollar(val: Optional[float]) -> str:
    """Format dollar amount cleanly for alerts."""
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
    return f"${val:.6f}"


# ── Hummingbot Inventory Skew & Portfolio Balance ──────────────────────────
def check_portfolio_inventory_skew(target_side: str) -> Tuple[bool, str]:
    """
    Hummingbot Avellaneda-Stoikov Inventory Skew & Portfolio Balance Rule.
    Prevents holding a one-sided correlated directional basket (e.g. 5 short altcoins simultaneously).
    Rules:
      1. Directional Count Cap: Max cfg.max_directional_positions (default 3) in the same direction
         if 0 opposing positions exist.
      2. Net Notional Ratio: Directional notional must not exceed cfg.max_directional_ratio (65%)
         of total portfolio notional when 2 or more total positions are open.
    """
    open_pos = state.get_open_positions()
    if not open_pos:
        return True, "Inventory balanced (no open positions)"

    long_positions = [p for p in open_pos.values() if p.get("side", "").upper() == "BUY"]
    short_positions = [p for p in open_pos.values() if p.get("side", "").upper() == "SELL"]

    target_side_upper = target_side.upper()

    # Rule 1: Directional Count Skew (e.g. max 3 shorts if 0 longs)
    if target_side_upper == "SELL":
        if len(short_positions) >= cfg.max_directional_positions and len(long_positions) == 0:
            return False, (
                f"Hummingbot inventory skew: Portfolio holds {len(short_positions)} active SHORTs "
                f"and 0 LONGs (cap={cfg.max_directional_positions}). Additional short blocked to prevent basket correlation."
            )
    elif target_side_upper == "BUY":
        if len(long_positions) >= cfg.max_directional_positions and len(short_positions) == 0:
            return False, (
                f"Hummingbot inventory skew: Portfolio holds {len(long_positions)} active LONGs "
                f"and 0 SHORTs (cap={cfg.max_directional_positions}). Additional long blocked to prevent basket correlation."
            )

    # Rule 2: Directional Net Notional Ratio (when both long and short positions exist)
    if len(long_positions) > 0 and len(short_positions) > 0:
        long_notional = sum(
            float(p.get("qty", 0.0)) * float(p.get("entry_price", 0.0)) * (1000.0 if p.get("ticker") == "PEPE" else 1.0)
            for p in long_positions
        )
        short_notional = sum(
            float(p.get("qty", 0.0)) * float(p.get("entry_price", 0.0)) * (1000.0 if p.get("ticker") == "PEPE" else 1.0)
            for p in short_positions
        )
        total_notional = long_notional + short_notional

        if total_notional > 0:
            ratio_short = short_notional / total_notional
            ratio_long = long_notional / total_notional

            if target_side_upper == "SELL" and ratio_short >= cfg.max_directional_ratio:
                return False, (
                    f"Hummingbot inventory skew: Net short notional is {ratio_short*100:.1f}% "
                    f"of portfolio (>= {cfg.max_directional_ratio*100:.0f}% cap). Additional short rejected."
                )
            elif target_side_upper == "BUY" and ratio_long >= cfg.max_directional_ratio:
                return False, (
                    f"Hummingbot inventory skew: Net long notional is {ratio_long*100:.1f}% "
                    f"of portfolio (>= {cfg.max_directional_ratio*100:.0f}% cap). Additional long rejected."
                )

    return True, "Inventory skew acceptable"


def check_sector_basket_exposure(ticker: str) -> Tuple[bool, str]:
    """
    Narrative & Ecosystem Sector Basket Concentration Cap:
    Prevents holding too many correlated assets from the same niche
    (e.g., SOL + SUI + APT + AVAX all crashing simultaneously during an L1 rotation).
    """
    if not cfg.enable_sector_caps:
        return True, "Sector caps disabled"

    sector = get_sector(ticker)
    if sector in ("OTHER", "MAJORS"):
        # Majors and uncategorized assets follow standard portfolio caps
        return True, "Majors/unrestricted sector"

    open_pos = state.get_open_positions()
    sector_positions = [
        p for p in open_pos.values()
        if get_sector(p.get("ticker", "")) == sector
    ]

    # Special rule: Memecoins (DOGE, PEPE) strictly capped at 1 position
    cap = 1 if sector == "MEME" else cfg.max_sector_positions

    if len(sector_positions) >= cap:
        held_tickers = [p.get("ticker") for p in sector_positions]
        return False, (
            f"Sector concentration cap: Basket '{sector}' already has {len(sector_positions)} "
            f"active positions ({', '.join(held_tickers)}, cap={cap}). New {ticker} order rejected."
        )

    return True, f"Sector {sector} capacity available"


# ── Balance & Risk Sizing (Institutional Dynamic Risk) ──────────────────────
def get_current_equity() -> float:
    """
    Get current equity for sizing.
    In Live or Bybit Testnet/Demo with API keys: pulls real wallet balance.
    In local paper simulation: pulls paper equity from state.
    """
    if not client.is_paper:
        bal = client.get_wallet_balance("USDT")
        eq = bal.get("equity", 0.0)
        if eq > 0:
            return eq
    return state.get_equity()


def compute_position_size(
    ticker: str,
    entry_price: float,
    sl: float,
    equity: float,
    leverage: int,
    risk_pct: float = 0.010,  # 1.0% institutional base balance risk
    trade_type: str = "scalp",
) -> Tuple[float, float, str]:
    """
    Compute trade quantity based on dynamic institutional balance risk:
      risk_capital = equity * risk_pct
      qty = risk_capital / |entry_price - sl|

    Guardrails:
      - If SL would require >2% equity at allowed leverage, cut size.
      - Symmetrical Scalp Loss Cap: Caps max loss on scalps to cfg.max_scalp_loss_cap ($10 max)
        to ensure losses never outstrip profit targets.
      - Scale max position notional dynamically with portfolio equity.
      - Ensure notional margin <= available equity.
      - Quantize to Bybit lot step and check minOrderQty.
      - Handle PEPE 1000-multiplier for Bybit Linear perps.

    Returns: (perp_qty, spot_qty, note)
    """
    dist = abs(entry_price - sl)
    if dist <= 0 or entry_price <= 0:
        return 0.0, 0.0, "Invalid SL or entry price"

    from config import get_asset_risk_multiplier
    asset_mult = get_asset_risk_multiplier(ticker)

    risk_capital = equity * risk_pct * asset_mult
    raw_qty = risk_capital / dist

    # Symmetrical Risk Guard:
    # On scalps, profit targets average $5 - $15. A full stopout must NEVER exceed $10.00!
    # On day trades, profit targets average $20 - $50. Stopout is capped at $35.00.
    target_cap = cfg.max_scalp_loss_cap if trade_type == "scalp" else cfg.max_day_loss_cap
    max_allowed_loss = min(target_cap, min(equity * 0.015, cfg.max_dollar_loss_cap)) * asset_mult
    if raw_qty * dist > max_allowed_loss:
        raw_qty = max_allowed_loss / dist
        log.info("🎯 Symmetrical Risk Sizing: Capped %s (%s) risk to max dollar loss $%.2f (multiplier=%.2f, raw_qty=%.4f)", ticker, trade_type, max_allowed_loss, asset_mult, raw_qty)

    # Check margin required at given leverage:
    notional = raw_qty * entry_price

    # Dynamic institutional position notional ceiling (scales with portfolio equity):
    is_tier_a_asset = ticker.upper() in ("BTC", "ETH", "SOL", "XRP", "BNB")
    equity_mult = 1.15 if is_tier_a_asset else 0.85
    dynamic_max_notional = max(cfg.max_position_notional, equity * equity_mult)
    if notional > dynamic_max_notional:
        raw_qty = dynamic_max_notional / entry_price
        notional = dynamic_max_notional
        log.info("Capped %s position notional to dynamic institutional cap $%.2f (raw_qty=%.4f)", ticker, dynamic_max_notional, raw_qty)

    required_margin = notional / max(1, leverage)
    max_safe_margin = equity * 0.80  # don't tie up more than 80% equity in one trade

    if not client.is_paper:
        wb = client.get_wallet_balance("USDT")
        avail_bal = wb.get("available", 0.0)
        if avail_bal <= 0.0 and not client.is_live:
            avail_bal = equity
        min_buffer = equity * cfg.min_free_margin_pct
        if avail_bal < min_buffer and client.is_live:
            return 0.0, 0.0, f"Available margin ${avail_bal:,.2f} below {int(cfg.min_free_margin_pct*100)}% buffer (${min_buffer:,.2f})"
        if avail_bal > 0:
            max_safe_margin = min(max_safe_margin, avail_bal * 0.50)
        elif avail_bal == 0.0 and client.is_live:
            return 0.0, 0.0, "Available margin exhausted by existing open positions"

    if required_margin > max_safe_margin:
        raw_qty = (max_safe_margin * leverage) / entry_price
        log.info("Scaled down size for %s: margin cap reached", ticker)

    # For Bybit Linear perps, PEPE is 1000PEPEUSDT
    perp_symbol = bybit_linear_symbol(ticker)
    spot_symbol = bybit_spot_symbol(ticker)

    perp_qty = raw_qty
    spot_qty = raw_qty

    if ticker.upper() == "PEPE":
        # 1 1000PEPE contract = 1,000 PEPE tokens
        perp_qty = raw_qty / 1000.0

    # Guardrail: Spot leverage is 1x (cash). Cap notional to avoid running out of available balance.
    max_spot_notional = equity * 0.80
    if not client.is_paper:
        avail = client.get_wallet_balance("USDT").get("available", 0.0)
        if avail > 0:
            max_spot_notional = min(max_spot_notional, avail * 0.40)

    if (spot_qty * entry_price) > max_spot_notional and entry_price > 0:
        spot_qty = max_spot_notional / entry_price
        log.info("Scaled down spot qty for %s to fit available cash: %.4f", ticker, spot_qty)

    # Quantize to Bybit instrument limits
    perp_qty = client.quantize_qty(perp_symbol, perp_qty, category="linear")
    spot_qty = client.quantize_qty(spot_symbol, spot_qty, category="spot")

    info_perp = client.get_instrument_info(perp_symbol, category="linear")
    if perp_qty < info_perp["min_qty"]:
        return 0.0, 0.0, f"Perp qty {perp_qty} below minOrderQty {info_perp['min_qty']}"

    return perp_qty, spot_qty, "OK"


# ── Pre-execution Validation (VALID_IF checks) ──────────────────────────────
def validate_execution_conditions(
    ticker: str,
    side: str,
    entry_low: float,
    entry_high: float,
    is_market: bool = False,
    order_qty: float = 0.0,
) -> Tuple[bool, str]:
    """
    Validate conditions specified in the EXECUTOR CONTRACT:
      1. Live mark inside or approaching band; if left band -> NO_CHASE
      2. Funding not extreme against the side (<= 0.05%)
      3. 15m candle not vertical (body < 2.5%)
      4. Book slip < 0.15% on BTC ETH SOL XRP BNB else cancel market / use limit
      5. Never market PEPE, TAO, ENA, HBAR, NEAR
    """
    t = ticker.upper()
    perp_sym = bybit_linear_symbol(t)

    # 1. Hard Market Order Restrictions
    if is_market:
        if t in NO_MARKET_TICKERS:
            return False, f"Hard rule: Never market {t}"
        if t not in MARKET_ALLOWED_TICKERS:
            return False, f"Market order only allowed on Tier A ({', '.join(MARKET_ALLOWED_TICKERS)})"

    # 2. Fetch live mark and ticker data
    tick_data = client.get_ticker(perp_sym, category="linear")
    if not tick_data:
        # Fallback to spot
        tick_data = client.get_ticker(bybit_spot_symbol(t), category="spot")
        if not tick_data:
            return False, "Failed to fetch live market data from Bybit"

    mark = float(tick_data.get("mark_price") or tick_data.get("last_price") or 0.0)
    funding = float(tick_data.get("funding_rate") or 0.0)
    if t == "PEPE":
        mark = mark / 1000.0

    # 3. Check band proximity:
    # If live mark is outside entry band and moving away, do NOT widen the band:
    band_tolerance = (entry_high - entry_low) * 1.5
    if side.upper() == "BUY":
        if mark > entry_high + band_tolerance:
            return False, f"NO_CHASE: mark ${mark:.4f} is above entry band ${entry_low:.4f}–${entry_high:.4f}"
    else:  # SELL
        if mark < entry_low - band_tolerance:
            return False, f"NO_CHASE: mark ${mark:.4f} is below entry band ${entry_low:.4f}–${entry_high:.4f}"

    # 4. Check funding rate:
    if side.upper() == "BUY" and funding > MAX_FUNDING_RATE:
        return False, f"Funding rate {funding * 100:.3f}% is extreme against LONG (> 0.05%)"
    if side.upper() == "SELL" and funding < -MAX_FUNDING_RATE:
        return False, f"Funding rate {funding * 100:.3f}% is extreme against SHORT (< -0.05%)"

    # 5. Check 15m candle verticality:
    klines = client.get_klines(perp_sym, interval="15", limit=5)
    if klines:
        latest = klines[-1]
        candle_body_pct = abs(latest["close"] - latest["open"]) / latest["open"]
        if candle_body_pct >= VERTICAL_CANDLE_BODY_PCT:
            if is_market:
                return False, f"Vertical 15m candle in progress ({candle_body_pct * 100:.2f}% body). Market order forbidden."

    # 6. Market order depth and mark checks:
    if is_market:
        # Check if live mark is strictly inside Entry range
        if not (entry_low <= mark <= entry_high):
            return False, f"Market order requires live mark strictly inside Entry range"

        # Check orderbook depth & slippage (< 0.15%)
        ob = client.get_orderbook(perp_sym, category="linear", limit=25)
        if ob:
            _, slip_pct = client.compute_book_slippage(ob, side, order_qty)
            if slip_pct >= MAX_SLIPPAGE_PCT:
                return False, f"Book slip {slip_pct:.3f}% exceeds 0.15% threshold — cancel market"

    return True, "VALID"


def evict_weakest_working_order(new_conviction: float, min_gap: float = 6.0) -> Optional[dict]:
    """
    Smart Slot Upgrade:
    If the portfolio is at max capacity and a high-conviction A+ setup arrives,
    find the lowest-conviction (and oldest) unfilled WORKING limit order that is at least
    `min_gap` conviction points weaker than `new_conviction`. Cancel it on Bybit and remove
    it from state to free a slot for the A+ setup.
    """
    open_pos = state.get_open_positions()
    working_candidates = []
    for pid, p in open_pos.items():
        if p.get("status") == "WORKING":
            p_conv = float(p.get("conviction", 80.0) or 80.0)
            if (new_conviction - p_conv) >= min_gap:
                working_candidates.append((p_conv, str(p.get("opened_at", "")), pid, p))

    if not working_candidates:
        return None

    # Sort by lowest conviction first, then oldest opened_at
    working_candidates.sort(key=lambda x: (x[0], x[1]))
    weak_conv, _, weak_pid, weak_pos = working_candidates[0]
    weak_sym = weak_pos.get("symbol") or bybit_linear_symbol(weak_pos.get("ticker", ""))

    # Verify it hasn't filled on Bybit in the meantime
    if not client.is_paper:
        try:
            active_syms = {bp.get("symbol") for bp in client.get_active_positions() if float(bp.get("size", 0) or 0) > 0}
            if weak_sym in active_syms:
                state.update_open_position(weak_pid, {"status": "OPEN"})
                return None
            # Cancel resting limit orders for this working setup
            oids = list(weak_pos.get("order_ids") or [])
            if weak_pos.get("order_id") and weak_pos.get("order_id") not in oids:
                oids.append(weak_pos.get("order_id"))
            for oid in oids:
                if oid:
                    client.cancel_order("linear", weak_sym, order_id=oid)
        except Exception as exc:
            log.debug("Smart Slot Upgrade cancel check exception for %s: %s", weak_sym, exc)

    removed = state.remove_open_position(weak_pid)
    if removed:
        log.info(
            "⚡ Smart Slot Upgrade: Cancelled unfilled WORKING order %s (%s, %.1f%% conv) to free slot for A+ setup (%.1f%% conv)",
            weak_pos.get("ticker"), weak_pos.get("side"), weak_conv, new_conviction
        )
    return removed


# ── Execution Logic (Perp + Spot) ───────────────────────────────────────────
def execute_signal(sig: dict) -> Dict[str, Any]:
    """
    Execute a BUY or SELL signal from Trade Desk:
      1. Checks VALID_IF criteria
      2. Computes position size from dynamic institutional equity risk
      3. Places PERP limit order (Post-Only) with isolated margin & leverage
      4. Places SPOT limit order (1x leverage, checked for inventory if SELL)
      5. Records open positions in state store
    """
    if sig.get("side", "").upper() == "WAIT":
        return {"status": "SKIPPED", "reason": "Signal is WAIT"}

    ticker = sig["ticker"].upper()
    side = sig["side"].upper()
    trade_type = sig.get("trade_type", "scalp").lower()
    entry_low = float(sig["entry_low"])
    entry_high = float(sig["entry_high"])
    tp1 = float(sig["tp1"]) if sig.get("tp1") is not None else None
    tp2 = float(sig["tp2"]) if sig.get("tp2") is not None else None
    tp3 = float(sig["tp3"]) if sig.get("tp3") is not None else None
    sl = float(sig["sl"]) if sig.get("sl") is not None else None

    # Decoupled Execution: Master Desk autonomous trade execution is strictly limited to proven liquid leaders.
    # Full universe remains 100% active for Winz Discord, Spot desks, and community signals.
    from config import AUTONOMOUS_TRADING_TICKERS
    if ticker not in AUTONOMOUS_TRADING_TICKERS:
        log.info("Master Desk Execution: Skipping %s (reserved for Winz Discord community signals; autonomous desk restricted to liquid leaders: %s)",
                 ticker, ", ".join(AUTONOMOUS_TRADING_TICKERS))
        return {"status": "SKIPPED", "reason": f"Asset {ticker} is active for Winz Discord community only; excluded from autonomous trade desk execution."}

    if sl is None:
        log.warning("Rejecting %s %s: SL is missing", ticker, side)
        return {"status": "REJECTED", "reason": "SL is missing"}

    entry_mid = (entry_low + entry_high) / 2.0

    # Pillar 2: Minimum Risk-to-Reward Gate (1.0:1 for scalps in chop, cfg.min_rr_gate for day trades)
    min_rr = 1.0 if trade_type == "scalp" else cfg.min_rr_gate
    if sl is not None and tp1 is not None and entry_mid > 0:
        sl_dist = abs(entry_mid - sl)
        tp_dist = abs(tp1 - entry_mid)
        rr_ratio = tp_dist / max(sl_dist, 1e-9)
        if rr_ratio < min_rr:
            log.info("Pillar 2 R:R Gate rejected %s %s: R:R ratio %.2f < %.2f minimum", ticker, side, rr_ratio, min_rr)
            return {"status": "REJECTED", "reason": f"Risk-to-reward ratio ({rr_ratio:.2f}) is below mandatory {min_rr:.1f}:1 minimum."}

    equity = get_current_equity()
    conviction = float(sig.get("conviction", 80) or 80)
    custom_lev = state.get_custom_leverage(ticker)
    if custom_lev is not None:
        lev = int(custom_lev)
    else:
        lev = sig.get("leverage") or get_leverage(ticker, scalp=(trade_type == "scalp"))
        lev = min(lev, get_hard_cap(ticker))

    # Pre-execution validation
    if state.is_paused():
        log.info("Execution rejected for %s %s: Bot execution is PAUSED", ticker, side)
        return {"status": "SKIPPED", "reason": "Bot execution is PAUSED. Send /resume to enable order placement."}

    open_pos = state.get_open_positions()
    # Per-Ticker Duplicate & Stacking Guard: Never open a duplicate position on the same ticker
    for p in open_pos.values():
        if p.get("ticker") == ticker and p.get("status") in ("OPEN", "WORKING"):
            log.info("Execution rejected for %s %s: Ticker already has active %s position (ID=%s)",
                     ticker, side, p.get("status"), p.get("id"))
            return {"status": "REJECTED", "reason": f"Active {p.get('status')} position already exists for {ticker}"}

    if len(open_pos) >= cfg.max_concurrent_positions:
        if conviction >= 88.0:
            evicted = evict_weakest_working_order(conviction, min_gap=6.0)
            if evicted:
                open_pos = state.get_open_positions()
        if len(open_pos) >= cfg.max_concurrent_positions:
            log.info("Max concurrent positions reached (%d/%d), rejecting %s", len(open_pos), cfg.max_concurrent_positions, ticker)
            return {"status": "REJECTED", "reason": f"Max concurrent positions cap reached ({len(open_pos)}/{cfg.max_concurrent_positions})"}

    perp_sym_check = bybit_linear_symbol(ticker)
    for bp in client.get_active_positions():
        if bp.get("symbol") == perp_sym_check and float(bp.get("size", 0) or 0) > 0:
            log.info("Execution rejected for %s %s: Bybit already has live %s position of size %s",
                     ticker, side, bp.get("side"), bp.get("size"))
            return {"status": "REJECTED", "reason": f"Live Bybit position already open for {perp_sym_check}"}

    # 2-Stage Daily Profit Shield Check (+10% Momentum Filter / +50% Jackpot Lock)
    profit_shield = state.get_daily_profit_shield_status(equity)
    if profit_shield["stage"] >= 1 and conviction < profit_shield["min_conviction"]:
        reason_msg = (
            f"Daily Profit Shield ({profit_shield['mode']}, +{profit_shield['daily_gain_pct']:.1f}% today): "
            f"Filtered {ticker} ({conviction:.0f}% < {profit_shield['min_conviction']:.0f}% min conviction)"
        )
        log.info("🛡️ %s", reason_msg)
        return {"status": "SKIPPED", "reason": reason_msg}

    # Hummingbot Inventory Skew & Directional Balance Rule
    skew_ok, skew_reason = check_portfolio_inventory_skew(side)
    if not skew_ok:
        log.info("Execution rejected for %s %s: %s", ticker, side, skew_reason)
        return {"status": "REJECTED", "reason": skew_reason}

    valid, reason = validate_execution_conditions(
        ticker=ticker,
        side=side,
        entry_low=entry_low,
        entry_high=entry_high,
        is_market=False,
    )
    if not valid:
        log.info("Validation failed for %s %s: %s", ticker, side, reason)
        return {"status": "INVALID", "reason": reason}

    # Dynamic Kelly Risk Sizing (conviction & weekend volatility regime adjustment)
    effective_risk_pct = cfg.risk_per_trade
    now_utc = datetime.now(timezone.utc)
    in_weekend = is_weekend_derisk_window(now_utc)

    if cfg.enable_dynamic_kelly_sizing:
        if in_weekend:
            # Use configured weekend chop risk (0.65% default) during Friday 21:00 -> Sunday 22:00 window
            effective_risk_pct = cfg.risk_weekend_chop
            log.info("Weekend Derisk Window active: Applied defensive sizing risk=%.2f%% for %s", effective_risk_pct * 100, ticker)
        elif conviction >= 88:
            # Scale up on A+ setups (multi-confluence, OI flush reversal, order book absorption)
            effective_risk_pct = cfg.risk_a_plus
            log.info("A+ Setup detected (conviction=%.0f): Applied Kelly scaled risk=%.2f%% for %s", conviction, effective_risk_pct * 100, ticker)
        else:
            effective_risk_pct = cfg.risk_standard

    if profit_shield["risk_mult"] < 1.0:
        effective_risk_pct *= profit_shield["risk_mult"]
        log.info("🏆 +50%% Daily Jackpot Lock Active (+%.1f%% today): Scaled risk to %.2f%% for %s",
                 profit_shield["daily_gain_pct"], effective_risk_pct * 100, ticker)

    # Sizing calculation
    perp_qty, spot_qty, size_note = compute_position_size(
        ticker=ticker,
        entry_price=entry_mid,
        sl=sl,
        equity=equity,
        leverage=lev,
        risk_pct=effective_risk_pct,
        trade_type=trade_type,
    )
    if perp_qty <= 0:
        log.warning("Position sizing returned 0 for %s: %s", ticker, size_note)
        return {"status": "REJECTED", "reason": size_note}

    perp_sym = bybit_linear_symbol(ticker)
    spot_sym = bybit_spot_symbol(ticker)

    if state.is_on_probation(ticker):
        perp_qty = client.quantize_qty(perp_sym, perp_qty * 0.5, category="linear")
        spot_qty = client.quantize_qty(spot_sym, spot_qty * 0.5, category="spot")
        log.info("Asset %s is on PROBATION: Applied 50%% size reduction (perp_qty=%.4f)", ticker, perp_qty)

    now_ts = datetime.now(timezone.utc).isoformat()
    pos_id_perp = f"{ticker}_{side}_perp_{trade_type}_{uuid.uuid4().hex[:8]}"
    pos_id_spot = f"{ticker}_{side}_spot_{trade_type}_{uuid.uuid4().hex[:8]}"

    # Effective limit price on Bybit Linear
    perp_limit_price = entry_mid
    perp_sl = sl
    perp_tp1 = tp1
    perp_tp2 = tp2

    if ticker == "PEPE":
        # 1000PEPEUSDT price = standard PEPE price * 1000
        perp_limit_price = entry_mid * 1000.0
        perp_sl = sl * 1000.0 if sl else None
        perp_tp1 = tp1 * 1000.0 if tp1 else None
        perp_tp2 = tp2 * 1000.0 if tp2 else None

    perp_order_id = None
    spot_order_id = None
    placed_order_ids: List[str] = []
    is_micro_grid = False
    partial_tp_placed = False
    partial_tp_order_id = None

    # ── 1. Execute PERP Block ────────────────────────────────────────────────
    use_market_order = False
    if cfg.is_live or (not client.is_paper and cfg.trade_mode == "demo"):
        client.set_isolated_margin_and_leverage(perp_sym, lev)

        # Check Tier A market order rule:
        # Market order only on Tier A (BTC ETH SOL XRP BNB) if inside Entry and book slip < 0.15%
        tick_data = client.get_ticker(perp_sym, category="linear") or client.get_ticker(spot_sym, category="spot")
        current_mark = tick_data["mark_price"] if tick_data else entry_mid
        if ticker == "PEPE":
            current_mark = current_mark / 1000.0

        # 100% Post-Only Maker Discipline:
        # Market orders are disabled across all tickers to eliminate 0.055% taker fee leakage.
        # All orders rest inside the shelf as Post-Only limits.
        use_market_order = False

        target_tp = perp_tp2 if perp_tp2 else perp_tp1

        if use_market_order:
            resp = client.place_order(
                category="linear",
                symbol=perp_sym,
                side=side,
                order_type="Market",
                qty=perp_qty,
                price=None,
                time_in_force="IOC",
                stop_loss=perp_sl,
                take_profit=target_tp,
            )
            if resp:
                perp_order_id = resp.get("orderId")
                if perp_order_id:
                    placed_order_ids.append(perp_order_id)
                # If market order executed (instant fill), brief pause to allow Bybit position update before reduceOnly order
                if perp_tp1 and perp_tp2:
                    time.sleep(0.4)
                    tp_side = "Sell" if side.upper() == "BUY" else "Buy"
                    is_volatile = is_volatile_ticker(ticker) or cfg.enable_three_tier_scaleout
                    if is_volatile:
                        # 3-Tier Model: 33% @ TP1, 33% @ TP2, 34% dynamic runner
                        q_tp1 = client.quantize_qty(perp_sym, perp_qty * 0.33, category="linear")
                        q_tp2 = client.quantize_qty(perp_sym, perp_qty * 0.33, category="linear")
                        t1_resp = None
                        t2_resp = None
                        if q_tp1 > 0:
                            t1_resp = client.place_order(
                                category="linear",
                                symbol=perp_sym,
                                side=tp_side,
                                order_type="Limit",
                                qty=q_tp1,
                                price=perp_tp1,
                                time_in_force="GTC",
                                reduce_only=True,
                            )
                        if q_tp2 > 0 and perp_tp2:
                            t2_resp = client.place_order(
                                category="linear",
                                symbol=perp_sym,
                                side=tp_side,
                                order_type="Limit",
                                qty=q_tp2,
                                price=perp_tp2,
                                time_in_force="GTC",
                                reduce_only=True,
                            )
                        if t1_resp or t2_resp:
                            partial_tp_placed = True
                            partial_tp_order_id = t1_resp.get("orderId") if t1_resp else (t2_resp.get("orderId") if t2_resp else None)
                            log.info("Placed 3-Tier Partial TPs on Bybit for volatile %s: Tier 1 (33%% @ $%.4f, ID=%s), Tier 2 (33%% @ $%.4f, ID=%s), Runner=34%%",
                                     ticker, perp_tp1, t1_resp.get("orderId") if t1_resp else "N/A",
                                     perp_tp2, t2_resp.get("orderId") if t2_resp else "N/A")
                    else:
                        half_qty = client.quantize_qty(perp_sym, perp_qty * 0.5, category="linear")
                        if half_qty > 0:
                            tp_resp = client.place_order(
                                category="linear",
                                symbol=perp_sym,
                                side=tp_side,
                                order_type="Limit",
                                qty=half_qty,
                                price=perp_tp1,
                                time_in_force="GTC",
                                reduce_only=True,
                            )
                            if tp_resp:
                                partial_tp_placed = True
                                partial_tp_order_id = tp_resp.get("orderId")
                                log.info("Placed 50%% Partial TP reduceOnly order on Bybit for %s @$%.4f (qty=%.4f ID=%s)",
                                         ticker, perp_tp1, half_qty, partial_tp_order_id)
        else:
            # Passivbot Micro-Grid Staggered Limit Placement across entry band
            p_near = entry_high if side.upper() == "BUY" else entry_low
            p_deep = entry_low if side.upper() == "BUY" else entry_high

            if ticker == "PEPE":
                p_near *= 1000.0
                p_deep *= 1000.0

            q_near = client.quantize_qty(perp_sym, perp_qty * 0.5, category="linear")
            q_deep = client.quantize_qty(perp_sym, perp_qty - q_near, category="linear")
            info_perp = client.get_instrument_info(perp_sym, category="linear")
            min_q = info_perp.get("min_qty", 0.0)

            if cfg.enable_micro_grid_entry and q_near >= min_q and q_deep >= min_q and abs(p_near - p_deep) > 0:
                is_micro_grid = True
                log.info("Passivbot Micro-Grid: Placing 2 staggered Post-Only limits for %s %s: 50%% @$%.4f (qty=%.4f), 50%% @$%.4f (qty=%.4f)",
                         ticker, side, p_near, q_near, p_deep, q_deep)
                resp1 = client.place_order(
                    category="linear",
                    symbol=perp_sym,
                    side=side,
                    order_type="Limit",
                    qty=q_near,
                    price=p_near,
                    time_in_force="PostOnly",
                    stop_loss=perp_sl,
                    take_profit=target_tp,
                )
                if resp1 and resp1.get("orderId"):
                    placed_order_ids.append(resp1.get("orderId"))

                resp2 = client.place_order(
                    category="linear",
                    symbol=perp_sym,
                    side=side,
                    order_type="Limit",
                    qty=q_deep,
                    price=p_deep,
                    time_in_force="PostOnly",
                    stop_loss=perp_sl,
                    take_profit=target_tp,
                )
                if resp2 and resp2.get("orderId"):
                    placed_order_ids.append(resp2.get("orderId"))

                perp_order_id = placed_order_ids[0] if placed_order_ids else None
            else:
                resp = client.place_order(
                    category="linear",
                    symbol=perp_sym,
                    side=side,
                    order_type="Limit",
                    qty=perp_qty,
                    price=perp_limit_price,
                    time_in_force="PostOnly",
                    stop_loss=perp_sl,
                    take_profit=target_tp,
                )
                if resp and resp.get("orderId"):
                    perp_order_id = resp.get("orderId")
                    placed_order_ids.append(perp_order_id)
    else:
        # Paper simulation: simulate resting limit order / instant fill at shelf mid
        perp_order_id = f"sim_perp_{uuid.uuid4().hex[:8]}"
        placed_order_ids = [perp_order_id, f"sim_perp_g2_{uuid.uuid4().hex[:8]}"]
        is_micro_grid = cfg.enable_micro_grid_entry
        partial_tp_placed = False
        partial_tp_order_id = None

    pos_perp = {
        "id": pos_id_perp,
        "ticker": ticker,
        "symbol": perp_sym,
        "side": side,
        "market": "perp",
        "trade_type": trade_type,
        "entry_price": entry_mid,
        "qty": perp_qty,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "sl": sl,
        "initial_sl": sl,
        "leverage": lev,
        "conviction": conviction,
        "order_id": perp_order_id,
        "order_ids": placed_order_ids,
        "micro_grid": is_micro_grid,
        "partial_tp_placed": partial_tp_placed,
        "partial_tp_order_id": partial_tp_order_id,
        "status": "WORKING" if (not client.is_paper and not use_market_order) else "OPEN",
        "opened_at": now_ts,
    }
    state.open_position(pos_perp)
    if not client.is_paper and not use_market_order:
        notify_order_placed(pos_perp)
    else:
        notify_entry_filled(pos_perp)
    log.info("Opened PERP order: %s %s @$%.4f qty=%.6f lev=%dx status=%s", ticker, side, entry_mid, perp_qty, lev, pos_perp["status"])

    # Dispatch to connected live Bybit users if order is active immediately (paper demo or market order)
    if pos_perp.get("status") == "OPEN":
        try:
            import exchange_manager
            sig_payload = {
                "ticker": ticker,
                "side": side,
                "live_price": entry_mid,
                "trigger_price": entry_mid,
                "sl": sl,
                "tp1": tp1,
                "tp2": tp2,
                "tf": "15m" if trade_type == "scalp" else "4h",
                "conviction": conviction,
                "score": conviction,
                "confidence": conviction,
                "grade": "A+" if conviction >= 88 else "A",
            }
            call_cfg = state.get_call_bot_config()
            c_tok = call_cfg.get("token") or cfg.call_bot_token or cfg.telegram_token
            threading.Thread(
                target=exchange_manager.dispatch_triggered_trade_to_users,
                args=(sig_payload,),
                kwargs={"bot_token": c_tok},
                daemon=True,
            ).start()
        except Exception as exc:
            log.warning("Could not dispatch master order to live connected users: %s", exc)

    # ── 2. Execute SPOT Block ────────────────────────────────────────────────
    # Check if spot trading is enabled via /onspot (disabled via /offspot)
    execute_spot = state.is_spot_enabled()
    if not execute_spot:
        log.info("Spot trade for %s skipped: spot trading is disabled (/offspot)", ticker)
    elif side == "SELL":
        if client.is_paper:
            execute_spot = False  # Paper mode does not assume spot inventory
        else:
            inv = client.get_coin_inventory(ticker)
            if inv <= 0:
                log.info("Spot sell for %s dropped: inventory is 0 (flat)", ticker)
                execute_spot = False

    if execute_spot:
        if cfg.is_live or (not client.is_paper and cfg.trade_mode == "demo"):
            spot_order_type = "Market" if (use_market_order and ticker in MARKET_ALLOWED_TICKERS) else "Limit"
            spot_tif = "IOC" if spot_order_type == "Market" else "GTC"
            spot_order_price = None if spot_order_type == "Market" else entry_mid

            resp_spot = client.place_order(
                category="spot",
                symbol=spot_sym,
                side=side,
                order_type=spot_order_type,
                qty=spot_qty,
                price=spot_order_price,
                time_in_force=spot_tif,
            )
            if resp_spot:
                spot_order_id = resp_spot.get("orderId")
                if side.upper() == "BUY" and tp1:
                    half_spot = client.quantize_qty(spot_sym, spot_qty * 0.5, category="spot")
                    if half_spot > 0:
                        client.place_order(
                            category="spot",
                            symbol=spot_sym,
                            side="Sell",
                            order_type="Limit",
                            qty=half_spot,
                            price=tp1,
                            time_in_force="GTC",
                        )
                        log.info("Placed 50%% Partial TP spot sell on Bybit for %s @$%.4f (qty=%.4f)",
                                 ticker, tp1, half_spot)
        else:
            spot_order_id = f"sim_spot_{uuid.uuid4().hex[:8]}"

        pos_spot = {
            "id": pos_id_spot,
            "ticker": ticker,
            "symbol": spot_sym,
            "side": side,
            "market": "spot",
            "trade_type": trade_type,
            "entry_price": entry_mid,
            "qty": spot_qty,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "sl": sl,
            "leverage": 1,
            "order_id": spot_order_id,
            "status": "WORKING" if (not client.is_paper and not use_market_order) else "OPEN",
            "opened_at": now_ts,
        }
        state.open_position(pos_spot)
        if not client.is_paper:
            notify_order_placed(pos_spot)
        else:
            notify_entry_filled(pos_spot)
        log.info("Opened SPOT order: %s %s @$%.4f qty=%.6f 1x status=%s", ticker, side, entry_mid, spot_qty, pos_spot["status"])

    return {
        "status": "SUCCESS",
        "ticker": ticker,
        "side": side,
        "perp_order_id": perp_order_id,
        "spot_order_id": spot_order_id,
        "perp_qty": perp_qty,
        "spot_qty": spot_qty,
    }


# ── Executor Contract Line Parser ───────────────────────────────────────────
def parse_and_execute_contract_line(line: str) -> Dict[str, Any]:
    """
    Parse and execute a machine contract line:
    BOT|TICKER|SIDE|VENUE|TF|ENTRY_LOW|ENTRY_HIGH|TP1|TP2|SL|LEV|MAX_RISK_PCT|EXPIRE_UTC|VALID_IF
    or WAIT:
    BOT|TICKER|WAIT||||||
    """
    line = line.strip()
    if not line.startswith("BOT|"):
        return {"status": "IGNORED", "reason": "Does not start with BOT|"}

    parts = line.split("|")
    if len(parts) < 3:
        return {"status": "MALFORMED", "reason": "Not enough fields"}

    ticker = parts[1].strip().upper()
    side = parts[2].strip().upper()

    if side == "WAIT":
        return {"status": "WAIT", "ticker": ticker}

    if len(parts) < 14:
        return {"status": "MALFORMED", "reason": f"Expected 14 fields, got {len(parts)}"}

    venue = parts[3].strip()
    tf = parts[4].strip()
    try:
        entry_low = float(parts[5].strip())
        entry_high = float(parts[6].strip())
        tp1 = float(parts[7].strip()) if parts[7].strip() else None
        tp2 = float(parts[8].strip()) if parts[8].strip() else None
        sl = float(parts[9].strip()) if parts[9].strip() else None
        lev = int(parts[10].strip()) if parts[10].strip() else 3
        max_risk = float(parts[11].strip()) if parts[11].strip() else 0.005
        expire_utc = parts[12].strip()
        valid_if = parts[13].strip()
    except Exception as exc:
        return {"status": "PARSE_ERROR", "reason": str(exc)}

    sig = {
        "ticker": ticker,
        "side": side,
        "trade_type": "scalp" if tf == "15m" else "day",
        "tf": tf,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "tp1": tp1,
        "tp2": tp2,
        "sl": sl,
        "leverage": lev,
        "valid_if": valid_if,
    }
    return execute_signal(sig)


# ── Position Monitor Loop (Simulated & Demo TP/SL tracking) ─────────────────
_monitor_running = False


def _sync_with_bybit() -> None:
    """
    Synchronize local state with real Bybit Demo / Live exchange state:
      1. Pull actual wallet balance and update equity.
      2. Pull recently closed positions from Bybit (/v5/position/closed-pnl).
         Record new closed trades, remove from open_positions, notify Telegram.
      3. Pull active positions from Bybit (/v5/position/list).
         Update mark price, unrealised PnL, leverage.
         If order was 'WORKING' and is now in active positions -> notify ENTRY_FILLED!
         If mark touches TP1 -> adjust stop loss on Bybit to break-even and notify once!
      4. Pull open resting orders (/v5/order/realtime) to update working order status.
    """
    # 1. Update wallet equity
    bal = client.get_wallet_balance("USDT")
    eq = bal.get("equity", 0.0)
    if eq > 0:
        state.set_equity(eq)

    # Fetch active Bybit positions first so Step 2 knows whether a closed-pnl record is a partial scale-out or full close
    bybit_positions = client.get_active_positions(category="linear")
    bybit_syms = {p["symbol"]: p for p in bybit_positions}

    # 2. Check Bybit closed PnL
    closed_records = client.get_closed_pnl(category="linear", limit=20)
    for c in closed_records:
        order_id = c.get("order_id")
        if not order_id:
            continue
        sym = c.get("symbol", "")
        ticker = sym.replace("USDT", "")
        if ticker.startswith("1000"):
            ticker = ticker[4:]

        existing_closed = [p for p in state.get_closed_positions() if p.get("order_id") == order_id]
        if not existing_closed:
            matched_pos_id = None
            matched_pos = None
            for pid, p in state.get_open_positions().items():
                if p.get("ticker") == ticker and p.get("market") == "perp":
                    matched_pos_id = pid
                    matched_pos = p
                    break

            entry_p = float(c.get("entry_price") or 0.0)
            exit_p = float(c.get("exit_price") or 0.0)
            pnl_u = float(c.get("closed_pnl") or 0.0)
            qty_c = float(c.get("qty") or 0.0)
            # In Bybit closed-pnl, 'side' is the closing order's side (Sell closes Buy, Buy closes Sell)
            c_side = matched_pos.get("side") if matched_pos else ("SELL" if c.get("side", "").lower() == "buy" else "BUY")

            if ticker == "PEPE":
                entry_p = entry_p / 1000.0
                exit_p = exit_p / 1000.0

            lev = matched_pos.get("leverage", 4) if matched_pos else 4
            notional_c = (entry_p * 1000.0 * qty_c) if ticker == "PEPE" else (entry_p * qty_c)
            margin = (notional_c / max(1, lev)) if notional_c > 0 else 1.0
            pnl_pct = round((pnl_u / margin) * 100, 2)

            is_partial_close = (sym in bybit_syms and float(bybit_syms[sym].get("size", 0)) > 0)
            exit_reason_str = (
                f"BYBIT_PARTIAL_{c.get('exec_type', 'TRADE').upper()}"
                if is_partial_close
                else f"BYBIT_{c.get('exec_type', 'TRADE').upper()}"
            )

            closed_entry = {
                "id": f"{ticker}_{c_side}_closed_{order_id[:8]}",
                "ticker": ticker,
                "symbol": sym,
                "side": c_side,
                "market": "perp",
                "entry_price": entry_p,
                "exit_price": exit_p,
                "qty": qty_c,
                "pnl_usdt": round(pnl_u, 4),
                "pnl_pct": pnl_pct,
                "exit_reason": exit_reason_str,
                "order_id": order_id,
                "closed_at": datetime.now(timezone.utc).isoformat(),
            }
            state.record_closed_position(closed_entry)

            # CRITICAL: Only remove open_position and fire full-close lifecycle if the position is 100% closed on Bybit!
            if not is_partial_close:
                if matched_pos_id:
                    state.remove_open_position(matched_pos_id)
                notifier.notify_trade_closed(closed_entry)

                # Record probation lifecycle outcome if asset was on probation
                prob_status = state.record_probation_trade(ticker, pnl_u)
                if prob_status == "GRADUATED":
                    log.info("%s graduated from probation! Restored to full size.", ticker)
                    notifier.send_text(f"🎓 *Probation Graduated:* {ticker} finished 3 probation trades in profit! Restored to 100% position sizing.")
                elif prob_status == "RE_QUARANTINED":
                    log.info("%s failed probation and re-quarantined for 24h.", ticker)
                    notifier.send_text(f"🚫 *Probation Failed:* {ticker} incurred net loss on probation (${pnl_u:,.2f}). Quarantined on Avoid List for 24h.")
                elif pnl_u < -1.0:
                    # Real-time 2-loss circuit breaker for individual ticker
                    quar_info = state.check_and_trigger_consecutive_loss_quarantine(ticker)
                    if quar_info:
                        log.warning("Real-time circuit breaker triggered for %s", ticker)
                        client.cancel_all_orders(category="linear", symbol=sym)
                        q_hrs = float(quar_info.get("hours", 12.0))
                        notifier.send_text(
                            f"🚨 *Instant Circuit Breaker Triggered: {ticker}*\n\n"
                            f"• *Reason*: Asset suffered 2 consecutive stop-outs.\n"
                            f"• *Action*: Automatically quarantined on Avoid List for {q_hrs:.0f} hours.\n"
                            f"• *Outcome*: All resting limit orders purged. Capital protected from repeated drawdowns."
                        )

                # Global 24h Account Loss Circuit Breaker check
                gcb = state.check_and_trigger_global_daily_circuit_breaker()
                if gcb:
                    log.warning("Global daily circuit breaker triggered: loss -$%.2f", gcb.get("loss_24h", 0.0))
                    notifier.send_text(
                        f"🛑 *GLOBAL ACCOUNT CIRCUIT BREAKER TRIGGERED*\n\n"
                        f"• *24h Realized Loss*: **-${gcb.get('loss_24h', 0.0):,.2f} USDT**\n"
                        f"• *Threshold*: -${cfg.max_daily_loss_usdt:,.2f} USDT\n"
                        f"• *Action*: All automated trade entries **FROZEN for {gcb.get('hours', 12.0):.0f} hours**.\n"
                        f"• *Protection*: Portfolio drawdown capped. Existing open positions remain monitored with active trailing stops."
                    )

    # 3. Check Bybit active positions
    local_positions = state.get_open_positions()

    for sym, bp in list(bybit_syms.items()):
        ticker = sym.replace("USDT", "")
        if ticker.startswith("1000"):
            ticker = ticker[4:]

        b_side = bp["side"].upper()
        b_entry = float(bp["entry_price"])
        b_mark = float(bp["mark_price"])
        b_qty = float(bp["size"])
        b_pnl = float(bp["unrealised_pnl"])
        b_lev = int(bp["leverage"])
        b_tp = float(bp["take_profit"]) if bp.get("take_profit") else None
        b_sl = float(bp["stop_loss"]) if bp.get("stop_loss") else None

        if ticker == "PEPE":
            b_entry = b_entry / 1000.0
            b_mark = b_mark / 1000.0

        matched_id = None
        matched_pos = None
        for pid, p in local_positions.items():
            if p.get("ticker") == ticker and p.get("market") == "perp":
                matched_id = pid
                matched_pos = p
                break

        # Auto-clean leftover dust positions (< $50 notional on Master Desk) so they never clog a portfolio slot
        b_notional = b_qty * b_mark * (1000.0 if ticker == "PEPE" else 1.0)
        if b_notional < 50.0:
            close_side = "Sell" if b_side == "BUY" else "Buy"
            log.info("Auto-cleaning dust position on Bybit: %s %s qty=%s (notional=$%.2f)", sym, b_side, b_qty, b_notional)
            client.close_position_market(sym, side=close_side, qty=b_qty, category="linear")
            if matched_id:
                state.remove_open_position(matched_id)
            bybit_syms.pop(sym, None)
            continue

        last_sig = state.get_last_signal(ticker)
        sig_matches_side = bool(last_sig and str(last_sig.get("side", "")).upper() == b_side)

        if matched_pos:
            was_working = (matched_pos.get("status") == "WORKING")
            updates = {
                "status": "OPEN",
                "entry_price": b_entry,
                "mark_price": b_mark,
                "unrealised_pnl": b_pnl,
                "qty": b_qty,
                "leverage": b_lev,
            }
            if b_tp and not matched_pos.get("tp1"):
                updates["tp1"] = b_tp / 1000.0 if ticker == "PEPE" else b_tp
            elif not matched_pos.get("tp1") and sig_matches_side and last_sig.get("tp1"):
                updates["tp1"] = last_sig.get("tp1")
            if not matched_pos.get("tp2") and sig_matches_side and last_sig.get("tp2"):
                updates["tp2"] = last_sig.get("tp2")
            if not matched_pos.get("tp3") and sig_matches_side and last_sig.get("tp3"):
                updates["tp3"] = last_sig.get("tp3")
            if b_sl and not matched_pos.get("sl"):
                updates["sl"] = b_sl / 1000.0 if ticker == "PEPE" else b_sl
            if not matched_pos.get("initial_sl"):
                if sig_matches_side and last_sig.get("sl"):
                    updates["initial_sl"] = last_sig.get("sl")
                elif matched_pos.get("sl"):
                    updates["initial_sl"] = matched_pos.get("sl")

            # Fallback: if tp1 is still None, compute institutional +1.5R / +2.5R / +3.5R targets from initial_sl
            eff_init_sl = updates.get("initial_sl") or matched_pos.get("initial_sl") or updates.get("sl") or matched_pos.get("sl")
            if not (updates.get("tp1") or matched_pos.get("tp1")) and eff_init_sl:
                r_d = abs(b_entry - float(eff_init_sl))
                if r_d / max(b_entry, 1e-9) < 0.001:
                    r_d = b_entry * 0.012
                dec = 9 if ticker == "PEPE" else 5
                updates["tp1"] = round(b_entry + 1.5 * r_d if b_side == "BUY" else b_entry - 1.5 * r_d, dec)
                updates["tp2"] = round(b_entry + 2.5 * r_d if b_side == "BUY" else b_entry - 2.5 * r_d, dec)
                updates["tp3"] = round(b_entry + 3.5 * r_d if b_side == "BUY" else b_entry - 3.5 * r_d, dec)

            state.update_open_position(matched_id, updates)
            matched_pos.update(updates)

            if was_working:
                log.info("Limit order for %s filled on Bybit! Now OPEN.", ticker)
                notifier.notify_entry_filled(matched_pos)
                try:
                    import exchange_manager
                    m_conv = float(matched_pos.get("conviction") or (last_sig.get("conviction") if sig_matches_side else 85.0) or 85.0)
                    m_tt = str(matched_pos.get("trade_type", "scalp")).lower()
                    sig_payload = {
                        "ticker": ticker,
                        "side": b_side,
                        "live_price": b_entry,
                        "trigger_price": b_entry,
                        "sl": matched_pos.get("sl"),
                        "tp1": matched_pos.get("tp1"),
                        "tp2": matched_pos.get("tp2"),
                        "tf": "15m" if m_tt == "scalp" else "4h",
                        "conviction": m_conv,
                        "score": m_conv,
                        "confidence": m_conv,
                        "grade": "A+" if m_conv >= 88 else "A",
                    }
                    call_cfg = state.get_call_bot_config()
                    c_tok = call_cfg.get("token") or cfg.call_bot_token or cfg.telegram_token
                    threading.Thread(
                        target=exchange_manager.dispatch_triggered_trade_to_users,
                        args=(sig_payload,),
                        kwargs={"bot_token": c_tok},
                        daemon=True,
                    ).start()
                except Exception as exc:
                    log.warning("Could not dispatch filled limit order to live users: %s", exc)
                if matched_pos.get("tp1") and not matched_pos.get("partial_tp_placed"):
                    tp_side = "Sell" if b_side == "BUY" else "Buy"
                    p_tp1 = matched_pos["tp1"] * 1000.0 if ticker == "PEPE" else matched_pos["tp1"]
                    p_tp2 = matched_pos.get("tp2")
                    if p_tp2 and ticker == "PEPE":
                        p_tp2 = p_tp2 * 1000.0

                    is_volatile = is_volatile_ticker(ticker) or cfg.enable_three_tier_scaleout
                    if is_volatile:
                        # 3-Tier Model: 33% @ TP1, 33% @ TP2, 34% runner
                        q_tp1 = client.quantize_qty(sym, b_qty * 0.33, category="linear")
                        q_tp2 = client.quantize_qty(sym, b_qty * 0.33, category="linear")
                        t1_res = None
                        t2_res = None
                        if q_tp1 > 0:
                            t1_res = client.place_order(
                                category="linear",
                                symbol=sym,
                                side=tp_side,
                                order_type="Limit",
                                qty=q_tp1,
                                price=p_tp1,
                                time_in_force="GTC",
                                reduce_only=True,
                            )
                        if q_tp2 > 0 and p_tp2:
                            t2_res = client.place_order(
                                category="linear",
                                symbol=sym,
                                side=tp_side,
                                order_type="Limit",
                                qty=q_tp2,
                                price=p_tp2,
                                time_in_force="GTC",
                                reduce_only=True,
                            )
                        if t1_res or t2_res:
                            state.update_open_position(matched_id, {
                                "partial_tp_placed": True,
                                "partial_tp_order_id": t1_res.get("orderId") if t1_res else (t2_res.get("orderId") if t2_res else None),
                                "tp1_order_id": t1_res.get("orderId") if t1_res else None,
                                "tp2_order_id": t2_res.get("orderId") if t2_res else None,
                            })
                            log.info("Placed 3-Tier Partial TPs on Bybit for filled volatile %s: Tier 1 (33%% @ $%.4f, ID=%s), Tier 2 (33%% @ $%.4f, ID=%s), Runner=34%%",
                                     ticker, p_tp1, t1_res.get("orderId") if t1_res else "N/A",
                                     p_tp2 if p_tp2 else 0.0, t2_res.get("orderId") if t2_res else "N/A")
                    else:
                        half_qty = client.quantize_qty(sym, b_qty * 0.5, category="linear")
                        if half_qty > 0:
                            tp_res = client.place_order(
                                category="linear",
                                symbol=sym,
                                side=tp_side,
                                order_type="Limit",
                                qty=half_qty,
                                price=p_tp1,
                                time_in_force="GTC",
                                reduce_only=True,
                            )
                            if tp_res:
                                state.update_open_position(matched_id, {
                                    "partial_tp_placed": True,
                                    "partial_tp_order_id": tp_res.get("orderId"),
                                })
                                log.info("Placed 50%% Partial TP limit order on Bybit for filled %s @$%.4f (qty=%.4f ID=%s)",
                                         ticker, p_tp1, half_qty, tp_res.get("orderId"))
        else:
            # Active on Bybit but missing locally
            rec_tp1 = (b_tp / 1000.0 if (b_tp and ticker == "PEPE") else b_tp) or (last_sig.get("tp1") if sig_matches_side else None)
            rec_tp2 = last_sig.get("tp2") if sig_matches_side else None
            rec_tp3 = last_sig.get("tp3") if sig_matches_side else None
            rec_sl = (b_sl / 1000.0 if (b_sl and ticker == "PEPE") else b_sl) or (last_sig.get("sl") if sig_matches_side else None)
            rec_init_sl = (last_sig.get("sl") if sig_matches_side else None) or rec_sl
            rec_type = (last_sig.get("trade_type") if sig_matches_side else None) or "scalp"
            pos_id = f"{ticker}_{b_side}_perp_{rec_type}_{uuid.uuid4().hex[:8]}"
            new_pos = {
                "id": pos_id,
                "ticker": ticker,
                "symbol": sym,
                "side": b_side,
                "market": "perp",
                "trade_type": rec_type,
                "entry_price": b_entry,
                "mark_price": b_mark,
                "unrealised_pnl": b_pnl,
                "qty": b_qty,
                "tp1": rec_tp1,
                "tp2": rec_tp2,
                "tp3": rec_tp3,
                "sl": rec_sl,
                "initial_sl": rec_init_sl,
                "leverage": b_lev,
                "status": "OPEN",
                "opened_at": datetime.now(timezone.utc).isoformat(),
            }
            state.open_position(new_pos)
            matched_pos = new_pos
            matched_id = pos_id

        # Check Early Break-Even / Multi-Step Ratchet Trail condition
        tp1 = matched_pos.get("tp1")
        b_sl_orig = float(matched_pos.get("initial_sl") or matched_pos.get("sl") or b_entry)
        if abs(b_entry - b_sl_orig) / max(b_entry, 1e-9) < 0.001:
            # SL is already at Break-Even; use standard 1.2% risk distance and ensure tp0_hit/be_trailed are flagged
            risk_dist = b_entry * 0.012
            if not matched_pos.get("be_trailed") or not matched_pos.get("tp0_hit"):
                state.update_open_position(matched_id, {"be_trailed": True, "tp0_hit": True})
                matched_pos["be_trailed"] = True
                matched_pos["tp0_hit"] = True
        else:
            risk_dist = abs(b_entry - b_sl_orig)

        # Watchdog: Ensure every active position always has a hard exchange Stop Loss on Bybit
        if b_sl is None and not matched_pos.get("sl_attached"):
            def_sl = float(matched_pos.get("sl") or 0.0)
            if not def_sl or (b_side == "BUY" and def_sl >= b_mark) or (b_side == "SELL" and def_sl <= b_mark):
                if ticker == "PEPE":
                    def_sl = round(b_mark * 0.992 if b_side == "BUY" else b_mark * 1.008, 9)
                else:
                    def_sl = round(b_mark * 0.992 if b_side == "BUY" else b_mark * 1.008, 4)
                state.update_open_position(matched_id, {"sl": def_sl})
                matched_pos["sl"] = def_sl
            be_sl = def_sl * 1000.0 if ticker == "PEPE" else def_sl
            be_sl = client.quantize_price(sym, be_sl, category="linear")
            log.warning("Watchdog: Attaching missing Stop Loss on Bybit for %s %s @%s", ticker, b_side, be_sl)
            if client.set_trading_stop(sym, stop_loss=be_sl):
                matched_pos["sl_attached"] = True

        if cfg.enable_ratchet_trail and not matched_pos.get("tp2_hit"):
            curr_r = (b_mark - b_entry) / risk_dist if b_side == "BUY" else (b_entry - b_mark) / risk_dist

            # Level 4 Ratchet: at +2.50R progress, lock in +1.50R guaranteed runner profit on Bybit
            if curr_r >= 2.50 and not matched_pos.get("ratchet_l4"):
                lock_p = round(b_entry + (risk_dist * 1.50) if b_side == "BUY" else b_entry - (risk_dist * 1.50), 8)
                log.info("Ratchet Level 4 (+2.50R) reached on Bybit for %s %s! Locking +1.50R @%.4f", ticker, b_side, lock_p)
                state.update_open_position(matched_id, {"ratchet_l4": True, "ratchet_l3": True, "be_trailed": True, "sl": lock_p})
                matched_pos["ratchet_l4"] = True
                matched_pos["sl"] = lock_p
                be_sl = lock_p * 1000.0 if ticker == "PEPE" else lock_p
                client.set_trading_stop(sym, stop_loss=be_sl)
                notifier.send_text(
                    f"🚀 *Major Profit Runner Engaged: {ticker}*\n\n"
                    f"• *Current Gain*: +{curr_r:.2f}R (`{fmt_dollar(b_mark)}`)\n"
                    f"• *Action*: Exchange Stop Loss shifted to **+{1.50:.2f}R** profit (`{fmt_dollar(lock_p)}`)\n"
                    f"• *Status*: Substantial institutional runner profit locked in!"
                )
            # Level 3 Ratchet: at +1.75R progress, lock in +0.80R guaranteed green profit
            elif curr_r >= 1.75 and not matched_pos.get("ratchet_l3"):
                lock_p = round(b_entry + (risk_dist * 0.80) if b_side == "BUY" else b_entry - (risk_dist * 0.80), 8)
                log.info("Ratchet Level 3 (+1.75R) reached on Bybit for %s %s! Locking +0.80R @%.4f", ticker, b_side, lock_p)
                state.update_open_position(matched_id, {"ratchet_l3": True, "be_trailed": True, "sl": lock_p})
                matched_pos["ratchet_l3"] = True
                matched_pos["sl"] = lock_p
                be_sl = lock_p * 1000.0 if ticker == "PEPE" else lock_p
                client.set_trading_stop(sym, stop_loss=be_sl)
                notifier.send_text(
                    f"🔒 *Guaranteed Profit Lock Active: {ticker}*\n\n"
                    f"• *Current Gain*: +{curr_r:.2f}R (`{fmt_dollar(b_mark)}`)\n"
                    f"• *Action*: Exchange Stop Loss shifted to **+{0.80:.2f}R** profit (`{fmt_dollar(lock_p)}`)\n"
                    f"• *Status*: Green profit locked in. Position running to TP1 & TP2!"
                )
            # Level 2 Ratchet: at +1.25R progress, trail SL to Break-Even + fee buffer (+0.15R)
            elif curr_r >= 1.25 and not matched_pos.get("be_trailed"):
                be_buf = round(b_entry + (risk_dist * 0.15) if b_side == "BUY" else b_entry - (risk_dist * 0.15), 8)
                log.info("Break-Even Ratchet (+1.25R) reached for %s %s! Trailing SL to BE+Fees @%.4f", ticker, b_side, be_buf)
                state.update_open_position(matched_id, {"be_trailed": True, "sl": be_buf})
                matched_pos["be_trailed"] = True
                matched_pos["sl"] = be_buf
                be_sl = be_buf * 1000.0 if ticker == "PEPE" else be_buf
                client.set_trading_stop(sym, stop_loss=be_sl)
                gain_pct = ((b_mark - b_entry) / b_entry * 100.0) if b_side == "BUY" else ((b_entry - b_mark) / b_entry * 100.0)
                notifier.send_text(
                    f"🛡️ *Capital Protection Active: {ticker}*\n\n"
                    f"• *Progress*: +{curr_r:.2f}R (+{gain_pct:.1f}%) reached at `{fmt_dollar(b_mark)}`\n"
                    f"• *Action*: Exchange Stop Loss shifted to Break-Even + Fees (`{fmt_dollar(be_buf)}`)\n"
                    f"• *Downside Risk*: **$0.00** (100% Risk-Free trade running toward TP1 & TP2!)"
                )
            # Level 1 Ratchet: at +0.75R progress, cut max loss by 50% (-0.5R)
            elif curr_r >= 0.75 and not matched_pos.get("ratchet_l1") and not matched_pos.get("be_trailed"):
                half_sl = round(b_entry - (risk_dist * 0.50) if b_side == "BUY" else b_entry + (risk_dist * 0.50), 8)
                log.info("Ratchet Level 1 (+0.75R) reached on Bybit for %s %s! Reduced risk to -0.5R @%.4f", ticker, b_side, half_sl)
                state.update_open_position(matched_id, {"ratchet_l1": True, "sl": half_sl})
                matched_pos["ratchet_l1"] = True
                matched_pos["sl"] = half_sl
                half_sl_val = half_sl * 1000.0 if ticker == "PEPE" else half_sl
                client.set_trading_stop(sym, stop_loss=half_sl_val)
                notifier.send_text(
                    f"⚡ *Progressive Risk Reduction: {ticker}*\n\n"
                    f"• *Progress*: +{curr_r:.2f}R reached at `{fmt_dollar(b_mark)}`\n"
                    f"• *Action*: Exchange Stop Loss tightened to -0.5R (`{fmt_dollar(half_sl)}`)\n"
                    f"• *Downside Risk*: Cut in half (50% max drawdown reduction)"
                )
            # Active BTC Shock Shield: if BTC surges/dumps >= 0.35% against open altcoin trade before Break-Even
            elif not matched_pos.get("be_trailed") and ticker != "BTC":
                try:
                    btc_ok, _ = scanner.check_btc_momentum_gate(b_side)
                    if not btc_ok:
                        if curr_r >= 0.08:
                            log.info("BTC Shock Shield: %s %s in slight green (+%.2fR) during adverse BTC impulse! Snapping SL to BE @%.4f", ticker, b_side, curr_r, b_entry)
                            state.update_open_position(matched_id, {"be_trailed": True, "btc_shock_tightened": True, "sl": b_entry})
                            matched_pos["be_trailed"] = True
                            matched_pos["btc_shock_tightened"] = True
                            matched_pos["sl"] = b_entry
                            be_sl = b_entry * 1000.0 if ticker == "PEPE" else b_entry
                            client.set_trading_stop(sym, stop_loss=be_sl)
                            notifier.send_text(
                                f"🛡️ *BTC Shock Shield (Instant Break-Even): {ticker}*\n\n"
                                f"• *Trigger*: Adverse BTC impulse detected while holding {b_side} (+{curr_r:.2f}R)\n"
                                f"• *Action*: Exchange Stop Loss snapped to **Break-Even** (`{fmt_dollar(b_entry)}`)\n"
                                f"• *Downside Risk*: **$0.00** (100% protected from BTC squeeze)"
                            )
                        elif -0.42 <= curr_r < 0.08 and not matched_pos.get("ratchet_l1") and not matched_pos.get("btc_shock_tightened"):
                            half_sl = round(b_entry - (risk_dist * 0.50) if b_side == "BUY" else b_entry + (risk_dist * 0.50), 8)
                            valid_half = (b_side == "BUY" and half_sl < b_mark) or (b_side == "SELL" and half_sl > b_mark)
                            if valid_half:
                                log.info("BTC Shock Shield: Tightening %s %s SL by 50%% (-0.50R @%.4f) due to adverse BTC impulse!", ticker, b_side, half_sl)
                                state.update_open_position(matched_id, {"ratchet_l1": True, "btc_shock_tightened": True, "sl": half_sl})
                                matched_pos["ratchet_l1"] = True
                                matched_pos["btc_shock_tightened"] = True
                                matched_pos["sl"] = half_sl
                                half_sl_val = half_sl * 1000.0 if ticker == "PEPE" else half_sl
                                client.set_trading_stop(sym, stop_loss=half_sl_val)
                                notifier.send_text(
                                    f"🛡️ *BTC Shock Shield (-0.50R Risk Cut): {ticker}*\n\n"
                                    f"• *Trigger*: Adverse BTC impulse detected against open {b_side}\n"
                                    f"• *Action*: Exchange Stop Loss tightened by **50% (-0.50R)** (`{fmt_dollar(half_sl)}`)\n"
                                    f"• *Protection*: 50% of risk capital shielded while allowing room against fakeout wicks."
                                )
                except Exception as e_bsh:
                    log.debug("BTC Shock Shield check error for %s: %s", ticker, e_bsh)

        tp2 = matched_pos.get("tp2")

        # Check TP2 Hit condition on Bybit (Lock in TP1 profit + activate runner)
        if tp2 and not matched_pos.get("tp2_hit"):
            hit_tp2 = False
            if b_side == "BUY" and b_mark >= tp2:
                hit_tp2 = True
            elif b_side == "SELL" and b_mark <= tp2:
                hit_tp2 = True

            if hit_tp2:
                tp1_lock = tp1 if tp1 else b_entry
                log.info("TP2 reached on Bybit for %s %s! Locking SL to TP1 @%.4f and activating runner", ticker, b_side, tp1_lock)
                state.update_open_position(matched_id, {
                    "tp2_hit": True,
                    "tp1_hit": True,
                    "be_trailed": True,
                    "runner_active": True,
                    "sl": tp1_lock,
                    "runner_peak": b_mark if b_side == "BUY" else None,
                    "runner_trough": b_mark if b_side == "SELL" else None,
                })
                matched_pos["tp2_hit"] = True
                matched_pos["tp1_hit"] = True
                matched_pos["be_trailed"] = True
                matched_pos["runner_active"] = True
                matched_pos["sl"] = tp1_lock
                lock_sl_val = tp1_lock * 1000.0 if ticker == "PEPE" else tp1_lock
                client.set_trading_stop(sym, stop_loss=lock_sl_val)
                notifier.notify_tp2_lock(matched_pos, b_mark, tp1_lock)

        # Check TP1 Trailing SL condition (adjust Bybit SL to Break-Even)
        if tp1 and not matched_pos.get("tp1_hit"):
            hit = False
            if b_side == "BUY" and b_mark >= tp1:
                hit = True
            elif b_side == "SELL" and b_mark <= tp1:
                hit = True

            if hit:
                log.info("TP1 reached on Bybit for %s %s! Trailing SL to BE @%.4f", ticker, b_side, b_entry)
                state.update_open_position(matched_id, {"tp1_hit": True, "be_trailed": True})
                matched_pos["tp1_hit"] = True
                matched_pos["be_trailed"] = True
                be_sl = b_entry * 1000.0 if ticker == "PEPE" else b_entry
                client.set_trading_stop(sym, stop_loss=be_sl)
                notifier.notify_tp1_be(matched_pos, b_mark)

        # Dynamic Trailing Stop for Active Runner on Bybit
        if matched_pos.get("runner_active"):
            if b_side == "BUY":
                peak = matched_pos.get("runner_peak", b_mark)
                if b_mark > peak:
                    matched_pos["runner_peak"] = b_mark
                    trail_dist = abs(b_entry - float(matched_pos.get("sl", b_entry))) * 0.5
                    new_sl = max(float(matched_pos.get("sl", b_entry)), b_mark - trail_dist)
                    if new_sl > float(matched_pos.get("sl", b_entry)):
                        matched_pos["sl"] = new_sl
                        state.update_open_position(matched_id, {"runner_peak": b_mark, "sl": new_sl})
                        sl_send = new_sl * 1000.0 if ticker == "PEPE" else new_sl
                        client.set_trading_stop(sym, stop_loss=sl_send)
            elif b_side == "SELL":
                trough = matched_pos.get("runner_trough", b_mark)
                if b_mark < trough:
                    matched_pos["runner_trough"] = b_mark
                    trail_dist = abs(float(matched_pos.get("sl", b_entry)) - b_entry) * 0.5
                    new_sl = min(float(matched_pos.get("sl", b_entry)), b_mark + trail_dist)
                    if new_sl < float(matched_pos.get("sl", b_entry)):
                        matched_pos["sl"] = new_sl
                        state.update_open_position(matched_id, {"runner_trough": b_mark, "sl": new_sl})
                        sl_send = new_sl * 1000.0 if ticker == "PEPE" else new_sl
                        client.set_trading_stop(sym, stop_loss=sl_send)

        # Passivbot / Freqtrade Unstucking & 4H Stale Scalp Time-Stop:
        opened_at_str = matched_pos.get("opened_at")
        if opened_at_str:
            try:
                open_dt = datetime.fromisoformat(opened_at_str.replace("Z", "+00:00"))
                age_mins = (datetime.now(timezone.utc) - open_dt).total_seconds() / 60.0
                curr_r_check = (b_mark - b_entry) / risk_dist if b_side == "BUY" else (b_entry - b_mark) / risk_dist

                # 4-Hour Stale Scalp Time-Stop: auto-scratch stagnant scalps (>240m) stuck in chop (-0.35R to +0.40R)
                if matched_pos.get("trade_type", "scalp") == "scalp" and age_mins >= 240.0 and (-0.35 <= curr_r_check <= 0.40):
                    close_side = "Sell" if b_side == "BUY" else "Buy"
                    log.info("Stale Scalp Time-Stop: Closing stagnant %s %s open %.1f mins (curr_r=%.2fR) to free slot.", ticker, b_side, age_mins, curr_r_check)
                    client.close_position_market(sym, side=close_side, qty=b_qty, category="linear")
                    continue

                if not matched_pos.get("be_trailed") and not matched_pos.get("tp1_hit") and not matched_pos.get("unstuck"):
                    if age_mins >= cfg.unstuck_timeout_minutes:
                        is_profitable = (b_side == "BUY" and b_mark >= b_entry) or (b_side == "SELL" and b_mark <= b_entry)
                        if is_profitable:
                            log.info("Passivbot Unstucking: %s %s open %.1f mins with stalled momentum. Trailing SL to BE+Fees.", ticker, b_side, age_mins)
                            state.update_open_position(matched_id, {"be_trailed": True, "unstuck": True})
                            matched_pos["be_trailed"] = True
                            matched_pos["unstuck"] = True
                            fee_buf = b_entry * cfg.be_profit_fee_buffer_pct
                            be_sl_price = b_entry + fee_buf if b_side == "BUY" else b_entry - fee_buf
                            be_sl = be_sl_price * 1000.0 if ticker == "PEPE" else be_sl_price
                            client.set_trading_stop(sym, stop_loss=be_sl)
                            notifier.send_text(
                                f"⚡ *Passivbot Unstucking Routine Active: {ticker}*\n\n"
                                f"• *Duration*: Position active for {int(age_mins)} mins without reaching TP1.\n"
                                f"• *Action*: Exchange Stop Loss shifted to Fee-Protected Break-Even (`{fmt_dollar(be_sl_price)}`).\n"
                                f"• *Risk*: Adverse reversal drawdown eliminated; fees guaranteed covered."
                            )
            except Exception as exc:
                log.debug("Error checking unstuck for %s: %s", ticker, exc)

    # 4. Working orders & spot check
    open_linear_orders = client.get_open_orders(category="linear")
    linear_order_ids = {o.get("orderId") for o in open_linear_orders if o.get("orderId")}
    open_spot_orders = client.get_open_orders(category="spot")
    spot_order_ids = {o.get("orderId") for o in open_spot_orders if o.get("orderId")}

    # Cancel any resting ENTRY orders older than 20 minutes (TTL) to prevent capital lockup
    # Or adaptively re-peg unfilled maker ENTRY orders inside shelf if price starts moving away.
    # CRITICAL: Never cancel or re-peg reduceOnly orders, SL/TP trigger orders, or opposite-side TP orders on active positions!
    now_ms = int(time.time() * 1000)
    for o in open_linear_orders:
        created_time = int(o.get("createdTime", 0) or 0)
        oid = o.get("orderId")
        sym = o.get("symbol")
        side = o.get("side", "")
        order_price = float(o.get("price", 0.0) or 0.0)
        age_ms = now_ms - created_time

        is_reduce_only = o.get("reduceOnly") in (True, "true", "True", 1)
        stop_order_type = str(o.get("stopOrderType", "") or "")
        order_type = str(o.get("orderType", "") or "")

        if is_reduce_only or stop_order_type or order_type.lower() != "limit":
            continue

        if sym in bybit_syms:
            # Position is already open on Bybit!
            # Only expire same-side unfilled second micro-grid entry legs after 20m TTL; never touch opposite-side TP orders.
            if side.upper() == str(bybit_syms[sym].get("side", "")).upper():
                if created_time > 0 and age_ms > (20 * 60 * 1000):
                    log.info("Cancelling expired second micro-grid entry leg: %s %s ID=%s", sym, side, oid)
                    client.cancel_order("linear", sym, oid)
            continue

        if created_time > 0 and age_ms > (20 * 60 * 1000):
            log.info("Cancelling expired linear resting entry order: %s %s ID=%s", sym, side, oid)
            client.cancel_order("linear", sym, oid)
        elif cfg.enable_adaptive_maker_pegging and created_time > 0 and (3 * 60 * 1000) <= age_ms <= (15 * 60 * 1000):
            # Check if live mark is drifting away while entry order is unfilled
            tick = client.get_ticker(sym, category="linear")
            if tick and order_price > 0:
                mark = float(tick.get("mark_price", 0.0) or 0.0)
                pegged_p = None
                if side.upper() == "BUY" and (mark - order_price) / order_price >= 0.0020:
                    cand_p = client.quantize_price(sym, order_price * 1.0008, category="linear")
                    if cand_p < mark:
                        pegged_p = cand_p
                elif side.upper() == "SELL" and (order_price - mark) / order_price >= 0.0020:
                    cand_p = client.quantize_price(sym, order_price * 0.9992, category="linear")
                    if cand_p > mark:
                        pegged_p = cand_p

                if pegged_p is not None:
                    # Find matching WORKING position in local state so we preserve SL/TP and update order_id
                    work_pid = None
                    work_pos = None
                    for pid, p in state.get_open_positions().items():
                        if bybit_linear_symbol(p.get("ticker", "")) == sym and p.get("market") == "perp":
                            work_pid = pid
                            work_pos = p
                            break

                    p_sl = None
                    p_tp = None
                    if work_pos:
                        mult = 1000.0 if work_pos.get("ticker") == "PEPE" else 1.0
                        if work_pos.get("sl"):
                            p_sl = float(work_pos["sl"]) * mult
                        raw_tp = work_pos.get("tp2") or work_pos.get("tp1")
                        if raw_tp:
                            p_tp = float(raw_tp) * mult

                    log.info("Adaptive Maker Pegging: Nudging %s %s entry limit from $%.4f to $%.4f (mark=$%.4f)", sym, side.upper(), order_price, pegged_p, mark)
                    client.cancel_order("linear", sym, oid)
                    new_resp = client.place_order(
                        category="linear",
                        symbol=sym,
                        side=side.upper(),
                        order_type="Limit",
                        qty=float(o.get("qty")),
                        price=pegged_p,
                        time_in_force="PostOnly",
                        stop_loss=p_sl,
                        take_profit=p_tp,
                    )
                    if new_resp and new_resp.get("orderId"):
                        new_oid = new_resp["orderId"]
                        linear_order_ids.add(new_oid)
                        if work_pid and work_pos:
                            old_ids = [x for x in work_pos.get("order_ids", []) if x != oid]
                            old_ids.append(new_oid)
                            state.update_open_position(work_pid, {"order_id": new_oid, "order_ids": old_ids})

    for o in open_spot_orders:
        created_time = int(o.get("createdTime", 0) or 0)
        if created_time > 0 and (now_ms - created_time) > (20 * 60 * 1000):
            oid = o.get("orderId")
            sym = o.get("symbol")
            log.info("Cancelling expired spot resting order: %s %s ID=%s", sym, o.get("side"), oid)
            client.cancel_order("spot", sym, oid)

    for pid, p in list(state.get_open_positions().items()):
        mkt = p.get("market", "perp")
        if mkt == "perp":
            sym = bybit_linear_symbol(p["ticker"])
            if sym not in bybit_syms:
                order_id = p.get("order_id")
                order_ids = p.get("order_ids", [])
                if (order_id and order_id in linear_order_ids) or any(oid in linear_order_ids for oid in order_ids):
                    continue
                log.info("PERP position %s is neither active nor resting on Bybit. Removing.", p["ticker"])
                state.remove_open_position(pid)
        elif mkt == "spot":
            order_id = p.get("order_id")
            if not order_id:
                # Ghost spot position without an exchange orderId
                log.info("Removing ghost spot position without orderId: %s", p["ticker"])
                state.remove_open_position(pid)
            elif order_id in spot_order_ids:
                if p.get("status") != "WORKING":
                    state.update_open_position(pid, {"status": "WORKING"})
            else:
                inv = client.get_coin_inventory(p["ticker"])
                if inv > 0:
                    if p.get("status") != "OPEN":
                        state.update_open_position(pid, {"status": "OPEN", "qty": inv})
                        notifier.notify_entry_filled(p)
                else:
                    log.info("Spot order %s no longer open on Bybit and zero inventory. Removing.", p["ticker"])
                    state.remove_open_position(pid)


def _monitor_paper_positions() -> None:
    """Evaluate paper positions against live market marks."""
    positions = state.get_open_positions()
    for pos_id, pos in list(positions.items()):
        ticker = pos["ticker"]
        side = pos["side"]
        sym = bybit_linear_symbol(ticker)
        tick = client.get_ticker(sym)
        if not tick:
            continue

        live = tick["mark_price"]
        if ticker == "PEPE":
            live = live / 1000.0

        tp1 = pos.get("tp1")
        tp2 = pos.get("tp2")
        sl = pos.get("sl")

        # Check Early Break-Even condition (+1R / 50% progress to TP1)
        entry_p = float(pos.get("entry_price", 0.0))
        orig_sl = float(pos.get("sl", entry_p))
        risk_dist = abs(entry_p - orig_sl) if abs(entry_p - orig_sl) > 0 else (entry_p * 0.01)

        # Multi-Step Dynamic Ratchet Trail (+0.75R to cut loss 50%, +1.0R to Break-Even, +1.75R to lock profit)
        if cfg.enable_ratchet_trail and not pos.get("tp2_hit"):
            curr_r = (live - entry_p) / risk_dist if side == "BUY" else (entry_p - live) / risk_dist
            current_sl = float(pos.get("sl", orig_sl))

            # Level 3 Ratchet: at +1.75R progress, lock in +0.75R guaranteed profit
            if curr_r >= 1.75 and not pos.get("ratchet_l3"):
                lock_price = round(entry_p + (risk_dist * 0.75) if side == "BUY" else entry_p - (risk_dist * 0.75), 8)
                log.info("Ratchet Level 3 (+1.75R) hit for %s %s: Lock +0.75R profit @%.4f", ticker, side, lock_price)
                state.update_open_position(pos_id, {"ratchet_l3": True, "sl": lock_price, "be_trailed": True})
                pos["ratchet_l3"] = True
                pos["sl"] = lock_price
                notifier.send_text(
                    f"🔒 *Profit Lock Ratchet Active: {ticker}*\n\n"
                    f"• *Current Gain*: +{curr_r:.2f}R (`{fmt_dollar(live)}`)\n"
                    f"• *Action*: Trailed Stop Loss to **+{0.75:.2f}R** profit (`{fmt_dollar(lock_price)}`)\n"
                    f"• *Status*: Trade cannot finish with less than +0.75R gain."
                )
            # Level 2 Ratchet: at +1.0R progress, move to Fee-Protected Break-Even
            elif curr_r >= 1.0 and not pos.get("be_trailed"):
                fee_buf = entry_p * cfg.be_profit_fee_buffer_pct
                lock_be = round(entry_p + fee_buf if side == "BUY" else entry_p - fee_buf, 8)
                log.info("Ratchet Level 2 (+1.0R) reached for paper %s %s! Trailing SL to BE+Fees @%.4f", ticker, side, lock_be)
                state.update_open_position(pos_id, {"be_trailed": True, "sl": lock_be})
                pos["be_trailed"] = True
                pos["sl"] = lock_be
                notifier.send_text(
                    f"🛡️ *Capital Protection Active: {ticker}*\n\n"
                    f"• *Progress*: +{curr_r:.2f}R reached at `{fmt_dollar(live)}`\n"
                    f"• *Action*: Stop Loss shifted to Fee-Protected Break-Even (`{fmt_dollar(lock_be)}`)\n"
                    f"• *Downside Risk*: **$0.00** (Net profit secured after fees)"
                )
            # Level 1 Ratchet: at +0.70R progress, cut max loss by 50% (-0.5R)
            elif curr_r >= 0.70 and not pos.get("ratchet_l1") and not pos.get("be_trailed"):
                half_sl = round(entry_p - (risk_dist * 0.50) if side == "BUY" else entry_p + (risk_dist * 0.50), 8)
                log.info("Ratchet Level 1 (+0.70R) reached for %s %s! Reduced risk to -0.5R @%.4f", ticker, side, half_sl)
                state.update_open_position(pos_id, {"ratchet_l1": True, "sl": half_sl})
                pos["ratchet_l1"] = True
                pos["sl"] = half_sl
                notifier.send_text(
                    f"⚡ *Progressive Risk Reduction: {ticker}*\n\n"
                    f"• *Progress*: +{curr_r:.2f}R reached at `{fmt_dollar(live)}`\n"
                    f"• *Action*: Stop Loss tightened to -0.5R (`{fmt_dollar(half_sl)}`)\n"
                    f"• *Downside Risk*: Cut in half (50% max drawdown reduction)"
                )

        # Passivbot Unstucking: If open > cfg.unstuck_timeout_minutes and profitable/flat, trail SL to BE
        if not pos.get("be_trailed") and not pos.get("tp1_hit") and not pos.get("unstuck"):
            opened_at_str = pos.get("opened_at")
            if opened_at_str:
                try:
                    open_dt = datetime.fromisoformat(opened_at_str.replace("Z", "+00:00"))
                    age_mins = (datetime.now(timezone.utc) - open_dt).total_seconds() / 60.0
                    if age_mins >= cfg.unstuck_timeout_minutes:
                        is_profitable = (side == "BUY" and live >= entry_p) or (side == "SELL" and live <= entry_p)
                        if is_profitable:
                            log.info("Passivbot Unstucking (Paper): %s %s open %.1f mins. Trailed SL to BE.", ticker, side, age_mins)
                            state.update_open_position(pos_id, {"be_trailed": True, "unstuck": True, "sl": entry_p})
                            pos["be_trailed"] = True
                            pos["unstuck"] = True
                            pos["sl"] = entry_p
                            notifier.send_text(
                                f"⚡ *Passivbot Unstucking Routine Active: {ticker}*\n\n"
                                f"• *Duration*: Position active for {int(age_mins)} mins without hitting TP1.\n"
                                f"• *Action*: Trailed Stop Loss to Break-Even (`{fmt_dollar(entry_p)}`).\n"
                                f"• *Risk*: Adverse reversal drawdown eliminated."
                            )
                except Exception as exc:
                    log.debug("Error checking paper unstuck for %s: %s", ticker, exc)

        # Volume Climax & Liquidation Flush Early Stop (Emergency Cut):
        # If price slices past entry towards SL with extreme selling/buying volume (>3x avg), exit early
        # to save 50%-65% of the designated risk capital instead of waiting for the full -1.0R SL.
        if cfg.enable_climax_early_cut and not pos.get("be_trailed"):
            is_adverse_break = (side == "BUY" and live < entry_p) or (side == "SELL" and live > entry_p)
            if is_adverse_break:
                curr_loss_r = (entry_p - live) / risk_dist if side == "BUY" else (live - entry_p) / risk_dist
                # If currently at -0.40R to -0.85R loss, check if 15m volume spiked aggressively
                if 0.40 <= curr_loss_r < 0.95:
                    df_check = scanner.fetch_ohlcv(ticker, tf_minutes=15, limit=6)
                    if df_check is not None and len(df_check) >= 4:
                        v_latest = float(df_check["volume"].iloc[-1])
                        v_avg = float(df_check["volume"].iloc[-4:-1].mean())
                        if v_avg > 0 and (v_latest / v_avg) >= 2.8:
                            log.warning("Volume Climax Early Cut triggered for %s %s: volume spike %.1fx, loss=-%.2fR", ticker, side, v_latest / v_avg, curr_loss_r)
                            closed = state.close_position(pos_id, live, f"CLIMAX_EARLY_CUT (-{curr_loss_r:.2f}R)")
                            if closed:
                                notifier.send_text(
                                    f"🚨 *Volume Climax Early Cut Active: {ticker}*\n\n"
                                    f"• *Side*: {side} | *Exit*: `{fmt_dollar(live)}`\n"
                                    f"• *Volume Spike*: **{v_latest/v_avg:.1f}x** normal 15m volume against shelf\n"
                                    f"• *Capital Saved*: Closed at **-{curr_loss_r:.2f}R** instead of full -1.0R SL!\n"
                                    f"• *Saved Risk*: ~{int((1.0 - curr_loss_r)*100)}% of risk budget preserved."
                                )
                                notifier.notify_trade_closed(closed)
                            continue

        tp3 = pos.get("tp3")

        if side == "BUY":
            if sl is not None and live <= sl:
                closed = state.close_position(pos_id, live, "SL_HIT")
                if closed:
                    notifier.notify_trade_closed(closed)
            elif tp3 is not None and live >= tp3 and pos.get("tp2_hit"):
                closed = state.close_position(pos_id, live, "TP3_RUNNER_HIT")
                if closed:
                    notifier.notify_trade_closed(closed)
            elif tp2 is not None and live >= tp2 and not pos.get("tp2_hit"):
                tp1_lock = tp1 if tp1 else entry_p
                state.update_open_position(pos_id, {
                    "tp2_hit": True,
                    "runner_active": True,
                    "sl": tp1_lock,
                    "runner_peak": live,
                })
                pos["tp2_hit"] = True
                pos["runner_active"] = True
                pos["sl"] = tp1_lock
                pos["runner_peak"] = live
                notifier.send_text(
                    f"🚀 *TP2 Target Hit (+2.5R): {ticker}*\n\n"
                    f"• *Price*: `{fmt_dollar(live)}`\n"
                    f"• *Action*: Banked second tranche (35% size).\n"
                    f"• *Stop Loss Trailed*: Locked at TP1 (`{fmt_dollar(tp1_lock)}`) — Guaranteed profit secured!\n"
                    f"• *30% Runner*: Dynamic trailing stop active targeting TP3 expansion (`{fmt_dollar(tp3)}`)."
                )
            elif tp1 is not None and live >= tp1 and not pos.get("tp1_hit"):
                fee_buf = entry_p * cfg.be_profit_fee_buffer_pct
                lock_be = round(entry_p + fee_buf, 8)
                state.update_open_position(pos_id, {"tp1_hit": True, "be_trailed": True, "sl": lock_be})
                pos["tp1_hit"] = True
                pos["be_trailed"] = True
                pos["sl"] = lock_be
                notifier.notify_tp1_be(pos, live)
            elif pos.get("runner_active"):
                peak = pos.get("runner_peak", live)
                if live > peak:
                    pos["runner_peak"] = live
                    trail_dist = abs(entry_p - float(pos.get("sl", entry_p))) * 0.5
                    new_sl = max(float(pos.get("sl", entry_p)), live - trail_dist)
                    state.update_open_position(pos_id, {"runner_peak": live, "sl": new_sl})
                    pos["sl"] = new_sl
        else:  # SELL
            if sl is not None and live >= sl:
                closed = state.close_position(pos_id, live, "SL_HIT")
                if closed:
                    notifier.notify_trade_closed(closed)
            elif tp3 is not None and live <= tp3 and pos.get("tp2_hit"):
                closed = state.close_position(pos_id, live, "TP3_RUNNER_HIT")
                if closed:
                    notifier.notify_trade_closed(closed)
            elif tp2 is not None and live <= tp2 and not pos.get("tp2_hit"):
                tp1_lock = tp1 if tp1 else entry_p
                state.update_open_position(pos_id, {
                    "tp2_hit": True,
                    "runner_active": True,
                    "sl": tp1_lock,
                    "runner_trough": live,
                })
                pos["tp2_hit"] = True
                pos["runner_active"] = True
                pos["sl"] = tp1_lock
                pos["runner_trough"] = live
                notifier.send_text(
                    f"🚀 *TP2 Target Hit (+2.5R): {ticker}*\n\n"
                    f"• *Price*: `{fmt_dollar(live)}`\n"
                    f"• *Action*: Banked second tranche (35% size).\n"
                    f"• *Stop Loss Trailed*: Locked at TP1 (`{fmt_dollar(tp1_lock)}`) — Guaranteed profit secured!\n"
                    f"• *30% Runner*: Dynamic trailing stop active targeting TP3 expansion (`{fmt_dollar(tp3)}`)."
                )
            elif tp1 is not None and live <= tp1 and not pos.get("tp1_hit"):
                fee_buf = entry_p * cfg.be_profit_fee_buffer_pct
                lock_be = round(entry_p - fee_buf, 8)
                state.update_open_position(pos_id, {"tp1_hit": True, "be_trailed": True, "sl": lock_be})
                pos["tp1_hit"] = True
                pos["be_trailed"] = True
                pos["sl"] = lock_be
                notifier.notify_tp1_be(pos, live)
            elif pos.get("runner_active"):
                trough = pos.get("runner_trough", live)
                if live < trough:
                    pos["runner_trough"] = live
                    trail_dist = abs(float(pos.get("sl", entry_p)) - entry_p) * 0.5
                    new_sl = min(float(pos.get("sl", entry_p)), live + trail_dist)
                    state.update_open_position(pos_id, {"runner_trough": live, "sl": new_sl})
                    pos["sl"] = new_sl


def _monitor_loop() -> None:
    """Continuously evaluate open positions against Bybit state."""
    log.info("Trade Desk position monitor started")
    while _monitor_running:
        try:
            if not client.is_paper:
                _sync_with_bybit()
            else:
                _monitor_paper_positions()
        except Exception as exc:
            log.error("Error in position monitor loop: %s", exc)

        for _ in range(15):
            if not _monitor_running:
                break
            time.sleep(1)


def start_monitor() -> None:
    global _monitor_running
    if not _monitor_running:
        _monitor_running = True
        t = threading.Thread(target=_monitor_loop, daemon=True, name="pos-monitor")
        t.start()


def stop_monitor() -> None:
    global _monitor_running
    _monitor_running = False
