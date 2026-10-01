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
        import engine
        sample_trades = [
            {"symbol": "SOLUSDT", "closed_pnl": 1200.0},
            {"symbol": "WLDUSDT", "closed_pnl": -2000.0},
        ]
        with patch.object(engine.client, "get_wallet_balance", return_value={"equity": 50000.0, "available": 40000.0}):
            report = learning_engine.generate_daily_feedback_report(sample_trades, auto_adapt=False)
            self.assertIn("XIRA DAILY INTELLIGENCE & LEARNING FEEDBACK", report)
            self.assertIn("Top Profitable Assets:", report)
            self.assertIn("SOL", report)
            self.assertIn("Assets With Most Losses:", report)
            self.assertIn("WLD", report)
            self.assertIn("-$2,000.00", report)

    def test_send_daily_feedback(self):
        import engine
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True}

        with patch("learning_engine.requests.post", return_value=mock_resp), \
             patch.object(engine, "_sync_with_bybit"), \
             patch.object(engine.client, "get_wallet_balance", return_value={"equity": 50000.0, "available": 40000.0}), \
             patch.object(engine.client, "get_closed_pnl", return_value=[]):
            res = learning_engine.send_daily_feedback(chat_id="12345", token="mock_token", auto_adapt=False)
            self.assertTrue(res)

    def test_quarantine_and_probation_lifecycle(self):
        from datetime import datetime, timezone, timedelta
        # 1. Clean slate
        state.remove_from_avoid_list(["TESTCOIN"])

        # 2. Quarantine asset
        quar_info = state.quarantine_asset("TESTCOIN", hours=24.0, reason="High toxic loss")
        self.assertIn("TESTCOIN", state.get_avoid_list())
        self.assertIn("TESTCOIN", state.get_quarantine_list())
        self.assertFalse(state.is_on_probation("TESTCOIN"))

        # 3. Simulate 24h expiration by setting expires_at to 1 hour ago
        s = state.get_state()
        past_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        s["quarantine"]["TESTCOIN"]["expires_at"] = past_time
        state._save(s)

        # 4. Trigger check_and_update_quarantines()
        self.assertTrue(state.is_on_probation("TESTCOIN"))
        self.assertNotIn("TESTCOIN", state.get_avoid_list())
        prob = state.get_probation_list()
        self.assertIn("TESTCOIN", prob)
        self.assertEqual(prob["TESTCOIN"]["trades_remaining"], 3)

        # 5. Simulate 3 profitable probation trades -> Graduation
        r1 = state.record_probation_trade("TESTCOIN", 20.0)
        self.assertEqual(r1, "CONTINUING")
        r2 = state.record_probation_trade("TESTCOIN", -5.0)
        self.assertEqual(r2, "CONTINUING")
        r3 = state.record_probation_trade("TESTCOIN", 15.0)
        self.assertEqual(r3, "GRADUATED")
        self.assertFalse(state.is_on_probation("TESTCOIN"))
        self.assertNotIn("TESTCOIN", state.get_avoid_list())

        # 6. Test probation failure and re-quarantine
        state.quarantine_asset("TESTCOIN", hours=24.0, reason="Test 2")
        s = state.get_state()
        s["quarantine"]["TESTCOIN"]["expires_at"] = past_time
        state._save(s)
        self.assertTrue(state.is_on_probation("TESTCOIN"))

        state.record_probation_trade("TESTCOIN", -50.0)
        state.record_probation_trade("TESTCOIN", -30.0)
        r_fail = state.record_probation_trade("TESTCOIN", 10.0)
        self.assertEqual(r_fail, "RE_QUARANTINED")
        self.assertIn("TESTCOIN", state.get_avoid_list())
        self.assertIn("TESTCOIN", state.get_quarantine_list())
        self.assertFalse(state.is_on_probation("TESTCOIN"))

        # Cleanup
        state.remove_from_avoid_list(["TESTCOIN"])


if __name__ == "__main__":
    unittest.main()
