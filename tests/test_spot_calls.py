import unittest
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np

import market_research


class TestSpotCalls(unittest.TestCase):
    @patch("scanner.fetch_ohlcv")
    def test_build_spot_signal(self, mock_fetch):
        # Create a mock 1D daily dataframe
        n = 30
        dates = pd.date_range("2026-01-01", periods=n, freq="D")
        closes = np.linspace(60000, 65000, n)
        df = pd.DataFrame({
            "open": closes * 0.99,
            "high": closes * 1.02,
            "low": closes * 0.98,
            "close": closes,
            "volume": [1000.0] * n,
        }, index=dates)
        mock_fetch.return_value = df

        sig = market_research.build_spot_signal("BTC")
        self.assertEqual(sig["ticker"], "BTC")
        self.assertEqual(sig["side"], "BUY")
        self.assertEqual(sig["trade_type"], "spot")
        self.assertEqual(sig["tf"], "1D")
        self.assertGreater(sig["tp1"], sig["entry_high"])
        self.assertGreater(sig["tp2"], sig["tp1"])
        self.assertLess(sig["sl"], sig["entry_low"])
        self.assertGreaterEqual(sig["rr"], 2.0)

    def test_generate_market_research_spot(self):
        sig = {
            "ticker": "ETH",
            "side": "BUY",
            "trade_type": "spot",
            "tf": "1D",
            "entry_low": 2400.0,
            "entry_high": 2500.0,
            "tp1": 2800.0,
            "tp2": 3200.0,
            "sl": 2200.0,
            "live_price": 2450.0,
        }
        note = market_research.generate_market_research(sig)
        self.assertIn("Spot Accumulation", note)
        self.assertIn("ETH", note)
        self.assertIn("The thesis:", note)
        self.assertIn("Verdict:", note)


if __name__ == "__main__":
    unittest.main()
