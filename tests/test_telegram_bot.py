"""
test_telegram_bot.py — Unit tests for interactive Telegram command handler.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

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


if __name__ == "__main__":
    unittest.main()
