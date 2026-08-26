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

    def test_broadcast_flag_stripped(self):
        a = handlers.parse_alert("/alert 2450 approaching demand --all",
                                 "XAUUSD", self.KNOWN)
        self.assertTrue(a.broadcast)
        self.assertEqual((a.target, a.symbol), (2450.0, "XAUUSD"))
        self.assertEqual(a.message, "approaching demand")

        b = handlers.parse_alert("/alert 2450 approach -all", "XAUUSD")
        self.assertTrue(b.broadcast)
        self.assertEqual(b.message, "approach")

    def test_no_broadcast_default(self):
        a = handlers.parse_alert("/alert 2450 plain note", "XAUUSD")
        self.assertFalse(a.broadcast)

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

    def test_market_broadcast_flag(self):
        t = handlers.parse_trade("/m 2440.00 y 2 0.5 10k --all",
                                 is_market=True)
        self.assertTrue(t.broadcast)
        self.assertEqual(
            (t.sl, t.widen, t.rr, t.risk_pct, t.account),
            (2440.0, True, 2.0, 0.5, "10k"))

        b = handlers.parse_trade("/m 2440.00 y 2 0.5 10k -all",
                                 is_market=True)
        self.assertTrue(b.broadcast)

    def test_pending_broadcast_flag(self):
        t = handlers.parse_trade("/p 2450 2455 n 3 1 5k --all",
                                 is_market=False)
        self.assertTrue(t.broadcast)
        self.assertEqual(t.entry, 2450.0)
        self.assertEqual(
            (t.sl, t.widen, t.rr, t.risk_pct, t.account),
            (2455.0, False, 3.0, 1.0, "5k"))

    def test_trade_broadcast_with_cc_guard(self):
        t = handlers.parse_trade("/m 2440 y 2 0.5 10k M15 4080 --all",
                                 is_market=True)
        self.assertTrue(t.broadcast)
        self.assertEqual(t.cc_timeframe, "M15")
        self.assertEqual(t.cc_price, 4080.0)

    def test_trade_no_broadcast_default(self):
        t = handlers.parse_trade("/m 2440 y 2 0.5 10k", is_market=True)
        self.assertFalse(t.broadcast)

    def test_smart_sl_with_timeframe(self):
        t = handlers.parse_trade(
            "/m 2440 n 2 0.5 10k --smart-sl 2445.5 M5", is_market=True)
        self.assertEqual(t.smart_sl, 2445.5)
        self.assertEqual(t.smart_sl_tf, "M5")
        self.assertIsNone(t.cc_timeframe)

    def test_smart_sl_short_flag_and_lowercase_tf(self):
        t = handlers.parse_trade(
            "/p 2450 2455 n 3 1 5k -ss 2451.25 m15", is_market=False)
        self.assertEqual((t.smart_sl, t.smart_sl_tf), (2451.25, "M15"))

    def test_smart_sl_requires_timeframe(self):
        with self.assertRaises(handlers.ParseError):
            handlers.parse_trade("/m 2440 n 2 0.5 10k --smart-sl 2445.5",
                                 is_market=True)

    def test_smart_sl_invalid_timeframe(self):
        with self.assertRaises(handlers.ParseError):
            handlers.parse_trade(
                "/m 2440 n 2 0.5 10k --smart-sl 2445.5 W2", is_market=True)

    def test_smart_sl_rejects_cc_pair_combination(self):
        with self.assertRaises(handlers.ParseError):
            handlers.parse_trade(
                "/m 2440 n 2 0.5 10k --smart-sl 2445.5 M5 H1 4080",
                is_market=True)

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

    def test_market_smart_sl(self):
        t = handlers.parse_trade("/m 2440 y 2 0.5 10k --smart-sl 2435 M5",
                                 is_market=True)
        self.assertAlmostEqual(t.smart_sl, 2435.0)
        self.assertEqual(t.smart_sl_tf, "M5")
        self.assertIsNone(t.cc_timeframe)

    def test_pending_smart_sl(self):
        t = handlers.parse_trade("/p 2450 2440 y 2 0.5 10k --smart-sl 2445 H1",
                                 is_market=False)
        self.assertAlmostEqual(t.smart_sl, 2445.0)
        self.assertEqual(t.smart_sl_tf, "H1")

    def test_smart_sl_short_alias(self):
        t = handlers.parse_trade("/m 2440 y 2 0.5 10k -ss 2435 D1",
                                 is_market=True)
        self.assertAlmostEqual(t.smart_sl, 2435.0)
        self.assertEqual(t.smart_sl_tf, "D1")

    def test_smart_sl_with_cc_guard_rejected(self):
        # one soft-stop guard per trade: the positional CC pair conflicts
        with self.assertRaises(handlers.ParseError):
            handlers.parse_trade(
                "/m 2440 y 2 0.5 10k --smart-sl 2435 M15 4080",
                is_market=True)

    def test_smart_sl_with_broadcast_flag_ok(self):
        t = handlers.parse_trade(
            "/m 2440 y 2 0.5 10k --smart-sl 2435 M15 --all",
            is_market=True)
        self.assertAlmostEqual(t.smart_sl, 2435.0)
        self.assertEqual(t.smart_sl_tf, "M15")
        self.assertTrue(t.broadcast)

    def test_smart_sl_missing_price_raises(self):
        with self.assertRaises(handlers.ParseError):
            handlers.parse_trade("/m 2440 y 2 0.5 10k --smart-sl",
                                 is_market=True)

    def test_smart_sl_bad_number_raises(self):
        with self.assertRaises(handlers.ParseError):
            handlers.parse_trade("/m 2440 y 2 0.5 10k --smart-sl abc",
                                 is_market=True)

    def test_old_xmult_is_rejected(self):
        # x2-style smart multiplier is no longer a valid trailing token; a
        # bare trailing value that's not --smart-sl falls through to the CC
        # guard parser and errors.
        with self.assertRaises(handlers.ParseError):
            handlers.parse_trade("/m 2440 y 2 0.5 10k x2", is_market=True)

    def test_market_with_cc_guard(self):
        t = handlers.parse_trade("/m 2440 y 2 0.5 10k M15 4080",
                                 is_market=True)
        self.assertEqual(t.cc_timeframe, "M15")
        self.assertEqual(t.cc_price, 4080.0)

    def test_market_without_cc_guard(self):
        t = handlers.parse_trade("/m 2440 y 2 0.5 10k", is_market=True)
        self.assertIsNone(t.cc_timeframe)
        self.assertIsNone(t.cc_price)

    def test_pending_with_cc_guard(self):
        t = handlers.parse_trade("/p 2450 2440 y 2 0.5 10k H1 2445",
                                 is_market=False)
        self.assertEqual(t.cc_timeframe, "H1")
        self.assertEqual(t.cc_price, 2445.0)

    def test_pending_without_cc_guard(self):
        t = handlers.parse_trade("/p 2450 2440 y 2 0.5 10k",
                                 is_market=False)
        self.assertIsNone(t.cc_timeframe)
        self.assertIsNone(t.cc_price)

    def test_market_bad_cc_timeframe_raises(self):
        with self.assertRaises(handlers.ParseError):
            handlers.parse_trade("/m 2440 y 2 0.5 10k X99 4080",
                                 is_market=True)

    def test_pending_bad_cc_timeframe_raises(self):
        with self.assertRaises(handlers.ParseError):
            handlers.parse_trade("/p 2450 2440 y 2 0.5 10k X99 2445",
                                 is_market=False)

    def test_market_cc_price_not_number_raises(self):
        with self.assertRaises(handlers.ParseError):
            handlers.parse_trade("/m 2440 y 2 0.5 10k M15 abc",
                                 is_market=True)

    def test_market_only_one_cc_arg_raises(self):
        with self.assertRaises(handlers.ParseError):
            handlers.parse_trade("/m 2440 y 2 0.5 10k M15", is_market=True)

    def test_pending_only_one_cc_arg_raises(self):
        with self.assertRaises(handlers.ParseError):
            handlers.parse_trade("/p 2450 2440 y 2 0.5 10k H1",
                                 is_market=False)


class CandleAlertParsing(unittest.TestCase):
    def test_full(self):
        a = handlers.parse_cc_alert(
            "/ccalert M15 2450 above XAUUSD breakout", "XAUUSD")
        self.assertEqual(a.timeframe, "M15")
        self.assertEqual(a.target, 2450.0)
        self.assertEqual(a.direction, "ABOVE")
        self.assertEqual(a.symbol, "XAUUSD")
        self.assertEqual(a.message, "breakout")

    def test_default_symbol_and_message(self):
        a = handlers.parse_cc_alert("/ccalert H1 2400 below", "XAUUSD")
        self.assertEqual(a.symbol, "XAUUSD")
        self.assertEqual(a.direction, "BELOW")
        self.assertIn("target", a.message)

    def test_lowercase_direction(self):
        a = handlers.parse_cc_alert("/ccalert M5 1.10 above", "EURUSD")
        self.assertEqual(a.direction, "ABOVE")

    def test_bad_timeframe(self):
        with self.assertRaises(handlers.ParseError):
            handlers.parse_cc_alert("/ccalert X99 2450 above", "XAUUSD")

    def test_bad_direction(self):
        with self.assertRaises(handlers.ParseError):
            handlers.parse_cc_alert("/ccalert M15 2450 sideways", "XAUUSD")

    def test_missing_args(self):
        with self.assertRaises(handlers.ParseError):
            handlers.parse_cc_alert("/ccalert M15 2450", "XAUUSD")
        with self.assertRaises(handlers.ParseError):
            handlers.parse_cc_alert("/ccalert", "XAUUSD")

    def test_bad_price(self):
        with self.assertRaises(handlers.ParseError):
            handlers.parse_cc_alert("/ccalert M15 abc above", "XAUUSD")

    def test_broadcast_flag(self):
        a = handlers.parse_cc_alert(
            "/ccalert M15 2450 above XAUUSD breakout --all", "XAUUSD")
        self.assertTrue(a.broadcast)
        self.assertEqual(a.symbol, "XAUUSD")
        self.assertEqual(a.message, "breakout")

        b = handlers.parse_cc_alert("/ccalert M15 2450 above --all", "XAUUSD")
        self.assertTrue(b.broadcast)
        self.assertEqual(b.symbol, "XAUUSD")

    def test_no_broadcast_default(self):
        a = handlers.parse_cc_alert("/ccalert M15 2450 above", "XAUUSD")
        self.assertFalse(a.broadcast)


if __name__ == "__main__":
    unittest.main()
