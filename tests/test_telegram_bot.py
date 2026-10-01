"""
test_telegram_bot.py — Unit tests for interactive Telegram command handler.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("TRADE_MODE", "demo")
os.environ.setdefault("DEMO_ENV", "paper")


class TestTelegramBot(unittest.TestCase):
    def setUp(self):
        import state
        state.set_paused(False)

    def test_help_command(self):
        import telegram_bot
        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/help", "12345")
            mock_reply.assert_called_once()
            self.assertIn("Xira Autonomous Trade Desk", mock_reply.call_args[0][1])

    def test_pause_resume_commands(self):
        import telegram_bot
        import state
        with patch.object(telegram_bot, "_reply"):
            telegram_bot.handle_command("/pause", "12345")
            self.assertTrue(state.is_paused())

            telegram_bot.handle_command("/resume", "12345")
            self.assertFalse(state.is_paused())

    def test_spot_toggle_commands(self):
        import telegram_bot
        import state
        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/offspot", "12345")
            self.assertFalse(state.is_spot_enabled())
            self.assertIn("Spot Trading DISABLED", mock_reply.call_args[0][1])

            telegram_bot.handle_command("/onspot", "12345")
            self.assertTrue(state.is_spot_enabled())
            self.assertIn("Spot Trading ENABLED", mock_reply.call_args[0][1])

            # Reset back to False
            state.set_spot_enabled(False)

    def test_status_command(self):
        import telegram_bot
        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/status", "12345")
            mock_reply.assert_called_once()
            self.assertIn("Xira Trade Desk Status", mock_reply.call_args[0][1])

    def test_scan_trigger_command(self):
        import telegram_bot
        scan_called = []
        def dummy_scan():
            scan_called.append(True)

        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/scan", "12345", scan_trigger_fn=dummy_scan)
            mock_reply.assert_called_once()

    def test_avoid_and_allow_commands(self):
        import telegram_bot
        import state

        # Test /avoid
        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/avoid DOGE PEPE", "12345")
            mock_reply.assert_called_once()
            avoid_list = state.get_avoid_list()
            self.assertIn("DOGE", avoid_list)
            self.assertIn("PEPE", avoid_list)

        # Test /avoided
        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/avoided", "12345")
            mock_reply.assert_called_once()
            self.assertIn("DOGE", mock_reply.call_args[0][1])

        # Test /allow
        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/allow DOGE", "12345")
            mock_reply.assert_called_once()
            avoid_list = state.get_avoid_list()
            self.assertNotIn("DOGE", avoid_list)
            self.assertIn("PEPE", avoid_list)

        # Cleanup
        state.remove_from_avoid_list(["PEPE"])

    def test_drop_command(self):
        import telegram_bot
        import state
        import engine

        with patch.object(engine.client, "get_active_positions", return_value=[]), \
             patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/drop XLM", "12345")
            mock_reply.assert_called_once()
            self.assertIn("XLM", state.get_avoid_list())

        # Cleanup
        state.remove_from_avoid_list(["XLM"])

    def test_derisk_command(self):
        import telegram_bot
        import engine

        fake_positions = [
            {
                "symbol": "BTCUSDT",
                "side": "Buy",
                "size": 1.0,
                "unrealised_pnl": 150.0,  # winner
            },
            {
                "symbol": "ETHUSDT",
                "side": "Buy",
                "size": 2.0,
                "unrealised_pnl": -50.0,  # loser
            },
        ]

        closed_calls = []
        def mock_close(sym, side, size):
            closed_calls.append((sym, side, size))
            return {"orderId": "mock_id"}

        with patch.object(engine.client, "get_active_positions", return_value=fake_positions), \
             patch.object(engine.client, "close_position_market", side_effect=mock_close), \
             patch.object(engine.client, "quantize_qty", return_value=1.0), \
             patch.object(engine.client, "get_instrument_info", return_value={"min_qty": 0.001}), \
             patch.object(engine, "_sync_with_bybit"), \
             patch.object(telegram_bot, "_reply") as mock_reply:

            telegram_bot.handle_command("/derisk", "12345")
            mock_reply.assert_called_once()
            text = mock_reply.call_args[0][1]

            # Verify winner 100% closed (BTC size 1.0)
            self.assertIn("Banked in Profit", text)
            self.assertIn("BTC", text)

            # Verify loser 50% trimmed (ETH size 1.0)
            self.assertIn("Trimmed Losses 50%", text)
            self.assertIn("ETH", text)

            # Check calls
            self.assertEqual(len(closed_calls), 2)
            self.assertEqual(closed_calls[0], ("BTCUSDT", "Sell", 1.0))
            self.assertEqual(closed_calls[1], ("ETHUSDT", "Sell", 1.0))

    def test_register_bot_commands(self):
        import telegram_bot
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True}

        with patch("requests.post", return_value=mock_resp):
            success = telegram_bot.register_bot_commands()
            self.assertTrue(success)

    def test_leverage_command(self):
        import telegram_bot
        import state
        import engine

        with patch.object(engine.client, "set_isolated_margin_and_leverage", return_value=True), \
             patch.object(telegram_bot, "_reply") as mock_reply:

            telegram_bot.handle_command("/leverage BTC 10", "12345")
            mock_reply.assert_called_once()
            self.assertIn("Leverage for BTC set to 10x", mock_reply.call_args[0][1])
            self.assertEqual(state.get_custom_leverage("BTC"), 10)
            self.assertEqual(state.get_effective_leverage("BTC"), 10)

    def test_existingleverage_command(self):
        import telegram_bot
        import engine

        with patch.object(engine.client, "get_active_positions", return_value=[]), \
             patch.object(telegram_bot, "_reply") as mock_reply:

            # Query all
            telegram_bot.handle_command("/existingleverage", "12345")
            mock_reply.assert_called_once()
            self.assertIn("Existing Leverage by Asset", mock_reply.call_args[0][1])

        with patch.object(engine.client, "get_active_positions", return_value=[]), \
             patch.object(telegram_bot, "_reply") as mock_reply:

            # Query specific ticker
            telegram_bot.handle_command("/existingleverage BTC", "12345")
            mock_reply.assert_called_once()
            self.assertIn("Leverage for BTC", mock_reply.call_args[0][1])

    def test_report_commands(self):
        import telegram_bot
        with patch("analytics.send_report") as mock_send, \
             patch.object(telegram_bot, "_reply") as mock_reply:

            for cmd in ("/hourlyreport", "/dailyreport", "/weeklyreport", "/monthlyreport"):
                telegram_bot.handle_command(cmd, "12345")
                self.assertIn("Analytics Report", mock_reply.call_args[0][1])

    def test_cancel_commands(self):
        import telegram_bot
        import engine

        with patch.object(engine.client, "cancel_all_orders", return_value=["ord_1", "ord_2"]), \
             patch.object(engine, "_sync_with_bybit"), \
             patch.object(engine.client, "get_wallet_balance", return_value={"available": 50000.0}), \
             patch.object(telegram_bot, "_reply") as mock_reply:

            # Test /cancelorder BTC ETH
            telegram_bot.handle_command("/cancelorder BTC ETH", "12345")
            self.assertIn("Cancel Order Result", mock_reply.call_args[0][1])
            self.assertIn("BTC", mock_reply.call_args[0][1])

            # Test /cancelallorders
            telegram_bot.handle_command("/cancelallorders", "12345")
            self.assertIn("Cancelled All Resting Orders", mock_reply.call_args[0][1])

    def test_tp_command(self):
        import telegram_bot
        import engine

        mock_positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 0.1, "unrealised_pnl": 50.0},
            {"symbol": "ETHUSDT", "side": "Buy", "size": 1.0, "unrealised_pnl": -20.0},
        ]

        with patch.object(type(engine.client), "is_paper", new_callable=PropertyMock, return_value=False), \
             patch.object(engine.client, "get_active_positions", return_value=mock_positions), \
             patch.object(engine.client, "close_position_market", return_value={"orderId": "tp_1"}) as mock_close, \
             patch.object(engine, "_sync_with_bybit"), \
             patch.object(engine.client, "get_wallet_balance", return_value={"equity": 50050.0, "available": 40000.0}), \
             patch.object(telegram_bot, "_reply") as mock_reply:

            # 1. Run /tp (should close only BTC, keep ETH open)
            telegram_bot.handle_command("/tp", "12345")
            self.assertIn("Take Profit Executed", mock_reply.call_args[0][1])
            self.assertIn("BTC", mock_reply.call_args[0][1])
            self.assertIn("50.00", mock_reply.call_args[0][1])
            self.assertIn("Kept Open", mock_reply.call_args[0][1])
            self.assertIn("ETH", mock_reply.call_args[0][1])
            mock_close.assert_called_once_with("BTCUSDT", "Sell", 0.1)

            # 2. When no positions in profit
            mock_close.reset_mock()
            with patch.object(engine.client, "get_active_positions", return_value=[mock_positions[1]]):
                telegram_bot.handle_command("/tp", "12345")
                self.assertIn("No positions are currently in profit", mock_reply.call_args[0][1])
                mock_close.assert_not_called()

    def test_feedback_commands(self):
        import telegram_bot
        import state

        with patch("learning_engine.send_daily_feedback") as mock_send, \
             patch.object(telegram_bot, "_reply") as mock_reply:

            # Test /feedback
            telegram_bot.handle_command("/feedback", "12345")
            self.assertIn("Compiling Daily Intelligence", mock_reply.call_args[0][1])

            # Test /feedbackbot
            telegram_bot.handle_command("/feedbackbot", "12345")
            self.assertIn("Daily Feedback Destination", mock_reply.call_args[0][1])

            # Test /setfeedbackbot
            with patch("requests.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {"ok": True}
                mock_post.return_value = mock_resp

                telegram_bot.handle_command("/setfeedbackbot mock_token_123 999888", "12345")
                self.assertIn("Feedback Bot Successfully Configured", mock_reply.call_args[0][1])
                cfg_fb = state.get_feedback_bot_config()
                self.assertEqual(cfg_fb["token"], "mock_token_123")
                self.assertEqual(cfg_fb["chat_id"], "999888")

                # Clean up state
                s = state.get_state()
                if "feedback_bot" in s:
                    del s["feedback_bot"]
                    state._save(s)


if __name__ == "__main__":
    unittest.main()

