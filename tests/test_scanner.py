"""
test_scanner.py — Unit tests for scanner signal logic.
Uses mock OHLCV data, no real network calls.
"""
import sys
import os
import types
import unittest
from unittest.mock import patch, MagicMock

import pandas as pd
import numpy as np

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock config singleton before importing scanner
os.environ.setdefault("TRADE_MODE", "demo")
os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test")


def _make_ohlcv(n: int = 100, trend: str = "neutral") -> pd.DataFrame:
    """Build synthetic OHLCV DataFrame."""
    np.random.seed(42)
    base = 60000.0
    opens, highs, lows, closes, volumes = [], [], [], [], []
    price = base
    for i in range(n):
        if trend == "up":
            price += np.random.uniform(0, 200)
        elif trend == "down":
            price -= np.random.uniform(0, 200)
        else:
            price += np.random.uniform(-100, 100)
        o = price
        c = price + np.random.uniform(-100, 100)
        h = max(o, c) + np.random.uniform(10, 80)
        lo = min(o, c) - np.random.uniform(10, 80)
        opens.append(o)
        highs.append(h)
        lows.append(lo)
        closes.append(c)
        volumes.append(np.random.uniform(100, 1000))

    idx = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    df = pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes,
        "quote_vol": [v * price for v in volumes],
    }, index=idx)
    return df


class TestSwingDetection(unittest.TestCase):
    def setUp(self):
        import scanner as s
        self.s = s

    def test_swing_lows_found(self):
        df = _make_ohlcv(100, "neutral")
        lows = self.s._find_swing_lows(df, n=5)
        self.assertGreater(len(lows), 0)

    def test_swing_highs_found(self):
        df = _make_ohlcv(100, "neutral")
        highs = self.s._find_swing_highs(df, n=5)
        self.assertGreater(len(highs), 0)


class TestTrendClassification(unittest.TestCase):
    def setUp(self):
        import scanner as s
        self.s = s

    def test_up_trend_detected(self):
        df = _make_ohlcv(100, "up")
        result = self.s._trend(df)
        self.assertEqual(result, "bullish_hh")

    def test_down_trend_detected(self):
        df = _make_ohlcv(100, "down")
        result = self.s._trend(df)
        self.assertEqual(result, "bearish_ll")


class TestSignalGeneration(unittest.TestCase):
    def test_wait_returned_when_no_data(self):
        import scanner as s
        with patch.object(s, "fetch_ohlcv", return_value=None), \
             patch.object(s, "fetch_live_price", return_value=60000.0):
            sig = s.analyze_ticker("BTC", "scalp")
        self.assertEqual(sig["side"], "WAIT")

    def test_wait_returned_on_price_error(self):
        import scanner as s
        with patch.object(s, "fetch_ohlcv", return_value=None), \
             patch.object(s, "fetch_live_price", return_value=None):
            sig = s.analyze_ticker("BTC", "scalp")
        self.assertEqual(sig["side"], "WAIT")

    def test_entry_range_present_on_buy(self):
        """Any BUY signal must have non-None entry_low and entry_high."""
        import scanner as s
        df = _make_ohlcv(150, "up")
        with patch.object(s, "fetch_ohlcv", return_value=df), \
             patch.object(s, "fetch_live_price", return_value=float(df["low"].quantile(0.1))):
            sig = s.analyze_ticker("BTC", "scalp")
        if sig["side"] == "BUY":
            self.assertIsNotNone(sig["entry_low"])
            self.assertIsNotNone(sig["entry_high"])

    def test_rr_minimum(self):
        """BUY/SELL signals must have R:R >= MIN_RR."""
        import scanner as s
        from config import MIN_RR
        df = _make_ohlcv(150, "neutral")
        with patch.object(s, "fetch_ohlcv", return_value=df), \
             patch.object(s, "fetch_live_price", return_value=float(df["close"].mean())):
            sig = s.analyze_ticker("ETH", "scalp")
        if sig["side"] in ("BUY", "SELL"):
            self.assertGreaterEqual(sig["rr"], MIN_RR)


class TestBtcFloor(unittest.TestCase):
    def setUp(self):
        import scanner as s
        self.s = s
        # Reset floor state
        s._btc_session_floor = None
        s._btc_broke_floor_at = None
        s._btc_hard_reclaim_at = None

    def test_floor_suppresses_longs(self):
        import scanner as s
        import time
        s._btc_session_floor = 60000.0
        s._btc_broke_floor_at = time.time()  # just broke
        self.assertTrue(s.alt_longs_suppressed())

    def test_no_suppression_after_hour(self):
        import scanner as s
        import time
        s._btc_session_floor = 60000.0
        s._btc_broke_floor_at = time.time() - 3700  # > 1 hour ago
        self.assertFalse(s.alt_longs_suppressed())


class TestRunScanStructure(unittest.TestCase):
    def test_all_tickers_returned(self):
        """run_scan must return a signal for every ticker in ALL_TICKERS."""
        import scanner as s
        from config import ALL_TICKERS
        df = _make_ohlcv(150, "neutral")
        with patch.object(s, "fetch_ohlcv", return_value=df), \
             patch.object(s, "fetch_live_price", return_value=60000.0), \
             patch.object(s, "fetch_24h_quote_vol", return_value=1e9):
            signals = s.run_scan("scalp")
        tickers_returned = {sig["ticker"] for sig in signals}
        for t in ALL_TICKERS:
            self.assertIn(t, tickers_returned, f"Missing ticker: {t}")

    def test_no_bundled_signals(self):
        """Each signal must be for exactly one ticker."""
        import scanner as s
        df = _make_ohlcv(150, "neutral")
        with patch.object(s, "fetch_ohlcv", return_value=df), \
             patch.object(s, "fetch_live_price", return_value=60000.0), \
             patch.object(s, "fetch_24h_quote_vol", return_value=1e9):
            signals = s.run_scan("scalp")
        for sig in signals:
            self.assertIsInstance(sig["ticker"], str)
            self.assertNotIn(",", sig["ticker"])  # never bundled


if __name__ == "__main__":
    unittest.main()
