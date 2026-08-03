"""Regression coverage for bot/formatting.py.

help_text() previously crashed in production: it embeds literal '[risk%]'
text inside a string that then undergoes %-formatting for the accounts
placeholder, and Python's % operator treats every '%' as a format code
unless doubled. This file exercises every formatter with real inputs so
a reintroduced unescaped '%' fails a test instead of a live /help call.
"""

import unittest

from crackedalert.alerts import CROSSING_DOWN, CROSSING_UP, Alert
from crackedalert.bot import formatting as fmt


class AllFormattersRunWithoutRaising(unittest.TestCase):
    def test_help_text_with_percent_literal(self):
        text = fmt.help_text(["5k", "10k", "raven", "demo"])
        self.assertIn("[risk%]", text)          # literal percent survives
        self.assertIn("accounts: 5k | 10k | raven | demo", text)

    def test_help_text_single_account(self):
        text = fmt.help_text(["live"])
        self.assertIn("accounts: live", text)

    def test_help_text_with_balances(self):
        text = fmt.help_text(["5k", "demo"],
                             balances={"5k": 1234.5, "demo": 1001113.25})
        self.assertIn("balances: 5k: 1234.5 | demo: 1001113.25", text)

    def test_help_text_balance_failure_shows_question_mark(self):
        text = fmt.help_text(["5k", "demo"],
                             balances={"5k": None, "demo": 100.0})
        self.assertIn("5k: ?", text)
        self.assertIn("demo: 100", text)

    def test_help_text_no_balances_no_line(self):
        text = fmt.help_text(["5k"])
        self.assertNotIn("balances:", text)

    def test_trade_usage(self):
        self.assertIn("[risk%]", fmt.trade_usage(True))
        self.assertIn("[risk%]", fmt.trade_usage(False))

    def test_alert_set(self):
        a = Alert("AB12", 111, "XAUUSD", 2450.0, CROSSING_UP, "note")
        text = fmt.alert_set(a, 2440.5)
        self.assertIn("AB12", text)
        self.assertIn("XAUUSD", text)

    def test_alert_fired(self):
        a = Alert("AB12", 111, "XAUUSD", 2450.0, CROSSING_DOWN, "note")
        self.assertIn("AB12", fmt.alert_fired(a))

    def test_alert_list(self):
        alerts = [Alert("AB12", 111, "XAUUSD", 2450.0, CROSSING_UP, "n1"),
                 Alert("CD34", 111, "EURUSD", 1.1, CROSSING_DOWN, "n2")]
        text = fmt.alert_list(alerts)
        self.assertIn("AB12", text)
        self.assertIn("CD34", text)

    def test_order_success_with_percent_risk(self):
        text = fmt.order_success(
            ticket=1, symbol="XAUUSD", direction="BUY", kind_label="MARKET",
            account="demo", lots=0.04, risk_pct=0.5, risk_usd=50.0,
            entry_label="2450.00", sl=2439.0, tp=2472.0, rr=2.0,
            widen_label="", digits=2)
        self.assertIn("0.5% risk", text)

    def test_order_success_dollar_risk(self):
        text = fmt.order_success(
            ticket=1, symbol="XAUUSD", direction="BUY", kind_label="MARKET",
            account="demo", lots=0.04, risk_pct=0.0, risk_usd=50.0,
            entry_label="2450.00", sl=2439.0, tp=2472.0, rr=2.0,
            widen_label="", digits=2, dollar_risk=True)
        self.assertIn("$50.00 risk", text)
        self.assertIn("lots: 0.04", text)

    def test_positions_list_empty(self):
        self.assertEqual(fmt.positions_list([], True), "no open positions.")
        self.assertEqual(fmt.positions_list([], False), "no working orders.")

    def test_positions_list_rows(self):
        rows = [
            {"id": 1, "side": "BUY", "symbol": "XAUUSD", "volume": 0.04,
             "price": 2450.0, "sl": 2439.0, "tp": 2472.0,
             "extra": "swap -1.20"},
            {"id": 2, "side": "SELL", "symbol": "XAUUSD", "volume": 0.1,
             "price": 2400.0, "sl": None, "tp": None, "extra": ""},
        ]
        text = fmt.positions_list(rows, True)
        self.assertIn("open positions:", text)
        self.assertIn("(1)  BUY XAUUSD 0.04 @ 2450  sl:2439 tp:2472", text)
        self.assertIn("(2)  SELL XAUUSD 0.10 @ 2400  sl:- tp:-", text)

    def test_orders_list_rows(self):
        rows = [
            {"id": 7, "side": "BUY", "symbol": "XAUUSD", "volume": 0.2,
             "price": 2400.2, "sl": 2395.0, "tp": 2415.0,
             "extra": "LIMIT"},
        ]
        text = fmt.positions_list(rows, False)
        self.assertIn("working orders:", text)
        self.assertIn("(7)  BUY XAUUSD 0.20 @ 2400.2  sl:2395 tp:2415", text)
        self.assertIn("[LIMIT]", text)

    def test_positions_usage(self):
        self.assertIn("/positions", fmt.positions_usage(True))
        self.assertIn("/orders", fmt.positions_usage(False))

    def test_positions_error(self):
        self.assertIn("demo", fmt.positions_error("demo", "boom"))
        self.assertIn("boom", fmt.positions_error("demo", "boom"))

    def test_order_failed(self):
        text = fmt.order_failed("XAUUSD", "BUY", "MARKET", "demo",
                                "no money", "10004")
        self.assertIn("no money", text)

    def test_simple_string_formatters(self):
        self.assertIn("2450", fmt.cancelled("2450") or fmt.cancelled("AB12"))
        fmt.cancel_usage()
        fmt.no_alerts()
        fmt.alert_usage()
        fmt.cancel_not_found("AB12")
        fmt.account_not_found("5k")
        fmt.price_fetch_error("XAUUSD")
        fmt.entry_label_market(2450.0)
        fmt.entry_label_pending(2400.0, 2400.2)


if __name__ == "__main__":
    unittest.main()
