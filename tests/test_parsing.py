"""Command parser tests, including the /help-example case the bash bot
got wrong (/alert 2450.00 approaching demand)."""

import unittest

from crackedalert.bot import handlers


class AlertParsing(unittest.TestCase):
    def test_target_only(self):
        a = handlers.parse_alert("/alert 2450.00", "XAUUSD")
        self.assertEqual((a.target, a.symbol), (2450.0, "XAUUSD"))
        self.assertEqual(a.message, "Price target reached.")

    KNOWN = {"XAUUSD", "EURUSD", "US30"}

    def test_with_symbol_and_message(self):
        a = handlers.parse_alert("/alert 2450.00 EURUSD watch this", "XAUUSD",
                                 self.KNOWN)
        self.assertEqual(a.symbol, "EURUSD")
        self.assertEqual(a.message, "watch this")

    def test_notes_only_not_eaten_as_symbol(self):
        # the /help example: 'approaching' must not become the symbol,
        # even though it matches the symbol regex when uppercased
        a = handlers.parse_alert("/alert 2450.00 approaching demand", "XAUUSD",
                                 self.KNOWN)
        self.assertEqual(a.symbol, "XAUUSD")
        self.assertEqual(a.message, "approaching demand")

    def test_lowercase_symbol_recognized_when_known(self):
        a = handlers.parse_alert("/alert 2450 eurusd note", "XAUUSD",
                                 self.KNOWN)
        self.assertEqual(a.symbol, "EURUSD")
        self.assertEqual(a.message, "note")

    def test_allcaps_fallback_before_first_connection(self):
        # no known set yet: only deliberate ALL-CAPS tokens count as symbols
        a = handlers.parse_alert("/alert 2450 EURUSD note", "XAUUSD", set())
        self.assertEqual(a.symbol, "EURUSD")
        b = handlers.parse_alert("/alert 2450 eurusd note", "XAUUSD", set())
        self.assertEqual(b.symbol, "XAUUSD")
        self.assertEqual(b.message, "eurusd note")

    def test_numeric_second_token_is_message(self):
        a = handlers.parse_alert("/alert 2450 2500 wait what", "XAUUSD")
        self.assertEqual(a.symbol, "XAUUSD")
        self.assertEqual(a.message, "2500 wait what")

    def test_missing_target_raises(self):
        with self.assertRaises(handlers.ParseError):
            handlers.parse_alert("/alert", "XAUUSD")
        with self.assertRaises(handlers.ParseError):
            handlers.parse_alert("/alert notanumber", "XAUUSD")


class TradeParsing(unittest.TestCase):
    def test_market(self):
        t = handlers.parse_trade("/m 2440.00 y 2 0.5 10k", is_market=True)
        self.assertIsNone(t.entry)
        self.assertEqual(
            (t.sl, t.widen, t.rr, t.risk_pct, t.account),
            (2440.0, True, 2.0, 0.5, "10k"))

    def test_pending(self):
        t = handlers.parse_trade("/p 2450.00 2455.00 n 3 1 5k",
                                 is_market=False)
        self.assertEqual(t.entry, 2450.0)
        self.assertEqual(
            (t.sl, t.widen, t.rr, t.risk_pct, t.account),
            (2455.0, False, 3.0, 1.0, "5k"))

    def test_wrong_arity(self):
        with self.assertRaises(handlers.ParseError):
            handlers.parse_trade("/m 2440.00 y 2 0.5", is_market=True)
        with self.assertRaises(handlers.ParseError):
            handlers.parse_trade("/p 2450 2455 n 3 1 5k extra",
                                 is_market=False)

    def test_bad_widen(self):
        with self.assertRaises(handlers.ParseError):
            handlers.parse_trade("/m 2440.00 x 2 0.5 10k", is_market=True)

    def test_nonpositive_rr_or_risk(self):
        with self.assertRaises(handlers.ParseError):
            handlers.parse_trade("/m 2440.00 y 0 0.5 10k", is_market=True)
        with self.assertRaises(handlers.ParseError):
            handlers.parse_trade("/m 2440.00 y 2 -1 10k", is_market=True)

    def test_dollar_risk_prefix(self):
        t = handlers.parse_trade("/m 2440.00 y 2 $50 demo", is_market=True)
        self.assertEqual(t.risk_usd, 50.0)
        self.assertEqual(t.risk_pct, 0.0)
        self.assertIsNone(t.entry)

    def test_dollar_risk_pending(self):
        t = handlers.parse_trade("/p 2400.00 2395.00 n 3 $100 demo",
                                 is_market=False)
        self.assertEqual(t.risk_usd, 100.0)
        self.assertEqual(t.entry, 2400.0)

    def test_dollar_risk_decimal(self):
        t = handlers.parse_trade("/m 2440.00 n 2 $12.50 demo",
                                 is_market=True)
        self.assertAlmostEqual(t.risk_usd, 12.5)

    def test_dollar_risk_nonpositive_rejected(self):
        with self.assertRaises(handlers.ParseError):
            handlers.parse_trade("/m 2440.00 y 2 $0 demo", is_market=True)
        with self.assertRaises(handlers.ParseError):
            handlers.parse_trade("/m 2440.00 y 2 $-5 demo", is_market=True)

    def test_dollar_risk_bad_number(self):
        with self.assertRaises(handlers.ParseError):
            handlers.parse_trade("/m 2440.00 y 2 $abc demo", is_market=True)


if __name__ == "__main__":
    unittest.main()
