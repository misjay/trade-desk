"""
analytics.py — Performance analytics, timeframe reporting & visual chart generation.
Supports /hourlyreport, /dailyreport, /weeklyreport, /monthlyreport with dark-themed PNG charts.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import requests

from config import cfg
import state

log = logging.getLogger(__name__)

# Dark theme palette
_BG = "#0d1117"
_PANEL = "#161b22"
_UP = "#26a641"
_DOWN = "#da3633"
_NEUTRAL = "#58a6ff"
_TEXT = "#e6edf3"
_MUTED = "#8b949e"
_GRID = "#21262d"


def _parse_time(ts_str: Optional[str]) -> Optional[datetime]:
    if not ts_str:
        return None
    try:
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        return datetime.fromisoformat(ts_str)
    except Exception:
        pass
    # If millisecond epoch string
    try:
        ms = float(ts_str)
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except Exception:
        return None


def get_trades_for_period(period: str) -> Tuple[List[dict], datetime, datetime]:
    """
    Retrieve closed trades within the specified timeframe:
      - hourly: last 1 hour
      - daily: last 24 hours
      - weekly: last 7 days
      - monthly: last 30 days
    """
    now = datetime.now(timezone.utc)
    delta_map = {
        "hourly": timedelta(hours=1),
        "daily": timedelta(days=1),
        "weekly": timedelta(days=7),
        "monthly": timedelta(days=30),
    }
    window = delta_map.get(period.lower(), timedelta(days=1))
    start_time = now - window

    import engine
    # Make sure we have latest closed PnL from Bybit
    engine._sync_with_bybit()

    all_closed = state.get_closed_positions()
    filtered: List[dict] = []

    for t in all_closed:
        closed_dt = _parse_time(t.get("closed_at") or t.get("updated_time"))
        if closed_dt and closed_dt >= start_time:
            filtered.append(t)

    # Sort chronologically
    filtered.sort(key=lambda x: _parse_time(x.get("closed_at") or x.get("updated_time")) or now)
    return filtered, start_time, now


def compute_metrics(trades: List[dict]) -> Dict[str, Any]:
    """Compute institutional performance metrics for a list of closed trades."""
    total_trades = len(trades)
    if total_trades == 0:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "breakevens": 0,
            "win_rate": 0.0,
            "net_pnl": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "profit_factor": 0.0,
            "best_trade": None,
            "worst_trade": None,
            "avg_trade_pnl": 0.0,
        }

    pnls = [float(t.get("pnl_usdt", 0.0)) for t in trades]
    wins = [p for p in pnls if p > 0.0]
    losses = [p for p in pnls if p < 0.0]
    bes = [p for p in pnls if p == 0.0]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    net_pnl = sum(pnls)

    win_rate = (len(wins) / total_trades * 100.0) if total_trades > 0 else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)

    best_trade = max(trades, key=lambda x: float(x.get("pnl_usdt", 0.0)))
    worst_trade = min(trades, key=lambda x: float(x.get("pnl_usdt", 0.0)))

    return {
        "total_trades": total_trades,
        "wins": len(wins),
        "losses": len(losses),
        "breakevens": len(bes),
        "win_rate": round(win_rate, 1),
        "net_pnl": round(net_pnl, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "avg_trade_pnl": round(net_pnl / total_trades, 2),
    }


def generate_analytics_chart(period: str, trades: List[dict], metrics: Dict[str, Any]) -> io.BytesIO:
    """
    Generate dark-theme institutional performance analytics chart (PNG bytes).
    Top Panel: Cumulative PnL curve with area fill & drawdown tracking.
    Bottom Panel: Individual trade outcome bars with ticker labels or active floating PnL.
    """
    plt.close("all")
    fig = plt.figure(figsize=(12, 6.5), facecolor=_BG)
    fig.patch.set_facecolor(_BG)

    title_period = period.upper()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # If there are closed trades in this period
    if trades:
        gs = fig.add_gridspec(2, 1, height_ratios=[1.2, 1], hspace=0.35)
        ax_curve = fig.add_subplot(gs[0])
        ax_bars = fig.add_subplot(gs[1])

        for ax in (ax_curve, ax_bars):
            ax.set_facecolor(_PANEL)
            ax.tick_params(colors=_TEXT, labelsize=9)
            ax.grid(True, color=_GRID, linestyle="--", alpha=0.7)
            for spine in ax.spines.values():
                spine.set_color(_GRID)

        # ── 1. Top: Cumulative PnL Curve ─────────────────────────────────────
        pnls = [float(t.get("pnl_usdt", 0.0)) for t in trades]
        cum_pnl = np.cumsum([0.0] + pnls)
        steps = list(range(len(cum_pnl)))

        color_curve = _UP if cum_pnl[-1] >= 0 else _DOWN
        ax_curve.plot(steps, cum_pnl, color=color_curve, linewidth=2.4, label="Cumulative PnL ($)")
        ax_curve.fill_between(steps, cum_pnl, 0, color=color_curve, alpha=0.18)
        ax_curve.axhline(0, color=_MUTED, linestyle=":", linewidth=1.2)

        # Annotate end PnL
        end_val = cum_pnl[-1]
        ax_curve.scatter([steps[-1]], [end_val], color=color_curve, s=50, zorder=5)
        ax_curve.annotate(
            f"${end_val:+,.2f} USDT",
            xy=(steps[-1], end_val),
            xytext=(10, 0),
            textcoords="offset points",
            color=_TEXT,
            fontsize=10,
            fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.3", fc=_PANEL, ec=color_curve, lw=1.2),
        )

        ax_curve.set_title(
            f"XIRA {title_period} REPORT — CUMULATIVE REALIZED PnL ({now_str})\n"
            f"Net: ${metrics['net_pnl']:+,.2f} | Win Rate: {metrics['win_rate']}% ({metrics['wins']}W / {metrics['losses']}L) | PF: {metrics['profit_factor']}",
            color=_TEXT, fontsize=11, fontweight="bold", pad=12
        )
        ax_curve.set_ylabel("Realized PnL (USDT)", color=_TEXT, fontsize=10)
        ax_curve.set_xlim(0, max(1, len(steps) - 1))

        # ── 2. Bottom: Individual Trade PnL Bars ─────────────────────────────
        bar_colors = [_UP if p >= 0 else _DOWN for p in pnls]
        tickers = [t.get("ticker", "") for t in trades]
        indices = list(range(len(pnls)))

        bars = ax_bars.bar(indices, pnls, color=bar_colors, width=0.6, edgecolor=_BG, linewidth=0.8)
        ax_bars.axhline(0, color=_MUTED, linestyle=":", linewidth=1.0)
        ax_bars.set_ylabel("Trade PnL (USDT)", color=_TEXT, fontsize=10)
        ax_bars.set_xlabel("Closed Trades Sequence", color=_TEXT, fontsize=10)
        ax_bars.set_xticks(indices)
        ax_bars.set_xticklabels(tickers, rotation=45, ha="right", fontsize=8, color=_TEXT)

        # Annotate bar values
        for bar, pnl in zip(bars, pnls):
            y_pos = bar.get_height()
            offset = 4 if y_pos >= 0 else -10
            ax_bars.annotate(
                f"${pnl:+,.0f}",
                xy=(bar.get_x() + bar.get_width() / 2, y_pos),
                xytext=(0, offset),
                textcoords="offset points",
                ha="center",
                fontsize=7.5,
                color=_TEXT,
                fontweight="bold",
            )

    else:
        # No closed trades in this period: Display Active Open Positions PnL
        import engine
        open_pos = state.get_open_positions()
        ax = fig.add_subplot(111)
        ax.set_facecolor(_PANEL)
        ax.tick_params(colors=_TEXT, labelsize=9)
        ax.grid(True, color=_GRID, linestyle="--", alpha=0.7)
        for spine in ax.spines.values():
            spine.set_color(_GRID)

        if open_pos:
            tickers = [p.get("ticker", "POS") for p in open_pos.values()]
            u_pnls = [float(p.get("unrealised_pnl", 0.0) or 0.0) for p in open_pos.values()]
            colors = [_UP if u >= 0 else _DOWN for u in u_pnls]

            bars = ax.bar(tickers, u_pnls, color=colors, width=0.5, edgecolor=_BG)
            ax.axhline(0, color=_MUTED, linestyle=":", linewidth=1.2)
            ax.set_ylabel("Floating Unrealized PnL (USDT)", color=_TEXT, fontsize=10)
            ax.set_title(
                f"XIRA {title_period} REPORT — 0 Closed Trades in Window\n"
                f"Active Bybit Floating Positions PnL Breakdown ({now_str})",
                color=_TEXT, fontsize=11, fontweight="bold", pad=12
            )

            for bar, u in zip(bars, u_pnls):
                y_pos = bar.get_height()
                offset = 4 if y_pos >= 0 else -12
                ax.annotate(
                    f"${u:+,.2f}",
                    xy=(bar.get_x() + bar.get_width() / 2, y_pos),
                    xytext=(0, offset),
                    textcoords="offset points",
                    ha="center",
                    fontsize=8.5,
                    color=_TEXT,
                    fontweight="bold",
                )
        else:
            ax.text(
                0.5, 0.5,
                f"No closed trades in {title_period} period\nand no open positions currently active.\nTrade Desk actively scanning.",
                color=_MUTED, fontsize=13, ha="center", va="center"
            )
            ax.set_title(f"XIRA {title_period} REPORT — NO TRADES IN WINDOW ({now_str})", color=_TEXT, fontsize=11, fontweight="bold")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=_BG, edgecolor="none")
    buf.seek(0)
    plt.close(fig)
    return buf


def format_report_text(period: str, metrics: Dict[str, Any], start_time: datetime, end_time: datetime) -> str:
    """Format structured markdown report caption for Telegram."""
    title_emoji = {"hourly": "⏱️", "daily": "📅", "weekly": "📆", "monthly": "🗓️"}.get(period.lower(), "📊")
    period_title = period.capitalize()

    start_str = start_time.strftime("%b %d, %H:%M")
    end_str = end_time.strftime("%H:%M UTC")

    import engine
    bal = engine.client.get_wallet_balance("USDT")
    eq = bal.get("equity", 0.0) if not engine.client.is_paper else state.get_equity()
    avail = bal.get("available", 0.0) if not engine.client.is_paper else eq

    open_pos = state.get_open_positions()
    total_u_pnl = sum(float(p.get("unrealised_pnl", 0.0) or 0.0) for p in open_pos.values())

    lines = [
        f"{title_emoji} *Xira {period_title} Performance Report*",
        f"_{start_str} – {end_str}_",
        "",
        f"• *Net Realized PnL*: `${metrics['net_pnl']:+,.2f} USDT`",
        f"• *Win Rate*: `{metrics['win_rate']}%` ({metrics['wins']}W / {metrics['losses']}L / {metrics['breakevens']}BE)",
        f"• *Profit Factor*: `{metrics['profit_factor']:.2f}`",
        f"• *Closed Trades*: `{metrics['total_trades']}`",
    ]

    if metrics["best_trade"]:
        bt = metrics["best_trade"]
        lines.append(f"• *Best Trade*: {bt.get('ticker')} `+{bt.get('pnl_usdt', 0):,.2f}` USDT")
    if metrics["worst_trade"]:
        wt = metrics["worst_trade"]
        lines.append(f"• *Worst Trade*: {wt.get('ticker')} `{wt.get('pnl_usdt', 0):,.2f}` USDT")

    lines.extend([
        "",
        "💼 *Account Snapshot:*",
        f"• *Wallet Equity*: `${eq:,.2f} USDT`",
        f"• *Available Margin*: `${avail:,.2f} USDT`",
        f"• *Active Positions*: `{len(open_pos)}` (Floating: `${total_u_pnl:+,.2f} USDT`)",
    ])

    return "\n".join(lines)


def send_report(chat_id: str, period: str) -> bool:
    """Generate metrics, plot visual analytics chart, and deliver to Telegram chat."""
    if not cfg.telegram_token:
        log.warning("Telegram token missing; cannot send report")
        return False

    trades, start_dt, end_dt = get_trades_for_period(period)
    metrics = compute_metrics(trades)
    caption = format_report_text(period, metrics, start_dt, end_dt)

    try:
        chart_buf = generate_analytics_chart(period, trades, metrics)
    except Exception as exc:
        log.error("Failed to generate analytics chart for %s: %s", period, exc)
        chart_buf = None

    url = f"https://api.telegram.org/bot{cfg.telegram_token}/sendPhoto"
    if chart_buf:
        try:
            files = {"photo": (f"{period}_report.png", chart_buf.getvalue(), "image/png")}
            data = {
                "chat_id": chat_id,
                "caption": caption[:1024],  # Telegram photo caption limit
                "parse_mode": "Markdown",
            }
            r = requests.post(url, data=data, files=files, timeout=20)
            if r.status_code == 200:
                log.info("Delivered %s report chart to chat %s", period, chat_id)
                # If caption was truncated, send remainder
                if len(caption) > 1024:
                    rem_url = f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage"
                    requests.post(rem_url, json={"chat_id": chat_id, "text": caption[1024:], "parse_mode": "Markdown"}, timeout=10)
                return True
            else:
                log.warning("sendPhoto failed: %s, falling back to sendMessage", r.text)
        except Exception as exc:
            log.warning("sendPhoto exception: %s", exc)

    # Fallback text message if photo delivery failed
    text_url = f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage"
    try:
        requests.post(text_url, json={"chat_id": chat_id, "text": caption, "parse_mode": "Markdown"}, timeout=10)
        return True
    except Exception as exc:
        log.error("Fallback text report failed: %s", exc)
        return False
