"""
config.py — Central configuration for Trade Desk Bot.
Loads .env, exposes typed settings, defines ticker universe,
leverage tiers, and per-rule constants.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Literal, Optional

from dotenv import load_dotenv

# ── Load environment ────────────────────────────────────────────────────────
_ENV_PATH = Path(__file__).parent / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)
else:
    load_dotenv()  # fall back to system env


# ── Typed config ────────────────────────────────────────────────────────────
@dataclass
class Config:
    # Bybit live
    bybit_api_key: str = field(default_factory=lambda: os.getenv("BYBIT_API_KEY") or os.getenv("BINANCE_API_KEY", ""))
    bybit_api_secret: str = field(default_factory=lambda: os.getenv("BYBIT_SECRET_KEY") or os.getenv("BYBIT_API_SECRET") or os.getenv("BINANCE_API_SECRET", ""))

    # Bybit testnet
    bybit_testnet_key: str = field(default_factory=lambda: os.getenv("BYBIT_TESTNET_API_KEY") or os.getenv("BINANCE_TESTNET_API_KEY", ""))
    bybit_testnet_secret: str = field(default_factory=lambda: os.getenv("BYBIT_TESTNET_API_SECRET") or os.getenv("BINANCE_TESTNET_API_SECRET", ""))

    # Mode
    trade_mode: Literal["demo", "live"] = field(
        default_factory=lambda: os.getenv("TRADE_MODE", "demo").lower()  # type: ignore[return-value]
    )

    # Telegram
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))

    # Risk
    risk_per_trade: float = field(default_factory=lambda: float(os.getenv("RISK_PER_TRADE", "0.005")))
    paper_equity: float = field(default_factory=lambda: float(os.getenv("PAPER_EQUITY", "10000")))

    # Scanner
    scalp_tf: int = field(default_factory=lambda: int(os.getenv("SCALP_TF", "15")))
    day_tf: int = field(default_factory=lambda: int(os.getenv("DAY_TF", "4")))
    scan_interval_scalp: int = field(default_factory=lambda: int(os.getenv("SCAN_INTERVAL_SCALP", "300")))
    scan_interval_day: int = field(default_factory=lambda: int(os.getenv("SCAN_INTERVAL_DAY", "1800")))
    desk_summary_interval: int = field(default_factory=lambda: int(os.getenv("DESK_SUMMARY_INTERVAL", "60")))

    # Leverage Override
    max_leverage_cap: Optional[int] = field(
        default_factory=lambda: int(os.getenv("MAX_LEVERAGE_CAP")) if os.getenv("MAX_LEVERAGE_CAP") else None
    )

    # Logging
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    @property
    def is_live(self) -> bool:
        return self.trade_mode == "live"

    @property
    def active_api_key(self) -> str:
        return self.bybit_api_key if self.is_live else self.bybit_testnet_key

    @property
    def active_api_secret(self) -> str:
        return self.bybit_api_secret if self.is_live else self.bybit_testnet_secret


# ── Singleton ───────────────────────────────────────────────────────────────
cfg = Config()


# ── Ticker universe ─────────────────────────────────────────────────────────
# Core 24 — in mandated desk order
CORE_TICKERS: List[str] = [
    "BTC", "ETH", "SOL", "XRP", "BNB",
    "DOGE", "ADA", "LINK", "AVAX", "SUI",
    "HYPE", "LTC", "AAVE", "ZEC", "UNI",
    "BCH", "TRX", "XLM", "TAO", "ONDO",
    "PEPE", "ENA", "HBAR", "NEAR",
]

# Extras — standalone, same rules, WAIT if illiquid
EXTRA_TICKERS: List[str] = [
    "ARB", "WLD", "STRK", "APT", "SEI",
    "INJ", "OP", "DOT", "ATOM", "FIL",
    "RENDER", "FET",
]

ALL_TICKERS: List[str] = CORE_TICKERS + EXTRA_TICKERS

# Bybit symbol → USDT pair
def symbol(ticker: str) -> str:
    return f"{ticker}USDT"

# TradingView URL
def tv_url(ticker: str, tf: str = "60") -> str:
    sym = f"BYBIT:{ticker}USDT"
    return f"https://www.tradingview.com/chart/?symbol={sym}&interval={tf}"


# ── Leverage tiers ───────────────────────────────────────────────────────────
# Format: ticker → (scalp_max, day_max, hard_cap)
# scalp_max / day_max: suggested range top
# hard_cap: absolute max we will ever set
LEVERAGE_MAP: Dict[str, Dict[str, int]] = {}

_TIER_A = {"scalp_suggest": 5, "day_suggest": 4, "hard_cap": 7}   # BTC ETH SOL XRP BNB
_TIER_B = {"scalp_suggest": 4, "day_suggest": 3, "hard_cap": 4}   # ZEC UNI BCH TRX XLM HYPE LINK AVAX SUI LTC AAVE ADA DOGE
_TIER_C = {"scalp_suggest": 3, "day_suggest": 2, "hard_cap": 3}   # TAO ONDO PEPE ENA HBAR NEAR (and extras if small-cap)

for t in ["BTC", "ETH", "SOL", "XRP", "BNB"]:
    LEVERAGE_MAP[t] = _TIER_A

for t in ["ZEC", "UNI", "BCH", "TRX", "XLM", "HYPE", "LINK", "AVAX", "SUI", "LTC", "AAVE", "ADA", "DOGE"]:
    LEVERAGE_MAP[t] = _TIER_B

for t in ["TAO", "ONDO", "PEPE", "ENA", "HBAR", "NEAR"]:
    LEVERAGE_MAP[t] = _TIER_C

# Default for extras — treat as tier C (conservative)
for t in EXTRA_TICKERS:
    LEVERAGE_MAP.setdefault(t, _TIER_C)


def get_leverage(ticker: str, scalp: bool = True) -> int:
    """Return suggested leverage for a ticker."""
    tier = LEVERAGE_MAP.get(ticker, _TIER_C)
    key = "scalp_suggest" if scalp else "day_suggest"
    lev = tier[key]
    if cfg.max_leverage_cap is not None:
        lev = min(lev, cfg.max_leverage_cap)
    return lev


def get_hard_cap(ticker: str) -> int:
    """Return absolute max leverage for a ticker."""
    cap = LEVERAGE_MAP.get(ticker, _TIER_C)["hard_cap"]
    if cfg.max_leverage_cap is not None:
        cap = min(cap, cfg.max_leverage_cap)
    return cap


# ── Tickers that must NEVER be market-ordered ────────────────────────────────
NO_MARKET_TICKERS: List[str] = ["PEPE", "TAO", "ENA", "HBAR", "NEAR"]

# ── Tickers that are PERP-only for sells (no spot short) ─────────────────────
PERP_ONLY_SELL: List[str] = ALL_TICKERS  # default — user must hold coin for spot sell

# ── Minimum liquidity threshold for extras (24h quote vol, USDT) ─────────────
MIN_LIQUIDITY_USDT = 50_000_000  # 50 M USDT daily volume

# ── Structure analysis constants ─────────────────────────────────────────────
SWING_LOOKBACK = 20        # candles each side for swing detection
DEMAND_ZONE_PCT = 0.003    # 0.3% tolerance for zone matching
VOLUME_SPIKE_MULT = 1.5    # volume spike = N × 20-period avg
TREND_LOOKBACK = 5         # consecutive HH/LL candles to classify trend

# ── R:R minimums ─────────────────────────────────────────────────────────────
MIN_RR = 1.5               # minimum risk:reward to emit a BUY/SELL (not WAIT)
