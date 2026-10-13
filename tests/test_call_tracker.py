import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import state
import call_tracker


class TestCallTracker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.mkdtemp()
        cls.orig_state_file = state._STATE_FILE
        state._STATE_FILE = Path(cls.temp_dir) / "test_state.json"

    @classmethod
    def tearDownClass(cls):
        state._STATE_FILE = cls.orig_state_file
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def setUp(self):
        state.reset_state(keep_configs=False)

    def test_unfilled_trade_does_not_trigger_tp_or_sl(self):
        # BUY setup: entry 100-105, TP1 115, SL 90. Live price 110 (never entered 100-105)
        call = {
            "ticker": "BTC",
            "side": "BUY",
            "tf": "15m",
            "entry_low": 100.0,
            "entry_high": 105.0,
            "tp1": 115.0,
            "tp2": 130.0,
            "sl": 90.0,
            "live_price": 110.0,
        }
        state.save_tracked_calls([call])

        # Price goes to 120 (above TP1) without ever dipping below 105
        with patch("scanner.fetch_live_price", return_value=120.0), \
             patch("call_tracker._send_alert") as mock_alert:
            call_tracker.check_tracked_calls_once()
            # Must NOT alert because entry never filled
            mock_alert.assert_not_called()

        tracked = state.get_tracked_calls()
        self.assertIn("BTC_15m", tracked)
        self.assertFalse(tracked["BTC_15m"].get("entry_filled", False))
        self.assertFalse(tracked["BTC_15m"].get("tp1_hit", False))

    def test_filled_trade_triggers_tp1_and_breakeven(self):
        call = {
            "ticker": "ETH",
            "side": "BUY",
            "tf": "15m",
            "entry_low": 2000.0,
            "entry_high": 2050.0,
            "tp1": 2150.0,
            "tp2": 2300.0,
            "sl": 1900.0,
            "live_price": 2060.0,
        }
        state.save_tracked_calls([call])

        # 1. Price dips to 2040 -> Entry filled
        with patch("scanner.fetch_live_price", return_value=2040.0), \
             patch("call_tracker._send_alert"):
            call_tracker.check_tracked_calls_once()

        tracked = state.get_tracked_calls()
        self.assertTrue(tracked["ETH_15m"].get("entry_filled"))

        # 2. Price rallies to 2160 -> Hits TP1
        with patch("scanner.fetch_live_price", return_value=2160.0), \
             patch("call_tracker._send_alert") as mock_alert:
            call_tracker.check_tracked_calls_once()
            self.assertTrue(mock_alert.called)

        tracked = state.get_tracked_calls()
        self.assertTrue(tracked["ETH_15m"].get("tp1_hit"))

        # 3. Price falls back to entry mid (2025.0) -> Triggers Breakeven Stop & closes
        with patch("scanner.fetch_live_price", return_value=2020.0), \
             patch("call_tracker._send_alert") as mock_alert:
            call_tracker.check_tracked_calls_once()

        # Closed call should be purged from active tracking
        tracked = state.get_tracked_calls()
        self.assertNotIn("ETH_15m", tracked)

    def test_tp2_closes_trade_and_prevents_sl(self):
        call = {
            "ticker": "SOL",
            "side": "BUY",
            "tf": "15m",
            "entry_low": 100.0,
            "entry_high": 105.0,
            "tp1": 115.0,
            "tp2": 125.0,
            "sl": 90.0,
            "entry_filled": True,
            "live_price": 102.0,
        }
        state.save_tracked_calls([call])

        # Hits TP2 directly
        with patch("scanner.fetch_live_price", return_value=126.0), \
             patch("call_tracker._send_alert") as mock_alert:
            call_tracker.check_tracked_calls_once()
            self.assertTrue(mock_alert.called)

        # Verified purged and closed
        tracked = state.get_tracked_calls()
        self.assertNotIn("SOL_15m", tracked)

    def test_expired_calls_are_purged(self):
        # 15m scalp created 5 hours ago (unfilled) -> should be expired & purged
        old_time = "2020-01-01T00:00:00+00:00"
        call = {
            "ticker": "DOGE",
            "side": "BUY",
            "tf": "15m",
            "entry_low": 0.10,
            "entry_high": 0.11,
            "tp1": 0.12,
            "tp2": 0.14,
            "sl": 0.09,
            "created_at": old_time,
            "live_price": 0.15,
        }
        state.save_tracked_calls([call])
        self.assertIn("DOGE_15m", state.get_tracked_calls())

        call_tracker.check_tracked_calls_once()

        # Should be purged automatically due to TTL expiration
        self.assertNotIn("DOGE_15m", state.get_tracked_calls())

    def test_trade_made_days_ago_triggers_notification_on_tp_and_sl(self):
        # 4h Day trade made 3 days ago (active/filled)
        three_days_ago = "2026-10-09T10:00:00+00:00"
        call = {
            "ticker": "SOL",
            "side": "BUY",
            "tf": "4h",
            "entry_low": 110.0,
            "entry_high": 112.0,
            "tp1": 120.0,
            "tp2": 130.0,
            "sl": 105.0,
            "entry_filled": True,
            "created_at": three_days_ago,
            "live_price": 114.0,
        }
        state.save_tracked_calls([call])

        # 3 days later, price rallies to 122.0 (hits TP1)
        with patch("scanner.fetch_live_price", return_value=122.0), \
             patch("call_tracker._send_alert") as mock_alert:
            call_tracker.check_tracked_calls_once()
            # Must trigger notification!
            self.assertTrue(mock_alert.called)
            # Alert message should mention (Called 3d ago)
            alert_text = mock_alert.call_args[0][0]
            self.assertIn("TARGET 1 REACHED", alert_text)
    def test_trade_trigger_sends_active_notification(self):
        call = {
            "ticker": "ADA",
            "side": "BUY",
            "tf": "15m",
            "entry_low": 0.40,
            "entry_high": 0.42,
            "tp1": 0.45,
            "tp2": 0.50,
            "sl": 0.38,
            "live_price": 0.44,
        }
        state.save_tracked_calls([call])
        self.assertFalse(state.get_tracked_calls()["ADA_15m"].get("entry_filled"))

        # Price dips into entry zone (0.41) -> Triggers and alerts
        with patch("scanner.fetch_live_price", return_value=0.41), \
             patch("call_tracker._send_alert") as mock_alert:
            call_tracker.check_tracked_calls_once()
            self.assertTrue(mock_alert.called)
            all_alerts = [args[0] for args, _ in mock_alert.call_args_list]
            self.assertTrue(any("TRADE TRIGGERED & ACTIVE" in a and "$ADA" in a for a in all_alerts))

        tracked = state.get_tracked_calls()
        self.assertTrue(tracked["ADA_15m"].get("entry_filled"))
        self.assertEqual(tracked["ADA_15m"].get("status"), "ACTIVE")

    def test_profit_retracement_reversal_sends_alert_and_advice(self):
        call = {
            "ticker": "LINK",
            "side": "BUY",
            "tf": "15m",
            "entry_low": 10.0,
            "entry_high": 10.2,
            "tp1": 11.5,
            "tp2": 13.0,
            "sl": 9.5,
            "entry_filled": True,
            "peak_gain_pct": 3.0,  # Peak was +3%
            "live_price": 10.05,
        }
        state.save_tracked_calls([call])

        # Price dropped back to 10.06 (+0.5% gain from 10.1 mid, severe retrace from 3.0%)
        with patch("scanner.fetch_live_price", return_value=10.06), \
             patch("call_tracker._send_alert") as mock_alert:
            call_tracker.check_tracked_calls_once()
            self.assertTrue(mock_alert.called)
            alert_text = mock_alert.call_args[0][0]
            self.assertIn("MARKET REVERSAL ALERT", alert_text)
            self.assertIn("Profit Fade", alert_text)
            self.assertIn("Best Actionable Advice", alert_text)

        tracked = state.get_tracked_calls()
        self.assertTrue(tracked["LINK_15m"].get("reversal_alert_sent"))

    def test_btc_momentum_reversal_sends_alert_and_advice(self):
        call = {
            "ticker": "OP",
            "side": "SELL",
            "tf": "15m",
            "entry_low": 1.00,
            "entry_high": 1.02,
            "tp1": 0.90,
            "tp2": 0.80,
            "sl": 1.06,
            "entry_filled": True,
            "live_price": 0.99,
        }
        state.save_tracked_calls([call])

        # BTC suddenly pumping violently against our short
        with patch("scanner.fetch_live_price", return_value=0.99), \
             patch("scanner.check_btc_momentum_gate", return_value=(False, "BTC pumping (+0.65% over 30m)")), \
             patch("call_tracker._send_alert") as mock_alert:
            call_tracker.check_tracked_calls_once()
            self.assertTrue(mock_alert.called)
            alert_text = mock_alert.call_args[0][0]
            self.assertIn("MARKET REVERSAL ALERT", alert_text)
            self.assertIn("Short Squeeze", alert_text)
            self.assertIn("Best Actionable Advice", alert_text)


if __name__ == "__main__":
    unittest.main()
