"""
test_scanner.py — Unit tests for scanner signal logic and desk methodology.
Uses mock OHLCV data and verifies standalone calls, entry ranges, and contract lines.
"""
import sys
import os
import unittest
from unittest.mock import patch

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("TRADE_MODE", "demo")
os.environ.setdefault("DEMO_ENV", "paper")


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
        "turnover": [v * price for v in volumes],
    }, index=idx)
    return df


class TestShelfDetection(unittest.TestCase):
    def test_detect_shelves(self):
        import scanner as s
        df = _make_ohlcv(60, "neutral")
        struct = s.detect_shelves_and_edges(df)
        self.assertIn("demand_shelf", struct)
        self.assertIn("supply_shelf", struct)
        self.assertGreater(struct["demand_shelf"][1], struct["demand_shelf"][0])
        self.assertGreater(struct["supply_shelf"][1], struct["supply_shelf"][0])

    def test_vertical_candle_detection(self):
        import scanner as s
        df = _make_ohlcv(60, "neutral")
        # Inject vertical candle (> 2.5% body)
        df.iloc[-1, df.columns.get_loc("open")] = 100.0
        df.iloc[-1, df.columns.get_loc("close")] = 105.0 # 5% move
        struct = s.detect_shelves_and_edges(df)
        self.assertTrue(struct["is_vertical"])


class TestSignalGeneration(unittest.TestCase):
    def test_wait_returned_when_no_data(self):
        import scanner as s
        with patch.object(s, "fetch_ohlcv", return_value=None):
            sig = s.analyze_ticker("BTC", "scalp")
        self.assertEqual(sig["side"], "WAIT")
        self.assertIn("BOT|BTC|WAIT", sig["bot_line"])

    def test_standalone_calls(self):
        """Every call must have its own ticker, no comma-separated bundling."""
        import scanner as s
        from config import CORE_TICKERS
        df = _make_ohlcv(60, "neutral")
        with patch.object(s, "fetch_ohlcv", return_value=df):
            core_sigs, extra_sigs, tape = s.run_scan("scalp")

        self.assertEqual(len(core_sigs), len(CORE_TICKERS))
        for sig in core_sigs:
            self.assertIsInstance(sig["ticker"], str)
            self.assertNotIn(",", sig["ticker"])
            self.assertIn("bot_line", sig)

    def test_contract_line_format(self):
        import scanner as s
        sig = s._make_buy_sell(
            ticker="SOL",
            side="BUY",
            trade_type="scalp",
            tf="15m",
            entry_low=150.0,
            entry_high=151.0,
            tp1=155.0,
            tp2=160.0,
            sl=148.5,
            rr=2.5,
            structure="Demand shelf test",
            reason="Held demand",
            live_price=150.5,
            leverage=4,
        )
        line = sig["bot_line"]
        self.assertTrue(line.startswith("BOT|SOL|BUY|PERP|15m|150.0000|151.0000|"))
        parts = line.split("|")
        self.assertEqual(len(parts), 14)

    def test_4h_trend_confluence(self):
        import scanner as s
        df_bullish = _make_ohlcv(30, "up")
        with patch.object(s, "fetch_ohlcv", return_value=df_bullish):
            trend = s.get_4h_trend("BTC")
            self.assertEqual(trend, "BULLISH")

        df_bearish = _make_ohlcv(30, "down")
        with patch.object(s, "fetch_ohlcv", return_value=df_bearish):
            trend = s.get_4h_trend("BTC")
            self.assertEqual(trend, "BEARISH")


if __name__ == "__main__":
    unittest.main()
