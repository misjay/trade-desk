"""
learning_engine.py — Continuous Machine Learning, Mistake Identification & Daily Feedback.

Provides:
  - Daily performance audit ranking assets by most profit and most loss.
  - Automatic mistake learning: flags toxic/whipsaw assets with repeated losses,
    low win rates, and high slippage, placing them on probation/avoid lists.
  - Flexible delivery: sends feedback into a dedicated secondary bot/channel or the primary bot.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from config import cfg
import state

log = logging.getLogger(__name__)


def analyze_asset_performance(trades: Optional[List[dict]] = None) -> Dict[str, Any]:
    """
    Audit closed trades and compute per-asset performance metrics:
    total trades, wins, losses, win rate, net PnL, profit factor, etc.
    """
    if trades is None:
        import engine
        # Synchronize latest closed PnL from Bybit
        engine._sync_with_bybit()
        # Query up to 100 recent closed trades from Bybit V5
        trades = engine.client.get_closed_pnl(limit=100)

    by_asset: Dict[str, Dict[str, Any]] = {}

    for t in trades:
        sym = t.get("symbol", "")
        ticker = sym.replace("USDT", "")
        if ticker.startswith("1000"):
            ticker = ticker[4:]
        if not ticker:
            ticker = t.get("ticker", "UNKNOWN")

        pnl = float(t.get("closed_pnl") if "closed_pnl" in t else t.get("pnl_usdt", 0.0) or 0.0)

        if ticker not in by_asset:
            by_asset[ticker] = {
                "ticker": ticker,
                "symbol": sym,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "breakevens": 0,
                "net_pnl": 0.0,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
                "win_pnls": [],
                "loss_pnls": [],
            }

        data = by_asset[ticker]
        data["trades"] += 1
        data["net_pnl"] += pnl

        if pnl > 0.0001:
            data["wins"] += 1
            data["gross_profit"] += pnl
            data["win_pnls"].append(pnl)
        elif pnl < -0.0001:
            data["losses"] += 1
            data["gross_loss"] += abs(pnl)
            data["loss_pnls"].append(pnl)
        else:
            data["breakevens"] += 1

    # Compute derived ratios for each asset
    for ticker, d in by_asset.items():
        total = d["trades"]
        d["win_rate"] = round((d["wins"] / total * 100.0) if total > 0 else 0.0, 1)
        d["net_pnl"] = round(d["net_pnl"], 2)
        d["profit_factor"] = round((d["gross_profit"] / d["gross_loss"]) if d["gross_loss"] > 0 else (99.0 if d["gross_profit"] > 0 else 0.0), 2)
        d["avg_win"] = round(sum(d["win_pnls"]) / len(d["win_pnls"]), 2) if d["win_pnls"] else 0.0
        d["avg_loss"] = round(sum(d["loss_pnls"]) / len(d["loss_pnls"]), 2) if d["loss_pnls"] else 0.0

    # Segregate profitable and losing assets
    profitable = [d for d in by_asset.values() if d["net_pnl"] > 0]
    profitable.sort(key=lambda x: x["net_pnl"], reverse=True)

    losing = [d for d in by_asset.values() if d["net_pnl"] < 0]
    losing.sort(key=lambda x: x["net_pnl"])  # Worst loss first (most negative)

    breakeven = [d for d in by_asset.values() if d["net_pnl"] == 0]

    # Toxic / severe underperformers: net loss <= -$300 AND win rate < 35% with >= 2 trades
    toxic = [
        d for d in losing
        if d["net_pnl"] <= -300.0 and d["win_rate"] < 35.0 and d["trades"] >= 2
    ]

    # Star / high-conviction assets: net PnL >= $200 AND win rate >= 50%
    stars = [
        d for d in profitable
        if d["net_pnl"] >= 200.0 and d["win_rate"] >= 50.0
    ]

    return {
        "all_assets": by_asset,
        "profitable": profitable,
        "losing": losing,
        "breakeven": breakeven,
        "toxic": toxic,
        "stars": stars,
        "total_trades": sum(d["trades"] for d in by_asset.values()),
        "total_net_pnl": round(sum(d["net_pnl"] for d in by_asset.values()), 2),
    }


def apply_learning_adaptations(analysis: Dict[str, Any], auto_avoid: bool = True) -> List[str]:
    """
    Learn from past mistakes:
      - Quarantines toxic assets by adding them to the avoid list so capital stops bleeding.
      - Recognizes top performing assets to preserve conviction.
    Returns human-readable adaptation notes.
    """
    adaptations: List[str] = []
    toxic_list = analysis.get("toxic", [])
    stars_list = analysis.get("stars", [])

    current_avoid = set(state.get_avoid_list())

    # 1. Address toxic losing assets
    for item in toxic_list:
        ticker = item["ticker"]
        loss_val = abs(item["net_pnl"])
        wr = item["win_rate"]
        trades = item["trades"]

        if auto_avoid:
            if ticker not in current_avoid:
                state.add_to_avoid_list([ticker])
                current_avoid.add(ticker)
                note = f"🚫 *Auto-Avoided {ticker}*: Sustained heavy losses (-${loss_val:,.2f}, {wr}% WR over {trades} trades). Quarantined to prevent further drawdown."
                adaptations.append(note)
                log.info("Learning Engine Adaptation: Added %s to avoid list", ticker)
                state.record_learning_event({
                    "action": "AUTO_AVOID",
                    "ticker": ticker,
                    "net_pnl": item["net_pnl"],
                    "win_rate": wr,
                    "trades": trades,
                    "reason": f"Severe loss (-${loss_val:,.2f}) with {wr}% win rate",
                })
            else:
                adaptations.append(f"🛡️ *{ticker} Quarantined*: Maintained on avoid list (-${loss_val:,.2f}, {wr}% WR).")
        else:
            adaptations.append(f"⚠️ *{ticker} Flagged as Toxic*: -${loss_val:,.2f} ({wr}% WR over {trades} trades). Recommend `/avoid {ticker}`.")

    # 2. Highlight star assets
    for item in stars_list:
        ticker = item["ticker"]
        pnl_val = item["net_pnl"]
        wr = item["win_rate"]
        trades = item["trades"]
        adaptations.append(f"⭐ *{ticker} High Conviction*: Top performer with +${pnl_val:,.2f} ({wr}% WR over {trades} trades).")

    if not toxic_list and not stars_list:
        adaptations.append("ℹ️ All active assets performing within normal statistical variance. No structural quarantine needed.")

    return adaptations


def generate_daily_feedback_report(trades: Optional[List[dict]] = None, auto_adapt: bool = True) -> str:
    """
    Generate the complete Daily Intelligence & Learning Feedback report.
    """
    analysis = analyze_asset_performance(trades)
    adaptations = apply_learning_adaptations(analysis, auto_avoid=auto_adapt)

    now_str = datetime.now(timezone.utc).strftime("%b %d, %Y %H:%M UTC")

    import engine
    bal = engine.client.get_wallet_balance("USDT")
    eq = bal.get("equity", 0.0) if not engine.client.is_paper else state.get_equity()
    avail = bal.get("available", 0.0) if not engine.client.is_paper else eq

    lines = [
        "🧠 *XIRA DAILY INTELLIGENCE & LEARNING FEEDBACK*",
        f"_{now_str}_",
        "",
    ]

    # 1. Top Profitable Assets
    profitable = analysis["profitable"]
    lines.append("🏆 *Top Profitable Assets:*")
    if profitable:
        for i, item in enumerate(profitable[:8], 1):
            t = item["ticker"]
            n = item["trades"]
            pnl = item["net_pnl"]
            wr = item["win_rate"]
            lines.append(f"{i}. *{t}* ({n} trades) `+${pnl:,.2f}` (Win Rate: {wr:.1f}%)")
    else:
        lines.append("_No net profitable assets in this evaluation cycle._")

    lines.append("")

    # 2. Top Losing Assets (Most Lost)
    losing = analysis["losing"]
    lines.append("🔻 *Assets With Most Losses:*")
    if losing:
        for i, item in enumerate(losing[:8], 1):
            t = item["ticker"]
            n = item["trades"]
            pnl = abs(item["net_pnl"])
            wr = item["win_rate"]
            lines.append(f"{i}. *{t}* ({n} trades) `-${pnl:,.2f}` (Win Rate: {wr:.1f}%)")
    else:
        lines.append("_No net losing assets in this evaluation cycle!_")

    lines.append("")

    # 3. Machine Learning & Lessons Learned
    lines.append("🧠 *Lessons Learned & Adaptive Actions:*")
    lines.extend([f"• {a}" for a in adaptations])

    lines.append("")

    # 4. Account Summary
    avoided = state.get_avoid_list()
    avoid_str = f"`{', '.join(avoided)}`" if avoided else "_None (all allowed)_"

    lines.extend([
        "💼 *Desk Health & Controls:*",
        f"• *Total Equity*: `${eq:,.2f} USDT`",
        f"• *Available Margin*: `${avail:,.2f} USDT`",
        f"• *Net Sample PnL*: `${analysis['total_net_pnl']:+,.2f} USDT` across `{analysis['total_trades']}` trades",
        f"• *Currently Avoided*: {avoid_str}",
        "",
        "_Use `/feedback` anytime to generate a fresh audit._"
    ])

    return "\n".join(lines)


def send_daily_feedback(
    chat_id: Optional[str] = None,
    token: Optional[str] = None,
    auto_adapt: bool = True,
) -> bool:
    """
    Deliver the daily feedback report via Telegram.
    Targets secondary feedback bot if configured; otherwise primary bot.
    """
    fb_cfg = state.get_feedback_bot_config()
    target_token = token or fb_cfg.get("token") or cfg.telegram_token
    target_chat = chat_id or fb_cfg.get("chat_id") or cfg.telegram_chat_id

    if not target_token or not target_chat:
        log.warning("Feedback delivery skipped: missing bot token or chat ID")
        return False

    report_text = generate_daily_feedback_report(auto_adapt=auto_adapt)

    url = f"https://api.telegram.org/bot{target_token}/sendMessage"
    payload = {
        "chat_id": target_chat,
        "text": report_text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    try:
        r = requests.post(url, json=payload, timeout=12)
        if r.status_code == 200 and r.json().get("ok"):
            log.info("Delivered Daily Learning Feedback to %s", target_chat)
            return True
        else:
            log.warning("Feedback delivery to %s failed: %s", target_chat, r.text)
    except Exception as exc:
        log.error("Feedback delivery error: %s", exc)

    return False
