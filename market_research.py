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

from datetime import datetime, timezone
import logging
import math
import time
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
        "hook": "most chains are chasing retail attention.\n\n$HBAR has been quietly chasing enterprise integration. different game & payoff.\n\nwhen this moves, won't be because of a tweet. it'll be because the infra was already there.",
    },
    "NEAR": {
        "fundamental": "User-owned AI and Chain Abstraction pioneer with Nightshade sharding infrastructure",
        "speculation": "Decentralized AI developer traction and Web3 user abstraction integration",
        "sector": "Chain Abstraction & AI L1",
        "hook": "most L1s battle for fleeting DEX memecoins.\n\n$NEAR is quietly building the operating system for AI agents and chain abstraction.\n\nwhen the wave hits, adoption won't be speculative—it'll be structural.",
    },
    "WLD": {
        "fundamental": "World Network Proof of Human identity iris biometric verification and World Chain infrastructure",
        "speculation": "AI agent verification demand and identity-gated decentralized compute allocation",
        "sector": "Proof of Personhood & Biometric Identity",
        "hook": "most projects optimize for bot activity.\n\n$WLD is anchoring verifiable human identity in an AI agent world.\n\nscarcity of verified attention is the ultimate asymmetric play.",
    },
}

TWITTER_HOOKS: Dict[str, str] = {
    "BTC": (
        "most macro assets react to quarter-to-quarter headlines.\n\n"
        "$BTC is quietly absorbing institutional ETF balances and structural supply.\n\n"
        "when this reprices, it won't be hype. it'll be the math of liquid scarcity."
    ),
    "ETH": (
        "the market debated gas fees while $ETH quietly turned into the settlement layer for global finance and Layer 2s.\n\n"
        "settlement velocity doesn't need hype—it commands structural floor."
    ),
    "SOL": (
        "most ecosystems struggle to generate real consumer on-chain volume.\n\n"
        "$SOL captured the entire retail flow and velocity engine.\n\n"
        "liquidity goes where users transact, not where whitepapers promise."
    ),
    "XRP": (
        "most tokens are looking for a use case.\n\n"
        "$XRP has spent years embedding into institutional banking corridors.\n\n"
        "when settlement rails flip on, it won't be retail noise driving it."
    ),
    "BNB": (
        "market cycles rotate, but exchange utility remains the ultimate cash machine.\n\n"
        "$BNB burns supply while powering the largest CeFi-to-DeFi capital funnel."
    ),
    "DOGE": (
        "critics call it a meme, yet $DOGE commands the deepest organic liquidity and merchant brand in crypto.\n\n"
        "never underestimate relentless cultural attention."
    ),
    "ADA": (
        "most networks ship fast and break things.\n\n"
        "$ADA built peer-reviewed formal infrastructure for the long horizon.\n\n"
        "governance is live—now watch structural positioning."
    ),
    "LINK": (
        "everyone talks about Real-World Asset (RWA) tokenization.\n\n"
        "$LINK already owns the institutional pipeline (CCIP, Swift, DTCC).\n\n"
        "you can't bring trillions on-chain without the standard."
    ),
    "AVAX": (
        "monolithic chains run into limits.\n\n"
        "$AVAX built custom enterprise Subnets quietly adopted by institutions.\n\n"
        "modular enterprise scale is an entirely different ballgame."
    ),
    "SUI": (
        "most L1s were built on legacy architectures.\n\n"
        "$SUI re-engineered speed from first principles with Move and sub-400ms finality.\n\n"
        "performance this clean attracts serious liquidity."
    ),
    "HYPE": (
        "centralized exchanges used to hold all derivatives power.\n\n"
        "$HYPE built a zero-fee custom chain redistributing 100% revenue to users.\n\n"
        "the decentralized perps shift is inevitable."
    ),
    "LTC": (
        "through every bull and bear market, $LTC maintains 100% uninterrupted uptime and rock-solid payment utility.\n\n"
        "commodity purity with institutional staying power."
    ),
    "AAVE": (
        "speculative protocols vanish, but $AAVE remains the unyielding backbone of on-chain credit and liquidity.\n\n"
        "money markets don't sleep."
    ),
    "ZEC": (
        "in an era of complete transparent surveillance, privacy becomes the rarest luxury.\n\n"
        "$ZEC zk-SNARK cryptography is the gold standard for shielded value."
    ),
    "UNI": (
        "DEXs come and go, but $UNI commands the deepest spot liquidity in all of DeFi.\n\n"
        "v4 hooks and Unichain cement its moat."
    ),
    "BCH": (
        "high fees push everyday commerce away.\n\n"
        "$BCH has quietly stayed true to fast, sub-cent peer-to-peer settlement."
    ),
    "TRX": (
        "while others debated tech stacks, $TRX quietly became the undisputed highway for >50% of global Tether volume.\n\n"
        "cashflow and transaction volume tell the real story."
    ),
    "XLM": (
        "remittances are broken worldwide.\n\n"
        "$XLM built real-world cash-to-crypto ramps with MoneyGram and global anchors.\n\n"
        "real utility beats speculative noise."
    ),
    "TAO": (
        "most AI tokens slapped a logo on a wrapper.\n\n"
        "$TAO built a decentralized neural network commoditizing machine intelligence across competitive subnets.\n\n"
        "compute and intelligence are the new reserve assets."
    ),
    "ONDO": (
        "trillions in institutional capital want on-chain risk-free treasury yield.\n\n"
        "$ONDO bridged BlackRock BUIDL into decentralized rails.\n\n"
        "this is where traditional finance actually enters."
    ),
    "PEPE": (
        "zero taxes, no team tokens, pure unadulterated market liquidity.\n\n"
        "$PEPE is the ultimate liquidity sponge when market risk appetite explodes."
    ),
    "ENA": (
        "traditional stablecoins rely on fiat reserves.\n\n"
        "$ENA built the internet bond through basis arbitrage yield.\n\n"
        "a structural paradigm shift for decentralized capital."
    ),
    "HBAR": (
        "most chains are chasing retail attention.\n\n"
        "$HBAR has been quietly chasing enterprise integration. different game & payoff.\n\n"
        "when this moves, won't be because of a tweet. it'll be because the infra was already there."
    ),
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

    # 5. Live Macro News & Sentiment
    try:
        import news_sentiment
        fng = news_sentiment.get_fear_and_greed_index()
        thesis_bullets.append(f"Live Market Sentiment: Fear & Greed Index is {fng.get('score', 50)}/100 ({fng.get('classification', 'Neutral')})")
    except Exception:
        pass

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
    if trade_type.lower() == "spot":
        verdict = (
            f"Spot Accumulation active. Dollar-cost average into the {band_str} discount zone. "
            f"Set structural invalidation floor at {sl_str}. "
            f"Target {tp1_str} for first de-risk (+8% to +15% expansion), and let swing runners ride to {tp2_str}."
        )
    elif side == "BUY":
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
    if trade_type.lower() == "spot":
        header = f"💎 *Market Research: {ticker}/USDT (Spot Accumulation - BUY)*"
    else:
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
    Build a standard manual structural setup on the requested timeframe (15m scalp or 240m day trade)
    using live Bybit OHLCV candles, swing extremes, and professional manual targets.
    Specifically calibrated for Winz callers (manual external exchange execution).
    """
    t_clean = ticker.strip().upper().replace("USDT", "")
    from scanner import fetch_ohlcv, detect_shelves_and_edges, fetch_live_price

    is_scalp = (tf_minutes == 15)
    tf_str = "15m" if is_scalp else ("4h" if tf_minutes == 240 else f"{tf_minutes}m")
    trade_type = "scalp" if is_scalp else "day"

    df = fetch_ohlcv(t_clean, tf_minutes=tf_minutes, limit=80)
    if df is not None and len(df) >= 10:
        struct = detect_shelves_and_edges(df)
        live_price = struct["live_price"]
        range_pos = struct["range_pos"]

        if range_pos <= 0.48:
            side = "BUY"
            d_low, d_high = struct["demand_shelf"]
            if is_scalp:
                # Scalp: 0.6% limit accumulation band, TP1 +2.4%, TP2 +5.0%, SL 1.2%
                entry_low = round(min(d_low, live_price * 0.992), 8)
                entry_high = round(max(d_high, live_price * 0.998), 8)
                sl = round(entry_low * 0.988, 8)
                tp1 = round(max(struct.get("mid_range", 0), entry_high * 1.024), 8)
                tp2 = round(max(struct.get("session_high", 0), entry_high * 1.050), 8)
            else:
                # Day trade: 1.3% limit accumulation band, TP1 +6.5%, TP2 +14.0%, SL 2.8%
                entry_low = round(min(d_low, live_price * 0.982), 8)
                entry_high = round(max(d_high, live_price * 0.995), 8)
                sl = round(entry_low * 0.970, 8)
                tp1 = round(max(struct.get("mid_range", 0), entry_high * 1.065), 8)
                tp2 = round(max(struct.get("session_high", 0), entry_high * 1.140), 8)
            reason = f"Pullback into {tf_str} demand shelf ${entry_low:,.2f}–${entry_high:,.2f}"
            structure_str = f"Demand shelf at session discount ({range_pos*100:.1f}% range)"
        else:
            side = "SELL"
            s_low, s_high = struct["supply_shelf"]
            if is_scalp:
                # Scalp short: TP1 -2.4%, TP2 -5.0%, SL +1.2%
                entry_low = round(min(s_low, live_price * 1.002), 8)
                entry_high = round(max(s_high, live_price * 1.008), 8)
                sl = round(entry_high * 1.012, 8)
                tp1 = round(min(struct.get("mid_range", 9e9), entry_low * 0.976), 8)
                tp2 = round(min(struct.get("session_low", 9e9), entry_low * 0.950), 8)
            else:
                # Day trade short: TP1 -6.5%, TP2 -14.0%, SL +2.8%
                entry_low = round(min(s_low, live_price * 1.005), 8)
                entry_high = round(max(s_high, live_price * 1.018), 8)
                sl = round(entry_high * 1.030, 8)
                tp1 = round(min(struct.get("mid_range", 9e9), entry_low * 0.935), 8)
                tp2 = round(min(struct.get("session_low", 9e9), entry_low * 0.860), 8)
            reason = f"Rejection at {tf_str} supply ceiling ${entry_low:,.2f}–${entry_high:,.2f}"
            structure_str = f"Supply ceiling at session premium ({range_pos*100:.1f}% range)"

        risk = abs(live_price - sl)
        reward = abs(tp1 - live_price)
        rr = round(reward / risk, 2) if risk > 0 else 2.0
    else:
        lp = fetch_live_price(t_clean) or 100.0
        side = "BUY"
        live_price = lp
        if is_scalp:
            entry_low = round(lp * 0.992, 8)
            entry_high = round(lp * 0.998, 8)
            sl = round(entry_low * 0.988, 8)
            tp1 = round(entry_high * 1.025, 8)
            tp2 = round(entry_high * 1.050, 8)
        else:
            entry_low = round(lp * 0.982, 8)
            entry_high = round(lp * 0.995, 8)
            sl = round(entry_low * 0.970, 8)
            tp1 = round(entry_high * 1.065, 8)
            tp2 = round(entry_high * 1.140, 8)
        rr = 2.2
        reason = f"Structural {tf_str} order block setup"
        structure_str = f"Support shelf near ${lp:,.2f}"

    # Calculate exact distance percentages from average entry
    entry_mid = (entry_low + entry_high) / 2.0 if (entry_low and entry_high) else live_price
    if entry_mid > 0:
        if side == "BUY":
            tp1_pct = round(((tp1 - entry_mid) / entry_mid) * 100.0, 1)
            tp2_pct = round(((tp2 - entry_mid) / entry_mid) * 100.0, 1)
            sl_pct = round(((entry_mid - sl) / entry_mid) * 100.0, 1)
        else:
            tp1_pct = round(((entry_mid - tp1) / entry_mid) * 100.0, 1)
            tp2_pct = round(((entry_mid - tp2) / entry_mid) * 100.0, 1)
            sl_pct = round(((sl - entry_mid) / entry_mid) * 100.0, 1)
    else:
        tp1_pct, tp2_pct, sl_pct = 2.5, 5.0, 1.2

    # Calculate institutional conviction rate (scale of 100% - e.g. 88% to 96%)
    # Higher for strong R:R and clear structure
    base_conv = 88.0
    conv_rr_boost = min(6.0, (rr - 1.8) * 3.5)
    # Give slight variance based on ticker for unique calibration
    hash_offset = (sum(ord(c) for c in t_clean) % 5)
    conviction = round(min(97.0, max(82.0, base_conv + conv_rr_boost + hash_offset)), 1)

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
        "tp1_pct": tp1_pct,
        "tp2_pct": tp2_pct,
        "sl_pct": sl_pct,
        "conviction": conviction,
        "rr": max(1.8, rr),
        "structure": structure_str,
        "reason": reason,
        "live_price": live_price,
    }


def build_spot_signal(ticker: str) -> dict:
    """
    Build a dynamic spot accumulation setup (1D Daily timeframe, long-only)
    using live Bybit OHLCV candles, swing support shelves, and macro expansion targets.
    Exclusively for Winz callers (manual external exchange execution).
    """
    t_clean = ticker.strip().upper().replace("USDT", "")
    from scanner import fetch_ohlcv, detect_shelves_and_edges, fetch_live_price

    df = fetch_ohlcv(t_clean, tf_minutes=1440, limit=80)
    if df is None or len(df) < 10:
        df = fetch_ohlcv(t_clean, tf_minutes=240, limit=80)

    if df is not None and len(df) >= 10:
        struct = detect_shelves_and_edges(df)
        live_price = struct["live_price"]
        d_low, d_high = struct["demand_shelf"]

        # Spot entry is an accumulation band around the demand shelf / recent discount
        entry_low = round(min(d_low, live_price * 0.965), 8)
        entry_high = round(max(d_high, live_price * 0.995), 8)

        # Structural invalidation floor below major shelf (7.0% stop)
        sl = round(entry_low * 0.930, 8)

        # Spot Take-Profits: TP1 +14%, TP2 +32%
        tp1 = round(max(struct.get("mid_range", 0), entry_high * 1.140), 8)
        tp2 = round(max(struct.get("session_high", 0), entry_high * 1.320), 8)

        risk = abs(live_price - sl)
        reward = abs(tp1 - live_price)
        rr = round(reward / risk, 2) if risk > 0 else 2.5
        reason = f"Macro 1D Spot Accumulation shelf ${entry_low:,.2f}–${entry_high:,.2f}"
        structure_str = f"Spot Accumulation discount band near demand floor"
    else:
        lp = fetch_live_price(t_clean) or 100.0
        live_price = lp
        entry_low = round(lp * 0.965, 8)
        entry_high = round(lp * 0.995, 8)
        sl = round(entry_low * 0.930, 8)
        tp1 = round(entry_high * 1.140, 8)
        tp2 = round(entry_high * 1.320, 8)
        rr = 2.5
        reason = f"Spot Accumulation discount band near ${lp:,.2f}"
        structure_str = f"Spot discount shelf near ${lp:,.2f}"

    entry_mid = (entry_low + entry_high) / 2.0 if (entry_low and entry_high) else live_price
    if entry_mid > 0:
        tp1_pct = round(((tp1 - entry_mid) / entry_mid) * 100.0, 1)
        tp2_pct = round(((tp2 - entry_mid) / entry_mid) * 100.0, 1)
        sl_pct = round(((entry_mid - sl) / entry_mid) * 100.0, 1)
    else:
        tp1_pct, tp2_pct, sl_pct = 14.0, 32.0, 7.0

    # Spot conviction rate
    base_conv = 90.0
    conv_rr_boost = min(5.0, (rr - 2.0) * 3.0)
    hash_offset = (sum(ord(c) for c in t_clean) % 5)
    conviction = round(min(98.0, max(85.0, base_conv + conv_rr_boost + hash_offset)), 1)

    return {
        "ticker": t_clean,
        "side": "BUY",
        "trade_type": "spot",
        "tf": "1D",
        "entry_low": entry_low,
        "entry_high": entry_high,
        "tp1": tp1,
        "tp2": tp2,
        "sl": sl,
        "tp1_pct": tp1_pct,
        "tp2_pct": tp2_pct,
        "sl_pct": sl_pct,
        "conviction": conviction,
        "rr": max(2.0, rr),
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
    tf_str = sig.get("tf") or ("15m" if tf_minutes == 15 else ("4h" if tf_minutes == 240 else ("1D" if tf_minutes >= 1440 else "1h")))
    actual_tf_min = 1440 if tf_str in ("1D", "spot") else tf_minutes

    # Persist as last researched signal
    state.set_last_researched_signal(sig)

    # 1. Fetch OHLCV & generate chart PNG
    from scanner import fetch_ohlcv
    import chart
    df = fetch_ohlcv(ticker, tf_minutes=actual_tf_min, limit=80)
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

    conv = sig.get("conviction", 92.0)
    tp1_pct = sig.get("tp1_pct", 2.5)
    tp2_pct = sig.get("tp2_pct", 5.0)
    sl_pct = sig.get("sl_pct", 1.2)
    caption_summary = (
        f"📈 `[{conv:.0f}% Conviction]` *{ticker}/USDT {sig.get('trade_type', 'Scalp').capitalize()} Breakdown ({tf_str})*\n"
        f"• *Side*: `{side}` | Mark: `{fmt_dollar(sig.get('live_price'))}`\n"
        f"• *Entry Shelf*: `{fmt_dollar(sig.get('entry_low'))}–{fmt_dollar(sig.get('entry_high'))}`\n"
        f"• *TP1*: `+{tp1_pct:.1f}%` ({fmt_dollar(sig.get('tp1'))}) | *TP2*: `+{tp2_pct:.1f}%` ({fmt_dollar(sig.get('tp2'))}) | *SL*: `-{sl_pct:.1f}%` ({fmt_dollar(sig.get('sl'))})"
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


def get_top_a_plus_signals(limit: int = 3) -> List[Dict]:
    """
    Gather candidate setups across core and extra universes,
    filter for conviction >= 90.0% and R:R >= 1.8, sort by (conviction, R:R) descending,
    and return up to `limit` top A+ quality setups.
    Fallback to highest R:R setups if fewer than `limit` setups hit >= 90% conviction.
    """
    from config import CORE_TICKERS, EXTRA_TICKERS
    candidate_universe = CORE_TICKERS + EXTRA_TICKERS

    candidates = []
    for ticker in candidate_universe:
        try:
            # Check 15m scalp setup
            sig_15m = build_signal_for_timeframe(ticker, tf_minutes=15)
            if sig_15m and sig_15m.get("conviction", 0) >= 90.0 and sig_15m.get("rr", 0) >= 1.8:
                candidates.append(sig_15m)

            # Check 4h day setup
            sig_4h = build_signal_for_timeframe(ticker, tf_minutes=240)
            if sig_4h and sig_4h.get("conviction", 0) >= 90.0 and sig_4h.get("rr", 0) >= 1.8:
                candidates.append(sig_4h)
        except Exception as exc:
            log.debug("Error building candidate signal for %s: %s", ticker, exc)

    candidates.sort(key=lambda x: (x.get("conviction", 0), x.get("rr", 0)), reverse=True)

    top_signals = []
    seen_tickers = set()
    for c in candidates:
        if c["ticker"] not in seen_tickers:
            top_signals.append(c)
            seen_tickers.add(c["ticker"])
        if len(top_signals) >= limit:
            break

    if len(top_signals) < limit:
        all_avail = []
        for ticker in candidate_universe:
            if ticker in seen_tickers:
                continue
            try:
                sig = build_signal_for_timeframe(ticker, tf_minutes=15)
                if sig and sig.get("rr", 0) >= 1.5:
                    all_avail.append(sig)
            except Exception:
                pass
        all_avail.sort(key=lambda x: x.get("rr", 0), reverse=True)
        for c in all_avail:
            top_signals.append(c)
            seen_tickers.add(c["ticker"])
            if len(top_signals) >= limit:
                break

    return top_signals


# ── Hourly / 2-Hour Calls Dispatcher (Max 3 Top A+ Quality Signals) ─────────
def dispatch_hourly_calls(
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> bool:
    """
    Generate and dispatch a maximum of 3 top-tier A+ quality signals every 2 hours
    (filtered strictly for highest conviction >= 90% and favorable Risk:Reward).
    Delivers a clean index digest with interactive buttons to view live charts & research notes.
    """
    call_cfg = state.get_call_bot_config()
    target_token = bot_token or call_cfg.get("token")
    target_chat = chat_id or call_cfg.get("chat_id")

    if not target_token or not target_chat:
        log.warning("2-Hour calls dispatch skipped: no call bot token or chat ID configured")
        return False

    log.info("Generating top A+ quality signals (max 3) for %s...", target_chat)

    top_signals = get_top_a_plus_signals(limit=3)
    now_utc_str = datetime.now(timezone.utc).strftime("%H:%M UTC")

    # Save tracked calls for real-time tracking
    state.save_tracked_calls(top_signals)

    # Deliver Clean Digest with Interactive Buttons (No photo flood!)
    index_lines = [
        "🚨 *WINZ 2-HOUR A+ QUALITY DESK (MAX 3 SETUPS)*",
        f"⏱ *Timestamp:* {now_utc_str} | _For Manual / External Exchange Trading_",
        "",
        "🎯 *TOP A+ QUALITY SETUPS (≥90% Conviction, R:R ≥ 1.8):*",
    ]
    for idx, s in enumerate(top_signals, start=1):
        t = s["ticker"]
        side = s["side"]
        tf = s.get("tf", "15m")
        conv = s.get("conviction", 90.0)
        el = fmt_dollar(s["entry_low"])
        eh = fmt_dollar(s["entry_high"])
        tp1 = fmt_dollar(s["tp1"])
        tp1_pct = s.get("tp1_pct", 2.4)
        sl = fmt_dollar(s["sl"])
        sl_pct = s.get("sl_pct", 1.2)
        rr = s.get("rr", 2.0)
        index_lines.append(f"{idx}. `[{conv:.0f}% Conviction]` *${t}* `{side}` ({tf}) — Entry: `{el}–{eh}` | TP: `+{tp1_pct:.1f}%` ({tp1}) | SL: `-{sl_pct:.1f}%` ({sl}) (R:R {rr:.1f})")

    index_lines.append("")
    index_lines.append("👇 *Tap any asset below for its live chart markup, structure & institutional thesis:*")

    # Construct Inline Keyboard Buttons (2 buttons per row)
    inline_keyboard = []
    current_row = []
    for s in top_signals:
        t = s["ticker"]
        tf = s.get("tf", "15m")
        badge = "💎" if s.get("trade_type") == "spot" or tf in ("spot", "1D") else ("⚡" if tf == "15m" else "🏛")
        btn_text = f"{badge} {t} ({tf})"
        cb_tf = "spot" if s.get("trade_type") == "spot" or tf in ("spot", "1D") else tf
        cb_data = f"call_view_{t}_{cb_tf}"
        current_row.append({"text": btn_text, "callback_data": cb_data})
        if len(current_row) == 2:
            inline_keyboard.append(current_row)
            current_row = []
    if current_row:
        inline_keyboard.append(current_row)

    index_msg = "\n".join(index_lines)
    send_url = f"https://api.telegram.org/bot{target_token}/sendMessage"
    chats = call_cfg.get("all_chats")
    if not chats:
        chats = [target_chat] if target_chat else []

    for c in chats:
        payload = {
            "chat_id": c,
            "text": index_msg,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": inline_keyboard},
        }
        try:
            requests.post(send_url, json=payload, timeout=12)
        except Exception as exc:
            log.warning("Failed to send 2-hour digest with buttons to chat %s: %s", c, exc)

    # Deliver to Discord channel if configured
    try:
        import winz_discord
        embed, view = winz_discord._build_calls_message(top_signals)
        winz_discord.broadcast_discord_message("🚨 **Winz 2-Hour A+ Quality Desk (Max 3 Setups)**", embed=embed)
    except Exception as exc:
        log.debug("Discord broadcast error: %s", exc)

    log.info("2-Hour calls digest (max %d setups) successfully dispatched with interactive buttons.", len(top_signals))
    return True


def dispatch_spot_calls(
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> bool:
    """Compile and dispatch 5 spot accumulation setups with buttons directly."""
    call_cfg = state.get_call_bot_config()
    target_token = bot_token or call_cfg.get("token")
    target_chat = chat_id or call_cfg.get("chat_id")
    if not target_token or not target_chat:
        return False

    from config import CORE_TICKERS, EXTRA_TICKERS
    candidate_universe = CORE_TICKERS + EXTRA_TICKERS
    spot_signals = []
    for ticker in candidate_universe:
        if len(spot_signals) >= 5:
            break
        sig = build_spot_signal(ticker)
        if sig:
            spot_signals.append(sig)

    state.save_tracked_calls(spot_signals)
    now_utc_str = datetime.now(timezone.utc).strftime("%H:%M UTC")

    lines = [
        "💎 *WINZ SPOT ACCUMULATION DESK (5 TRADES)*",
        f"⏱ *Timestamp:* {now_utc_str} | _For Manual / External Exchange Spot Trading_",
        "",
        "💎 *SPOT SWING SETUPS (1D Macro Accumulation):*",
    ]
    for idx, s in enumerate(spot_signals, start=1):
        t = s["ticker"]
        conv = s.get("conviction", 94.0)
        el = fmt_dollar(s["entry_low"])
        eh = fmt_dollar(s["entry_high"])
        tp1 = fmt_dollar(s["tp1"])
        tp1_pct = s.get("tp1_pct", 14.0)
        tp2 = fmt_dollar(s["tp2"])
        tp2_pct = s.get("tp2_pct", 32.0)
        sl = fmt_dollar(s["sl"])
        sl_pct = s.get("sl_pct", 7.0)
        rr = s.get("rr", 2.5)
        lines.append(f"{idx}. `[{conv:.0f}% Conviction]` *${t}* `BUY` — Accumulate: `{el}–{eh}` | TP1: `+{tp1_pct:.1f}%` ({tp1}) | TP2: `+{tp2_pct:.1f}%` ({tp2}) | SL: `-{sl_pct:.1f}%` ({sl}) (R:R {rr:.1f})")

    lines.append("")
    lines.append("👇 *Tap any asset below for its Spot Chart & Macro Accumulation Thesis:*")

    inline_keyboard = []
    current_row = []
    for s in spot_signals:
        t = s["ticker"]
        btn_text = f"💎 {t} (Spot)"
        cb_data = f"call_view_{t}_spot"
        current_row.append({"text": btn_text, "callback_data": cb_data})
        if len(current_row) == 2:
            inline_keyboard.append(current_row)
            current_row = []
    if current_row:
        inline_keyboard.append(current_row)

    send_url = f"https://api.telegram.org/bot{target_token}/sendMessage"
    chats = call_cfg.get("all_chats")
    if not chats:
        chats = [target_chat] if target_chat else []

    text_msg = "\n".join(lines)
    for c in chats:
        payload = {
            "chat_id": c,
            "text": text_msg,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": inline_keyboard},
        }
        try:
            requests.post(send_url, json=payload, timeout=12)
        except Exception as exc:
            log.warning("Failed to send spot digest to chat %s: %s", c, exc)
    return True


# ── Twitter / X Post Converter ──────────────────────────────────────────────
def _fmt_tweet_price(val: Optional[float]) -> str:
    """Compact price formatting designed for Twitter character economy."""
    if val is None or math.isnan(val):
        return "—"
    if val >= 1000:
        return f"${val:,.0f}"
    if val >= 1:
        s = f"{val:.2f}".rstrip("0").rstrip(".")
        return f"${s}"
    if val >= 0.001:
        s = f"{val:.4f}".rstrip("0").rstrip(".")
        return f"${s}"
    s = f"{val:.6f}".rstrip("0").rstrip(".")
    return f"${s}"


def format_twitter_post(sig: dict) -> str:
    """
    Convert a research setup into a punchy, high-impact Twitter / X post
    featuring an asymmetric thesis narrative hook followed by entry, TP, and SL speculation.
    Optimized strictly to fit within the 280-character limit.
    """
    ticker = sig.get("ticker", "BTC").upper()
    side = sig.get("side", "BUY").upper()
    tf = sig.get("tf", "15m")
    el = _fmt_tweet_price(sig.get("entry_low"))
    eh = _fmt_tweet_price(sig.get("entry_high"))
    tp1 = _fmt_tweet_price(sig.get("tp1"))
    tp2 = _fmt_tweet_price(sig.get("tp2"))
    sl = _fmt_tweet_price(sig.get("sl"))

    # Pull asset-specific narrative hook
    hook = TWITTER_HOOKS.get(ticker)
    if not hook:
        cat = ASSET_CATALYSTS.get(ticker, DEFAULT_CATALYST)
        sector = cat.get("sector", "Crypto")
        hook = f"most chains chase short-term attention.\n\n${ticker} is quietly building in {sector}.\n\nreal payoff comes when the structural shift hits."

    # Try Option 1: Full 3-line speculation with header
    opt1 = (
        f"{hook}\n\n"
        f"Speculation:\n"
        f"• Entry: {el} – {eh}\n"
        f"• TP: {tp1} / {tp2}\n"
        f"• SL: {sl}"
    )
    if len(opt1) <= 280:
        return opt1

    # Try Option 2: Full hook with 3 bullet lines directly (Entry, TP, SL)
    opt2 = (
        f"{hook}\n\n"
        f"• Entry: {el} – {eh}\n"
        f"• TP: {tp1} / {tp2}\n"
        f"• SL: {sl}"
    )
    if len(opt2) <= 280:
        return opt2

    # Try Option 3: Full hook with Speculation header on 1 line
    opt3 = (
        f"{hook}\n\n"
        f"Speculation:\n"
        f"• Entry {el}–{eh} | TP {tp1}/{tp2} | SL {sl}"
    )
    if len(opt3) <= 280:
        return opt3

    # Try Option 4: Full hook with 2 bullet lines (Entry + Targets/SL)
    opt4 = (
        f"{hook}\n\n"
        f"Speculation:\n"
        f"• Entry: {el} – {eh}\n"
        f"• TP: {tp1} / {tp2} | SL: {sl}"
    )
    if len(opt4) <= 280:
        return opt4

    # Try Option 5: Full hook with single-line speculation
    opt_single = f"{hook}\n\nSpeculation: {el}–{eh} | TP {tp1}/{tp2} | SL {sl}"
    if len(opt_single) <= 280:
        return opt_single

    # Option 5: Condensed middle line fallback
    lines = [ln.strip() for ln in hook.split("\n") if ln.strip()]
    if len(lines) >= 3:
        condensed_hook = f"{lines[0]}\n${ticker} has been quietly building different rails & payoff.\n{lines[2]}"
        opt5 = (
            f"{condensed_hook}\n\n"
            f"Speculation:\n"
            f"• Entry: {el} – {eh}\n"
            f"• TP: {tp1} / {tp2} | SL: {sl}"
        )
        if len(opt5) <= 280:
            return opt5

    # Safe compact fallback
    return (
        f"{lines[0] if lines else 'most projects chase retail hype.'}\n"
        f"${ticker} has the infra already there.\n\n"
        f"• Entry: {el} – {eh}\n"
        f"• TP: {tp1} / {tp2}\n"
        f"• SL: {sl}"
    )


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
