"""
chart.py — Generates desk-markup candlestick charts as PNG bytes.

Each chart includes:
  - Candlesticks (last 80–100 candles)
  - Demand or Supply box shaded (entry range)
  - Horizontal dashed lines: Entry Low, Entry High, TP1, TP2, SL
  - Labels on every line
  - Title: TICKER SIDE TF
  - Dark desk-markup style
"""
from __future__ import annotations

import io
import logging
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # headless, no display required
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import mplfinance as mpf
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ── Desk color palette ────────────────────────────────────────────────────────
_BG = "#0d1117"
_PANEL = "#161b22"
_UP = "#26a641"
_DOWN = "#da3633"
_ENTRY_BOX = "#1f6feb"
_TP1_COLOR = "#3fb950"
_TP2_COLOR = "#56d364"
_SL_COLOR = "#f85149"
_ENTRY_LOW_COLOR = "#388bfd"
_ENTRY_HIGH_COLOR = "#79c0ff"
_TEXT = "#e6edf3"
_MUTED = "#8b949e"

_STYLE = mpf.make_mpf_style(
    base_mpf_style="nightclouds",
    marketcolors=mpf.make_marketcolors(
        up=_UP, down=_DOWN,
        edge={"up": _UP, "down": _DOWN},
        wick={"up": _UP, "down": _DOWN},
        volume={"up": _UP, "down": _DOWN},
    ),
    facecolor=_PANEL,
    edgecolor=_MUTED,
    figcolor=_BG,
    gridcolor="#21262d",
    gridstyle="--",
    gridaxis="both",
    y_on_right=True,
)


def generate_chart(
    df: pd.DataFrame,
    ticker: str,
    side: str,
    tf: str,
    entry_low: Optional[float],
    entry_high: Optional[float],
    tp1: Optional[float],
    tp2: Optional[float],
    sl: Optional[float],
    live_price: Optional[float] = None,
    candle_count: int = 80,
) -> bytes:
    """
    Render a desk-markup candlestick chart and return PNG bytes.

    Parameters
    ----------
    df          : OHLCV DataFrame with DatetimeIndex
    ticker      : Ticker symbol, e.g. 'BTC'
    side        : 'BUY' | 'SELL' | 'WAIT'
    tf          : Timeframe string, e.g. '15m'
    entry_low   : Lower bound of entry range
    entry_high  : Upper bound of entry range
    tp1, tp2    : Take-profit levels
    sl          : Stop-loss level
    live_price  : Current market price (optional — drawn as thin white line)
    candle_count: How many candles to display (right-side window)
    """
    if df is None or len(df) < 10:
        return _error_png(ticker)

    # Trim to last N candles
    plot_df = df.tail(candle_count).copy()
    plot_df = plot_df[["open", "high", "low", "close", "volume"]]

    # Price range for padding
    price_min = plot_df["low"].min()
    price_max = plot_df["high"].max()
    price_range = price_max - price_min
    y_min = price_min - price_range * 0.08
    y_max = price_max + price_range * 0.15

    # Expand to include all level lines
    all_levels = [l for l in [entry_low, entry_high, tp1, tp2, sl, live_price] if l is not None]
    if all_levels:
        y_min = min(y_min, min(all_levels) - price_range * 0.05)
        y_max = max(y_max, max(all_levels) + price_range * 0.05)

    # Level line definitions — drawn via matplotlib axhline after mpf.plot
    # (mplfinance hlines= kwarg API varies by version; axhline is always safe)
    _level_lines = []

    def _reg_line(val, color, style="--", width=1.0):
        if val is not None:
            _level_lines.append((val, color, style, width))

    _reg_line(entry_low,  _ENTRY_LOW_COLOR,  "--", 1.2)
    _reg_line(entry_high, _ENTRY_HIGH_COLOR, "--", 1.2)
    _reg_line(tp1,        _TP1_COLOR,        "--", 1.0)
    _reg_line(tp2,        _TP2_COLOR,        ":",  1.0)
    _reg_line(sl,         _SL_COLOR,         "--", 1.2)
    if live_price is not None:
        _reg_line(live_price, "#ffffff", "-", 0.7)

    title = f"{ticker}  {side}  {tf}"

    fig, axes = mpf.plot(
        plot_df,
        type="candle",
        style=_STYLE,
        title=title,
        ylabel="",
        volume=True,
        tight_layout=True,
        returnfig=True,
        figsize=(12, 7),
    )

    ax = axes[0]  # main price axis

    # ── Draw level lines via matplotlib (version-agnostic) ───────────────
    for val, color, style, width in _level_lines:
        ax.axhline(y=val, color=color, linestyle=style, linewidth=width,
                   alpha=0.85, zorder=3)

    # ── Shade entry box ──────────────────────────────────────────────────
    if entry_low is not None and entry_high is not None:
        ax.axhspan(
            entry_low, entry_high,
            alpha=0.18,
            color=_ENTRY_BOX,
            zorder=0,
        )

    # ── Label every line ─────────────────────────────────────────────────
    def _label(val, text, color, side_right=True):
        if val is None:
            return
        x_pos = 1.001 if side_right else -0.001
        ax.annotate(
            f" {text}: ${_fmt(val)}",
            xy=(x_pos, val),
            xycoords=("axes fraction", "data"),
            fontsize=7.5,
            color=color,
            va="center",
            ha="left" if side_right else "right",
            fontweight="bold",
        )

    _label(entry_high, "Entry High", _ENTRY_HIGH_COLOR)
    _label(entry_low,  "Entry Low",  _ENTRY_LOW_COLOR)
    _label(tp1,        "TP1",        _TP1_COLOR)
    _label(tp2,        "TP2",        _TP2_COLOR)
    _label(sl,         "SL",         _SL_COLOR)
    if live_price is not None:
        _label(live_price, "Live", "#ffffff")

    # ── Legend box ────────────────────────────────────────────────────────
    patches = []
    if entry_low is not None:
        patches.append(mpatches.Patch(color=_ENTRY_BOX, alpha=0.5, label=f"Entry {_fmt(entry_low)}–{_fmt(entry_high)}"))
    if tp1 is not None:
        patches.append(mpatches.Patch(color=_TP1_COLOR, label=f"TP1 {_fmt(tp1)}"))
    if tp2 is not None:
        patches.append(mpatches.Patch(color=_TP2_COLOR, label=f"TP2 {_fmt(tp2)}"))
    if sl is not None:
        patches.append(mpatches.Patch(color=_SL_COLOR, label=f"SL {_fmt(sl)}"))

    if patches:
        ax.legend(
            handles=patches,
            loc="upper left",
            fontsize=7,
            framealpha=0.6,
            facecolor=_PANEL,
            edgecolor=_MUTED,
            labelcolor=_TEXT,
        )

    ax.set_ylim(y_min, y_max)
    fig.patch.set_facecolor(_BG)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _fmt(val: float) -> str:
    """Format a price value for chart labels."""
    if val >= 10000:
        return f"{val:,.0f}"
    if val >= 1000:
        return f"{val:,.1f}"
    if val >= 10:
        return f"{val:.2f}"
    if val >= 0.1:
        return f"{val:.4f}"
    return f"{val:.6f}"


def _error_png(ticker: str) -> bytes:
    """Return a minimal error chart when no data is available."""
    fig, ax = plt.subplots(figsize=(6, 3), facecolor=_BG)
    ax.set_facecolor(_PANEL)
    ax.text(0.5, 0.5, f"{ticker}\nNo chart data", color=_TEXT,
            ha="center", va="center", fontsize=14, transform=ax.transAxes)
    ax.axis("off")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()
