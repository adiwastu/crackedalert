"""Scheduled one-shot pending orders: /later parsing, the store, the
time math, and the fire paths of the runner (see scheduled_order.py)."""

import asyncio
import calendar
import shutil
import tempfile
import unittest
from dataclasses import dataclass, field

from crackedalert import risk
from crackedalert.bot.handlers import (LaterArgs, ParseError, order_success_text,
                                       parse_later, parse_trade)
from crackedalert.ctrader.market import SymbolInfo
from crackedalert.ctrader.trading import OrderResult
from crackedalert.scheduled_order import (MAX_ATTEMPTS, ScheduledOrderRunner,
                                          ScheduledOrderStore, in_band,
                                          next_occurrence, parse_fire_time)

SPEC = "2336 2330 n 3 1 5k"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def utc_ts(year, month, day, hour, minute):
    return calendar.timegm((year, month, day, hour, minute, 0))


class Sent:
    """Collects (chat_id, text) the way notify() would."""

    def __init__(self):
        self.messages = []

    async def __call__(self, chat_id, text):
        self.messages.append((chat_id, text))

    def joined(self):
        return "\n".join(text for _, text in self.messages)


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeBid:
    def __init__(self, bid=2340.0):
        self.bid = bid

    async def __call__(self, shortcode):
        return self.bid


class FakeWatchSubscribe:
    def __init__(self):
        self.calls = []

    async def __call__(self, shortcode, symbol):
        self.calls.append((shortcode, symbol))
        return SymbolInfo(symbol_id=9, name=symbol.upper(), digits=2,
                          lot_size=10000, min_volume=100, step_volume=100,
                          pip_position=2)


@dataclass
class FakeAccount:
    environment: str = "demo"
    ctid_account_id: int = 123


@dataclass
class FakeSettings:
    display_utc_offset: float = 7.0
    trade_symbol: str = "XAUUSD"
    accounts: dict = field(default_factory=lambda: {"5k": FakeAccount()})


class FakeTrader:
    """Real plan math; fails the first `fail_times` placements."""

    def __init__(self, fail_times=0, error=None):
        self.calls = []
        self.fail_times = fail_times
        self.error = error or RuntimeError("boom")

    async def execute(self, args, is_market, risk_usd=None):
        self.calls.append(args)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.error
        plan = risk.plan_pending(2340.0, 2340.3, args.entry, args.sl,
                                 args.widen, args.rr, args.risk_pct,
                                 10000.0)
        result = OrderResult(order_id=777, execution_type="ORDER_PLACED")
        symbol = SymbolInfo(symbol_id=1, name="XAUUSD", digits=2,
                            lot_size=10000, min_volume=100,
                            step_volume=100, pip_position=2)
        return plan, symbol, result, 0.15


def fire_time_testcase_offsets():
    """(now, expected) pairs for 06:00 at UTC+7."""
    return [
        # local 2026-10-10 05:00 -> fires today 06:00 local
        (utc_ts(2026, 10, 9, 22, 0), utc_ts(2026, 10, 9, 23, 0)),
        # local 06:30 -> tomorrow
        (utc_ts(2026, 10, 9, 23, 30), utc_ts(2026, 10, 10, 23, 0)),
        # exactly at the fire time -> tomorrow, never an instant fire
        (utc_ts(2026, 10, 9, 23, 0), utc_ts(2026, 10, 10, 23, 0)),
    ]


class ParseFireTimeTests(unittest.TestCase):
    def test_accepts_h_and_hh_hours(self):
        self.assertEqual(parse_fire_time("06:00"), "06:00")
        self.assertEqual(parse_fire_time("6:00"), "06:00")
        self.assertEqual(parse_fire_time("23:59"), "23:59")

    def test_rejects_garbage(self):
        for bad in ("6:5", "0600", "24:00", "06:60", "06:00:00",
                    "-1:30", "abc", "", "6:", ":00"):
            self.assertIsNone(parse_fire_time(bad), bad)


class NextOccurrenceTests(unittest.TestCase):
    def test_before_the_time_fires_today(self):
        for now, expected in fire_time_testcase_offsets():
            self.assertEqual(next_occurrence(now, "06:00", 7.0), expected)

    def test_utc(self):
        self.assertEqual(
            next_occurrence(utc_ts(2026, 10, 10, 5, 59), "06:00", 0.0),
            utc_ts(2026, 10, 10, 6, 0))
        self.assertEqual(
            next_occurrence(utc_ts(2026, 10, 10, 6, 0) + 31, "06:00", 0.0),
            utc_ts(2026, 10, 11, 6, 0))

    def test_fractional_offset(self):
        # UTC+5.5: local 06:30 -> next 06:00 local is 23:30 UTC.
        self.assertEqual(
            next_occurrence(utc_ts(2026, 10, 10, 1, 0), "06:00", 5.5),
            utc_ts(2026, 10, 11, 0, 30))


class InBandTests(unittest.TestCase):
    def test_inclusive_on_both_ends(self):
        self.assertTrue(in_band(2330.0, 2330.0, 2350.0))
        self.assertTrue(in_band(2350.0, 2330.0, 2350.0))
        self.assertTrue(in_band(2340.0, 2330.0, 2350.0))

    def test_outside_either_side(self):
        self.assertFalse(in_band(2329.9, 2330.0, 2350.0))
        self.assertFalse(in_band(2350.1, 2330.0, 2350.0))


class ParseLaterTests(unittest.TestCase):
    def test_arm_positional(self):
        kind, args = parse_later(
            "/later 06:00 2330 2350 p " + SPEC)
        self.assertEqual(kind, "arm")
        self.assertEqual(args, LaterArgs("06:00", 2330.0, 2350.0, SPEC))

    def test_hour_is_normalized(self):
        _, args = parse_later("/later 6:00 2330 2350 p " + SPEC)
        self.assertEqual(args.fire_time, "06:00")

    def test_arm_named_flag_spec(self):
        kind, args = parse_later(
            "/later 06:30 2330 2350 p --entry 2336 --sl 2341 --widen n "
            "--rr 3 --risk 1 --account 5k")
        self.assertEqual(kind, "arm")
        self.assertEqual(args.spec, "--entry 2336 --sl 2341 --widen n "
                                    "--rr 3 --risk 1 --account 5k")

    def test_arm_with_rule_parameter_flags(self):
        kind, args = parse_later(
            "/later --time 06:00 --band-low 2330 --band-high 2350 p "
            + SPEC)
        self.assertEqual(kind, "arm")
        self.assertEqual(args, LaterArgs("06:00", 2330.0, 2350.0, SPEC))

    def test_rule_parameter_flag_aliases(self):
        _, args = parse_later(
            "/later --at 6:00 --low 2330 --high 2350 p " + SPEC)
        self.assertEqual(args, LaterArgs("06:00", 2330.0, 2350.0, SPEC))

    def test_flag_mode_requires_all_three_rule_parameters(self):
        for text in (
                "/later --time 06:00 --band-low 2330 p " + SPEC,
                "/later --band-low 2330 --band-high 2350 p " + SPEC,
                "/later --time 06:00 --band-high 2350 p " + SPEC):
            with self.assertRaises(ParseError, msg=text):
                parse_later(text)

    def test_unknown_rule_flag_is_rejected(self):
        with self.assertRaises(ParseError):
            parse_later("/later --time 06:00 --band-low 2330 "
                        "--band-high 2350 --nope x p " + SPEC)

    def test_the_p_separator_is_mandatory(self):
        for text in (
                "/later --time 06:00 --band-low 2330 --band-high 2350",
                "/later 06:00 2330 2350",
                "/later 06:00 2330 2350 m " + SPEC):
            with self.assertRaises(ParseError, msg=text):
                parse_later(text)

    def test_the_spec_parses_like_a_real_p(self):
        _, args = parse_later("/later 06:00 2330 2350 p " + SPEC)
        self.assertEqual(parse_trade("/p " + args.spec, False),
                         parse_trade("/p " + SPEC, False))

    def test_bare_command_lists(self):
        self.assertEqual(parse_later("/later"), ("list", None))

    def test_off_with_and_without_id(self):
        self.assertEqual(parse_later("/later off"), ("off", None))
        self.assertEqual(parse_later("/later off 7"), ("off", 7))
        self.assertEqual(parse_later("/later stop 7"), ("off", 7))

    def test_rejects_bad_input(self):
        bad = [
            "/later 25:00 2330 2350 p " + SPEC,      # hour out of range
            "/later 6:5 2330 2350 p " + SPEC,        # minutes not 2 digits
            "/later 06:00 2350 2330 p " + SPEC,      # low above high
            "/later 06:00 2330 2350 m " + SPEC,      # market not supported
            "/later 06:00 2330 2350 p 2336",         # incomplete spec
            "/later 06:00 2330 2350 p junk junk",    # nonsense spec
            "/later 06:00 2330 2350",                # too few tokens
            "/later off abc",                        # id not a number
        ]
        for text in bad:
            with self.assertRaises(ParseError, msg=text):
                parse_later(text)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = ScheduledOrderStore(self.dir + "/test.db")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def arm(self, chat_id=1, next_fire=999):
        return self.store.arm(chat_id, "5k", "06:00", 2330.0, 2350.0,
                              SPEC, next_fire, created_at=100)

    def test_arm_roundtrip(self):
        rule = self.arm()
        loaded = self.store.get(rule.id)
        self.assertEqual(loaded.account, "5k")
        self.assertEqual(loaded.fire_time, "06:00")
        self.assertEqual(loaded.band_low, 2330.0)
        self.assertEqual(loaded.band_high, 2350.0)
        self.assertEqual(loaded.spec, SPEC)
        self.assertEqual(loaded.status, "armed")
        self.assertEqual(loaded.attempts, 0)
        self.assertEqual([r.id for r in self.store.armed()], [rule.id])

    def test_reschedule_updates_next_fire_and_attempts(self):
        rule = self.arm()
        self.store.reschedule(rule.id, 123456, 2)
        loaded = self.store.get(rule.id)
        self.assertEqual(loaded.next_fire, 123456)
        self.assertEqual(loaded.attempts, 2)
        self.assertEqual(loaded.status, "armed")

    def test_settle_removes_it_from_armed(self):
        rule = self.arm()
        self.store.settle(rule.id, "done")
        self.assertEqual(self.store.armed(), [])
        self.assertEqual(self.store.get(rule.id).status, "done")
        self.assertIsNotNone(self.store.get(rule.id).settled_at)

    def test_cancel_only_when_armed_and_owned(self):
        rule = self.arm(chat_id=42)
        self.assertTrue(self.store.cancel(rule.id, 42))
        self.assertFalse(self.store.cancel(rule.id, 42))   # already done
        other = self.arm(chat_id=43)
        self.assertFalse(self.store.cancel(other.id, 99))  # wrong chat
        self.assertEqual([r.id for r in self.store.armed()], [other.id])

    def test_recent_lists_armed_first(self):
        first = self.arm(next_fire=900)
        second = self.arm(next_fire=800)
        self.store.settle(first.id, "skipped")
        ids = [r.id for r in self.store.recent()]
        self.assertEqual(ids, [second.id, first.id])


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = ScheduledOrderStore(self.dir + "/test.db")
        self.clock = FakeClock(now=1000.0)
        self.sent = Sent()
        self.bid = FakeBid(2340.0)
        self.trader = FakeTrader()
        self.pending_cc = {}
        self.cancel_watch = {}
        self.runner = ScheduledOrderRunner(
            self.store, self.trader, FakeSettings(), self.sent, self.bid,
            watch_subscribe=FakeWatchSubscribe(),
            pending_cc=self.pending_cc,
            order_cancel_watch=self.cancel_watch,
            parse_spec=parse_trade, format_success=order_success_text,
            clock=self.clock)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def arm(self, next_fire=980.0, spec=SPEC):
        """A rule due 20s ago (within the grace window)."""
        return self.runner.arm(1, "5k", "06:00", 2330.0, 2350.0, spec,
                               int(next_fire))

    def evaluate(self, rule):
        run(self.runner._evaluate(rule, self.clock.now))

    def test_fires_in_band(self):
        rule = self.arm()
        self.evaluate(rule)
        self.assertEqual(len(self.trader.calls), 1)
        self.assertEqual(self.trader.calls[0].entry, 2336.0)
        self.assertEqual(self.trader.calls[0].account, "5k")
        self.assertEqual(self.store.get(rule.id).status, "done")
        self.assertIn("#777", self.sent.joined())
        self.assertIn("inside band", self.sent.joined())

    def test_a_late_fire_within_grace_still_runs(self):
        rule = self.arm(next_fire=self.clock.now - 500)
        self.evaluate(rule)
        self.assertEqual(self.store.get(rule.id).status, "done")

    def test_skips_when_price_is_outside_the_band(self):
        self.bid.bid = 2360.0
        rule = self.arm()
        self.evaluate(rule)
        self.assertEqual(self.trader.calls, [])
        self.assertEqual(self.store.get(rule.id).status, "skipped")
        self.assertIn("skipped", self.sent.joined())

    def test_a_fetch_failure_retries_then_fires(self):
        self.bid.bid = None
        rule = self.arm()
        self.evaluate(rule)
        loaded = self.store.get(rule.id)
        self.assertEqual(loaded.status, "armed")
        self.assertEqual(loaded.attempts, 1)
        self.assertEqual(loaded.next_fire, self.clock.now + 600)
        self.assertIn("retrying", self.sent.joined())

        self.clock.now = loaded.next_fire
        self.bid.bid = 2340.0
        self.evaluate(self.store.armed()[0])
        self.assertEqual(self.store.get(rule.id).status, "done")
        self.assertEqual(len(self.trader.calls), 1)

    def test_three_failures_give_up(self):
        self.bid.bid = None
        rule = self.arm()
        for _ in range(MAX_ATTEMPTS):
            self.evaluate(self.store.armed()[0])
            self.clock.now += 600
        loaded = self.store.get(rule.id)
        self.assertEqual(loaded.status, "failed")
        self.assertEqual(self.trader.calls, [])
        self.assertIn("gave up", self.sent.joined())

    def test_a_retry_rechecks_the_band(self):
        self.trader.fail_times = 1
        rule = self.arm()
        self.evaluate(rule)                       # fires, broker errors
        self.assertEqual(self.store.get(rule.id).status, "armed")
        self.clock.now += 600
        self.bid.bid = 2360.0                     # price left the band
        self.evaluate(self.store.armed()[0])
        self.assertEqual(self.store.get(rule.id).status, "skipped")
        self.assertEqual(len(self.trader.calls), 1)

    def test_a_missed_window_rearms_for_tomorrow(self):
        rule = self.arm(next_fire=self.clock.now - 2000)
        self.evaluate(rule)
        loaded = self.store.get(rule.id)
        self.assertEqual(loaded.status, "armed")
        self.assertGreater(loaded.next_fire, self.clock.now)
        self.assertIn("missed", self.sent.joined())

    def test_followups_register_like_a_manual_p(self):
        rule = self.arm(spec=SPEC + " --smart-sl 2338 M15 --cancel 2325 "
                                 "--all")
        self.evaluate(rule)
        guard = self.pending_cc["777"]
        self.assertEqual(guard["timeframe"], "M15")
        self.assertEqual(guard["cc_price"], 2338.0)
        self.assertEqual(guard["direction"], "BUY")
        self.assertEqual(guard["account"], "5k")
        self.assertTrue(guard["broadcast"])
        watch = self.cancel_watch["777"]
        self.assertEqual(watch["level"], 2325.0)
        self.assertEqual(watch["symbol_id"], 9)
        self.assertEqual(watch["chat_id"], 1)
        self.assertIn("cancel condition set", self.sent.joined())

    def test_the_run_loop_evaluates_due_rules(self):
        rule = self.arm(next_fire=int(self.clock.now - 5))

        async def scenario():
            task = asyncio.ensure_future(self.runner.run())
            try:
                for _ in range(100):
                    if self.store.get(rule.id).status != "armed":
                        break
                    await asyncio.sleep(0.01)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        run(scenario())
        self.assertEqual(self.store.get(rule.id).status, "done")
        self.assertEqual(len(self.trader.calls), 1)


class OrderSuccessTextTests(unittest.TestCase):
    def test_a_pending_plan_reads_like_a_manual_p(self):
        args = parse_trade("/p " + SPEC, False)
        plan = risk.plan_pending(2340.0, 2340.3, 2336.0, 2330.0, False,
                                 3.0, 1.0, 10000.0)
        result = OrderResult(order_id=777, execution_type="ORDER_PLACED")
        symbol = SymbolInfo(symbol_id=1, name="XAUUSD", digits=2,
                            lot_size=10000, min_volume=100,
                            step_volume=100, pip_position=2)
        text = order_success_text(plan, symbol, result, 0.15, args)
        self.assertIn("BUY", text)
        self.assertIn("PENDING", text)
        self.assertIn("#777", text)
        self.assertIn("2336.30", text)   # entry with its placement


if __name__ == "__main__":
    unittest.main()
