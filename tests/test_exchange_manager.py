"""
tests/test_exchange_manager.py — Unit tests for CEX exchange connection & multi-user execution.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import exchange_manager
import user_store


class TestExchangeManager(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.mkdtemp()
        cls.orig_users_file = user_store._USERS_FILE
        user_store._USERS_FILE = Path(cls.temp_dir) / "test_users_exchange.json"
        # Isolate secret encryption key
        cls.orig_secret_file = exchange_manager._SECRET_KEY_FILE
        exchange_manager._SECRET_KEY_FILE = Path(cls.temp_dir) / ".test_secret.key"
        exchange_manager._fernet_instance = None

    @classmethod
    def tearDownClass(cls):
        user_store._USERS_FILE = cls.orig_users_file
        exchange_manager._SECRET_KEY_FILE = cls.orig_secret_file
        exchange_manager._fernet_instance = None
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def setUp(self):
        if user_store._USERS_FILE.exists():
            user_store._USERS_FILE.unlink()

    def test_encryption_and_decryption_roundtrip(self):
        raw_key = "my_bybit_super_secret_key_12345"
        encrypted = exchange_manager.encrypt_secret(raw_key)
        self.assertNotEqual(raw_key, encrypted)
        decrypted = exchange_manager.decrypt_secret(encrypted)
        self.assertEqual(raw_key, decrypted)

    def test_mask_key(self):
        self.assertEqual(exchange_manager.mask_key("aB3dEfGh123489xZ"), "aB3d...89xZ")
        self.assertEqual(exchange_manager.mask_key("short"), "****")

    def test_verify_and_connect_bybit(self):
        mock_client = MagicMock()
        mock_client.get_wallet_balance.return_value = {"equity": 1500.50, "available": 1200.00}

        with patch("bybit_client.BybitClient", return_value=mock_client):
            success, msg, eq = exchange_manager.verify_and_connect_bybit(
                chat_id="user_200",
                api_key="AK_1234567890",
                api_secret="SEC_9876543210",
                is_testnet=False,
                risk_per_trade=75.0,
            )
            self.assertTrue(success)
            self.assertEqual(eq, 1500.50)

            # Check user_store state
            ex = user_store.get_user_exchange("user_200")
            self.assertTrue(ex["connected"])
            self.assertEqual(ex["name"], "bybit")
            self.assertEqual(ex["risk_per_trade_usdt"], 75.0)
            self.assertIn("...", ex["api_key_masked"])
            # Ensure decrypted credentials match
            raw_k = exchange_manager.decrypt_secret(ex["encrypted_api_key"])
            self.assertEqual(raw_k, "AK_1234567890")

    def test_disconnect_user_exchange(self):
        user_store.set_user_exchange(
            chat_id="user_201",
            exchange_name="bybit",
            api_key_enc="enc_k",
            api_secret_enc="enc_s",
            api_key_masked="aB...xZ",
        )
        self.assertTrue(user_store.get_user_exchange("user_201")["connected"])

        user_store.disconnect_user_exchange("user_201")
        ex = user_store.get_user_exchange("user_201")
        self.assertFalse(ex["connected"])
        self.assertEqual(ex["encrypted_api_key"], "")

    def test_execute_trade_live_bybit(self):
        # 1. Connect user
        user_store.set_user_exchange(
            chat_id="user_300",
            exchange_name="bybit",
            api_key_enc=exchange_manager.encrypt_secret("TEST_KEY"),
            api_secret_enc=exchange_manager.encrypt_secret("TEST_SECRET"),
            api_key_masked="TEST...KEY",
            risk_per_trade=100.0,
        )

        mock_client = MagicMock()
        mock_client.quantize_qty.return_value = 0.5
        mock_client.get_instrument_info.return_value = {"min_qty": 0.01}
        mock_client.place_order.return_value = {"orderId": "ORD_9999"}

        with patch.object(exchange_manager, "get_user_bybit_client", return_value=mock_client):
            success, msg = exchange_manager.execute_trade_for_user(
                chat_id="user_300",
                ticker="SOL",
                side="BUY",
                live_price=150.0,
                sl=142.0,
                tp1=160.0,
            )
            self.assertTrue(success)
            self.assertIn("Live Bybit order placed", msg)
            mock_client.set_isolated_margin_and_leverage.assert_called_once()
            mock_client.place_order.assert_called_once()

            # Verify position registered in user's portfolio
            open_pos = user_store.get_user_open_positions("user_300")
            self.assertEqual(len(open_pos), 1)

    def test_execute_trade_paused_or_avoided(self):
        user_store.set_user_paused("user_301", True)
        success, msg = exchange_manager.execute_trade_for_user(
            chat_id="user_301", ticker="BTC", side="BUY", live_price=60000.0
        )
        self.assertFalse(success)
        self.assertIn("paused", msg.lower())

        user_store.set_user_paused("user_301", False)
        user_store.add_user_avoid("user_301", ["DOGE"])
        success2, msg2 = exchange_manager.execute_trade_for_user(
            chat_id="user_301", ticker="DOGE", side="BUY", live_price=0.15
        )
        self.assertFalse(success2)
        self.assertIn("avoid list", msg2.lower())

    def test_dispatch_triggered_trade_to_users(self):
        user_store.set_user_exchange(
            chat_id="user_auto_1",
            exchange_name="bybit",
            api_key_enc=exchange_manager.encrypt_secret("KEY1"),
            api_secret_enc=exchange_manager.encrypt_secret("SEC1"),
            api_key_masked="KEY1...1",
            auto_trade=True,
        )

        with patch.object(exchange_manager, "execute_trade_for_user", return_value=(True, "OK")):
            with patch("telegram_bot._reply") as mock_reply:
                sig = {
                    "ticker": "ETH",
                    "side": "BUY",
                    "live_price": 2500.0,
                    "tp1": 2600.0,
                    "sl": 2450.0,
                }
                count = exchange_manager.dispatch_triggered_trade_to_users(sig)
                self.assertEqual(count, 1)
                mock_reply.assert_called_once()
                self.assertIn("Winz Auto-Trade Executed", mock_reply.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
