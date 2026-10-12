"""
config.py — Central configuration for Trade Desk Bot.
Loads .env, defines ticker universe in mandated desk order, leverage tiers,
and risk parameters (0.5% balance risk per trade).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple

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

    # Dedicated External Calls Bot (for manual trading calls / non-automated exchanges)
    call_bot_token: str = field(default_factory=lambda: os.getenv("CALL_BOT_TOKEN", ""))
    call_bot_chat_id: str = field(default_factory=lambda: os.getenv("CALL_BOT_CHAT_ID", ""))

    # Discord Bot Integration (Winz)
    discord_bot_token: str = field(default_factory=lambda: os.getenv("DISCORD_BOT_TOKEN", ""))
    discord_channel_id: str = field(default_factory=lambda: os.getenv("DISCORD_CHANNEL_ID", ""))

    # Risk: exactly 0.5% of balance per trade
    risk_per_trade: float = field(default_factory=lambda: float(os.getenv("RISK_PER_TRADE", "0.005")))
    paper_equity: float = field(default_factory=lambda: float(os.getenv("PAPER_EQUITY", "10000")))

    # Loss Mitigation & Exposure Caps
    max_concurrent_positions: int = field(default_factory=lambda: int(os.getenv("MAX_CONCURRENT_POSITIONS", "6")))
    min_free_margin_pct: float = field(default_factory=lambda: float(os.getenv("MIN_FREE_MARGIN_PCT", "0.30")))
    max_position_notional: float = field(default_factory=lambda: float(os.getenv("MAX_POSITION_NOTIONAL", "25000.0")))

    # GitHub Best Practices: Hummingbot, Freqtrade, Passivbot parameters
    max_directional_positions: int = field(default_factory=lambda: int(os.getenv("MAX_DIRECTIONAL_POSITIONS", "3")))
    max_directional_ratio: float = field(default_factory=lambda: float(os.getenv("MAX_DIRECTIONAL_RATIO", "0.65")))
    enable_market_regime_filter: bool = field(default_factory=lambda: os.getenv("ENABLE_MARKET_REGIME_FILTER", "true").lower() in ("true", "1", "yes"))
    enable_micro_grid_entry: bool = field(default_factory=lambda: os.getenv("ENABLE_MICRO_GRID_ENTRY", "true").lower() in ("true", "1", "yes"))
    unstuck_timeout_minutes: int = field(default_factory=lambda: int(os.getenv("UNSTUCK_TIMEOUT_MINUTES", "90")))

    # Institutional Edge Parameters: ATR, Order Flow, OI, 3-Tier Scaleout
    enable_atr_stops: bool = field(default_factory=lambda: os.getenv("ENABLE_ATR_STOPS", "true").lower() in ("true", "1", "yes"))
    atr_multiplier: float = field(default_factory=lambda: float(os.getenv("ATR_MULTIPLIER", "1.2")))
    enable_orderflow_imbalance: bool = field(default_factory=lambda: os.getenv("ENABLE_ORDERFLOW_IMBALANCE", "true").lower() in ("true", "1", "yes"))
    enable_open_interest_filter: bool = field(default_factory=lambda: os.getenv("ENABLE_OPEN_INTEREST_FILTER", "true").lower() in ("true", "1", "yes"))
    enable_three_tier_scaleout: bool = field(default_factory=lambda: os.getenv("ENABLE_THREE_TIER_SCALEOUT", "true").lower() in ("true", "1", "yes"))

    # Precision Risk & Gatekeeper Parameters
    enable_btc_momentum_gate: bool = field(default_factory=lambda: os.getenv("ENABLE_BTC_MOMENTUM_GATE", "true").lower() in ("true", "1", "yes"))
    enable_btc_regime_lock: bool = field(default_factory=lambda: os.getenv("ENABLE_BTC_REGIME_LOCK", "true").lower() in ("true", "1", "yes"))
    enable_strict_4h_confluence: bool = field(default_factory=lambda: os.getenv("ENABLE_STRICT_4H_CONFLUENCE", "true").lower() in ("true", "1", "yes"))
    enable_wick_confirmation: bool = field(default_factory=lambda: os.getenv("ENABLE_WICK_CONFIRMATION", "true").lower() in ("true", "1", "yes"))
    enable_dynamic_kelly_sizing: bool = field(default_factory=lambda: os.getenv("ENABLE_DYNAMIC_KELLY_SIZING", "true").lower() in ("true", "1", "yes"))
    risk_a_plus: float = field(default_factory=lambda: float(os.getenv("RISK_A_PLUS", "0.0070")))
    risk_standard: float = field(default_factory=lambda: float(os.getenv("RISK_STANDARD", "0.0045")))
    risk_weekend_chop: float = field(default_factory=lambda: float(os.getenv("RISK_WEEKEND_CHOP", "0.0030")))
    enable_daily_pruning: bool = field(default_factory=lambda: os.getenv("ENABLE_DAILY_PRUNING", "true").lower() in ("true", "1", "yes"))

    # Global Daily Loss Circuit Breaker: Freeze all trading for 12h if rolling 24h loss exceeds limit
    enable_global_daily_circuit_breaker: bool = field(default_factory=lambda: os.getenv("ENABLE_GLOBAL_DAILY_CIRCUIT_BREAKER", "true").lower() in ("true", "1", "yes"))
    max_daily_loss_usdt: float = field(default_factory=lambda: float(os.getenv("MAX_DAILY_LOSS_USDT", "500.0")))
    global_circuit_breaker_freeze_hours: float = field(default_factory=lambda: float(os.getenv("GLOBAL_CIRCUIT_BREAKER_FREEZE_HOURS", "12.0")))

    # High-Impact Macro Economic Event Blackout (CPI, FOMC, NFP)
    enable_macro_news_blackout: bool = field(default_factory=lambda: os.getenv("ENABLE_MACRO_NEWS_BLACKOUT", "true").lower() in ("true", "1", "yes"))
    macro_blackout_pre_mins: int = field(default_factory=lambda: int(os.getenv("MACRO_BLACKOUT_PRE_MINS", "30")))
    macro_blackout_post_mins: int = field(default_factory=lambda: int(os.getenv("MACRO_BLACKOUT_POST_MINS", "30")))

    # Advanced Precision Upgrades: Sector Baskets, Orderbook Wall Frontrunning, Multi-Step Ratchet Trail
    enable_sector_caps: bool = field(default_factory=lambda: os.getenv("ENABLE_SECTOR_CAPS", "true").lower() in ("true", "1", "yes"))
    max_sector_positions: int = field(default_factory=lambda: int(os.getenv("MAX_SECTOR_POSITIONS", "2")))
    enable_wall_frontrun: bool = field(default_factory=lambda: os.getenv("ENABLE_WALL_FRONTRUN", "true").lower() in ("true", "1", "yes"))
    enable_ratchet_trail: bool = field(default_factory=lambda: os.getenv("ENABLE_RATCHET_TRAIL", "true").lower() in ("true", "1", "yes"))

    # Phase 4 Institutional Edge: Funding Carry Bias, Adaptive Maker Pegging, Climax Early Cut, HWM Dynamic Leverage
    enable_funding_bias: bool = field(default_factory=lambda: os.getenv("ENABLE_FUNDING_BIAS", "true").lower() in ("true", "1", "yes"))
    enable_adaptive_maker_pegging: bool = field(default_factory=lambda: os.getenv("ENABLE_ADAPTIVE_MAKER_PEGGING", "true").lower() in ("true", "1", "yes"))
    enable_climax_early_cut: bool = field(default_factory=lambda: os.getenv("ENABLE_CLIMAX_EARLY_CUT", "true").lower() in ("true", "1", "yes"))
    enable_hwm_drawdown_leverage: bool = field(default_factory=lambda: os.getenv("ENABLE_HWM_DRAWDOWN_LEVERAGE", "true").lower() in ("true", "1", "yes"))

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

# Weekend defensive trading universe (high-liquidity majors only)
WEEKEND_MAJORS_ONLY: List[str] = ["BTC", "ETH", "SOL"]

NO_MARKET_TICKERS: List[str] = ["PEPE", "TAO", "ENA", "HBAR", "NEAR"]


def is_weekend_derisk_window(now_utc: Optional[datetime] = None) -> bool:
    """
    Returns True if current UTC time is inside the Friday Afternoon -> Sunday Night derisking window.
    Window: Friday 14:00 UTC through Sunday 22:00 UTC.
    Covers US cash session close, CME settlement, and illiquid weekend chop.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    weekday = now_utc.weekday()  # Monday=0, Friday=4, Saturday=5, Sunday=6
    hour = now_utc.hour

    if weekday == 4 and hour >= 14:  # Friday starting at 14:00 UTC
        return True
    if weekday == 5:                 # All of Saturday
        return True
    if weekday == 6 and hour < 22:   # Sunday until 22:00 UTC
        return True
    return False


# ── High-Impact Macro Economic Events Schedule (CPI, FOMC, NFP) ────────────
# Times are in UTC.
# U.S. BLS reports (CPI, NFP): 8:30 AM Eastern (12:30 UTC during EDT, 13:30 UTC during EST).
# FOMC rate decisions: 2:00 PM Eastern (18:00 UTC during EST / 19:00 UTC during EDT).
MACRO_EVENTS: List[Dict[str, str]] = [
    # 2026 CPI Releases (8:30 AM ET)
    {"event": "CPI (Jan)", "time": "2026-02-10T13:30:00Z"},
    {"event": "CPI (Feb)", "time": "2026-03-11T12:30:00Z"},
    {"event": "CPI (Mar)", "time": "2026-04-14T12:30:00Z"},
    {"event": "CPI (Apr)", "time": "2026-05-12T12:30:00Z"},
    {"event": "CPI (May)", "time": "2026-06-10T12:30:00Z"},
    {"event": "CPI (Jun)", "time": "2026-07-14T12:30:00Z"},
    {"event": "CPI (Jul)", "time": "2026-08-12T12:30:00Z"},
    {"event": "CPI (Aug)", "time": "2026-09-11T12:30:00Z"},
    {"event": "CPI (Sep)", "time": "2026-10-14T12:30:00Z"},
    {"event": "CPI (Oct)", "time": "2026-11-10T13:30:00Z"},
    {"event": "CPI (Nov)", "time": "2026-12-10T13:30:00Z"},

    # 2026 NFP / Employment Situation (8:30 AM ET)
    {"event": "NFP (Dec)", "time": "2026-01-09T13:30:00Z"},
    {"event": "NFP (Jan)", "time": "2026-02-11T13:30:00Z"},
    {"event": "NFP (Feb)", "time": "2026-03-06T13:30:00Z"},
    {"event": "NFP (Mar)", "time": "2026-04-03T12:30:00Z"},
    {"event": "NFP (Apr)", "time": "2026-05-08T12:30:00Z"},
    {"event": "NFP (May)", "time": "2026-06-05T12:30:00Z"},
    {"event": "NFP (Jun)", "time": "2026-07-02T12:30:00Z"},
    {"event": "NFP (Jul)", "time": "2026-08-07T12:30:00Z"},
    {"event": "NFP (Aug)", "time": "2026-09-04T12:30:00Z"},
    {"event": "NFP (Sep)", "time": "2026-10-02T12:30:00Z"},
    {"event": "NFP (Oct)", "time": "2026-11-06T13:30:00Z"},
    {"event": "NFP (Nov)", "time": "2026-12-04T13:30:00Z"},

    # 2026 FOMC Interest Rate Decisions (2:00 PM ET / 18:00 UTC or 19:00 UTC)
    {"event": "FOMC Rate Decision", "time": "2026-01-28T19:00:00Z"},
    {"event": "FOMC Rate Decision", "time": "2026-03-18T18:00:00Z"},
    {"event": "FOMC Rate Decision", "time": "2026-04-29T18:00:00Z"},
    {"event": "FOMC Rate Decision", "time": "2026-06-17T18:00:00Z"},
    {"event": "FOMC Rate Decision", "time": "2026-07-29T18:00:00Z"},
    {"event": "FOMC Rate Decision", "time": "2026-09-16T18:00:00Z"},
    {"event": "FOMC Rate Decision", "time": "2026-10-28T18:00:00Z"},
    {"event": "FOMC Rate Decision", "time": "2026-12-09T19:00:00Z"},
]


def check_macro_news_blackout(now_utc: Optional[datetime] = None) -> Tuple[bool, str]:
    """
    Checks if the current time falls inside a high-impact macro news blackout window.
    Default window: 30 minutes before release through 30 minutes after release.
    Prevents toxic execution slippage and whipsaw liquidations during FOMC, CPI, and NFP.
    """
    if not cfg.enable_macro_news_blackout:
        return False, "Macro blackout disabled"

    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    pre_delta = timedelta(minutes=cfg.macro_blackout_pre_mins)
    post_delta = timedelta(minutes=cfg.macro_blackout_post_mins)

    for item in MACRO_EVENTS:
        ev_name = item["event"]
        ev_dt = datetime.fromisoformat(item["time"].replace("Z", "+00:00"))
        start_blackout = ev_dt - pre_delta
        end_blackout = ev_dt + post_delta

        if start_blackout <= now_utc <= end_blackout:
            if now_utc < ev_dt:
                mins_left = (ev_dt - now_utc).total_seconds() / 60.0
                return True, f"High-Impact Macro Event Incoming: {ev_name} in {mins_left:.0f} mins. Order entry locked."
            else:
                mins_elapsed = (now_utc - ev_dt).total_seconds() / 60.0
                mins_rem = cfg.macro_blackout_post_mins - mins_elapsed
                return True, f"High-Impact Macro Event Volatility: {ev_name} released {mins_elapsed:.0f} mins ago. Order entry locked for {mins_rem:.0f} mins."

    return False, "Clear of scheduled macro events"

# Volatile high-beta assets prioritized for 3-tier fast profit lock (33% @ TP1, 33% @ TP2, 34% runner)
VOLATILE_TICKERS: List[str] = [
    "DOGE", "ENA", "HYPE", "SUI", "ONDO", "PEPE", "WLD", "SEI", "TAO", "AVAX", "NEAR"
]


def is_volatile_ticker(ticker: str) -> bool:
    """Check if ticker belongs to the volatile / high-beta cohort."""
    return ticker.upper() in VOLATILE_TICKERS

# ── Sector & Correlation Groups ─────────────────────────────────────────────
# Prevents over-concentrated directional exposure into a single narrative/ecosystem
SECTOR_MAP: Dict[str, str] = {
    # Layer 1 / Smart Contract Alternatives
    "SOL": "L1", "SUI": "L1", "APT": "L1", "SEI": "L1", "AVAX": "L1", "NEAR": "L1", "HBAR": "L1", "DOT": "L1", "ATOM": "L1",
    # Memecoins
    "DOGE": "MEME", "PEPE": "MEME",
    # DeFi / Infrastructure
    "AAVE": "DEFI", "UNI": "DEFI", "LINK": "DEFI", "ONDO": "DEFI", "ENA": "DEFI", "INJ": "DEFI",
    # AI / Compute
    "TAO": "AI", "RENDER": "AI", "FET": "AI", "WLD": "AI",
    # Layer 2s
    "ARB": "L2", "OP": "L2", "STRK": "L2",
    # Majors / Legacy Pow & PoS
    "BTC": "MAJORS", "ETH": "MAJORS", "BNB": "MAJORS", "XRP": "MAJORS", "LTC": "MAJORS", "BCH": "MAJORS", "TRX": "MAJORS", "XLM": "MAJORS", "ZEC": "MAJORS",
}


def get_sector(ticker: str) -> str:
    """Return narrative sector group for ticker."""
    return SECTOR_MAP.get(ticker.upper(), "OTHER")


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
