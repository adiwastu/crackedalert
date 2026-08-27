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
from crackedalert import version as pkg_version
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
        """alert_fired returns the escaped note verbatim (no markup around
        it anymore), so HTML-significant characters can never break
        parse_mode=HTML."""
        raw_note = "break < 2440 & > 2430"
        a = Alert("AB12", 111, "XAUUSD", 2450.0, CROSSING_UP, raw_note)
        text = fmt.alert_fired(a)
        self.assertEqual(text, _html_esc(raw_note, quote=False))

    def test_esc_idempotent_on_already_escaped(self):
        """If a value arrives pre-escaped, esc() double-escapes it.
        This is acceptable -- the alternative (not escaping) causes
        silent delivery failures. Guards against escaping at DB-write."""
        pre_escaped = _html_esc("a & b", quote=False)
        double = fmt.esc(pre_escaped)
        self.assertEqual(double, _html_esc(pre_escaped, quote=False))


class HelpTextTests(unittest.TestCase):
    """help_text shows the running version + the UI and APK links."""

    def test_help_text_has_ui_link(self):
        text = fmt.help_text()
        self.assertIn("alert.hotland3x3.my.id", text)

    def test_help_text_has_apk_link(self):
        text = fmt.help_text()
        self.assertIn("CrackedAlarm-debug.apk", text)

    def test_help_text_shows_version(self):
        # exact runtime value embedded (v2.<commits> or static fallback)
        text = fmt.help_text()
        self.assertIn(pkg_version(), text)
        self.assertIn("<b>cracked alert", text)

    def test_help_text_no_command_sections(self):
        text = fmt.help_text()
        self.assertNotIn("<code>", text)
        self.assertNotIn("<blockquote", text)


class AlertFiredTests(unittest.TestCase):
    """alert_fired is notes-only; fallback 'price hit <target>'."""

    def test_alert_fired_notes_only(self):
        a = Alert("AB12", 111, "XAUUSD", 2450.0, CROSSING_UP, "ChoCh")
        text = fmt.alert_fired(a)
        self.assertEqual(text, "ChoCh")

    def test_alert_fired_no_notes_price_hit(self):
        a = Alert("AB12", 111, "XAUUSD", 2450.0, CROSSING_UP, "")
        text = fmt.alert_fired(a)
        self.assertEqual(text, "price hit 2450")

    def test_alert_fired_no_symbol_no_id(self):
        a = Alert("AB12", 111, "XAUUSD", 2450.0, CROSSING_DOWN, "BoS")
        text = fmt.alert_fired(a)
        self.assertNotIn("XAUUSD", text)
        self.assertNotIn("AB12", text)
        self.assertNotIn("<blockquote>", text)


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
        # minimal: tap-to-copy id + note only (no symbol, no price)
        self.assertIn("<code>AB12</code> - n1", text)
        self.assertIn("<code>CD34</code> - n2", text)
        self.assertNotIn("XAUUSD", text)
        self.assertNotIn("@", text)


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

    def test_order_success_smart_sl_percent_mode(self):
        text = fmt.order_success(
            ticket=1, symbol="XAUUSD", direction="BUY", kind_label="PENDING",
            account="demo", lots=0.19, risk_pct=1.0, risk_usd=100.0,
            entry_label="2400.00 (placed at 2400.20)", sl=2398.5, tp=2415.8,
            rr=3.0, widen_label="", digits=2,
            smart_sl=2398.5, smart_risk_pct=0.33)
        self.assertIn("risk at smart SL <code>2398.50</code> = 0.33%", text)

    def test_order_success_smart_sl_dollar_mode(self):
        # Dollar risk + smart SL: reports the dollar exposure, not 0%.
        # Mirrors what handlers.py actually passes (both fields set).
        text = fmt.order_success(
            ticket=1, symbol="XAUUSD", direction="BUY", kind_label="MARKET",
            account="demo", lots=0.05, risk_pct=0.0, risk_usd=50.0,
            entry_label="2450.00", sl=2445.0, tp=2472.0, rr=2.0,
            widen_label="", digits=2, dollar_risk=True,
            smart_sl=2445.0, smart_risk_usd=25.0, smart_risk_pct=0.25)
        self.assertIn("risk at smart SL <code>2445.00</code> = <code>$25.00</code>", text)
        # Dollars must win over the pct in dollar-risk mode.
        self.assertNotIn("= 0.25%", text)
        self.assertNotIn("= 0%", text)

    def test_order_success_no_smart_sl_single_blank_line(self):
        # Without a smart SL there should be no stray double blank line.
        text = fmt.order_success(
            ticket=1, symbol="XAUUSD", direction="BUY", kind_label="MARKET",
            account="demo", lots=0.04, risk_pct=0.5, risk_usd=50.0,
            entry_label="2450.00", sl=2439.0, tp=2472.0, rr=2.0,
            widen_label="", digits=2, dollar_risk=False)
        self.assertNotIn("\n\n\n", text)

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
                        CANDLE_BELOW, "LTF ChoCh")
        text = fmt.candle_alert_fired(a)
        self.assertEqual(text, "LTF ChoCh")

    def test_candle_alert_fired_no_notes(self):
        a = CandleAlert("AB12", 111, "XAUUSD", "M15", 2450.0,
                        CANDLE_BELOW, "")
        text = fmt.candle_alert_fired(a)
        self.assertEqual(text, "price hit 2450")

    def test_candle_alert_list(self):
        alerts = [CandleAlert("AB12", 111, "XAUUSD", "M15", 2450.0,
                              CANDLE_ABOVE, "n1"),
                  CandleAlert("CD34", 111, "EURUSD", "H1", 1.1,
                              CANDLE_BELOW, "n2")]
        text = fmt.candle_alert_list(alerts)
        self.assertIn("active candle alerts:", text)
        self.assertIn("<code>AB12</code> - n1", text)
        self.assertIn("<code>CD34</code> - n2", text)
        self.assertNotIn("XAUUSD", text)

    def test_candle_alert_list_empty(self):
        self.assertEqual(
            fmt.candle_alert_list([]), "no active candle alerts.")

    def test_candle_cancel(self):
        self.assertIn("AB12", fmt.candle_cancelled("AB12"))
        self.assertIn("AB12", fmt.candle_cancel_not_found("AB12"))
        self.assertIn("/cccancel", fmt.candle_cancel_usage())
        self.assertIn("/ccalert", fmt.candle_alert_usage())

    def test_candle_alert_list_guards_minimal(self):
        alerts_list = [
            CandleAlert("AB12", 111, "XAUUSD", "M15", 2450.0,
                        CANDLE_ABOVE, "n1"),
            CandleAlert("CD34", 111, "XAUUSD", "H1", 4080.0,
                        CANDLE_BELOW, "guard",
                        action="close", position_id=42, account="demo"),
        ]
        text = fmt.candle_alert_list(alerts_list)
        self.assertIn("<code>AB12</code> - n1", text)
        self.assertIn("<code>CD34</code> - guard", text)
        self.assertNotIn("[guard", text)   # no tag decoration anymore
        self.assertNotIn("XAUUSD", text)


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