"""
market_research.py — Institutional Market Research Notes for Trade Desk Calls.

Formats deep institutional thesis, conditional triggers, and verdicts for signals,
delivering directly to the configured Feedback Bot destination.

Format Mandate (matches institutional research style):
  The thesis:
  - <Fundamental Catalyst bullet>
  - <Market Driver & Speculation bullet>
  - Price is now X% off the peak — <structure validation>
  - The <TF> RSI is <value>; <momentum/correction context>

  What to watch:
  - If $X-Y forms support with a <TF> close, that's a reaccumulation zone for a <SIDE>
  - If it breaks below $Z (invalidation baseline), the setup is fully consumed and any <side> is dead
  - $P is the pivot — if price reclaims that, the breakout structure is re-confirmed

  Verdict: <Actionable summary assessing confirmation, entry readiness, and target execution.>
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, Optional, Tuple

import numpy as np
import requests

from bybit_client import BybitClient
from config import cfg, bybit_linear_symbol, bybit_spot_symbol
from scanner import fmt_dollar
import state

log = logging.getLogger(__name__)

# Standalone market client for research data
_client = BybitClient(mode="demo", demo_env="paper")

# ── Asset Fundamental Narratives & Catalysts ────────────────────────────────
ASSET_CATALYSTS: Dict[str, Dict[str, str]] = {
    "BTC": {
        "fundamental": "Institutional spot ETF capital inflows and post-halving structural supply deficit",
        "speculation": "Macro interest rate cuts and sovereign digital gold reserve positioning",
        "sector": "Tier-A Macro Store of Value",
    },
    "ETH": {
        "fundamental": "Ethereum Dencun L2 blob gas deflation and Layer-1 settlement revenue",
        "speculation": "Institutional staking yield adoption and ETF rotation momentum",
        "sector": "Smart Contract Settlement Layer",
    },
    "SOL": {
        "fundamental": "Retail on-chain DEX velocity, high transaction fee generation, and Firedancer validator upgrade",
        "speculation": "High-beta liquidity capture and memecoin ecosystem trading volume dominance",
        "sector": "High-Throughput Monolithic L1",
    },
    "XRP": {
        "fundamental": "Cross-border banking liquidity corridors and institutional RLUSD stablecoin launch",
        "speculation": "Post-SEC regulatory clarity and multi-jurisdictional settlement momentum",
        "sector": "Enterprise Cross-Border Liquidity",
    },
    "BNB": {
        "fundamental": "Binance ecosystem exchange utility, Launchpool capital lockup, and algorithmic BEP auto-burn",
        "speculation": "Centralized exchange market share defense and CeFi-to-DeFi liquidity bridge",
        "sector": "Exchange Ecosystem Infrastructure",
    },
    "DOGE": {
        "fundamental": "Decentralized PoW payment network liquidity and global merchant acceptance",
        "speculation": "Retail speculative proxy and high-profile social media payment integration speculation",
        "sector": "Cultural Liquidity & Retail Sentiment",
    },
    "ADA": {
        "fundamental": "Cardano Voltaire governance era decentralization and Chang hard fork execution",
        "speculation": "Peer-reviewed formal verification thesis and Layer-1 capital rotation",
        "sector": "Proof-of-Stake Enterprise L1",
    },
    "LINK": {
        "fundamental": "Chainlink Cross-Chain Interoperability Protocol (CCIP) and institutional Swift/DTCC tokenization pilot",
        "speculation": "Real-World Asset (RWA) data oracle dominance and staking v0.2 accumulation",
        "sector": "Decentralized Oracle Standard",
    },
    "AVAX": {
        "fundamental": "Avalanche institutional enterprise tokenization (California DMV / Evergreen Subnets)",
        "speculation": "Subnet scaling adoption and high-beta L1 rotation",
        "sector": "Multi-Chain Subnet Architecture",
    },
    "SUI": {
        "fundamental": "Move-VM object-centric architecture, Mysticeti sub-400ms finality, and native USDC integration",
        "speculation": "Next-generation high-speed Layer 1 capital absorption against Solana",
        "sector": "Next-Gen Move L1",
    },
    "HYPE": {
        "fundamental": "Hyperliquid custom L1 Perp DEX with zero gas fees and 100% protocol revenue sharing",
        "speculation": "Decentralized derivatives market share takeover from centralized exchanges",
        "sector": "On-Chain Derivatives Infrastructure",
    },
    "LTC": {
        "fundamental": "Legacy Scrypt PoW payment reliability and zero-downtime historical uptime",
        "speculation": "Commodity classification status and institutional ETF filing speculation",
        "sector": "Legacy Proof-of-Work Payments",
    },
    "AAVE": {
        "fundamental": "Dominant decentralized money market with v3 multi-chain revenue scaling and GHO stablecoin minting",
        "speculation": "DeFi governance fee-switch narrative and institutional lending utilization",
        "sector": "Blue-Chip DeFi Lending",
    },
    "ZEC": {
        "fundamental": "Zero-knowledge (zk-SNARKs) privacy cryptography and Halving structural emission reduction",
        "speculation": "Regulatory compliant shielded asset accumulation and privacy preservation demand",
        "sector": "Zero-Knowledge Privacy Pioneer",
    },
    "UNI": {
        "fundamental": "Uniswap v4 singleton contract hooks architecture and dedicated Unichain app-chain rollout",
        "speculation": "DeFi volume dominance and protocol revenue capture distribution",
        "sector": "Automated Market Maker Standard",
    },
    "BCH": {
        "fundamental": "High-throughput on-chain payment scalability with low miner fees and CashTokens smart contracts",
        "speculation": "Peer-to-peer medium of exchange rotation and ASIC mining difficulty dynamics",
        "sector": "Peer-to-Peer Electronic Cash",
    },
    "TRX": {
        "fundamental": "Settlement backbone hosting over 50% of all circulating global Tether (USDT) volume",
        "speculation": "SunPump meme ecosystem burn and stablecoin velocity yield",
        "sector": "Global Stablecoin Settlement Layer",
    },
    "XLM": {
        "fundamental": "Stellar Soroban smart contracts, MoneyGram global cash-to-crypto ramps, and cross-border remits",
        "speculation": "Enterprise banking corridors and financial inclusion settlement partnerships",
        "sector": "Anchor Network Cross-Border Rails",
    },
    "TAO": {
        "fundamental": "Bittensor decentralized machine learning intelligence network and specialized subnets tokenomics",
        "speculation": "Open-source AI compute commoditization and institutional Artificial Intelligence narrative",
        "sector": "Decentralized AI Intelligence Network",
    },
    "ONDO": {
        "fundamental": "BlackRock BUIDL integration, tokenized US Treasuries (OUSG/USDY), and institutional RWA yield",
        "speculation": "Institutional RWA adoption and on-chain risk-free rate streaming",
        "sector": "Tokenized Real-World Assets (RWA)",
    },
    "PEPE": {
        "fundamental": "Zero-tax pure community tokenomics with deep decentralized liquidity pools on Ethereum",
        "speculation": "High-beta market sentiment proxy and speculative meme leverage velocity",
        "sector": "Ethereum Cultural Meme Proxy",
    },
    "ENA": {
        "fundamental": "Ethena delta-neutral synthetic dollar (USDe) cash-and-carry basis trade yield protocol",
        "speculation": "Derivatives funding market expansion and centralized exchange margin collateral integration",
        "sector": "Synthetic Dollar & Basis Yield",
    },
    "HBAR": {
        "fundamental": "Hedera Hashgraph asynchronous Byzantine fault tolerance and Governing Council (Google, IBM, Dell)",
        "speculation": "Enterprise supply chain tracking and tokenized asset custody adoption",
        "sector": "Enterprise Hashgraph DLT",
    },
    "NEAR": {
        "fundamental": "User-owned AI and Chain Abstraction pioneer with Nightshade sharding infrastructure",
        "speculation": "Decentralized AI developer traction and Web3 user abstraction integration",
        "sector": "Chain Abstraction & AI L1",
    },
    "WLD": {
        "fundamental": "World Network Proof of Human identity iris biometric verification and World Chain infrastructure",
        "speculation": "AI agent verification demand and identity-gated decentralized compute allocation",
        "sector": "Proof of Personhood & Biometric Identity",
    },
}

DEFAULT_CATALYST: Dict[str, str] = {
    "fundamental": "High-liquidity derivatives order flow with structural market depth",
    "speculation": "Sector momentum and technical order block rotation",
    "sector": "Active Perpetual Derivatives Market",
}


# ── Technical Metrics Helpers ───────────────────────────────────────────────
def compute_rsi(closes: np.ndarray, period: int = 14) -> float:
    """Calculate 14-period RSI from price series."""
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def get_market_telemetry(ticker: str) -> Dict[str, Any]:
    """Fetch live ticker telemetry, RSI, 24h range, and 4H macro alignment."""
    sym = bybit_linear_symbol(ticker)
    t_data = _client.get_ticker(sym, category="linear")
    if not t_data:
        t_data = _client.get_ticker(bybit_spot_symbol(ticker), category="spot") or {}

    mark_price = float(t_data.get("mark_price") or t_data.get("last_price") or 0.0)
    funding_rate = float(t_data.get("funding_rate") or 0.0)
    turnover_24h = float(t_data.get("turnover_24h") or 0.0)

    # Historical klines for RSI and 4H macro
    from scanner import fetch_ohlcv
    df_15m = fetch_ohlcv(ticker, tf_minutes=15, limit=50)
    df_4h = fetch_ohlcv(ticker, tf_minutes=240, limit=50)

    rsi_15m = 50.0
    rsi_4h = 50.0
    high_24h = mark_price * 1.05
    low_24h = mark_price * 0.95
    ema20_4h = mark_price

    if df_15m is not None and not df_15m.empty:
        rsi_15m = compute_rsi(df_15m["close"].values, period=14)
        high_24h = float(df_15m["high"].tail(96).max()) if len(df_15m) >= 10 else float(df_15m["high"].max())
        low_24h = float(df_15m["low"].tail(96).min()) if len(df_15m) >= 10 else float(df_15m["low"].min())

    if df_4h is not None and not df_4h.empty:
        rsi_4h = compute_rsi(df_4h["close"].values, period=14)
        ema20_4h = float(df_4h["close"].ewm(span=20, adjust=False).mean().iloc[-1])

    return {
        "mark_price": mark_price,
        "funding_rate": funding_rate,
        "turnover_24h": turnover_24h,
        "rsi_15m": rsi_15m,
        "rsi_4h": rsi_4h,
        "high_24h": high_24h,
        "low_24h": low_24h,
        "ema20_4h": ema20_4h,
    }


# ── Market Research Generator ───────────────────────────────────────────────
def generate_market_research(sig: dict) -> str:
    """
    Synthesize an institutional research note for a trading signal / call
    formatted strictly according to the requested 3-section layout.
    """
    ticker = sig.get("ticker", "UNKNOWN").upper()
    side = sig.get("side", "WAIT").upper()
    tf = sig.get("tf", "15m")
    trade_type = sig.get("trade_type", "scalp").capitalize()
    el = sig.get("entry_low")
    eh = sig.get("entry_high")
    tp1 = sig.get("tp1")
    tp2 = sig.get("tp2")
    sl = sig.get("sl")
    structure = sig.get("structure", "")
    reason = sig.get("reason", "")
    live_price = sig.get("live_price")

    cat = ASSET_CATALYSTS.get(ticker, DEFAULT_CATALYST)
    telemetry = get_market_telemetry(ticker)

    mark = live_price or telemetry["mark_price"]
    high_24h = max(telemetry["high_24h"], mark)
    low_24h = min(telemetry["low_24h"], mark)
    rsi_4h = telemetry["rsi_4h"]
    rsi_15m = telemetry["rsi_15m"]
    funding = telemetry["funding_rate"]

    # Calculate distance off peak and distance off low
    off_peak_pct = round(abs(mark - high_24h) / high_24h * 100.0, 1) if high_24h > 0 else 0.0
    off_low_pct = round(abs(mark - low_24h) / low_24h * 100.0, 1) if low_24h > 0 else 0.0

    # Format Dollar Strings
    el_str = fmt_dollar(el)
    eh_str = fmt_dollar(eh)
    tp1_str = fmt_dollar(tp1)
    tp2_str = fmt_dollar(tp2)
    sl_str = fmt_dollar(sl)
    mark_str = fmt_dollar(mark)
    high_str = fmt_dollar(high_24h)
    low_str = fmt_dollar(low_24h)
    band_str = f"{el_str}–{eh_str}"

    # Determine RSI description
    if rsi_4h >= 70:
        rsi_desc = f"overbought on the spike ({rsi_4h:.0f}); mean-reversion is expected"
    elif rsi_4h <= 30:
        rsi_desc = f"deeply oversold ({rsi_4h:.0f}); seller exhaustion is forming"
    elif rsi_4h >= 55:
        rsi_desc = f"constructive in bullish territory ({rsi_4h:.0f}); momentum favors buyers"
    elif rsi_4h <= 45:
        rsi_desc = f"subdued ({rsi_4h:.0f}); bearish drift persists below median"
    else:
        rsi_desc = f"neutral at {rsi_4h:.0f}; the consolidation is orderly"

    # Funding note
    if funding > 0.0003:
        funding_note = f"Elevated positive funding (+{funding*100:.3f}%) shows aggressive long crowding"
    elif funding < -0.0001:
        funding_note = f"Negative funding ({funding*100:.3f}%) indicates short crowding and short-squeeze fuel"
    else:
        funding_note = f"Funding rate is balanced ({funding*100:+.4f}%) with neutral derivative positioning"

    # ── Section 1: The Thesis ────────────────────────────────────────────────
    thesis_bullets = []
    # 1. Fundamental driver
    thesis_bullets.append(f"The {ticker} core driver is a real fundamental catalyst: {cat['fundamental']} ({cat['sector']})")

    # 2. Speculation / derivative context
    thesis_bullets.append(f"Recent session order flow is steered by {cat['speculation']} — {funding_note}")

    # 3. Drawdown from peak or bounce from low & structure
    if side == "BUY":
        thesis_bullets.append(f"Price is currently {off_peak_pct}% off the session peak ({high_str}) — the pullback into the shelf provides structural support")
    else:
        thesis_bullets.append(f"Price is currently {off_low_pct}% expanded off the session low ({low_str}) — the rally into the supply ceiling is losing momentum")

    # 4. RSI & timeframe state
    thesis_bullets.append(f"The 4H RSI is {rsi_desc}")

    # ── Section 2: What to watch ─────────────────────────────────────────────
    watch_bullets = []
    if side == "BUY":
        watch_bullets.append(f"If {band_str} forms support with a {tf} candle close, that's a confirmed reaccumulation zone for a LONG")
        watch_bullets.append(f"If it breaks below {sl_str} (invalidation baseline), the demand shelf is fully consumed and the long setup is invalidated")
        watch_bullets.append(f"{tp1_str} is the primary pivot — if price reclaims that level, continuation towards {tp2_str} is confirmed")
    elif side == "SELL":
        watch_bullets.append(f"If {band_str} acts as resistance with a {tf} candle close, that's a confirmed distribution zone for a SHORT")
        watch_bullets.append(f"If price breaks above {sl_str} (invalidation baseline), the supply shelf is violated and the short setup is dead")
        watch_bullets.append(f"{tp1_str} is the downside pivot — if price breaks through that level, downward continuation towards {tp2_str} is confirmed")
    else:
        watch_bullets.append(f"If price establishes a clean shelf near {low_str} or {high_str}, watch for edge liquidity taps")
        watch_bullets.append(f"Range extremes between {low_str} and {high_str} must be respected before taking directional risk")
        watch_bullets.append(f"Mid-range chop must resolve into an edge before a directional call is initiated")

    # ── Section 3: Verdict ───────────────────────────────────────────────────
    if side == "BUY":
        verdict = (
            f"Actionable setup active. A LONG from {band_str} is favored while {sl_str} holds as the structural floor. "
            f"Do not chase if price trades above {eh_str} without an entry retest. "
            f"Confirm the {tf} close holds above the demand band, target {tp1_str} for first de-risk, and let runners ride to {tp2_str}."
        )
    elif side == "SELL":
        verdict = (
            f"Actionable setup active. A SHORT from {band_str} is favored while {sl_str} caps any invalidation attempt. "
            f"Do not chase into lower support. "
            f"Wait for the {tf} candle to test the supply band and reject. Target {tp1_str} for first de-risk, and let runners ride to {tp2_str}."
        )
    else:
        verdict = (
            f"No clean entry right now. Price is mid-range between {low_str} and {high_str}. "
            f"A LONG from current mark is chasing mid-range drift, and a SHORT lacks favorable risk-to-reward. "
            f"Stand aside until price retests structural edges."
        )

    # ── Assemble Output ──────────────────────────────────────────────────────
    header = f"📊 *Market Research: {ticker}/USDT ({trade_type} - {side})*"
    lines = [
        header,
        "",
        "*The thesis:*",
    ]
    for b in thesis_bullets:
        lines.append(f"- {b}")

    lines.append("")
    lines.append("*What to watch:*")
    for b in watch_bullets:
        lines.append(f"- {b}")

    lines.append("")
    lines.append(f"*Verdict:* {verdict}")

    return "\n".join(lines)


# ── Dynamic Timeframe Setup Generator ───────────────────────────────────────
def build_signal_for_timeframe(ticker: str, tf_minutes: int = 15) -> dict:
    """
    Build a dynamic structural signal on the requested timeframe (15m scalp or 240m day trade)
    using live Bybit OHLCV candles, swing extremes, and demand/supply shelves.
    """
    t_clean = ticker.strip().upper().replace("USDT", "")
    from scanner import fetch_ohlcv, detect_shelves_and_edges

    tf_str = "15m" if tf_minutes == 15 else ("4h" if tf_minutes == 240 else f"{tf_minutes}m")
    trade_type = "scalp" if tf_minutes == 15 else "day"

    df = fetch_ohlcv(t_clean, tf_minutes=tf_minutes, limit=80)
    if df is not None and len(df) >= 10:
        struct = detect_shelves_and_edges(df)
        live_price = struct["live_price"]
        range_pos = struct["range_pos"]

        if range_pos <= 0.45:
            side = "BUY"
            d_low, d_high = struct["demand_shelf"]
            entry_low = min(d_low, live_price * 0.996)
            entry_high = max(d_high, live_price * 1.002)
            sl = round(entry_low * 0.988, 8)
            tp1 = round(struct["mid_range"], 8)
            tp2 = round(struct["session_high"], 8)
            reason = f"Pullback into {tf_str} demand shelf ${entry_low:,.2f}–${entry_high:,.2f}"
            structure_str = f"Demand shelf at session discount ({range_pos*100:.1f}% range)"
        else:
            side = "SELL"
            s_low, s_high = struct["supply_shelf"]
            entry_low = min(s_low, live_price * 0.998)
            entry_high = max(s_high, live_price * 1.004)
            sl = round(entry_high * 1.012, 8)
            tp1 = round(struct["mid_range"], 8)
            tp2 = round(struct["session_low"], 8)
            reason = f"Rejection at {tf_str} supply ceiling ${entry_low:,.2f}–${entry_high:,.2f}"
            structure_str = f"Supply ceiling at session premium ({range_pos*100:.1f}% range)"

        risk = abs(live_price - sl)
        reward = abs(tp1 - live_price)
        rr = round(reward / risk, 2) if risk > 0 else 1.5
    else:
        from scanner import fetch_live_price
        lp = fetch_live_price(t_clean) or 100.0
        side = "BUY"
        live_price = lp
        entry_low = lp * 0.995
        entry_high = lp * 1.002
        sl = lp * 0.985
        tp1 = lp * 1.025
        tp2 = lp * 1.050
        rr = 2.0
        reason = f"Structural {tf_str} order block setup"
        structure_str = f"Support shelf near ${lp:,.2f}"

    return {
        "ticker": t_clean,
        "side": side,
        "trade_type": trade_type,
        "tf": tf_str,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "tp1": tp1,
        "tp2": tp2,
        "sl": sl,
        "rr": max(1.5, rr),
        "structure": structure_str,
        "reason": reason,
        "live_price": live_price,
    }


# ── Chart & Research Delivery Helper ────────────────────────────────────────
def send_research_with_chart(
    sig: dict,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
    tf_minutes: int = 15,
) -> bool:
    """
    Generate candlestick chart with marked up levels and deliver alongside
    the full institutional research note.
    """
    fb_cfg = state.get_feedback_bot_config()
    target_token = bot_token or fb_cfg.get("token") or cfg.telegram_token
    target_chat = chat_id or fb_cfg.get("chat_id") or cfg.telegram_chat_id

    if not target_token or not target_chat:
        log.warning("Research delivery skipped: missing bot token or chat ID")
        return False

    ticker = sig.get("ticker", "BTC").upper()
    side = sig.get("side", "BUY").upper()
    tf_str = "15m" if tf_minutes == 15 else ("4h" if tf_minutes == 240 else "1h")

    # Persist as last researched signal
    state.set_last_researched_signal(sig)

    # 1. Fetch OHLCV & generate chart PNG
    from scanner import fetch_ohlcv
    import chart
    df = fetch_ohlcv(ticker, tf_minutes=tf_minutes, limit=80)
    png_bytes = chart.generate_chart(
        df=df,
        ticker=ticker,
        side=side,
        tf=tf_str,
        entry_low=sig.get("entry_low"),
        entry_high=sig.get("entry_high"),
        tp1=sig.get("tp1"),
        tp2=sig.get("tp2"),
        sl=sig.get("sl"),
        live_price=sig.get("live_price"),
    )

    caption_summary = (
        f"📈 *{ticker}/USDT {sig.get('trade_type', 'Scalp').capitalize()} Breakdown ({tf_str})*\n"
        f"• *Side*: `{side}` | Mark: `{fmt_dollar(sig.get('live_price'))}`\n"
        f"• *Entry Shelf*: `{fmt_dollar(sig.get('entry_low'))}–{fmt_dollar(sig.get('entry_high'))}`\n"
        f"• *TP1*: `{fmt_dollar(sig.get('tp1'))}` | *TP2*: `{fmt_dollar(sig.get('tp2'))}` | *SL*: `{fmt_dollar(sig.get('sl'))}`"
    )

    # Send chart image if generated
    if png_bytes and len(png_bytes) > 1000:
        photo_url = f"https://api.telegram.org/bot{target_token}/sendPhoto"
        try:
            files = {"photo": (f"{ticker}_{tf_str}_chart.png", png_bytes, "image/png")}
            data = {
                "chat_id": target_chat,
                "caption": caption_summary,
                "parse_mode": "Markdown",
            }
            requests.post(photo_url, data=data, files=files, timeout=20)
        except Exception as exc:
            log.warning("sendPhoto failed for %s: %s", ticker, exc)

    # Send detailed research note text
    research_text = generate_market_research(sig)
    text_url = f"https://api.telegram.org/bot{target_token}/sendMessage"
    payload = {
        "chat_id": target_chat,
        "text": research_text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(text_url, json=payload, timeout=12)
        return r.status_code == 200 and r.json().get("ok")
    except Exception as exc:
        log.error("Failed to send research note to %s: %s", target_chat, exc)
        return False


def send_call_research_to_feedback_bot(sig: dict) -> bool:
    """Convenience alias for automatic scan dispatch."""
    tf_minutes = 240 if str(sig.get("tf", "")).lower() in ("4h", "240") else 15
    return send_research_with_chart(sig, tf_minutes=tf_minutes)


# ── Twitter / X Post Converter ──────────────────────────────────────────────
def format_twitter_post(sig: dict) -> str:
    """
    Convert a research setup into a punchy, high-impact Twitter / X post
    strictly optimized to fit within the 280-character limit.
    """
    ticker = sig.get("ticker", "BTC").upper()
    side = sig.get("side", "BUY").upper()
    tf = sig.get("tf", "15m")
    el = fmt_dollar(sig.get("entry_low"))
    eh = fmt_dollar(sig.get("entry_high"))
    tp1 = fmt_dollar(sig.get("tp1"))
    tp2 = fmt_dollar(sig.get("tp2"))
    sl = fmt_dollar(sig.get("sl"))
    rr = sig.get("rr", 2.0)

    cat = ASSET_CATALYSTS.get(ticker, DEFAULT_CATALYST)
    sector = cat.get("sector", "Crypto")

    telemetry = get_market_telemetry(ticker)
    rsi_4h = telemetry.get("rsi_4h", 50.0)

    # Single tweet layout (< 280 chars)
    tweet = (
        f"${ticker} {side} Setup ({tf}) 🎯\n\n"
        f"Thesis: {sector}. 4H RSI {rsi_4h:.0f}.\n"
        f"• Entry: {el} – {eh}\n"
        f"• Targets: {tp1} / {tp2} (R:R {rr:.1f})\n"
        f"• Invalidation: {sl}\n\n"
        f"Verdict: Confirm {tf} shelf hold before entering.\n\n"
        f"#{ticker} #CryptoTrading"
    )

    if len(tweet) > 280:
        tweet = (
            f"${ticker} {side} ({tf}) 🎯\n"
            f"• Entry: {el} – {eh}\n"
            f"• Targets: {tp1} / {tp2}\n"
            f"• Stop: {sl} (R:R {rr:.1f})\n"
            f"• 4H RSI: {rsi_4h:.0f}\n\n"
            f"Verdict: Confirm {tf} shelf hold.\n"
            f"#{ticker} #Crypto"
        )

    return tweet


def send_twitter_post(
    sig: dict,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> bool:
    """Format and deliver Twitter post to the requested chat."""
    fb_cfg = state.get_feedback_bot_config()
    target_token = bot_token or fb_cfg.get("token") or cfg.telegram_token
    target_chat = chat_id or fb_cfg.get("chat_id") or cfg.telegram_chat_id

    if not target_token or not target_chat:
        return False

    tweet = format_twitter_post(sig)
    char_count = len(tweet)
    msg = f"🐦 *Twitter / X Post ({char_count}/280 chars):*\n\n```\n{tweet}\n```\n_Tap to copy & paste directly into X/Twitter._"

    url = f"https://api.telegram.org/bot{target_token}/sendMessage"
    payload = {
        "chat_id": target_chat,
        "text": msg,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=12)
        return r.status_code == 200 and r.json().get("ok")
    except Exception as exc:
        log.error("Failed to deliver Twitter post: %s", exc)
        return False
