"""
test_analytics.py — Unit tests for analytics module, metrics calculation, and chart rendering.
"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("TRADE_MODE", "demo")
os.environ.setdefault("DEMO_ENV", "paper")

import analytics


class TestAnalytics(unittest.TestCase):
    def test_compute_metrics_empty(self):
        m = analytics.compute_metrics([])
        self.assertEqual(m["total_trades"], 0)
        self.assertEqual(m["win_rate"], 0.0)
        self.assertEqual(m["net_pnl"], 0.0)

    def test_compute_metrics_with_trades(self):
        trades = [
            {"ticker": "BTC", "pnl_usdt": 100.0, "closed_at": "2026-10-01T00:00:00Z"},
            {"ticker": "ETH", "pnl_usdt": -50.0, "closed_at": "2026-10-01T00:10:00Z"},
            {"ticker": "SOL", "pnl_usdt": 200.0, "closed_at": "2026-10-01T00:20:00Z"},
        ]
        m = analytics.compute_metrics(trades)
        self.assertEqual(m["total_trades"], 3)
        self.assertEqual(m["wins"], 2)
        self.assertEqual(m["losses"], 1)
        self.assertAlmostEqual(m["win_rate"], 66.7, places=1)
        self.assertEqual(m["net_pnl"], 250.0)
        self.assertEqual(m["best_trade"]["ticker"], "SOL")
        self.assertEqual(m["worst_trade"]["ticker"], "ETH")

    def test_generate_chart_bytes(self):
        trades = [
            {"ticker": "BTC", "pnl_usdt": 100.0, "closed_at": "2026-10-01T00:00:00Z"},
            {"ticker": "ETH", "pnl_usdt": -50.0, "closed_at": "2026-10-01T00:10:00Z"},
        ]
        m = analytics.compute_metrics(trades)
        buf = analytics.generate_analytics_chart("daily", trades, m)
        data = buf.getvalue()
        self.assertTrue(len(data) > 1000)
        self.assertTrue(data.startswith(b"\x89PNG"))

    def test_generate_chart_empty_trades(self):
        m = analytics.compute_metrics([])
        buf = analytics.generate_analytics_chart("hourly", [], m)
        data = buf.getvalue()
        self.assertTrue(len(data) > 1000)
        self.assertTrue(data.startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
