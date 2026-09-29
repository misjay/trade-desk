# Trade Desk Bot

Two-sided crypto trade desk for Bybit V5 SPOT + PERPS (LONG and SHORT).
- **Core 24** tickers in mandated order, each analyzed standalone.
- **12 Extras** (liquid only).
- **Demo mode** (paper trades) or **Live mode** (real Bybit orders).
- **Telegram** notifications: full PERP + SPOT cards with chart images, fill/TP/SL alerts, hourly summary.
- **Dashboard** at `http://localhost:8765` (auto-refreshes every 15s).

---

## Setup

### 1. Python environment
```powershell
cd C:\Users\USER\.gemini\antigravity\scratch\trade-desk
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Configure credentials
```powershell
copy .env.example .env
notepad .env
```

Fill in:
| Key | Value |
|-----|-------|
| `BYBIT_API_KEY` | Your Bybit V5 API key (live) |
| `BYBIT_SECRET_KEY` | Your Bybit V5 API secret |
| `BYBIT_TESTNET_API_KEY` | Bybit Testnet key (demo) |
| `BYBIT_TESTNET_API_SECRET` | Bybit Testnet secret |
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | Your Telegram chat/channel ID |

> **Get Testnet keys**: https://testnet.bybit.com (Bybit V5 Testnet)

### 3. Start in Demo mode (default)
```powershell
python main.py
```

### 4. Open Dashboard
Open `dashboard.html` in your browser while the bot is running.

---

## Modes

| Flag | Behavior |
|------|----------|
| `python main.py` | Uses `TRADE_MODE` from `.env` (default: `demo`) |
| `python main.py --demo` | Force demo — paper trades, testnet API |
| `python main.py --live` | Force live — real Binance orders |
| `python main.py --reset` | Reset `state.json` (clears paper positions) |
| `python main.py --no-telegram` | Disable Telegram (local testing) |

---

## Scanner Logic

| Signal | Condition |
|--------|-----------|
| **BUY** | Price pulls into demand zone (swing low + volume support). Trend not bearish LL with vol spike. |
| **SELL** | Price rejects supply zone (swing high). Trend not bullish HH with vol spike. |
| **WAIT** | Price mid-range, no precise $ level, BTC floor/reclaim suppression active, or R:R < 1.5. |

BTC Floor Rule:
- BTC breaks session low → alt longs suppressed for 1 hour.
- BTC hard reclaims → alt shorts suppressed for 1 hour.

---

## Leverage Tiers

| Tier | Tickers | Scalp | Day | Hard Cap |
|------|---------|-------|-----|----------|
| A | BTC ETH SOL XRP BNB | 5x | 4x | 7x |
| B | DOGE ADA LINK AVAX SUI HYPE LTC AAVE ZEC UNI BCH TRX XLM | 4x | 3x | 4x |
| C | TAO ONDO PEPE ENA HBAR NEAR + Extras | 3x | 2x | 3x |

All positions: **isolated margin**, **risk 0.5%–1% equity** per call.

---

## Telegram Alerts

Every BUY/SELL fires:
1. Chart image (candlesticks + demand/supply box + TP/SL lines)
2. Full PERP block (entry range, TP1, TP2, SL, leverage, order line)
3. Full SPOT block (same levels, 1x leverage)

WAIT: text only (no image).
Fills/TP/SL: instant update message.
Hourly: full desk summary (all 24+ tickers).

---

## File Structure

```
trade-desk/
├── main.py         ← entry point, scheduler
├── config.py       ← tickers, leverage map, constants
├── scanner.py      ← OHLCV fetch + structure analysis
├── engine.py       ← order execution (demo + live)
├── chart.py        ← candlestick PNG generator
├── notifier.py     ← Telegram formatter + sender
├── state.py        ← JSON position/equity store
├── state.json      ← auto-created at runtime
├── dashboard.html  ← live browser dashboard
├── requirements.txt
├── .env            ← your credentials (git-ignored)
├── .env.example    ← template
└── tests/
    ├── test_scanner.py
    ├── test_engine.py
    ├── test_notifier.py
    └── test_chart.py
```

---

## Running Tests

```powershell
python -m pytest tests/ -v
```

---

## Go-Live Checklist

- [ ] Run demo for at least one full trading session.
- [ ] Verify Telegram messages arrive correctly.
- [ ] Check `state.json` for accurate paper P&L.
- [ ] Set Binance live API key with **Futures + Spot** trade permissions.
- [ ] Whitelist your IP on the Binance API key.
- [ ] Flip `TRADE_MODE=live` in `.env`.
- [ ] Start with small `RISK_PER_TRADE=0.005` (0.5%).

---

*Not financial advice. Verify price, funding, and book before acting.*
