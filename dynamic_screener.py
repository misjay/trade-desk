"""
dynamic_screener.py — Whole-Exchange Opportunistic Radar Screener.

Dynamically sweeps all 350+ Bybit USDT Linear perpetuals for unlisted opportunities:
  - Scans beyond CORE_TICKERS and EXTRA_TICKERS.
  - Identifies sudden volume surges (RVOL >= 2.5x) and price momentum anomalies.
  - Enforces minimum institutional liquidity ($15M+ 24h turnover).
  - Validates candidates through Winz's order block & liquidity shelf engine.
  - Broadcasts "⚡ OPPORTUNISTIC RADAR CALL" and executes one-time auto-trades.
  - Ephemeral: Does NOT add the token to persistent watchlists or config.py.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from config import cfg, ALL_TICKERS, bybit_linear_symbol
import notifier
import scanner
import state

log = logging.getLogger("dynamic_screener")

# Non-crypto traditional equities / commodities listed on Bybit derivatives
EXCLUDED_NON_CRYPTO = {
    "XAU", "XAG", "SOXL", "SOXS", "KORU", "SKHYNIX", "NVDA", "TSLA",
    "AAPL", "AMZN", "MSFT", "GOOGL", "META", "SPY", "QQQ", "US500", "US30",
}

# Cooldown between automated opportunistic radar triggers (in seconds)
OPPORTUNISTIC_COOLDOWN_SEC = 3600  # 1 hour minimum cooldown between automated triggers
_last_radar_trigger_time = 0.0


def sweep_unlisted_opportunities(
    min_turnover_usdt: float = 15_000_000.0,
    min_24h_move_pct: float = 4.0,
    top_n: int = 5,
) -> List[Dict[str, Any]]:
    """
    Sweeps the entire Bybit Linear market for liquid, unlisted momentum movers.
    Returns sorted list of candidate dictionaries.
    """
    url = "https://api.bybit.com/v5/market/tickers"
    try:
        r = requests.get(url, params={"category": "linear"}, timeout=10)
        data = r.json()
        raw_list = data.get("result", {}).get("list", [])
    except Exception as exc:
        log.error("Failed to fetch Bybit tickers for radar sweep: %s", exc)
        return []

    if not raw_list:
        return []

    candidates = []
    registered_set = set(ALL_TICKERS)
    avoided_set = set(state.get_avoid_list())
    quarantine_set = set(state.get_quarantine_list().keys())

    for item in raw_list:
        sym = item.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        ticker = sym[:-4]

        # 1. Skip registered watchlists and excluded equities
        if ticker in registered_set or ticker in EXCLUDED_NON_CRYPTO:
            continue
        if ticker in avoided_set or ticker in quarantine_set:
            continue

        turnover = float(item.get("turnover24h", 0.0) or 0.0)
        if turnover < min_turnover_usdt:
            continue

        price_24h_pct = abs(float(item.get("price24hPcnt", 0.0) or 0.0)) * 100.0
        if price_24h_pct < min_24h_move_pct:
            continue

        last_p = float(item.get("lastPrice", 0.0) or 0.0)
        if last_p <= 0:
            continue

        # Score by combination of relative momentum & volume turnover
        score = (price_24h_pct * 0.6) + ((turnover / 50_000_000.0) * 0.4)

        candidates.append({
            "ticker": ticker,
            "symbol": sym,
            "live_price": last_p,
            "turnover_usdt": turnover,
            "turnover_str": f"${turnover / 1_000_000.0:.1f}M",
            "price_24h_pct": price_24h_pct,
            "score": score,
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:top_n]


def run_opportunistic_scan_and_dispatch(
    manual: bool = False,
    bot_token: Optional[str] = None,
) -> Optional[dict]:
    """
    Main evaluation pipeline:
      1. Sweeps exchange for unlisted momentum anomalies.
      2. Validates candidates through structural order block logic.
      3. Dispatches call and auto-executes trade if an A+ setup is found.
    """
    global _last_radar_trigger_time

    # Cooldown check for automated runs
    if not manual and (time.time() - _last_radar_trigger_time) < OPPORTUNISTIC_COOLDOWN_SEC:
        log.debug("Opportunistic radar in cooldown (elapsed: %.1fs < %ds)", time.time() - _last_radar_trigger_time, OPPORTUNISTIC_COOLDOWN_SEC)
        return None

    log.info("Running whole-exchange Opportunistic Radar sweep across unlisted Bybit perps...")
    candidates = sweep_unlisted_opportunities()
    if not candidates:
        log.info("Opportunistic Radar: No unlisted candidate met liquidity ($15M+) and momentum thresholds.")
        return None

    btc_regime = scanner.get_btc_macro_regime()
    log.info("Opportunistic Radar found %d candidates. BTC regime: %s", len(candidates), btc_regime)

    for cand in candidates:
        ticker = cand["ticker"]
        sig = scanner.analyze_ticker(ticker, trade_type="scalp")
        if not sig or sig.get("side") not in ("BUY", "SELL"):
            continue

        # Enforce macro regime: if BTC is in STRONG_BEAR, altcoin longs are forbidden
        if btc_regime == "STRONG_BEAR" and sig.get("side") == "BUY":
            log.info("Opportunistic radar skipped BUY on %s due to BTC STRONG_BEAR flush.", ticker)
            continue

        conviction = float(sig.get("conviction") or 0.0)
        rr = float(sig.get("rr") or 0.0)

        # High-conviction filter for unlisted opportunism (>= 80% conviction, >= 1:2.0 RR)
        if conviction >= 80.0 and rr >= 1.95:
            log.info("Opportunistic Radar TRIGGERED for %s %s (Conviction: %.1f%%, RR: 1:%.1f)", ticker, sig.get("side"), conviction, rr)
            _last_radar_trigger_time = time.time()

            # Mark as opportunistic
            sig["is_opportunistic"] = True
            sig["turnover_str"] = cand["turnover_str"]

            # 1. Format and broadcast announcement
            call_msg = format_radar_call_card(sig)
            if not bot_token:
                call_cfg = state.get_call_bot_config()
                bot_token = call_cfg.get("token") or cfg.call_bot_token or cfg.telegram_token

            from telegram_bot import _reply
            # Dispatch to configured signal channels
            active_channels = state.get_signal_channels()
            for ch in active_channels:
                try:
                    _reply(ch, call_msg, bot_token=bot_token)
                except Exception as e_ch:
                    log.debug("Error sending radar call to channel %s: %s", ch, e_ch)

            # 2. Track call in live tracker (handles TP/SL/Trailing in background)
            try:
                import call_tracker
                call_tracker.track_call(sig, bot_token=bot_token)
            except Exception as e_track:
                log.warning("Could not track opportunistic call: %s", e_track)

            # 3. Dispatch auto-trade execution to active users
            try:
                import exchange_manager
                exchange_manager.dispatch_triggered_trade_to_users(sig, bot_token=bot_token)
            except Exception as e_trade:
                log.warning("Could not execute opportunistic auto-trade: %s", e_trade)

            return sig

    log.info("Opportunistic Radar sweep completed. Candidates evaluated, waiting for precise shelf retest.")
    return None


def format_radar_call_card(sig: dict) -> str:
    """Formats a distinct, eye-catching Telegram card for unlisted radar calls."""
    ticker = sig.get("ticker", "TOKEN")
    side = sig.get("side", "BUY")
    side_emoji = "🟢 LONG" if side == "BUY" else "🔴 SHORT"
    lp = float(sig.get("live_price", 0.0))
    low_e = float(sig.get("entry_low", lp))
    high_e = float(sig.get("entry_high", lp))
    tp1 = float(sig.get("tp1", 0.0))
    sl = float(sig.get("sl", 0.0))
    rr = float(sig.get("rr", 2.0))
    conv = float(sig.get("conviction", 82.0))
    vol = sig.get("turnover_str", "$25M+")
    reason = sig.get("reason", "Structural liquidity shelf breakout")

    gain_pct = abs(tp1 - lp) / lp * 100.0 if lp > 0 else 0.0
    risk_pct = abs(lp - sl) / lp * 100.0 if lp > 0 else 0.0

    return (
        f"⚡ *WINZ OPPORTUNISTIC RADAR CALL* ⚡\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 *New Market Discovery (Unlisted Opportunity)*\n\n"
        f"• *Asset*: `${ticker}USDT` ({side_emoji} | 15m)\n"
        f"• *Conviction*: `{conv:.0f}% Institutional Edge`\n"
        f"• *24h Liquidity*: `{vol} USDT` (Deep Volume Confirmed)\n\n"
        f"🎯 *Execution Coordinates*:\n"
        f"• *Trigger Price*: `${lp:,.4f}`\n"
        f"• *Entry Band*: `${low_e:,.4f} – ${high_e:,.4f}`\n"
        f"• *Take Profit (TP1)*: `${tp1:,.4f}` (`+{gain_pct:.1f}%`)\n"
        f"• *Stop Loss (SL)*: `${sl:,.4f}` (`-{risk_pct:.1f}%`)\n"
        f"• *Risk:Reward*: `1:{rr:.1f}`\n\n"
        f"💡 *Risk & Portfolio Rules*:\n"
        f"• *Leverage*: Max `5x – 10x Isolated`\n"
        f"• *Status*: **One-Time Alpha Execution**.\n"
        f"  _This token is NOT permanently added to desk lists. Once Take-Profit or Stop-Loss hits, the trade closes and the slot frees automatically._\n\n"
        f"🧠 *Algorithmic Trigger Thesis*:\n"
        f"{reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👉 _Auto-trade accounts with open slots will execute automatically._"
    )
