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
