"""
test_learning_engine.py — Unit tests for Continuous Learning Engine & Daily Intelligence Feedback.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import learning_engine
import state


class TestLearningEngine(unittest.TestCase):
    def setUp(self):
        state.set_paused(False)

    def test_analyze_asset_performance(self):
        sample_trades = [
            {"symbol": "SOLUSDT", "closed_pnl": 500.0},
            {"symbol": "SOLUSDT", "closed_pnl": 300.0},
            {"symbol": "WLDUSDT", "closed_pnl": -800.0},
            {"symbol": "WLDUSDT", "closed_pnl": -400.0},
            {"symbol": "BTCUSDT", "closed_pnl": 100.0},
            {"symbol": "BTCUSDT", "closed_pnl": -50.0},
        ]

        analysis = learning_engine.analyze_asset_performance(sample_trades)
        
        # Verify profitable assets ranking
        profitable = analysis["profitable"]
        self.assertEqual(profitable[0]["ticker"], "SOL")
        self.assertEqual(profitable[0]["net_pnl"], 800.0)
        self.assertEqual(profitable[0]["win_rate"], 100.0)

        # Verify losing assets ranking (worst first)
        losing = analysis["losing"]
        self.assertEqual(losing[0]["ticker"], "WLD")
        self.assertEqual(losing[0]["net_pnl"], -1200.0)
        self.assertEqual(losing[0]["win_rate"], 0.0)

        # Verify toxic assets identified
        toxic_tickers = [d["ticker"] for d in analysis["toxic"]]
        self.assertIn("WLD", toxic_tickers)

        # Verify star assets identified
        star_tickers = [d["ticker"] for d in analysis["stars"]]
        self.assertIn("SOL", star_tickers)

    def test_apply_learning_adaptations(self):
        sample_trades = [
            {"symbol": "WLDUSDT", "closed_pnl": -600.0},
            {"symbol": "WLDUSDT", "closed_pnl": -400.0},
        ]
        # Start clean
        state.remove_from_avoid_list(["WLD"])

        analysis = learning_engine.analyze_asset_performance(sample_trades)
        adaptations = learning_engine.apply_learning_adaptations(analysis, auto_avoid=True)

        # Verify WLD was auto-avoided
        self.assertIn("WLD", state.get_avoid_list())
        self.assertTrue(any("Auto-Avoided WLD" in a for a in adaptations))

        # Cleanup
        state.remove_from_avoid_list(["WLD"])

    def test_generate_report_format(self):
        sample_trades = [
            {"symbol": "SOLUSDT", "closed_pnl": 1200.0},
            {"symbol": "WLDUSDT", "closed_pnl": -2000.0},
        ]
        with patch("engine.client.get_wallet_balance", return_value={"equity": 50000.0, "available": 40000.0}):
            report = learning_engine.generate_daily_feedback_report(sample_trades, auto_adapt=False)
            self.assertIn("XIRA DAILY INTELLIGENCE & LEARNING FEEDBACK", report)
            self.assertIn("Top Profitable Assets:", report)
            self.assertIn("SOL", report)
            self.assertIn("Assets With Most Losses:", report)
            self.assertIn("WLD", report)
            self.assertIn("-$2,000.00", report)

    def test_send_daily_feedback(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True}

        with patch("requests.post", return_value=mock_resp), \
             patch("engine.client.get_wallet_balance", return_value={"equity": 50000.0, "available": 40000.0}):
            res = learning_engine.send_daily_feedback(chat_id="12345", token="mock_token", auto_adapt=False)
            self.assertTrue(res)


if __name__ == "__main__":
    unittest.main()
