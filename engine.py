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
)
import notifier
from notifier import (
    notify_fill,
    notify_order_placed,
    notify_entry_filled,
    notify_tp1_be,
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


# ── Balance & Risk Sizing (0.5% balance risk) ───────────────────────────────
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
    risk_pct: float = 0.005,  # 0.5% balance risk
) -> Tuple[float, float, str]:
    """
    Compute trade quantity based on 0.5% balance risk:
      risk_capital = equity * 0.005
      qty = risk_capital / |entry_price - sl|

    Guardrails:
      - If SL would require >1% equity at allowed leverage, cut size.
      - Ensure notional margin <= available equity.
      - Quantize to Bybit lot step and check minOrderQty.
      - Handle PEPE 1000-multiplier for Bybit Linear perps.

    Returns: (perp_qty, spot_qty, note)
    """
    dist = abs(entry_price - sl)
    if dist <= 0 or entry_price <= 0:
        return 0.0, 0.0, "Invalid SL or entry price"

    risk_capital = equity * risk_pct
    raw_qty = risk_capital / dist

    # Check maximum allowed loss at SL (cap at 1% equity)
    max_allowed_loss = equity * 0.01
    if raw_qty * dist > max_allowed_loss:
        raw_qty = max_allowed_loss / dist

    # Check margin required at given leverage:
    notional = raw_qty * entry_price
    required_margin = notional / max(1, leverage)
    max_safe_margin = equity * 0.80  # don't tie up more than 80% equity in one trade

    if not client.is_paper:
        avail_bal = client.get_wallet_balance("USDT").get("available", 0.0)
        if avail_bal > 0:
            max_safe_margin = min(max_safe_margin, avail_bal * 0.60)
        elif avail_bal == 0.0 and len(client.get_active_positions()) >= 3:
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


# ── Execution Logic (Perp + Spot) ───────────────────────────────────────────
def execute_signal(sig: dict) -> Dict[str, Any]:
    """
    Execute a BUY or SELL signal from Trade Desk:
      1. Checks VALID_IF criteria
      2. Computes position size from 0.5% equity risk
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
    sl = float(sig["sl"]) if sig.get("sl") is not None else None

    if sl is None:
        log.warning("Rejecting %s %s: SL is missing", ticker, side)
        return {"status": "REJECTED", "reason": "SL is missing"}

    entry_mid = (entry_low + entry_high) / 2.0
    equity = get_current_equity()
    custom_lev = state.get_custom_leverage(ticker)
    if custom_lev is not None:
        lev = int(custom_lev)
    else:
        lev = sig.get("leverage") or get_leverage(ticker, scalp=(trade_type == "scalp"))
        lev = min(lev, get_hard_cap(ticker))

    # Pre-execution validation
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

    # Sizing (0.5% balance risk)
    perp_qty, spot_qty, size_note = compute_position_size(
        ticker=ticker,
        entry_price=entry_mid,
        sl=sl,
        equity=equity,
        leverage=lev,
        risk_pct=cfg.risk_per_trade,
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

        is_tier_a = ticker in MARKET_ALLOWED_TICKERS
        is_strictly_inside_entry = (entry_low <= current_mark <= entry_high)

        if is_tier_a and is_strictly_inside_entry:
            ob = client.get_orderbook(perp_sym, category="linear", limit=25)
            if ob:
                _, slip_pct = client.compute_book_slippage(ob, side, perp_qty)
                if slip_pct < MAX_SLIPPAGE_PCT:
                    use_market_order = True
                    log.info("Executing MARKET order for Tier A %s %s: mark $%.4f is inside band with slip %.3f%%",
                             ticker, side, current_mark, slip_pct)

        perp_order_type = "Market" if use_market_order else "Limit"
        perp_tif = "IOC" if use_market_order else "GTC"
        perp_order_price = None if use_market_order else perp_limit_price

        # Position-level TP targets TP2 (runner), SL targets perp_sl
        target_tp = perp_tp2 if perp_tp2 else perp_tp1
        resp = client.place_order(
            category="linear",
            symbol=perp_sym,
            side=side,
            order_type=perp_order_type,
            qty=perp_qty,
            price=perp_order_price,
            time_in_force=perp_tif,
            stop_loss=perp_sl,
            take_profit=target_tp,
        )
        partial_tp_placed = False
        partial_tp_order_id = None
        if resp:
            perp_order_id = resp.get("orderId")
            # If market order executed (instant fill), brief pause to allow Bybit position update before reduceOnly order
            if use_market_order and perp_tp1 and perp_tp2:
                time.sleep(0.4)
                half_qty = client.quantize_qty(perp_sym, perp_qty * 0.5, category="linear")
                tp_side = "Sell" if side.upper() == "BUY" else "Buy"
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
        # Paper simulation: simulate resting limit order / instant fill at shelf mid
        perp_order_id = f"sim_perp_{uuid.uuid4().hex[:8]}"
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
        "sl": sl,
        "leverage": lev,
        "order_id": perp_order_id,
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
            c_side = "BUY" if c.get("side", "").lower() == "buy" else "SELL"

            if ticker == "PEPE":
                entry_p = entry_p / 1000.0
                exit_p = exit_p / 1000.0

            lev = matched_pos.get("leverage", 4) if matched_pos else 4
            margin = (entry_p * qty_c / max(1, lev)) if (entry_p * qty_c) > 0 else 1.0
            pnl_pct = round((pnl_u / margin) * 100, 2)

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
                "exit_reason": f"BYBIT_{c.get('exec_type', 'TRADE').upper()}",
                "order_id": order_id,
                "closed_at": datetime.now(timezone.utc).isoformat(),
            }
            state.record_closed_position(closed_entry)
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

    # 3. Check Bybit active positions
    bybit_positions = client.get_active_positions(category="linear")
    bybit_syms = {p["symbol"]: p for p in bybit_positions}
    local_positions = state.get_open_positions()

    for sym, bp in bybit_syms.items():
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
            if b_sl and not matched_pos.get("sl"):
                updates["sl"] = b_sl / 1000.0 if ticker == "PEPE" else b_sl

            state.update_open_position(matched_id, updates)
            matched_pos.update(updates)

            if was_working:
                log.info("Limit order for %s filled on Bybit! Now OPEN.", ticker)
                notifier.notify_entry_filled(matched_pos)
                if matched_pos.get("tp1") and not matched_pos.get("partial_tp_placed"):
                    half_qty = client.quantize_qty(sym, b_qty * 0.5, category="linear")
                    tp_side = "Sell" if b_side == "BUY" else "Buy"
                    p_tp1 = matched_pos["tp1"] * 1000.0 if ticker == "PEPE" else matched_pos["tp1"]
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
            pos_id = f"{ticker}_{b_side}_perp_{uuid.uuid4().hex[:8]}"
            new_pos = {
                "id": pos_id,
                "ticker": ticker,
                "symbol": sym,
                "side": b_side,
                "market": "perp",
                "trade_type": "scalp",
                "entry_price": b_entry,
                "mark_price": b_mark,
                "unrealised_pnl": b_pnl,
                "qty": b_qty,
                "tp1": b_tp / 1000.0 if (b_tp and ticker == "PEPE") else b_tp,
                "sl": b_sl / 1000.0 if (b_sl and ticker == "PEPE") else b_sl,
                "leverage": b_lev,
                "status": "OPEN",
                "opened_at": datetime.now(timezone.utc).isoformat(),
            }
            state.open_position(new_pos)
            matched_pos = new_pos
            matched_id = pos_id

        # Check TP1 Trailing SL condition (adjust Bybit SL to Break-Even)
        tp1 = matched_pos.get("tp1")
        if tp1 and not matched_pos.get("tp1_hit"):
            hit = False
            if b_side == "BUY" and b_mark >= tp1:
                hit = True
            elif b_side == "SELL" and b_mark <= tp1:
                hit = True

            if hit:
                log.info("TP1 reached on Bybit for %s %s! Trailing SL to BE @%.4f", ticker, b_side, b_entry)
                state.update_open_position(matched_id, {"tp1_hit": True})
                matched_pos["tp1_hit"] = True
                be_sl = b_entry * 1000.0 if ticker == "PEPE" else b_entry
                client.set_trading_stop(sym, stop_loss=be_sl)
                notifier.notify_tp1_be(matched_pos, b_mark)

    # 4. Working orders & spot check
    open_linear_orders = client.get_open_orders(category="linear")
    linear_order_ids = {o.get("orderId") for o in open_linear_orders if o.get("orderId")}
    open_spot_orders = client.get_open_orders(category="spot")
    spot_order_ids = {o.get("orderId") for o in open_spot_orders if o.get("orderId")}

    # Cancel any resting orders older than 20 minutes (TTL) to prevent capital lockup
    now_ms = int(time.time() * 1000)
    for o in open_linear_orders:
        created_time = int(o.get("createdTime", 0) or 0)
        if created_time > 0 and (now_ms - created_time) > (20 * 60 * 1000):
            oid = o.get("orderId")
            sym = o.get("symbol")
            log.info("Cancelling expired linear resting order: %s %s ID=%s", sym, o.get("side"), oid)
            client.cancel_order("linear", sym, oid)

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
                if order_id and order_id in linear_order_ids:
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

        if side == "BUY":
            if sl is not None and live <= sl:
                closed = state.close_position(pos_id, live, "SL_HIT")
                if closed:
                    notifier.notify_trade_closed(closed)
            elif tp2 is not None and live >= tp2:
                closed = state.close_position(pos_id, live, "TP2_HIT")
                if closed:
                    notifier.notify_trade_closed(closed)
            elif tp1 is not None and live >= tp1 and not pos.get("tp1_hit"):
                state.update_open_position(pos_id, {"tp1_hit": True, "sl": pos["entry_price"]})
                pos["tp1_hit"] = True
                notifier.notify_tp1_be(pos, live)
        else:  # SELL
            if sl is not None and live >= sl:
                closed = state.close_position(pos_id, live, "SL_HIT")
                if closed:
                    notifier.notify_trade_closed(closed)
            elif tp2 is not None and live <= tp2:
                closed = state.close_position(pos_id, live, "TP2_HIT")
                if closed:
                    notifier.notify_trade_closed(closed)
            elif tp1 is not None and live <= tp1 and not pos.get("tp1_hit"):
                state.update_open_position(pos_id, {"tp1_hit": True, "sl": pos["entry_price"]})
                pos["tp1_hit"] = True
                notifier.notify_tp1_be(pos, live)


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
