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
    @classmethod
    def setUpClass(cls):
        import tempfile
        import shutil
        from pathlib import Path
        import state
        import user_store
        import exchange_manager
        cls.temp_dir = tempfile.mkdtemp()
        cls.orig_state_file = state._STATE_FILE
        state._STATE_FILE = Path(cls.temp_dir) / "test_state.json"
        cls.orig_users_file = user_store._USERS_FILE
        user_store._USERS_FILE = Path(cls.temp_dir) / "test_users_state.json"
        cls.orig_secret_file = exchange_manager._SECRET_KEY_FILE
        exchange_manager._SECRET_KEY_FILE = Path(cls.temp_dir) / ".test_secret.key"
        exchange_manager._fernet_instance = None

    @classmethod
    def tearDownClass(cls):
        import shutil
        import state
        import user_store
        import exchange_manager
        state._STATE_FILE = cls.orig_state_file
        user_store._USERS_FILE = cls.orig_users_file
        exchange_manager._SECRET_KEY_FILE = cls.orig_secret_file
        exchange_manager._fernet_instance = None
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def setUp(self):
        import state
        from config import cfg
        cfg.telegram_chat_id = "12345"
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

    def test_research_command(self):
        import telegram_bot
        with patch("market_research.generate_market_research", return_value="The thesis:\n- Sample thesis"), \
             patch("market_research.send_call_research_to_feedback_bot") as mock_send, \
             patch.object(telegram_bot, "_reply") as mock_reply:

            telegram_bot.handle_command("/research BTC", "12345")
            mock_reply.assert_called()
            self.assertIn("Institutional Research & Charts for BTC", mock_reply.call_args[0][1])

    def test_probation_and_avoided_commands(self):
        import telegram_bot
        import state

        # When nothing on probation
        with patch.object(state, "get_probation_list", return_value={}), \
             patch.object(state, "get_quarantine_list", return_value={}), \
             patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/probation", "12345")
            self.assertIn("All systems clear", mock_reply.call_args[0][1])

        # Quarantine asset
        mock_quar = {
            "TESTX": {
                "ticker": "TESTX",
                "expires_at": "2026-10-03T00:00:00+00:00",
                "reason": "Drawdown limit"
            }
        }
        with patch.object(state, "get_probation_list", return_value={}), \
             patch.object(state, "get_quarantine_list", return_value=mock_quar), \
             patch.object(state, "get_avoid_list", return_value=["TESTX"]), \
             patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/probation", "12345")
            self.assertIn("Quarantined Assets", mock_reply.call_args[0][1])
            self.assertIn("TESTX", mock_reply.call_args[0][1])

            telegram_bot.handle_command("/avoided", "12345")
            self.assertIn("TESTX", mock_reply.call_args[0][1])
            self.assertIn("quarantine", mock_reply.call_args[0][1])

    def test_all_bot_commands_registered(self):
        import telegram_bot
        cmd_names = [c["command"] for c in telegram_bot.BOT_COMMANDS]

        # Verify key commands are in BOT_COMMANDS
        required = [
            "status", "positions", "tp", "derisk", "dailyreport", "hourlyreport",
            "weeklyreport", "monthlyreport", "hourlycalls", "setcallbot", "callbot",
            "feedback", "research", "tweet", "feedbackbot", "setfeedbackbot",
            "probation", "leverage", "existingleverage", "scan", "avoid", "allow",
            "avoided", "drop", "pause", "resume", "onspot", "offspot", "cancelorder",
            "cancelallorders", "close", "closeall", "help"
        ]
        for cmd in required:
            self.assertIn(cmd, cmd_names, f"Command /{cmd} missing from BOT_COMMANDS")

        # Telegram constraints:
        for c in telegram_bot.BOT_COMMANDS:
            self.assertTrue(1 <= len(c["command"]) <= 32)
            self.assertTrue(c["command"].islower())
            self.assertTrue(1 <= len(c["description"]) <= 256)

        # Test registration
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True}
        with patch("requests.post", return_value=mock_resp):
            success = telegram_bot.register_bot_commands()
            self.assertTrue(success)


    def test_commands_while_paused_remain_paused(self):
        """
        Verify that issuing ANY command while paused executes the instruction
        normally, includes the paused reminder footer, and NEVER automatically resumes.
        Only explicit /resume should resume.
        """
        import telegram_bot
        import state
        import engine

        state.set_paused(True)
        self.assertTrue(state.is_paused())

        # 1. /onspot while paused
        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/onspot", "12345")
            self.assertTrue(state.is_spot_enabled())
            self.assertTrue(state.is_paused(), "Bot must remain paused after /onspot")
            self.assertIn("Execution remains PAUSED", mock_reply.call_args[0][1])

        # 2. /offspot while paused
        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/offspot", "12345")
            self.assertFalse(state.is_spot_enabled())
            self.assertTrue(state.is_paused(), "Bot must remain paused after /offspot")
            self.assertIn("Execution remains PAUSED", mock_reply.call_args[0][1])

        # 3. /avoid and /allow while paused
        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/avoid AVAX", "12345")
            self.assertIn("AVAX", state.get_avoid_list())
            self.assertTrue(state.is_paused(), "Bot must remain paused after /avoid")
            self.assertIn("Execution remains PAUSED", mock_reply.call_args[0][1])

        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/allow AVAX", "12345")
            self.assertNotIn("AVAX", state.get_avoid_list())
            self.assertTrue(state.is_paused(), "Bot must remain paused after /allow")
            self.assertIn("Execution remains PAUSED", mock_reply.call_args[0][1])

        # 4. /leverage while paused
        with patch.object(engine.client, "set_isolated_margin_and_leverage", return_value=True), \
             patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/leverage AVAX 5", "12345")
            self.assertEqual(state.get_custom_leverage("AVAX"), 5)
            self.assertTrue(state.is_paused(), "Bot must remain paused after /leverage")
            self.assertIn("Execution remains PAUSED", mock_reply.call_args[0][1])

        # 5. /cancelallorders while paused
        with patch.object(engine.client, "cancel_all_orders", return_value=[]), \
             patch.object(engine, "_sync_with_bybit"), \
             patch.object(engine.client, "get_wallet_balance", return_value={"available": 100000.0}), \
             patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/cancelallorders", "12345")
            self.assertTrue(state.is_paused(), "Bot must remain paused after /cancelallorders")
            self.assertIn("Execution remains PAUSED", mock_reply.call_args[0][1])

        # 6. /scan while paused
        scan_executed = []
        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/scan", "12345", scan_trigger_fn=lambda: scan_executed.append(True))
            self.assertTrue(state.is_paused(), "Bot must remain paused after /scan")
            self.assertIn("Execution remains PAUSED", mock_reply.call_args[0][1])

        # 7. Finally, only explicit /resume unpauses the bot
        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/resume", "12345")
            self.assertFalse(state.is_paused(), "Bot should be resumed after explicit /resume")
            self.assertIn("Automated execution RESUMED", mock_reply.call_args[0][1])

    def test_research_timeframes_command(self):
        import telegram_bot
        import market_research

        with patch.object(telegram_bot, "_reply") as mock_reply, \
             patch.object(market_research, "build_signal_for_timeframe") as mock_build, \
             patch.object(market_research, "send_research_with_chart") as mock_send:

            mock_build.return_value = {
                "ticker": "ETH", "side": "BUY", "tf": "15m",
                "entry_low": 2400.0, "entry_high": 2420.0,
                "tp1": 2460.0, "tp2": 2500.0, "sl": 2370.0, "rr": 2.0
            }

            # 1. Natural language research (day and scalp) ETH
            telegram_bot.handle_command("research (day and scalp) ETH", "12345")
            mock_reply.assert_called()
            self.assertIn("Compiling Day & Scalp", mock_reply.call_args[0][1])

            # 2. Scalp only: research (scalp) SOL
            telegram_bot.handle_command("research (scalp) SOL", "12345")
            self.assertIn("Compiling Scalp", mock_reply.call_args[0][1])

            # 3. Day only: /research day BTC
            telegram_bot.handle_command("/research day BTC", "12345")
            self.assertIn("Compiling Day", mock_reply.call_args[0][1])

    def test_twitter_post_command(self):
        import telegram_bot
        import market_research

        with patch.object(market_research, "send_twitter_post") as mock_send_tweet:
            mock_send_tweet.return_value = True

            # 1. /tweet BTC
            telegram_bot.handle_command("/tweet BTC", "12345")
            mock_send_tweet.assert_called()

            # 2. Natural language: convert to Twitter post
            mock_send_tweet.reset_mock()
            telegram_bot.handle_command("convert to Twitter post", "12345")
            mock_send_tweet.assert_called()

    def test_non_admin_blocked_from_master_desk_commands(self):
        import telegram_bot
        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/scan", "99999_stranger")
            mock_reply.assert_called_once()
            self.assertIn("Access Restricted: Master Desk Command", mock_reply.call_args[0][1])

    def test_public_user_duplicated_commands(self):
        import telegram_bot
        import user_store
        import state

        stranger_id = "88888_trader"

        # 1. /status for public user shows personal portfolio, does NOT call engine Bybit
        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/status", stranger_id)
            mock_reply.assert_called_once()
            self.assertIn("Your Personal Trading Desk Status", mock_reply.call_args[0][1])
            self.assertIn("$10,000.00 USDT", mock_reply.call_args[0][1])

        # 2. /pause & /resume isolate between public user and master desk
        with patch.object(telegram_bot, "_reply") as mock_reply:
            state.set_paused(False)
            telegram_bot.handle_command("/pause", stranger_id)
            self.assertTrue(user_store.is_user_paused(stranger_id))
            self.assertFalse(state.is_paused())  # Master desk remains unpaused!

            telegram_bot.handle_command("/resume", stranger_id)
            self.assertFalse(user_store.is_user_paused(stranger_id))

        # 3. /avoid & /allow & /avoided isolate to public user
        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/avoid DOGE PEPE", stranger_id)
            user_avoid = user_store.get_user_avoid_list(stranger_id)
            self.assertIn("DOGE", user_avoid)
            self.assertIn("PEPE", user_avoid)
            self.assertNotIn("DOGE", state.get_avoid_list())  # Master desk unaffected!

            telegram_bot.handle_command("/allow DOGE", stranger_id)
            user_avoid = user_store.get_user_avoid_list(stranger_id)
            self.assertNotIn("DOGE", user_avoid)
            self.assertIn("PEPE", user_avoid)

        # 4. /leverage & /existingleverage isolate to public user
        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/leverage SOL 20", stranger_id)
            self.assertEqual(user_store.get_user_leverage(stranger_id, "SOL"), 20)
            telegram_bot.handle_command("/existingleverage", stranger_id)
            self.assertIn("20x", mock_reply.call_args[0][1])

        # 5. /trade opens a paper position for public user
        with patch("scanner.fetch_live_price", return_value=150.0):
            with patch.object(telegram_bot, "_reply") as mock_reply:
                telegram_bot.handle_command("/trade SOL BUY", stranger_id)
                open_pos = user_store.get_user_open_positions(stranger_id)
                self.assertEqual(len(open_pos), 1)
                self.assertIn("Opened Personal Demo Trade", mock_reply.call_args[0][1])

                # /positions displays the trade
                telegram_bot.handle_command("/positions", stranger_id)
                self.assertIn("Your Personal Active Positions", mock_reply.call_args[0][1])
                self.assertIn("SOL", mock_reply.call_args[0][1])

        # 6. /tp and /closeall operate on public user's positions
        with patch("scanner.fetch_live_price", return_value=160.0):
            with patch.object(telegram_bot, "_reply") as mock_reply:
                telegram_bot.handle_command("/tp", stranger_id)
                self.assertIn("Personal Take Profit Executed", mock_reply.call_args[0][1])
                self.assertEqual(len(user_store.get_user_open_positions(stranger_id)), 0)

        # 7. /resetdemo resets the public user's portfolio
        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/resetdemo", stranger_id)
            self.assertIn("Personal Demo Portfolio Reset", mock_reply.call_args[0][1])
            self.assertEqual(user_store.get_user_equity(stranger_id), 10000.0)

    def test_subscribe_and_unsubscribe_commands(self):
        import telegram_bot
        import state
        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/subscribe", "77777_user")
            self.assertTrue(state.is_call_bot_subscribed("77777_user"))
            self.assertIn("Subscribed to Winz Signals & Alerts", mock_reply.call_args[0][1])

            telegram_bot.handle_command("/unsubscribe", "77777_user")
            self.assertFalse(state.is_call_bot_subscribed("77777_user"))
            self.assertIn("Unsubscribed from Broadcasts", mock_reply.call_args[0][1])

    def test_public_user_help_menu(self):
        import telegram_bot
        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/help", "99999_stranger")
            mock_reply.assert_called_once()
    def test_cex_connection_telegram_commands(self):
        import telegram_bot
        import user_store
        import exchange_manager

        user_id = "55555_trader"

        # 1. /myexchange when not connected
        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/myexchange", user_id)
            self.assertIn("No Exchange Account Connected", mock_reply.call_args[0][1])

        # 2. /connect bybit <KEY> <SECRET>
        with patch("exchange_manager.verify_and_connect_bybit", return_value=(True, "OK", 1250.0)):
            with patch.object(telegram_bot, "_reply") as mock_reply:
                with patch.object(telegram_bot, "_delete_message") as mock_delete:
                    telegram_bot.handle_command("/connect bybit REALKEY REALSECRET", user_id, message_id=42)
                    mock_delete.assert_called_once_with(user_id, 42, bot_token=None)
                    self.assertIn("Bybit Exchange Connected Successfully", mock_reply.call_args[0][1])

        # 3. /risk 100
        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/risk 100", user_id)
            self.assertIn("Risk Sizing Updated", mock_reply.call_args[0][1])
            self.assertEqual(user_store.get_user_exchange(user_id)["risk_per_trade_usdt"], 100.0)

        # 4. /autotrade off and on
        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/autotrade off", user_id)
            self.assertFalse(user_store.get_user_exchange(user_id)["auto_trade"])
            telegram_bot.handle_command("/autotrade on", user_id)
            self.assertTrue(user_store.get_user_exchange(user_id)["auto_trade"])

        # 5. /disconnect
        with patch.object(telegram_bot, "_reply") as mock_reply:
            telegram_bot.handle_command("/disconnect", user_id)
            self.assertIn("Exchange Disconnected", mock_reply.call_args[0][1])
            self.assertFalse(user_store.get_user_exchange(user_id)["connected"])


if __name__ == "__main__":
    unittest.main()


