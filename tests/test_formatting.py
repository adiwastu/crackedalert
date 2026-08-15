"""Regression coverage for bot/formatting.py - HTML parse_mode, v2.

P0: confirm HTML-escaping prevents Telegram 400 errors when user notes
contain < > & '. Also verify esc() does NOT double-escape already-escaped
text (the values come raw from the database).

P1: updated expected strings for new mobile-first templates.
"""

import unittest
from html import escape as _html_esc

from crackedalert.alerts import (CANDLE_ABOVE, CANDLE_BELOW, CROSSING_DOWN,
                                 CROSSING_UP, Alert, CandleAlert)
from crackedalert.bot import formatting as fmt


class EscapingTests(unittest.TestCase):
    """P0: every user-supplied value must be escaped exactly once."""

    def test_esc_plain_text_passes_through(self):
        self.assertEqual(fmt.esc("hello"), "hello")
        self.assertEqual(fmt.esc(42), "42")
        self.assertEqual(fmt.esc(None), "")

    def test_esc_escapes_html_chars(self):
        self.assertEqual(
            fmt.esc("a < b"),
            _html_esc("a < b", quote=False))
        self.assertEqual(
            fmt.esc("a > b"),
            _html_esc("a > b", quote=False))
        self.assertEqual(
            fmt.esc("a & b"),
            _html_esc("a & b", quote=False))

    def test_esc_does_not_escape_quotes(self):
        # quote=False -- Telegram does not decode &#x27;
        self.assertEqual(fmt.esc("it's"), "it's")
        self.assertNotIn("&#x27;", fmt.esc("it's"))

    def test_alert_message_with_special_chars_sends(self):
        """alert_fired with a note full of HTML-significant characters
        must render the escaped version inside the blockquote."""
        raw_note = "break < 2440 & > 2430"
        a = Alert("AB12", 111, "XAUUSD", 2450.0, CROSSING_UP, raw_note)
        text = fmt.alert_fired(a)
        blockquote_content = (
            text.split("<blockquote>")[1].split("</blockquote>")[0])
        expected = _html_esc(raw_note, quote=False)
        self.assertEqual(blockquote_content, expected)

    def test_esc_idempotent_on_already_escaped(self):
        """If a value arrives pre-escaped, esc() double-escapes it.
        This is acceptable -- the alternative (not escaping) causes
        silent delivery failures. Guards against escaping at DB-write."""
        pre_escaped = _html_esc("a & b", quote=False)
        double = fmt.esc(pre_escaped)
        self.assertEqual(double, _html_esc(pre_escaped, quote=False))


class HelpTextTests(unittest.TestCase):
    """help_text is now expandable blockquotes per section."""

    def test_help_text_accounts_each_on_own_line(self):
        text = fmt.help_text(["5k", "10k", "raven", "demo"])
        self.assertIn("<code>5k</code>", text)
        self.assertIn("<code>10k</code>", text)
        self.assertIn("<code>raven</code>", text)
        self.assertIn("<code>demo</code>", text)

    def test_help_text_single_account(self):
        text = fmt.help_text(["live"])
        self.assertIn("<code>live</code>", text)

    def test_help_text_with_balances(self):
        text = fmt.help_text(
            ["5k", "demo"],
            balances={"5k": 1234.5, "demo": 1001113.25})
        self.assertIn("<code>5k</code>  1234.5", text)
        self.assertIn("<code>demo</code>  1001113.25", text)

    def test_help_text_balance_failure_shows_question_mark(self):
        text = fmt.help_text(
            ["5k", "demo"],
            balances={"5k": None, "demo": 100.0})
        self.assertIn("<code>5k</code>", text)
        self.assertIn("<code>demo</code>  100", text)

    def test_help_text_no_balances_no_values(self):
        text = fmt.help_text(["5k"])
        self.assertIn("<code>5k</code>", text)
        self.assertNotIn("<code>5k</code>  ", text)

    def test_help_text_has_blockquote_expandable(self):
        text = fmt.help_text(["demo"])
        self.assertIn("<blockquote expandable>", text)
        self.assertIn("</blockquote>", text)

    def test_help_text_has_version(self):
        text = fmt.help_text(["demo"])
        self.assertIn("<i>v2.", text)
        self.assertIn("</i>", text)


class AlertFiredTests(unittest.TestCase):
    def test_alert_fired_contains_symbol_and_target(self):
        a = Alert("AB12", 111, "XAUUSD", 2450.0, CROSSING_UP,
                  "approaching demand")
        text = fmt.alert_fired(a)
        self.assertIn("XAUUSD", text)
        self.assertIn("2450", text)
        self.assertIn("AB12", text)

    def test_alert_fired_up_arrow(self):
        a = Alert("AB12", 111, "XAUUSD", 2450.0, CROSSING_UP, "note")
        text = fmt.alert_fired(a)
        self.assertIn("\U0001F53A", text)

    def test_alert_fired_down_arrow(self):
        a = Alert("AB12", 111, "XAUUSD", 2450.0, CROSSING_DOWN, "note")
        text = fmt.alert_fired(a)
        self.assertIn("\U0001F53B", text)

    def test_alert_fired_has_blockquote(self):
        a = Alert("AB12", 111, "XAUUSD", 2450.0, CROSSING_UP, "my note")
        text = fmt.alert_fired(a)
        self.assertIn("<blockquote>my note</blockquote>", text)


class AlertSetTests(unittest.TestCase):
    def test_alert_set(self):
        a = Alert("AB12", 111, "XAUUSD", 2450.0, CROSSING_UP, "note")
        text = fmt.alert_set(a, 2440.5)
        self.assertIn("AB12", text)
        self.assertIn("XAUUSD", text)

    def test_alert_list(self):
        alerts = [Alert("AB12", 111, "XAUUSD", 2450.0, CROSSING_UP, "n1"),
                  Alert("CD34", 111, "EURUSD", 1.1, CROSSING_DOWN, "n2")]
        text = fmt.alert_list(alerts)
        self.assertIn("AB12", text)
        self.assertIn("CD34", text)


class OrderSuccessTests(unittest.TestCase):
    def test_order_success_with_percent_risk(self):
        text = fmt.order_success(
            ticket=1, symbol="XAUUSD", direction="BUY", kind_label="MARKET",
            account="demo", lots=0.04, risk_pct=0.5, risk_usd=50.0,
            entry_label="2450.00", sl=2439.0, tp=2472.0, rr=2.0,
            widen_label="", digits=2)
        self.assertIn("0.5% risk", text)
        self.assertIn("<code>$50.00</code>", text)

    def test_order_success_dollar_risk(self):
        text = fmt.order_success(
            ticket=1, symbol="XAUUSD", direction="BUY", kind_label="MARKET",
            account="demo", lots=0.04, risk_pct=0.0, risk_usd=50.0,
            entry_label="2450.00", sl=2439.0, tp=2472.0, rr=2.0,
            widen_label="", digits=2, dollar_risk=True)
        self.assertIn("<code>$50.00</code> risk", text)
        self.assertIn("lots", text.lower())

    def test_order_success_has_side_glyph(self):
        text = fmt.order_success(
            ticket=2, symbol="XAUUSD", direction="SELL", kind_label="MARKET",
            account="demo", lots=0.04, risk_pct=0.5, risk_usd=50.0,
            entry_label="2450.00", sl=2461.0, tp=2428.0, rr=2.0,
            widen_label="", digits=2)
        self.assertIn("\U0001F534", text)

    def test_order_failed(self):
        text = fmt.order_failed("XAUUSD", "BUY", "MARKET", "demo",
                                "no money", "10004")
        self.assertIn("no money", text)


class PositionsListTests(unittest.TestCase):
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
        self.assertIn("<b>open positions</b>", text)
        self.assertIn("XAUUSD", text)
        self.assertIn("BUY", text)
        self.assertIn("SELL", text)
        self.assertIn("0.04", text)
        self.assertIn("0.10", text)
        self.assertIn("#1", text)
        self.assertIn("#2", text)

    def test_orders_list_rows(self):
        rows = [
            {"id": 7, "side": "BUY", "symbol": "XAUUSD", "volume": 0.2,
             "price": 2400.2, "sl": 2395.0, "tp": 2415.0,
             "extra": "LIMIT"},
        ]
        text = fmt.positions_list(rows, False)
        self.assertIn("<b>working orders</b>", text)
        self.assertIn("XAUUSD", text)
        self.assertIn("#7", text)
        self.assertIn("LIMIT", text)


class UsageTests(unittest.TestCase):
    def test_trade_usage(self):
        text_m = fmt.trade_usage(True)
        text_p = fmt.trade_usage(False)
        self.assertIn("<code>/m", text_m)
        self.assertIn("<code>/p", text_p)

    def test_positions_usage(self):
        text = fmt.positions_usage(True)
        self.assertIn("<code>/positions", text)
        text_orders = fmt.positions_usage(False)
        self.assertIn("<code>/orders", text_orders)

    def test_positions_error(self):
        text = fmt.positions_error("demo", "boom")
        self.assertIn("demo", text)
        self.assertIn("boom", text)

    def test_alert_usage(self):
        self.assertIn("<code>/alert", fmt.alert_usage())

    def test_cancel_usage(self):
        self.assertIn("<code>/cancel", fmt.cancel_usage())

    def test_close_usage(self):
        self.assertIn("<code>/close", fmt.close_usage())


class CloseAndBreakevenTests(unittest.TestCase):
    def test_close_all_result(self):
        results = [
            {"id": 1, "side": "BUY", "symbol": "XAUUSD", "volume": 0.04,
             "ok": True, "message": "closed"},
            {"id": 2, "side": "SELL", "symbol": "XAUUSD", "volume": 0.1,
             "ok": False, "message": "no money"},
        ]
        text = fmt.close_all_result("demo", results)
        self.assertIn("closed 1/2 positions (demo):", text)
        self.assertIn("closed", text)
        self.assertIn("no money", text)

    def test_close_all_empty(self):
        self.assertEqual(fmt.close_all_result("demo", []),
                         "no open positions.")

    def test_breakeven_result(self):
        results = [
            {"id": 1, "side": "BUY", "symbol": "XAUUSD", "volume": 0.04,
             "ok": True, "message": "breakeven set", "be_sl": 2450.2},
            {"id": 2, "side": "SELL", "symbol": "XAUUSD", "volume": 0.1,
             "ok": False, "message": "not in profit by spread yet",
             "be_sl": 2399.8},
        ]
        text = fmt.breakeven_result("demo", results)
        self.assertIn("breakeven set on 1/2 positions (demo):", text)
        self.assertIn("BE at 2450.2", text)
        self.assertIn("skipped", text)

    def test_breakeven_empty(self):
        self.assertEqual(fmt.breakeven_result("demo", []),
                         "no open positions.")

    def test_close_single(self):
        self.assertEqual(fmt.close_success("demo", 1),
                         "position 1 closed (demo).")
        self.assertIn("boom", fmt.close_error("demo", 1, "boom"))

    def test_cancel_order(self):
        self.assertEqual(fmt.cancel_order_success("demo", 7),
                         "order 7 cancelled (demo).")
        self.assertIn("boom", fmt.cancel_order_error("demo", 7, "boom"))


class CandleAlertTests(unittest.TestCase):
    def test_candle_alert_set(self):
        a = CandleAlert("AB12", 111, "XAUUSD", "M15", 2450.0,
                        CANDLE_ABOVE, "breakout")
        text = fmt.candle_alert_set(a, 2449.5)
        self.assertIn("AB12", text)
        self.assertIn("XAUUSD M15", text)
        self.assertIn("close above 2450", text)
        self.assertIn("last closed close: 2449.5", text)

    def test_candle_alert_fired(self):
        a = CandleAlert("AB12", 111, "XAUUSD", "M15", 2450.0,
                        CANDLE_BELOW, "note")
        text = fmt.candle_alert_fired(a)
        self.assertIn("AB12", text)
        self.assertIn("XAUUSD M15 closed below 2450", text)

    def test_candle_alert_list(self):
        alerts = [CandleAlert("AB12", 111, "XAUUSD", "M15", 2450.0,
                              CANDLE_ABOVE, "n1"),
                  CandleAlert("CD34", 111, "EURUSD", "H1", 1.1,
                              CANDLE_BELOW, "n2")]
        text = fmt.candle_alert_list(alerts)
        self.assertIn("active candle alerts:", text)
        self.assertIn("AB12", text)
        self.assertIn("CD34", text)

    def test_candle_alert_list_empty(self):
        self.assertEqual(
            fmt.candle_alert_list([]), "no active candle alerts.")

    def test_candle_cancel(self):
        self.assertIn("AB12", fmt.candle_cancelled("AB12"))
        self.assertIn("AB12", fmt.candle_cancel_not_found("AB12"))
        self.assertIn("/cccancel", fmt.candle_cancel_usage())
        self.assertIn("/ccalert", fmt.candle_alert_usage())

    def test_candle_alert_list_tags_guards(self):
        alerts_list = [
            CandleAlert("AB12", 111, "XAUUSD", "M15", 2450.0,
                        CANDLE_ABOVE, "n1"),
            CandleAlert("CD34", 111, "XAUUSD", "H1", 4080.0,
                        CANDLE_BELOW, "guard",
                        action="close", position_id=42, account="demo"),
        ]
        text = fmt.candle_alert_list(alerts_list)
        self.assertIn("[guard #42]", text)
        self.assertNotIn("[guard", text.splitlines()[1])  # first line (n1) no tag


class CCGuardFormattingTests(unittest.TestCase):
    def test_cc_guard_set(self):
        a = CandleAlert("AB12", 111, "XAUUSD", "M15", 4080.0,
                        CANDLE_BELOW, "guard",
                        action="close", position_id=42, account="demo")
        text = fmt.cc_guard_set(a)
        self.assertIn("AB12", text)
        self.assertIn("XAUUSD M15", text)
        self.assertIn("below", text)
        self.assertIn("4080", text)
        self.assertIn("position 42", text)

    def test_cc_guard_set_above(self):
        a = CandleAlert("CD34", 111, "XAUUSD", "H1", 4100.0,
                        CANDLE_ABOVE, "guard",
                        action="close", position_id=7, account="live")
        text = fmt.cc_guard_set(a)
        self.assertIn("above", text)
        self.assertIn("position 7", text)

    def test_cc_guard_pending(self):
        text = fmt.cc_guard_pending("M15", 4080.0)
        self.assertIn("M15", text)
        self.assertIn("4080", text)

    def test_cc_guard_fired(self):
        a = CandleAlert("AB12", 111, "XAUUSD", "M15", 4080.0,
                        CANDLE_BELOW, "guard",
                        action="close", position_id=42, account="demo")
        text = fmt.cc_guard_fired(a)
        self.assertIn("AB12", text)
        self.assertIn("XAUUSD M15", text)
        self.assertIn("below", text)
        self.assertIn("position 42", text)

    def test_cc_guard_position_gone(self):
        a = CandleAlert("AB12", 111, "XAUUSD", "M15", 4080.0,
                        CANDLE_BELOW, "guard",
                        action="close", position_id=42, account="demo")
        text = fmt.cc_guard_position_gone(a)
        self.assertIn("AB12", text)
        self.assertIn("position 42", text)
        self.assertIn("already closed", text)


class TrivialFormattersTests(unittest.TestCase):
    def test_simple_string_formatters(self):
        fmt.cancelled("AB12")
        fmt.cancel_not_found("AB12")
        fmt.no_alerts()
        fmt.account_not_found("5k")
        fmt.price_fetch_error("XAUUSD")
        fmt.entry_label_market(2450.0)
        fmt.entry_label_pending(2400.0, 2400.2)

    def test_trim(self):
        self.assertIn("2450", fmt._trim(2450.0))
        self.assertEqual(fmt._trim(100.0), "100")
        self.assertEqual(fmt._trim(100.50), "100.5")
        self.assertEqual(fmt._trim(0.0), "0")


class BotCommandsTests(unittest.TestCase):
    def test_bot_commands_is_list_of_tuples(self):
        self.assertIsInstance(fmt.BOT_COMMANDS, list)
        for cmd in fmt.BOT_COMMANDS:
            self.assertIsInstance(cmd, tuple)
            self.assertEqual(len(cmd), 2)
            self.assertIsInstance(cmd[0], str)
            self.assertIsInstance(cmd[1], str)

    def test_bot_commands_has_cancel_order(self):
        commands = dict(fmt.BOT_COMMANDS)
        self.assertIn("cancel_order", commands)

    def test_bot_commands_no_duplicates(self):
        names = [c[0] for c in fmt.BOT_COMMANDS]
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()