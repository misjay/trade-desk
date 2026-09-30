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


if __name__ == "__main__":
    unittest.main()
