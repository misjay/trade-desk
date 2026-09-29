"""
test_chart.py — Tests that chart generation returns valid PNG bytes.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TRADE_MODE", "demo")
os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test")

import numpy as np
import pandas as pd


def _make_ohlcv(n=80):
    np.random.seed(7)
    price = 60000.0
    rows = []
    for i in range(n):
        price += np.random.uniform(-200, 200)
        o = price
        c = price + np.random.uniform(-100, 100)
        h = max(o, c) + np.random.uniform(10, 100)
        lo = min(o, c) - np.random.uniform(10, 100)
        rows.append((o, h, lo, c, np.random.uniform(100, 1000)))
    idx = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)


class TestChartGeneration(unittest.TestCase):
    def setUp(self):
        import chart as c
        self.chart = c

    def test_returns_bytes(self):
        df = _make_ohlcv()
        result = self.chart.generate_chart(
            df, "BTC", "BUY", "15m",
            entry_low=59000, entry_high=60500,
            tp1=63000, tp2=66000,
            sl=58000, live_price=59800,
        )
        self.assertIsInstance(result, bytes)
        self.assertGreater(len(result), 1000)

    def test_returns_png(self):
        df = _make_ohlcv()
        result = self.chart.generate_chart(
            df, "ETH", "SELL", "4h",
            entry_low=3100, entry_high=3200,
            tp1=2900, tp2=2700,
            sl=3350, live_price=3150,
        )
        # PNG magic bytes: \x89PNG
        self.assertTrue(result[:4] == b"\x89PNG")

    def test_error_png_on_empty_df(self):
        """Should not crash on empty DataFrame — returns error PNG."""
        result = self.chart.generate_chart(
            None, "SOL", "BUY", "15m",
            entry_low=None, entry_high=None,
            tp1=None, tp2=None, sl=None,
        )
        self.assertIsInstance(result, bytes)
        self.assertGreater(len(result), 100)

    def test_sell_chart_generates(self):
        df = _make_ohlcv()
        result = self.chart.generate_chart(
            df, "XRP", "SELL", "1h",
            entry_low=0.55, entry_high=0.58,
            tp1=0.48, tp2=0.42,
            sl=0.61, live_price=0.57,
        )
        self.assertGreater(len(result), 1000)


if __name__ == "__main__":
    unittest.main()
