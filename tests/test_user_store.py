"""
tests/test_user_store.py — Unit tests for multi-tenant user isolation.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

import user_store


class TestUserStore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.mkdtemp()
        cls.orig_file = user_store._USERS_FILE
        user_store._USERS_FILE = Path(cls.temp_dir) / "test_users_state.json"

    @classmethod
    def tearDownClass(cls):
        user_store._USERS_FILE = cls.orig_file
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def setUp(self):
        # Clear test file between tests
        if user_store._USERS_FILE.exists():
            user_store._USERS_FILE.unlink()

    def test_default_user_creation_and_equity(self):
        u1 = user_store.get_user("user_101")
        self.assertEqual(u1["user_id"], "user_101")
        self.assertEqual(u1["paper_equity"], 10000.0)
        self.assertFalse(u1["is_paused"])

        user_store.set_user_equity("user_101", 12500.0)
        self.assertEqual(user_store.get_user_equity("user_101"), 12500.0)

        # User 102 should be separate
        u2 = user_store.get_user("user_102")
        self.assertEqual(u2["paper_equity"], 10000.0)

    def test_user_pause_isolation(self):
        user_store.set_user_paused("user_101", True)
        self.assertTrue(user_store.is_user_paused("user_101"))
        self.assertFalse(user_store.is_user_paused("user_102"))

    def test_open_and_close_position(self):
        pos = {
            "ticker": "SOL",
            "side": "BUY",
            "entry_price": 100.0,
            "qty": 2.0,
            "tp1": 110.0,
            "sl": 95.0,
            "leverage": 5,
        }
        opened = user_store.open_user_position("user_101", pos)
        pid = opened["id"]
        self.assertIn(pid, user_store.get_user_open_positions("user_101"))
        self.assertEqual(len(user_store.get_user_open_positions("user_102")), 0)

        # Close position at 110 (profit: +10% on $200 notional with 5x leverage -> +$20 USDT)
        closed = user_store.close_user_position("user_101", pid, exit_price=110.0, reason="TP1_HIT")
        self.assertIsNotNone(closed)
        self.assertEqual(closed["status"], "CLOSED")
        self.assertEqual(closed["pnl_usdt"], 20.0)
        self.assertEqual(user_store.get_user_equity("user_101"), 10020.0)

        # Session stats
        stats = user_store.get_user_session_stats("user_101")
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(stats["total_pnl_usdt"], 20.0)

    def test_take_profit_and_derisk(self):
        pos1 = {"ticker": "BTC", "side": "BUY", "entry_price": 60000.0, "qty": 0.1, "leverage": 10}
        pos2 = {"ticker": "ETH", "side": "BUY", "entry_price": 3000.0, "qty": 1.0, "leverage": 10}
        user_store.open_user_position("user_101", pos1)
        user_store.open_user_position("user_101", pos2)

        # Mock prices: BTC in profit (61000), ETH in loss (2900)
        prices = {"BTC": 61000.0, "ETH": 2900.0}
        fetch_fn = lambda t: prices.get(t, 0.0)

        # TP should close BTC only
        closed_tp = user_store.tp_user_positions("user_101", fetch_fn)
        self.assertEqual(len(closed_tp), 1)
        self.assertEqual(closed_tp[0]["ticker"], "BTC")

        # Open positions should now only have ETH
        open_pos = user_store.get_user_open_positions("user_101")
        self.assertEqual(len(open_pos), 1)

        # Derisk should trim ETH size by 50%
        w, trimmed = user_store.derisk_user_positions("user_101", fetch_fn)
        self.assertEqual(len(w), 0)
        self.assertEqual(len(trimmed), 1)
        self.assertEqual(trimmed[0]["qty"], 0.5)

    def test_avoid_list_and_leverage(self):
        user_store.add_user_avoid("user_101", ["DOGE", "PEPE"])
        self.assertEqual(user_store.get_user_avoid_list("user_101"), ["DOGE", "PEPE"])
        self.assertEqual(user_store.get_user_avoid_list("user_102"), [])

        user_store.remove_user_avoid("user_101", ["PEPE"])
        self.assertEqual(user_store.get_user_avoid_list("user_101"), ["DOGE"])

        user_store.set_user_leverage("user_101", "SOL", 15)
        self.assertEqual(user_store.get_user_leverage("user_101", "SOL"), 15)
        self.assertEqual(user_store.get_user_leverage("user_102", "SOL"), 10)

    def test_reset_demo(self):
        user_store.set_user_equity("user_101", 15000.0)
        user_store.reset_user_demo("user_101")
        self.assertEqual(user_store.get_user_equity("user_101"), 10000.0)


if __name__ == "__main__":
    unittest.main()
