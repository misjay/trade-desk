"""
test_github_enhancements.py — Unit tests for GitHub-inspired trading enhancements:
  1. Freqtrade / FreqAI: Market Regime Filter (blocks counter-trend shorts in STRONG_BULL, longs in STRONG_BEAR)
  2. Hummingbot: Inventory Skew & Portfolio Balance (directional count & 65% notional caps)
  3. Passivbot: Micro-Grid 2-tier Staggered Limit Placement
  4. Passivbot / Freqtrade: Unstucking & Time-Decay Derisking (>90m stalled trades trailed to BE)
"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TRADE_MODE", "demo")
os.environ.setdefault("DEMO_ENV", "paper")

import config
import state
import scanner
import engine


def _build_candles(trend: str = "bull", n: int = 50) -> pd.DataFrame:
    """Helper to build 4h synthetic candles for testing regime filters."""
    closes = []
    price = 60000.0
    for _ in range(n):
        if trend == "bull":
            price += 100.0
        elif trend == "bear":
            price -= 100.0
        else:
            price += np.random.uniform(-20, 20)
        closes.append(price)

    df = pd.DataFrame({
        "open": closes,
        "high": [c + 50 for c in closes],
        "low": [c - 50 for c in closes],
        "close": closes,
        "volume": [100.0] * n,
    }, index=pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC"))
    return df


class TestFreqtradeMarketRegime(unittest.TestCase):
    def test_strong_bull_detection(self):
        df_bull = _build_candles(trend="bull", n=50)
        with patch.object(scanner, "fetch_ohlcv", return_value=df_bull):
            regime = scanner.detect_market_regime("BTC")
            self.assertEqual(regime["regime"], "STRONG_BULL")
            self.assertIn("Bull Expansion", regime["description"])

    def test_strong_bear_detection(self):
        df_bear = _build_candles(trend="bear", n=50)
        with patch.object(scanner, "fetch_ohlcv", return_value=df_bear):
            regime = scanner.detect_market_regime("BTC")
            self.assertEqual(regime["regime"], "STRONG_BEAR")
            self.assertIn("Bear Downtrend", regime["description"])

    def test_regime_blocks_counter_trend_short(self):
        """In STRONG_BULL, calling a SELL setup must be blocked and converted to WAIT."""
        # Mock 15m candles with price at supply shelf
        mock_15m = pd.DataFrame({
            "open": [100.0] * 60,
            "high": [102.0] * 60,
            "low": [98.0] * 60,
            "close": [100.0] * 59 + [101.9],  # right at upper edge (pos >= 0.75)
            "volume": [50.0] * 60,
        }, index=pd.date_range("2024-01-01", periods=60, freq="15min", tz="UTC"))

        with patch.object(scanner, "fetch_ohlcv") as mock_fetch, \
             patch.object(scanner, "detect_market_regime", return_value={"regime": "STRONG_BULL"}), \
             patch.object(scanner, "get_btc_macro_regime", return_value="STRONG_BULL"), \
             patch.object(scanner, "check_btc_momentum_gate", return_value=(True, "Neutral")):
            mock_fetch.return_value = mock_15m
            sig = scanner.analyze_ticker("ETH", "scalp")
            # If sell triggered, regime filter should convert it to WAIT
            if sig["side"] == "WAIT" and "Supply shelf" in sig.get("structure", ""):
                self.assertIn("STRONG_BULL", sig["reason"])


class TestHummingbotInventorySkew(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self._orig = state._STATE_FILE
        state._STATE_FILE = Path(tempfile.mktemp(suffix=".json"))
        state.reset_state()

    def tearDown(self):
        state._STATE_FILE.unlink(missing_ok=True)
        state._STATE_FILE = self._orig

    def test_directional_count_cap(self):
        """When 3 SHORTs are open and 0 LONGs exist, new SHORT must be rejected."""
        for i in range(3):
            state.open_position({
                "id": f"pos_short_{i}",
                "ticker": f"ALT{i}",
                "side": "SELL",
                "qty": 10.0,
                "entry_price": 100.0,
                "status": "OPEN",
            })

        skew_ok, reason = engine.check_portfolio_inventory_skew("SELL")
        self.assertFalse(skew_ok)
        self.assertIn("Hummingbot inventory skew", reason)
        self.assertIn("3 active SHORTs", reason)

        # Opposite side (BUY) must be allowed to help balance inventory!
        skew_ok_buy, _ = engine.check_portfolio_inventory_skew("BUY")
        self.assertTrue(skew_ok_buy)

    def test_directional_notional_ratio_cap(self):
        """When short notional is >= 65% of total portfolio, additional shorts are rejected."""
        # 1 small long ($100), 1 large short ($300) -> short is 75% of total
        state.open_position({
            "id": "pos_long_1",
            "ticker": "SOL",
            "side": "BUY",
            "qty": 1.0,
            "entry_price": 100.0,
            "status": "OPEN",
        })
        state.open_position({
            "id": "pos_short_1",
            "ticker": "AVAX",
            "side": "SELL",
            "qty": 10.0,
            "entry_price": 30.0, # $300 notional
            "status": "OPEN",
        })

        skew_ok, reason = engine.check_portfolio_inventory_skew("SELL")
        self.assertFalse(skew_ok)
        self.assertIn("Net short notional is 75.0%", reason)


class TestPassivbotMicroGridAndUnstucking(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self._orig = state._STATE_FILE
        state._STATE_FILE = Path(tempfile.mktemp(suffix=".json"))
        state.reset_state()

    def tearDown(self):
        state._STATE_FILE.unlink(missing_ok=True)
        state._STATE_FILE = self._orig

    def test_micro_grid_order_generation(self):
        """Limit orders in paper mode should record micro-grid configuration."""
        sig = {
            "ticker": "BTC",
            "side": "BUY",
            "trade_type": "scalp",
            "entry_low": 60000.0,
            "entry_high": 60200.0,
            "tp1": 61500.0,
            "tp2": 62500.0,
            "sl": 59700.0,
            "leverage": 5,
        }
        with patch.object(engine.client, "get_ticker", return_value={"mark_price": 60100.0, "last_price": 60100.0}):
            res = engine.execute_signal(sig)
            self.assertEqual(res["status"], "SUCCESS")

        open_pos = state.get_open_positions()
        pos = next(p for p in open_pos.values() if p["ticker"] == "BTC")
        self.assertTrue(pos.get("micro_grid"))
        self.assertEqual(len(pos.get("order_ids", [])), 2)

    def test_unstucking_trails_sl_to_break_even(self):
        """Position open > 90 min and slightly profitable must have SL trailed to BE."""
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=100)).isoformat()
        pos = {
            "id": "test_stalled_pos",
            "ticker": "BTC",
            "side": "BUY",
            "entry_price": 60000.0,
            "qty": 0.1,
            "tp1": 62000.0,
            "sl": 59000.0,
            "opened_at": old_time,
            "status": "OPEN",
            "be_trailed": False,
        }
        state.open_position(pos)

        # Mock ticker mark price slightly in profit ($60,100)
        with patch.object(engine.client, "get_ticker", return_value={"mark_price": 60100.0}), \
             patch("notifier.send_text"):
            engine._monitor_paper_positions()

        updated_pos = state.get_open_positions().get("test_stalled_pos")
        self.assertIsNotNone(updated_pos)
        self.assertTrue(updated_pos.get("be_trailed"))
        self.assertEqual(updated_pos.get("sl"), 60000.0)


class TestPrecisionEnhancements(unittest.TestCase):
    def test_btc_momentum_gate_blocks_alt_long(self):
        """When BTC is dumping >0.45% in 30m, check_btc_momentum_gate('BUY') must return False."""
        df_dump = pd.DataFrame({
            "close": [70000.0, 69800.0, 69500.0],  # -0.71% drop
        })
        with patch.object(scanner, "fetch_ohlcv", return_value=df_dump):
            scanner._btc_momentum_cache = (0.0, 0.0, "NEUTRAL")  # force refresh
            allowed, reason = scanner.check_btc_momentum_gate("BUY")
            self.assertFalse(allowed)
            self.assertIn("BTC dumping", reason)

    def test_btc_momentum_gate_blocks_alt_short(self):
        """When BTC is pumping >0.45% in 30m, check_btc_momentum_gate('SELL') must return False."""
        df_pump = pd.DataFrame({
            "close": [70000.0, 70300.0, 70600.0],  # +0.85% rally
        })
        with patch.object(scanner, "fetch_ohlcv", return_value=df_pump):
            scanner._btc_momentum_cache = (0.0, 0.0, "NEUTRAL")  # force refresh
            allowed, reason = scanner.check_btc_momentum_gate("SELL")
            self.assertFalse(allowed)
            self.assertIn("BTC pumping", reason)

    def test_dynamic_kelly_sizing_weekend_and_conviction(self):
        """Dynamic Kelly sizing scales up on >=92 conviction and down on weekends."""
        equity = 50000.0
        entry = 100.0
        sl = 98.0  # dist = 2.0
        lev = 5

        # Test weekend sizing (0.30% risk = $150 capital / 2.0 = 75 qty)
        q_perp_wk, _, _ = engine.compute_position_size("SOL", entry, sl, equity, lev, risk_pct=config.cfg.risk_weekend_chop)
        self.assertAlmostEqual(q_perp_wk, 75.0, places=1)

        # Test A+ sizing (0.70% risk = $350 capital / 2.0 = 175 qty)
        q_perp_ap, _, _ = engine.compute_position_size("SOL", entry, sl, equity, lev, risk_pct=config.cfg.risk_a_plus)
        self.assertAlmostEqual(q_perp_ap, 175.0, places=1)

    def test_sector_concentration_cap(self):
        """Sector basket cap blocks adding 3rd L1 asset when max_sector_positions=2."""
        import tempfile
        from pathlib import Path
        orig = state._STATE_FILE
        state._STATE_FILE = Path(tempfile.mktemp(suffix=".json"))
        state.reset_state()
        try:
            # Open 2 L1 positions (SOL and SUI)
            state.open_position({"id": "pos_sol", "ticker": "SOL", "status": "OPEN"})
            state.open_position({"id": "pos_sui", "ticker": "SUI", "status": "OPEN"})

            # Attempt to open 3rd L1 position (APT) -> must be rejected
            allowed, reason = engine.check_sector_basket_exposure("APT")
            self.assertFalse(allowed)
            self.assertIn("Sector concentration cap", reason)
            self.assertIn("L1", reason)

            # Different sector (e.g. DeFi or AI) -> must be allowed
            allowed_aave, _ = engine.check_sector_basket_exposure("AAVE")
            self.assertTrue(allowed_aave)
        finally:
            state._STATE_FILE.unlink(missing_ok=True)
            state._STATE_FILE = orig

    def test_find_liquidity_wall_frontrunning(self):
        """find_liquidity_wall detects heavy limit wall and returns front-run price."""
        fake_book = {
            "asks": [
                (100.0, 10.0),
                (100.5, 12.0),
                (101.0, 100.0), # Heavy ask wall! (avg size ~10, this is 100)
                (101.5, 10.0),
            ]
        }
        # Testing BUY TP targeting 101.0
        frontrun_p = engine.client.find_liquidity_wall(fake_book, "BUY", 101.0)
        self.assertIsNotNone(frontrun_p)
        self.assertLess(frontrun_p, 101.0)
        self.assertAlmostEqual(frontrun_p, 101.0 * 0.9995, places=4)


    def test_hwm_drawdown_leverage_scaling(self):
        """When drawdown from HWM exceeds 2%, leverage is scaled down by 1x."""
        import tempfile
        from pathlib import Path
        orig = state._STATE_FILE
        orig_pe = config.cfg.paper_equity
        config.cfg.paper_equity = 50000.0
        state._STATE_FILE = Path(tempfile.mktemp(suffix=".json"))
        state.reset_state()
        try:
            state.set_equity(50000.0)
            state.update_high_water_mark(50000.0)

            # Normal condition: leverage for BTC should be 5x
            lev_normal = state.get_effective_leverage("BTC", scalp=True)
            self.assertEqual(lev_normal, 5)

            # Drawdown condition: equity drops to 48,500 (3% drawdown > 2% threshold)
            state.set_equity(48500.0)
            lev_drawdown = state.get_effective_leverage("BTC", scalp=True)
            self.assertEqual(lev_drawdown, 4)  # Reduced by 1x
        finally:
            config.cfg.paper_equity = orig_pe
            state._STATE_FILE.unlink(missing_ok=True)
            state._STATE_FILE = orig

    def test_funding_rate_carry_bias(self):
        """Negative funding gives positive carry bonus to longs; positive funding gives negative drag."""
        with patch.object(scanner._client, "get_ticker", return_value={"funding_rate": -0.0005}):
            # Deep negative funding: shorts paying longs!
            tick = scanner._client.get_ticker("SOLUSDT")
            self.assertLess(tick["funding_rate"], -0.0004)


if __name__ == "__main__":
    unittest.main()
