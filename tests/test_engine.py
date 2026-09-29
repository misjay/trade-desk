"""
test_engine.py — Tests for demo position sizing and state management.
"""
import os
import sys
import unittest
from pathlib import Path
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TRADE_MODE", "demo")
os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test")


class TestPositionSizing(unittest.TestCase):
    def test_qty_basic(self):
        import engine
        qty = engine._compute_qty(
            entry_mid=60000.0,
            sl=59700.0,    # $300 risk per unit
            equity=10000.0,
            risk_pct=0.005  # 0.5%
        )
        # Raw: (10000 * 0.005) / 300 = 0.1667
        # But notional = 0.1667 * 60000 = 10000 > 20% equity cap (2000)
        # So qty gets capped: 2000 / 60000 ≈ 0.033333
        expected = round(10000.0 * 0.20 / 60000.0, 6)
        self.assertAlmostEqual(qty, expected, places=4)

    def test_qty_notional_cap(self):
        import engine
        # Very tight SL would produce huge qty — should be capped at 20% equity
        qty = engine._compute_qty(
            entry_mid=100.0,
            sl=99.99,       # $0.01 risk → massive qty
            equity=10000.0,
            risk_pct=0.005,
        )
        notional = qty * 100.0
        self.assertLessEqual(notional, 10000.0 * 0.20 + 1)  # +1 for float rounding

    def test_qty_zero_sl(self):
        import engine
        qty = engine._compute_qty(
            entry_mid=100.0,
            sl=100.0,   # SL == entry → undefined
            equity=10000.0,
            risk_pct=0.005,
        )
        self.assertEqual(qty, 0.0)


class TestDemoState(unittest.TestCase):
    def setUp(self):
        # Use a temp state file
        import state as st
        self._orig = st._STATE_FILE
        st._STATE_FILE = Path(tempfile.mktemp(suffix=".json"))
        st.reset_state()

    def tearDown(self):
        import state as st
        st._STATE_FILE.unlink(missing_ok=True)
        st._STATE_FILE = self._orig

    def test_open_and_close_position_pnl(self):
        import state as st
        pos = {
            "id": "BTC_BUY_perp_scalp_test",
            "ticker": "BTC",
            "side": "BUY",
            "market": "perp",
            "trade_type": "scalp",
            "entry_price": 60000.0,
            "qty": 0.01,
            "tp1": 61000.0,
            "tp2": 62000.0,
            "sl": 59500.0,
            "leverage": 5,
            "binance_order_id": None,
            "opened_at": "2024-01-01T00:00:00+00:00",
        }
        st.open_position(pos)
        self.assertIn("BTC_BUY_perp_scalp_test", st.get_open_positions())

        closed = st.close_position("BTC_BUY_perp_scalp_test", 61000.0, "TP1_HIT")
        self.assertIsNotNone(closed)
        # PnL: (61000 - 60000) / 60000 * 5 * (60000 * 0.01) = positive
        self.assertGreater(closed["pnl_usdt"], 0)
        self.assertNotIn("BTC_BUY_perp_scalp_test", st.get_open_positions())

    def test_equity_updates_on_close(self):
        import state as st
        initial = st.get_equity()
        pos = {
            "id": "ETH_SELL_perp_scalp_test",
            "ticker": "ETH",
            "side": "SELL",
            "market": "perp",
            "trade_type": "scalp",
            "entry_price": 3000.0,
            "qty": 1.0,
            "tp1": 2900.0,
            "tp2": 2800.0,
            "sl": 3100.0,
            "leverage": 3,
            "binance_order_id": None,
            "opened_at": "2024-01-01T00:00:00+00:00",
        }
        st.open_position(pos)
        closed = st.close_position("ETH_SELL_perp_scalp_test", 2900.0, "TP1_HIT")
        self.assertIsNotNone(closed)
        new_equity = st.get_equity()
        self.assertGreater(new_equity, initial)  # winning trade


if __name__ == "__main__":
    unittest.main()
