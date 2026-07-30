"""Alert store + engine tests (in-memory SQLite, fake notifier)."""

import asyncio
import os
import tempfile
import unittest

from crackedalert import alerts
from crackedalert.bot import formatting as fmt


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = alerts.AlertStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_create_and_list(self):
        a = self.store.create(111, "xauusd", 2450.0,
                              alerts.CROSSING_UP, "demand zone")
        self.assertEqual(len(a.id), 4)
        self.assertEqual(a.symbol, "XAUUSD")
        rows = self.store.for_chat(111)
        self.assertEqual([r.id for r in rows], [a.id])
        self.assertEqual(self.store.for_chat(222), [])

    def test_cancel_ownership(self):
        a = self.store.create(111, "XAUUSD", 2450.0,
                              alerts.CROSSING_UP, "x")
        self.assertFalse(self.store.cancel(a.id, 999))   # not the owner
        self.assertTrue(self.store.cancel(a.id.lower(), 111))
        self.assertFalse(self.store.cancel(a.id, 111))   # already gone

    def test_active_symbols(self):
        self.store.create(1, "XAUUSD", 1.0, alerts.CROSSING_UP, "a")
        self.store.create(1, "EURUSD", 2.0, alerts.CROSSING_DOWN, "b")
        self.assertEqual(self.store.active_symbols(), {"XAUUSD", "EURUSD"})

    def test_tsv_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            tsv = os.path.join(tmp, "cracked_alerts.tsv")
            with open(tsv, "w", encoding="utf-8") as f:
                f.write("AB12\t111\tXAUUSD\t2450.00\tCROSSING_UP\tzone\n")
                f.write("badline\n")
                f.write("CD34\t222\tXAUUSD\t2400.00\tCROSSING_DOWN\tsupport\n")
            count = self.store.import_tsv(tsv)
            self.assertEqual(count, 2)
            self.assertTrue(os.path.exists(tsv + ".imported"))
            self.assertEqual(len(self.store.for_chat(111)), 1)
            self.assertEqual(len(self.store.for_chat(222)), 1)

    def test_import_missing_file_is_noop(self):
        self.assertEqual(self.store.import_tsv("/nonexistent/file.tsv"), 0)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.store = alerts.AlertStore(":memory:")
        self.sent = []

        async def notify(chat_id, text):
            self.sent.append((chat_id, text))

        self.engine = alerts.AlertEngine(self.store, notify, fmt.alert_fired)

    def tearDown(self):
        self.store.close()

    def test_crossing_up_fires_at_or_above_target(self):
        a = self.store.create(111, "XAUUSD", 2450.0,
                              alerts.CROSSING_UP, "note")
        run(self.engine.on_tick("XAUUSD", 2449.0, 2449.4))   # mid 2449.2
        self.assertEqual(self.sent, [])
        run(self.engine.on_tick("XAUUSD", 2449.9, 2450.3))   # mid 2450.1
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], 111)
        self.assertIn(a.id, self.sent[0][1])
        self.assertEqual(self.store.for_chat(111), [])       # deleted

    def test_crossing_down_fires_at_or_below_target(self):
        self.store.create(111, "XAUUSD", 2400.0,
                          alerts.CROSSING_DOWN, "note")
        run(self.engine.on_tick("XAUUSD", 2400.5, 2400.9))   # mid 2400.7
        self.assertEqual(self.sent, [])
        run(self.engine.on_tick("XAUUSD", 2399.5, 2399.9))   # mid 2399.7
        self.assertEqual(len(self.sent), 1)

    def test_failed_notify_keeps_alert(self):
        self.store.create(111, "XAUUSD", 2450.0,
                          alerts.CROSSING_UP, "note")

        async def broken_notify(chat_id, text):
            raise RuntimeError("telegram down")

        engine = alerts.AlertEngine(self.store, broken_notify,
                                    fmt.alert_fired)
        run(engine.on_tick("XAUUSD", 2450.0, 2450.4))
        self.assertEqual(len(self.store.for_chat(111)), 1)   # retained

    def test_other_symbol_untouched(self):
        self.store.create(111, "EURUSD", 1.10, alerts.CROSSING_UP, "x")
        run(self.engine.on_tick("XAUUSD", 2000.0, 2000.4))
        self.assertEqual(self.sent, [])


class DirectionInference(unittest.TestCase):
    def test_bash_parity(self):
        self.assertEqual(alerts.infer_direction(2400.0, 2450.0),
                         alerts.CROSSING_UP)      # live below target
        self.assertEqual(alerts.infer_direction(2500.0, 2450.0),
                         alerts.CROSSING_DOWN)    # live above target
        self.assertEqual(alerts.infer_direction(2450.0, 2450.0),
                         alerts.CROSSING_DOWN)    # tie: bash "else" branch


if __name__ == "__main__":
    unittest.main()
