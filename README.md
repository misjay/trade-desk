# Xira — Bybit SPOT & PERPS Trade Desk

Two-sided quantitative crypto trade desk bot for Bybit V5 (SPOT and PERPS). LONG and SHORT are equal.

- **Risk Model**: Exactly **0.5% of balance** risked per trade (`qty = (balance * 0.005) / |entry - sl|`).
- **Core 24** tickers in mandated desk order:
  `BTC ETH SOL XRP BNB DOGE ADA LINK AVAX SUI HYPE LTC AAVE ZEC UNI BCH TRX XLM TAO ONDO PEPE ENA HBAR NEAR`
- **Extras** (only if 24h quote volume >= 50M USDT, else NONE):
  `ARB WLD STRK APT SEI INJ OP DOT ATOM FIL RENDER FET`
- **NO IMAGES**: Chart field is a TradingView URL only (`https://www.tradingview.com/chart/?symbol=BINANCE:TICKERUSDT`).
- **Modes**:
  - **Demo (default)**: Paper trading simulation against live Bybit orderbooks & mark prices, or Bybit V5 Testnet / Demo UTA API if keys are provided.
  - **Live**: Real Bybit V5 Spot + Linear Perpetual orders.
- **Order Execution**:
  - Limit orders inside printed entry band using `PostOnly` (maker execution).
  - Native Bybit TP/SL configuration.
  - Market orders permitted ONLY on Tier A (`BTC, ETH, SOL, XRP, BNB`) if mark is inside Entry and book slip < 0.15%.
  - NEVER market `PEPE, TAO, ENA, HBAR, NEAR` or a vertical 15m candle.
  - Spot sell executed only if base coin inventory > 0 (checks wallet; flat drops spot sell).
- **Executor Contract Parser**:
  - Directly ingests and executes `BOT|TICKER|SIDE|VENUE|TF|ENTRY_LOW|ENTRY_HIGH|TP1|TP2|SL|LEV|MAX_RISK_PCT|EXPIRE_UTC|VALID_IF` lines.
- **Dashboard**: Real-time web UI at `http://localhost:8765` serving `state.json`.

---

## Setup & Quickstart

### 1. Python Environment
```powershell
cd C:\Users\USER\.gemini\antigravity\scratch\trade-desk
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. Configure Credentials (`.env`)
```powershell
copy .env.example .env
```
Key configuration parameters:
| Variable | Description | Default |
|---|---|---|
| `TRADE_MODE` | `demo` or `live` | `demo` |
| `DEMO_ENV` | `paper`, `testnet`, or `demo` | `paper` |
| `RISK_PER_TRADE` | Risk fraction of equity per trade | `0.005` (0.5%) |
| `PAPER_EQUITY` | Initial paper balance in USDT | `10000` |
| `BYBIT_API_KEY` | Live Bybit V5 API key | (empty) |
| `BYBIT_SECRET_KEY` | Live Bybit V5 API secret | (empty) |
| `BYBIT_TESTNET_API_KEY` | Bybit Testnet / Demo API key | (empty) |
| `BYBIT_TESTNET_API_SECRET` | Bybit Testnet / Demo API secret | (empty) |
| `TELEGRAM_BOT_TOKEN` | Optional Telegram bot token | (empty) |
| `TELEGRAM_CHAT_ID` | Optional Telegram chat ID | (empty) |

---

## Usage Commands

### 1. Start Automated Trading Bot in Demo Mode (Default)
```powershell
.\.venv\Scripts\python.exe main.py
```
Runs the continuous autonomous loop:
- Scans 15m scalp candles every 5 minutes.
- Scans 4h day candles every 30 minutes.
- Sizing strictly calculates 0.5% balance risk.
- Executes paper orders on Bybit orderbooks (or Testnet/Demo API if configured).
- Serves dashboard at `http://localhost:8765`.

### 2. Run Single Live Desk Report
```powershell
.\.venv\Scripts\python.exe main.py --desk
```
Fetches live market data from Bybit, evaluates all 24 core tickers and liquid extras, and prints the full Desk publication with EXECUTOR CONTRACT lines.

### 3. Ingest and Execute Machine Contract Line
```powershell
.\.venv\Scripts\python.exe main.py --execute "BOT|SUI|SELL|PERP|15m|1.2039|1.2096|1.1741|1.1548|1.2169|4|0.005|2026-09-30 18:55 UTC|live mark inside or approaching band; funding not extreme against the side; 15m candle not vertical; book slip < 0.15% on BTC ETH SOL XRP BNB else cancel; never market PEPE TAO ENA HBAR NEAR"
```

### 4. Switch to Live Trading Mode
In `.env`, set:
```ini
TRADE_MODE=live
BYBIT_API_KEY=your_live_key
BYBIT_SECRET_KEY=your_live_secret
```
Or run with CLI flag:
```powershell
.\.venv\Scripts\python.exe main.py --live
```

### 5. Reset Positions & Paper Equity
```powershell
.\.venv\Scripts\python.exe main.py --reset
```

---

## Risk & Leverage Tiers

All positions are **Isolated Margin**.
- **Tier A** (`BTC ETH SOL XRP BNB`): Scalp 3x–5x, Day 2x–4x, Max 7x.
- **Tier B** (`DOGE ADA LINK AVAX SUI HYPE LTC AAVE ZEC UNI BCH TRX XLM`): Scalp 4x, Day 3x, Max 4x.
- **Tier C** (`TAO ONDO PEPE ENA HBAR NEAR` + Extras): Scalp 3x, Day 2x, Max 3x.
- **Sizing Formula**:
  $$\text{Risk Capital} = \text{Balance} \times 0.005$$
  $$\text{Quantity} = \frac{\text{Risk Capital}}{|\text{Entry} - \text{SL}|}$$
  If SL requires $> 1\%$ equity at allowed leverage, size is automatically scaled down.
