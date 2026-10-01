"""
test_engine.py — Tests for Bybit Trade Desk Engine, 0.5% Risk Sizing, and Guardrails.
"""
import os
import sys
import unittest
from pathlib import Path
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["TRADE_MODE"] = "demo"
os.environ["DEMO_ENV"] = "paper"


class TestEngineAndSizing(unittest.TestCase):
    def test_position_sizing_half_percent(self):
        import engine
        # Equity = $10,000, 0.5% risk = $50 risk capital
        # BTC Entry = $80,000, SL = $79,500 ($500 distance per unit)
        # Expected raw qty = 50 / 500 = 0.1 BTC
        perp_qty, spot_qty, note = engine.compute_position_size(
            ticker="BTC",
            entry_price=80000.0,
            sl=79500.0,
            equity=10000.0,
            leverage=5,
            risk_pct=0.005,
        )
        self.assertEqual(note, "OK")
        self.assertAlmostEqual(perp_qty, 0.1, places=3)
        self.assertAlmostEqual(spot_qty, 0.1, places=3)

    def test_position_sizing_pepe_contract_multiplier(self):
        import engine
        # Bybit Linear uses 1000PEPE contracts
        # Equity = $10,000, 0.5% risk = $50
        # PEPE price = 0.000010, SL = 0.000009 ($0.000001 dist)
        # Raw qty tokens = 50 / 0.000001 = 50,000,000 tokens
        # In 1000PEPE contracts = 50,000 contracts
        perp_qty, spot_qty, note = engine.compute_position_size(
            ticker="PEPE",
            entry_price=0.000010,
            sl=0.000009,
            equity=10000.0,
            leverage=3,
            risk_pct=0.005,
        )
        self.assertEqual(note, "OK")
        self.assertGreater(perp_qty, 0)
        self.assertEqual(perp_qty, 50000.0)

    def test_never_market_rule(self):
        import engine
        for t in ["PEPE", "TAO", "ENA", "HBAR", "NEAR"]:
            valid, reason = engine.validate_execution_conditions(
                ticker=t,
                side="BUY",
                entry_low=10.0,
                entry_high=10.2,
                is_market=True,
            )
            self.assertFalse(valid)
            self.assertIn("Never market", reason)

    def test_no_chase_rule(self):
        import engine
        from unittest.mock import patch
        # If live mark has left the band (e.g. Price is far above demand shelf)
        with patch.object(engine.client, "get_ticker", return_value={"last_price": 84000.0, "mark_price": 84000.0}):
            valid, reason = engine.validate_execution_conditions(
                ticker="BTC",
                side="BUY",
                entry_low=60000.0,
                entry_high=60200.0,
            )
            self.assertFalse(valid)
            self.assertIn("NO_CHASE", reason)


class TestContractParsing(unittest.TestCase):
    def test_parse_wait_line(self):
        import engine
        res = engine.parse_and_execute_contract_line("BOT|BTC|WAIT||||||")
        self.assertEqual(res["status"], "WAIT")
        self.assertEqual(res["ticker"], "BTC")

    def test_parse_buy_line(self):
        import engine
        from unittest.mock import patch
        line = "BOT|ETH|BUY|PERP|15m|2500|2520|2650|2750|2460|4|0.005|2026-09-30 20:00 UTC|valid"
        with patch.object(engine.client, "get_ticker", return_value={"last_price": 3000.0, "mark_price": 3000.0}):
            res = engine.parse_and_execute_contract_line(line)
            # Should be processed (either NO_CHASE or executed based on live mark)
            self.assertIn(res["status"], ["INVALID", "SUCCESS", "REJECTED"])


class TestDemoState(unittest.TestCase):
    def setUp(self):
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
            "order_id": None,
            "opened_at": "2024-01-01T00:00:00+00:00",
        }
        st.open_position(pos)
        self.assertIn("BTC_BUY_perp_scalp_test", st.get_open_positions())

        closed = st.close_position("BTC_BUY_perp_scalp_test", 61000.0, "TP1_HIT")
        self.assertIsNotNone(closed)
        self.assertGreater(closed["pnl_usdt"], 0)
        self.assertNotIn("BTC_BUY_perp_scalp_test", st.get_open_positions())

    def test_sync_with_bybit_flow(self):
        """Verify that _sync_with_bybit correctly syncs active positions and closed PnL."""
        import engine
        import state as st
        from unittest.mock import MagicMock

        mock_active = [{
            "symbol": "HYPEUSDT",
            "side": "Sell",
            "size": 326.96,
            "entry_price": 90.46,
            "mark_price": 90.51,
            "unrealised_pnl": -16.348,
            "stop_loss": 91.23,
            "take_profit": 88.03,
            "leverage": 4,
        }]
        mock_closed = [{
            "order_id": "test_order_closed_1",
            "symbol": "BTCUSDT",
            "side": "Sell",
            "qty": 0.024,
            "closed_pnl": 31.57,
            "entry_price": 83000.0,
            "exit_price": 84381.0,
            "updated_time": "1790682579052",
            "exec_type": "Trade",
        }]

        with unittest.mock.patch.object(engine.client, "get_wallet_balance", return_value={"equity": 50000.0}), \
             unittest.mock.patch.object(engine.client, "get_active_positions", return_value=mock_active), \
             unittest.mock.patch.object(engine.client, "get_closed_pnl", return_value=mock_closed), \
             unittest.mock.patch.object(engine.client, "get_open_orders", return_value=[]):
            engine._sync_with_bybit()

        self.assertEqual(st.get_equity(), 50000.0)
        open_pos = st.get_open_positions()
        self.assertTrue(any(p["ticker"] == "HYPE" and p["side"] == "SELL" for p in open_pos.values()))
        closed_pos = st.get_closed_positions()
        self.assertTrue(any(c["order_id"] == "test_order_closed_1" for c in closed_pos))


if __name__ == "__main__":
    unittest.main()
