"""
config.py — Central configuration for Trade Desk Bot.
Loads .env, defines ticker universe in mandated desk order, leverage tiers,
and risk parameters (0.5% balance risk per trade).
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
    load_dotenv()


# ── Typed config ────────────────────────────────────────────────────────────
@dataclass
class Config:
    # Bybit Live
    bybit_api_key: str = field(default_factory=lambda: os.getenv("BYBIT_API_KEY", ""))
    bybit_api_secret: str = field(
        default_factory=lambda: os.getenv("BYBIT_SECRET_KEY") or os.getenv("BYBIT_API_SECRET", "")
    )

    # Bybit Testnet / Demo
    bybit_testnet_key: str = field(default_factory=lambda: os.getenv("BYBIT_TESTNET_API_KEY", ""))
    bybit_testnet_secret: str = field(default_factory=lambda: os.getenv("BYBIT_TESTNET_API_SECRET", ""))

    # Mode: "demo" (default) or "live"
    trade_mode: Literal["demo", "live"] = field(
        default_factory=lambda: os.getenv("TRADE_MODE", "demo").lower()  # type: ignore[return-value]
    )

    # Demo environment: "paper" (local broker with live Bybit books), "testnet", or "demo"
    demo_env: Literal["paper", "testnet", "demo"] = field(
        default_factory=lambda: os.getenv("DEMO_ENV", "paper").lower()  # type: ignore[return-value]
    )

    # Telegram alerts (text only, NO IMAGES)
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))

    # Dedicated Secondary Feedback Bot / Channel (Optional)
    feedback_bot_token: str = field(default_factory=lambda: os.getenv("FEEDBACK_BOT_TOKEN", ""))
    feedback_chat_id: str = field(default_factory=lambda: os.getenv("FEEDBACK_CHAT_ID", ""))

    # Risk: exactly 0.5% of balance per trade
    risk_per_trade: float = field(default_factory=lambda: float(os.getenv("RISK_PER_TRADE", "0.005")))
    paper_equity: float = field(default_factory=lambda: float(os.getenv("PAPER_EQUITY", "10000")))

    # Loss Mitigation & Exposure Caps
    max_concurrent_positions: int = field(default_factory=lambda: int(os.getenv("MAX_CONCURRENT_POSITIONS", "6")))
    min_free_margin_pct: float = field(default_factory=lambda: float(os.getenv("MIN_FREE_MARGIN_PCT", "0.30")))
    max_position_notional: float = field(default_factory=lambda: float(os.getenv("MAX_POSITION_NOTIONAL", "25000.0")))

    # Scanner settings
    scalp_tf: int = field(default_factory=lambda: int(os.getenv("SCALP_TF", "15")))
    day_tf: int = field(default_factory=lambda: int(os.getenv("DAY_TF", "4")))
    scan_interval_scalp: int = field(default_factory=lambda: int(os.getenv("SCAN_INTERVAL_SCALP", "300")))
    scan_interval_day: int = field(default_factory=lambda: int(os.getenv("SCAN_INTERVAL_DAY", "1800")))
    desk_summary_interval: int = field(default_factory=lambda: int(os.getenv("DESK_SUMMARY_INTERVAL", "60")))

    # Max leverage cap override (if any)
    max_leverage_cap: Optional[int] = field(
        default_factory=lambda: int(os.getenv("MAX_LEVERAGE_CAP")) if os.getenv("MAX_LEVERAGE_CAP") else None
    )

    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    @property
    def is_live(self) -> bool:
        return self.trade_mode == "live"

    @property
    def active_api_key(self) -> str:
        if self.is_live:
            return self.bybit_api_key
        # Check testnet / demo / general bybit key
        for k in [self.bybit_testnet_key, os.getenv("BYBIT_DEMO_API_KEY", ""), self.bybit_api_key]:
            if k and not k.startswith("your_"):
                return k.strip()
        return ""

    @property
    def active_api_secret(self) -> str:
        if self.is_live:
            return self.bybit_api_secret
        for s in [self.bybit_testnet_secret, os.getenv("BYBIT_DEMO_API_SECRET", ""), os.getenv("BYBIT_DEMO_SECRET", ""), self.bybit_api_secret]:
            if s and not s.startswith("your_"):
                return s.strip()
        return ""

    @property
    def effective_demo_env(self) -> str:
        explicit = os.getenv("DEMO_ENV", "").lower()
        if explicit in ("demo", "testnet", "paper"):
            return explicit
        if self.active_api_key and self.active_api_secret:
            return "demo"
        return "paper"


# ── Singleton ───────────────────────────────────────────────────────────────
cfg = Config()


# ── Ticker universe ─────────────────────────────────────────────────────────
# Mandated Core 24 order every run:
CORE_TICKERS: List[str] = [
    "BTC", "ETH", "SOL", "XRP", "BNB",
    "DOGE", "ADA", "LINK", "AVAX", "SUI",
    "HYPE", "LTC", "AAVE", "ZEC", "UNI",
    "BCH", "TRX", "XLM", "TAO", "ONDO",
    "PEPE", "ENA", "HBAR", "NEAR",
]

# Extras if liquid (24h quote volume >= 50M USDT):
EXTRA_TICKERS: List[str] = [
    "ARB", "WLD", "STRK", "APT", "SEI",
    "INJ", "OP", "DOT", "ATOM", "FIL",
    "RENDER", "FET",
]

ALL_TICKERS: List[str] = CORE_TICKERS + EXTRA_TICKERS

# Market order allowed ONLY on Tier A if mark inside Entry and slip < 0.15%
MARKET_ALLOWED_TICKERS: List[str] = ["BTC", "ETH", "SOL", "XRP", "BNB"]

# Hard ban: never market PEPE, TAO, ENA, HBAR, NEAR or a vertical 15m candle
NO_MARKET_TICKERS: List[str] = ["PEPE", "TAO", "ENA", "HBAR", "NEAR"]


# ── Exchange & Chart URL Helpers ────────────────────────────────────────────
def bybit_linear_symbol(ticker: str) -> str:
    """Map ticker to Bybit USDT Linear Perpetual symbol."""
    t = ticker.upper()
    if t == "PEPE":
        return "1000PEPEUSDT"
    return f"{t}USDT"


def bybit_spot_symbol(ticker: str) -> str:
    """Map ticker to Bybit Spot symbol."""
    return f"{ticker.upper()}USDT"


def tv_url(ticker: str, tf: str = "15") -> str:
    """
    Mandated TradingView chart URL:
    https://www.tradingview.com/chart/?symbol=BINANCE:TICKERUSDT (15m scalp / 1h or 4h day).
    PEPE = BINANCE:PEPEUSDT.
    """
    t = ticker.upper()
    if t == "PEPE":
        return "https://www.tradingview.com/chart/?symbol=BINANCE:PEPEUSDT"
    return f"https://www.tradingview.com/chart/?symbol=BINANCE:{t}USDT"


# ── Leverage tiers ───────────────────────────────────────────────────────────
# BTC ETH SOL XRP BNB: scalp 3x-5x / day 2x-4x, max 7x
# Mid liquid: 2x-4x
# TAO ONDO PEPE ENA HBAR NEAR: 2x-3x
LEVERAGE_MAP: Dict[str, Dict[str, int]] = {}

_TIER_A = {"scalp_suggest": 5, "day_suggest": 4, "hard_cap": 7}
_TIER_B = {"scalp_suggest": 4, "day_suggest": 3, "hard_cap": 4}
_TIER_C = {"scalp_suggest": 3, "day_suggest": 2, "hard_cap": 3}

for t in ["BTC", "ETH", "SOL", "XRP", "BNB"]:
    LEVERAGE_MAP[t] = _TIER_A

for t in ["DOGE", "ADA", "LINK", "AVAX", "SUI", "HYPE", "LTC", "AAVE", "ZEC", "UNI", "BCH", "TRX", "XLM"]:
    LEVERAGE_MAP[t] = _TIER_B

for t in ["TAO", "ONDO", "PEPE", "ENA", "HBAR", "NEAR"]:
    LEVERAGE_MAP[t] = _TIER_C

for t in EXTRA_TICKERS:
    LEVERAGE_MAP.setdefault(t, _TIER_C)


def get_leverage(ticker: str, scalp: bool = True) -> int:
    """Return suggested leverage for a ticker."""
    tier = LEVERAGE_MAP.get(ticker.upper(), _TIER_C)
    key = "scalp_suggest" if scalp else "day_suggest"
    lev = tier[key]
    if cfg.max_leverage_cap is not None:
        lev = min(lev, cfg.max_leverage_cap)
    return lev


def get_hard_cap(ticker: str) -> int:
    """Return absolute max leverage cap for a ticker."""
    cap = LEVERAGE_MAP.get(ticker.upper(), _TIER_C)["hard_cap"]
    if cfg.max_leverage_cap is not None:
        cap = min(cap, cfg.max_leverage_cap)
    return cap


# ── Structure analysis constants ─────────────────────────────────────────────
SWING_LOOKBACK = 15            # candles for swing shelf detection
MIN_LIQUIDITY_USDT = 50_000_000 # 50 M USDT minimum daily quote volume for extras
MAX_SLIPPAGE_PCT = 0.15        # 0.15% maximum slippage for market orders
MAX_FUNDING_RATE = 0.0005      # 0.05% threshold for extreme funding rate
VERTICAL_CANDLE_BODY_PCT = 0.025 # 2.5% body in 15m classifies as vertical runaway candle
MIN_RR = 1.5                   # minimum 1:1.5 Risk:Reward ratio
