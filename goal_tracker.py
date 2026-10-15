"""
goal_tracker.py — Adaptive Compounding & Target-Seeking Portfolio Engine for Winz.

Enables users to set a Starting Capital, Target Capital, and Timeframe (e.g., $10 -> $100 in 30 days).
Features:
  1. Adaptive daily run-rate compounding calculation
  2. Strict 5% daily equity loss limit (anti-revenge trading)
  3. Market condition awareness (freezes longs during BTC bear flushes)
  4. Real-time progress tracking & benchmark pacing (Ahead/Behind schedule)
  5. Goal feasibility validator (flags unrealistic growth rates and recalculates required timeframe)
  6. Dynamic position sizing accounting for leverage, Bybit fees (0.055% taker), slippage, and stop-loss distance
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union

log = logging.getLogger(__name__)

# Bybit fee & friction constants
BYBIT_TAKER_FEE_PCT = 0.055     # 0.055% per execution
ROUNDTRIP_FEE_PCT = 0.110        # Entry + Exit
ESTIMATED_SLIPPAGE_PCT = 0.05    # Typical limit/market slip buffer
TOTAL_FRICTION_PCT = ROUNDTRIP_FEE_PCT + ESTIMATED_SLIPPAGE_PCT  # ~0.16% on notional

MAX_SAFE_DAILY_RATE_PCT = 20.0   # If required rate exceeds 20%/day, flag as infeasible to protect capital


def calculate_compound_daily_rate(start: float, target: float, days: int) -> float:
    """Calculate the required compound daily growth rate: target = start * (1 + r)^days."""
    if start <= 0 or target <= start or days <= 0:
        return 0.0
    ratio = target / start
    r = (ratio ** (1.0 / days)) - 1.0
    return round(r * 100.0, 2)


def get_expected_equity_at_day(start: float, target: float, total_days: int, day_index: int) -> float:
    """Calculate the theoretical benchmark equity curve for day_index (0 to total_days)."""
    if start <= 0 or total_days <= 0:
        return start
    r = (target / start) ** (1.0 / total_days) - 1.0
    return round(start * ((1.0 + r) ** day_index), 2)


def assess_goal_feasibility(
    current_equity: float,
    target_capital: float,
    remaining_days: int,
    max_daily_loss_pct: float = 5.0,
) -> Dict[str, Any]:
    """
    Evaluates whether the goal is mathematically and practically realistic.
    If required daily rate > MAX_SAFE_DAILY_RATE_PCT, suggests a revised safe timeframe.
    """
    if current_equity <= 0 or remaining_days <= 0 or target_capital <= current_equity:
        return {
            "is_feasible": False,
            "required_daily_pct": 0.0,
            "reason": "Invalid capital or timeframe parameters.",
            "recommended_days": remaining_days,
        }

    req_daily = calculate_compound_daily_rate(current_equity, target_capital, remaining_days)

    if req_daily > MAX_SAFE_DAILY_RATE_PCT:
        # Calculate realistic timeframe using safe growth rate (e.g. 5% to 8% daily)
        safe_rate = min(8.0, max_daily_loss_pct * 1.2)
        safe_rate_dec = safe_rate / 100.0
        try:
            needed_days = math.ceil(math.log(target_capital / current_equity) / math.log(1.0 + safe_rate_dec))
        except Exception:
            needed_days = remaining_days * 2

        return {
            "is_feasible": False,
            "required_daily_pct": req_daily,
            "reason": (
                f"Target requires {req_daily:.1f}% daily growth, which exceeds the safe threshold "
                f"({MAX_SAFE_DAILY_RATE_PCT:.0f}%/day). Risk of ruin is too high under a {max_daily_loss_pct:.1f}% daily cap."
            ),
            "recommended_days": needed_days,
            "recommended_daily_rate": safe_rate,
        }

    return {
        "is_feasible": True,
        "required_daily_pct": req_daily,
        "reason": "Goal is within healthy risk parameters.",
        "recommended_days": remaining_days,
        "recommended_daily_rate": req_daily,
    }


def calculate_trade_risk_budget(
    current_equity: float,
    morning_equity: float,
    max_daily_loss_pct: float,
    today_realized_loss: float = 0.0,
) -> Dict[str, float]:
    """
    Calculate remaining daily dollar risk before hitting the 5% daily loss limit.
    """
    base_eq = morning_equity if morning_equity > 0 else current_equity
    max_daily_dollar_loss = base_eq * (max_daily_loss_pct / 100.0)
    remaining_risk_budget = max(0.0, max_daily_dollar_loss - abs(today_realized_loss))

    return {
        "max_daily_dollar_loss": round(max_daily_dollar_loss, 2),
        "today_loss_so_far": round(abs(today_realized_loss), 2),
        "remaining_risk_budget": round(remaining_risk_budget, 2),
        "is_budget_exhausted": remaining_risk_budget <= 0.05,
    }


def size_goal_trade(
    current_equity: float,
    morning_equity: float,
    sl_distance_pct: float,
    leverage: int = 5,
    max_daily_loss_pct: float = 5.0,
    today_realized_loss: float = 0.0,
) -> Dict[str, Any]:
    """
    Sizes trade margin so that if Stop Loss is hit, loss + fees <= remaining daily risk budget.
    """
    budget_info = calculate_trade_risk_budget(current_equity, morning_equity, max_daily_loss_pct, today_realized_loss)
    rem_budget = budget_info["remaining_risk_budget"]

    if budget_info["is_budget_exhausted"]:
        return {
            "allowed": False,
            "reason": f"Daily loss cap ({max_daily_loss_pct}%) reached. Trading paused for today to preserve capital.",
            "margin_usdt": 0.0,
            "notional_usdt": 0.0,
        }

    # Safe trade risk: allocate up to 50% - 70% of remaining daily budget to a single setup
    allocated_risk = min(rem_budget, rem_budget * 0.70)
    sl_pct_clean = max(0.4, float(sl_distance_pct))
    
    # Loss on notional = notional * (sl_pct + friction_pct)
    effective_loss_pct = (sl_pct_clean + TOTAL_FRICTION_PCT) / 100.0
    
    # required notional = allocated_risk / effective_loss_pct
    max_notional_by_risk = allocated_risk / effective_loss_pct if effective_loss_pct > 0 else 0.0
    
    # Cap notional by wallet margin availability: max margin = current_equity * 0.85
    max_margin_by_wallet = current_equity * 0.85
    max_notional_by_wallet = max_margin_by_wallet * leverage

    final_notional = min(max_notional_by_risk, max_notional_by_wallet)
    final_margin = final_notional / leverage if leverage > 0 else 0.0

    # Minimum exchange notional guard ($5.00 on Bybit)
    if final_notional < 5.0:
        if current_equity >= 5.0:
            final_margin = 5.0 / leverage
            final_notional = 5.0
        else:
            return {
                "allowed": False,
                "reason": "Available capital is below exchange minimum order size ($5.00).",
                "margin_usdt": 0.0,
                "notional_usdt": 0.0,
            }

    est_loss_at_sl = round(final_notional * effective_loss_pct, 2)

    return {
        "allowed": True,
        "margin_usdt": round(final_margin, 2),
        "notional_usdt": round(final_notional, 2),
        "leverage": leverage,
        "est_loss_at_sl": est_loss_at_sl,
        "remaining_daily_budget": rem_budget,
    }


def evaluate_goal_status(goal: dict, current_equity: float) -> dict:
    """
    Evaluates where the user stands against their flight plan:
      - Ahead / Behind schedule
      - Pacing in days
      - Required daily rate from current balance
    """
    start_cap = float(goal.get("starting_capital", 10.0))
    target_cap = float(goal.get("target_capital", 100.0))
    total_days = int(goal.get("timeframe_days", 30))
    max_loss_pct = float(goal.get("max_daily_loss_pct", 5.0))
    start_date_str = goal.get("start_date", datetime.now(timezone.utc).date().isoformat())

    try:
        start_date = datetime.fromisoformat(start_date_str).date()
    except Exception:
        start_date = datetime.now(timezone.utc).date()

    now_date = datetime.now(timezone.utc).date()
    elapsed_days = max(0, (now_date - start_date).days)
    remaining_days = max(1, total_days - elapsed_days)

    expected_now = get_expected_equity_at_day(start_cap, target_cap, total_days, elapsed_days)
    expected_today_target = get_expected_equity_at_day(start_cap, target_cap, total_days, elapsed_days + 1)

    initial_daily_rate = calculate_compound_daily_rate(start_cap, target_cap, total_days)
    req_daily_rate = calculate_compound_daily_rate(current_equity, target_cap, remaining_days)

    net_profit = current_equity - start_cap
    total_return_pct = (net_profit / start_cap) * 100.0 if start_cap > 0 else 0.0

    delta_from_expected = current_equity - expected_now
    is_ahead = delta_from_expected >= 0.0

    # Calculate pacing in days
    pace_days = 0
    if initial_daily_rate > 0 and current_equity > 0:
        try:
            virtual_day = math.log(current_equity / start_cap) / math.log(1.0 + (initial_daily_rate / 100.0))
            pace_days = round(virtual_day - elapsed_days, 1)
        except Exception:
            pace_days = 0.0

    feasibility = assess_goal_feasibility(current_equity, target_cap, remaining_days, max_loss_pct)

    return {
        "start_capital": start_cap,
        "target_capital": target_cap,
        "total_days": total_days,
        "elapsed_days": elapsed_days,
        "remaining_days": remaining_days,
        "current_equity": round(current_equity, 2),
        "net_profit": round(net_profit, 2),
        "total_return_pct": round(total_return_pct, 1),
        "expected_now": expected_now,
        "expected_today_target": expected_today_target,
        "initial_daily_rate_pct": initial_daily_rate,
        "required_daily_rate_pct": req_daily_rate,
        "is_ahead": is_ahead,
        "delta_from_expected": round(delta_from_expected, 2),
        "pace_days": pace_days,
        "feasibility": feasibility,
    }


def format_goal_progress_card(goal: dict, current_equity: float, morning_equity: Optional[float] = None) -> str:
    """Renders a rich, visually intuitive Telegram dashboard for the user's flight plan."""
    status = evaluate_goal_status(goal, current_equity)
    m_eq = morning_equity if (morning_equity and morning_equity > 0) else current_equity
    
    today_pnl = current_equity - m_eq
    today_pnl_pct = (today_pnl / m_eq) * 100.0 if m_eq > 0 else 0.0
    pnl_sign = "+" if today_pnl >= 0 else ""

    max_loss_pct = float(goal.get("max_daily_loss_pct", 5.0))
    budget_info = calculate_trade_risk_budget(current_equity, m_eq, max_loss_pct, min(0.0, today_pnl))

    # Pace status label
    if status["is_ahead"]:
        pace_label = f"🟢 Ahead of Schedule (+{status['pace_days']} days pace)"
    elif abs(status["delta_from_expected"]) < 0.20:
        pace_label = "🟡 Exactly On Track"
    else:
        pace_label = f"🔴 Behind Schedule ({status['pace_days']} days behind)"

    # Feasibility note
    feas = status["feasibility"]
    if not feas["is_feasible"]:
        feas_banner = (
            f"\n\n⚠️ *Goal Feasibility Alert:*\n"
            f"_{feas['reason']}_\n"
            f"👉 *Recommended Adjustment:* Extend target to `{feas['recommended_days']} days` to keep risk safe."
        )
    else:
        feas_banner = ""

    # Visual progress bar (10 blocks)
    pct_progress = min(100.0, max(0.0, ((current_equity - status["start_capital"]) / (status["target_capital"] - status["start_capital"])) * 100.0))
    filled_blocks = int(round(pct_progress / 10.0))
    bar = "█" * filled_blocks + "░" * (10 - filled_blocks)

    card = (
        f"🎯 *TARGET GROWTH FLIGHT PLAN* 🎯\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏁 *Target Goal*: `${status['start_capital']:,.2f} ➔ ${status['target_capital']:,.2f}` in `{status['total_days']} Days`\n"
        f"⏳ *Timeframe*: Day `{status['elapsed_days'] + 1} of {status['total_days']}` ({status['remaining_days']} days left)\n"
        f"📊 *Progress*: `[{bar}]` *{pct_progress:.1f}%*\n\n"
        f"💰 *Current Equity*: `${status['current_equity']:,.2f}` ({status['total_return_pct']:+.1f}%)\n"
        f"📅 *Today's PnL*: `{pnl_sign}${today_pnl:.2f}` (`{today_pnl_pct:+.1f}%`)\n"
        f"📍 *Today's Benchmark*: `${status['expected_now']:,.2f}` ➔ Target: `${status['expected_today_target']:,.2f}`\n"
        f"⏱ *Pacing Status*: {pace_label}\n\n"
        f"⚡ *Run-Rate Requirements*:\n"
        f"• Original Daily Target: `+{status['initial_daily_rate_pct']:.1f}% / day`\n"
        f"• Adaptive Required Rate: `+{status['required_daily_rate_pct']:.1f}% / day`\n\n"
        f"🛡️ *Risk Controls (Active)*:\n"
        f"• Leverage Cap: `{goal.get('leverage', 5)}x Isolated`\n"
        f"• Max Daily Loss ({max_loss_pct}%): `-${budget_info['max_daily_dollar_loss']:.2f}`\n"
        f"• Remaining Risk Buffer Today: `${budget_info['remaining_risk_budget']:.2f}`\n"
        f"━━━━━━━━━━━━━━━━━━━━"
        f"{feas_banner}\n"
        f"💡 _Type `/goalplan` to view today's recommended high-conviction trade._"
    )
    return card


def recommend_goal_trade(
    goal: dict,
    current_equity: float,
    morning_equity: float,
    candidate_signals: List[dict],
    btc_regime: str = "NEUTRAL",
) -> Dict[str, Any]:
    """
    Selects the optimal trade setup to fulfill today's required return
    while strictly respecting the 5% daily loss cap and BTC market regime.
    """
    # 1. Market condition guardrail
    if btc_regime == "STRONG_BEAR":
        # Altcoin longs strictly forbidden in bear flush
        candidate_signals = [s for s in candidate_signals if s.get("side") == "SELL" or s.get("ticker") == "BTC"]

    # 2. Risk budget check
    max_loss_pct = float(goal.get("max_daily_loss_pct", 5.0))
    today_pnl = current_equity - morning_equity
    today_loss = min(0.0, today_pnl)
    budget = calculate_trade_risk_budget(current_equity, morning_equity, max_loss_pct, today_loss)

    if budget["is_budget_exhausted"]:
        return {
            "has_trade": False,
            "status": "DAILY_LIMIT_HIT",
            "message": f"Daily loss cap ({max_loss_pct}%) reached today. New trades frozen to preserve equity.",
            "trade": None,
        }

    status = evaluate_goal_status(goal, current_equity)
    req_daily_pct = status["required_daily_rate_pct"]
    lev = int(goal.get("leverage", 5))

    actionable = [s for s in candidate_signals if s.get("side") in ("BUY", "SELL")]
    if not actionable:
        return {
            "has_trade": False,
            "status": "WAITING_FOR_SETUP",
            "message": "Market conditions unfavorable or no clean A+ edge at key shelves. Standing aside to protect capital.",
            "trade": None,
        }

    # Rank candidate signals by speed to target and conviction
    def _trade_score(s):
        conv = float(s.get("conviction", 80.0))
        live = float(s.get("live_price", 0.0))
        tp1 = float(s.get("tp1", 0.0))
        dist = abs(tp1 - live) / live * 100.0 if live > 0 else 2.0
        return conv / max(dist, 0.4)

    ranked = sorted(actionable, key=_trade_score, reverse=True)
    best_sig = ranked[0]

    # Calculate trade sizing
    live_p = float(best_sig.get("live_price", 0.0))
    sl_p = float(best_sig.get("sl", 0.0))
    tp1_p = float(best_sig.get("tp1", 0.0))
    sl_dist = abs(live_p - sl_p) / live_p * 100.0 if live_p > 0 else 1.2
    tp_dist = abs(tp1_p - live_p) / live_p * 100.0 if live_p > 0 else 2.0

    sizing = size_goal_trade(
        current_equity=current_equity,
        morning_equity=morning_equity,
        sl_distance_pct=sl_dist,
        leverage=lev,
        max_daily_loss_pct=max_loss_pct,
        today_realized_loss=today_loss,
    )

    if not sizing["allowed"]:
        return {
            "has_trade": False,
            "status": "RISK_EXCEEDED",
            "message": sizing.get("reason", "Trade risk exceeds safe parameters."),
            "trade": None,
        }

    est_gain_usdt = round(sizing["notional_usdt"] * (tp_dist / 100.0), 2)
    est_return_on_equity = round((est_gain_usdt / current_equity) * 100.0, 1)

    rec_trade = {
        "ticker": best_sig.get("ticker"),
        "side": best_sig.get("side"),
        "tf": best_sig.get("tf", "15m"),
        "conviction": best_sig.get("conviction", 88),
        "entry_price": live_p,
        "entry_range": f"{best_sig.get('entry_low')} – {best_sig.get('entry_high')}",
        "tp1": tp1_p,
        "tp_dist_pct": round(tp_dist, 2),
        "sl": sl_p,
        "sl_dist_pct": round(sl_dist, 2),
        "rr": best_sig.get("rr", 2.0),
        "leverage": lev,
        "recommended_margin": sizing["margin_usdt"],
        "recommended_notional": sizing["notional_usdt"],
        "est_gain_usdt": est_gain_usdt,
        "est_loss_at_sl": sizing["est_loss_at_sl"],
        "est_return_on_equity_pct": est_return_on_equity,
        "meets_daily_target": est_return_on_equity >= req_daily_pct,
        "structure": best_sig.get("structure"),
        "reason": best_sig.get("reason"),
    }

    return {
        "has_trade": True,
        "status": "RECOMMENDED",
        "trade": rec_trade,
    }
