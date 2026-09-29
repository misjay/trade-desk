"""
test_notifier.py — Tests for Telegram card formatting.
No real network calls.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TRADE_MODE", "demo")
os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test")


def _make_sig(side="BUY", ticker="BTC"):
    return {
        "ticker": ticker,
        "side": side,
        "trade_type": "scalp",
        "tf": "15m",
        "entry_low": 91600.0,
        "entry_high": 92800.0,
        "tp1": 95000.0,
        "tp2": 98000.0,
        "sl": 90200.0,
        "rr": 2.1,
        "structure": "Demand zone $91600–$92800 | Trend: neutral",
        "reason": "Price pulling into demand. R:R 2.1.",
        "live_price": 92000.0,
        "tv_url": "https://www.tradingview.com/chart/?symbol=BINANCE:BTCUSDT",
        "timestamp": "2024-01-01T00:00:00+00:00",
    }


class TestCardFormatting(unittest.TestCase):
    def setUp(self):
        import notifier as n
        self.n = n

    def test_buy_card_has_entry_range(self):
        sig = _make_sig("BUY")
        card = self.n.format_buy_sell_card(sig)
        self.assertIn("$91,600", card)
        self.assertIn("$92,800", card)

    def test_buy_card_has_perp_and_spot_blocks(self):
        sig = _make_sig("BUY")
        card = self.n.format_buy_sell_card(sig)
        self.assertIn("PERP", card)
        self.assertIn("SPOT", card)

    def test_sell_card_says_limit_sell(self):
        sig = _make_sig("SELL")
        card = self.n.format_buy_sell_card(sig)
        self.assertIn("Limit sell", card)

    def test_buy_card_says_limit_buy(self):
        sig = _make_sig("BUY")
        card = self.n.format_buy_sell_card(sig)
        self.assertIn("Limit buy", card)

    def test_wait_card_no_entry_range(self):
        sig = {
            "ticker": "SOL",
            "side": "WAIT",
            "trade_type": "scalp",
            "tf": "15m",
            "entry_low": None,
            "entry_high": None,
            "structure": "Mid-range",
            "reason": "No clean entry",
            "live_price": 145.0,
            "tv_url": "https://www.tradingview.com/chart/?symbol=BINANCE:SOLUSDT",
            "timestamp": "2024-01-01T00:00:00+00:00",
        }
        card = self.n.format_wait_card(sig)
        self.assertIn("SOL", card)
        self.assertIn("WAIT", card)
        self.assertNotIn("Entry (limit)", card)

    def test_not_financial_advice_footer(self):
        sig = _make_sig()
        card = self.n.format_buy_sell_card(sig)
        self.assertIn("Not financial advice", card)

    def test_tp1_tp2_sl_present(self):
        sig = _make_sig()
        card = self.n.format_buy_sell_card(sig)
        self.assertIn("TP1", card)
        self.assertIn("TP2", card)
        self.assertIn("SL", card)

    def test_leverage_field_present(self):
        sig = _make_sig()
        card = self.n.format_buy_sell_card(sig)
        self.assertIn("isolated", card.lower())

    def test_spot_leverage_1x(self):
        sig = _make_sig()
        card = self.n.format_buy_sell_card(sig)
        self.assertIn("1x", card)

    def test_summary_has_core_tickers(self):
        from config import CORE_TICKERS
        signals = [_make_sig("WAIT", t) for t in CORE_TICKERS]
        for s in signals:
            s["side"] = "WAIT"
            s["live_price"] = 100.0
        text = self.n.format_desk_summary(signals, equity=10000.0, stats={
            "wins": 2, "losses": 1, "breakevens": 0, "total_pnl_usdt": 120.5
        })
        for t in CORE_TICKERS:
            self.assertIn(t, text)


if __name__ == "__main__":
    unittest.main()
