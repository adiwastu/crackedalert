"""Alert store + engine tests (in-memory SQLite, fake notifier)."""

import asyncio
import os
import tempfile
import unittest

from crackedalert import alerts
from crackedalert import risk
from crackedalert.bot import formatting as fmt
from crackedalert.bot import handlers


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

    def test_broadcast_persist_and_default(self):
        plain = self.store.create(1, "XAUUSD", 1.0, alerts.CROSSING_UP, "p")
        self.assertFalse(plain.broadcast)
        bcast = self.store.create(1, "XAUUSD", 2.0, alerts.CROSSING_UP,
                                  "b", broadcast=True)
        self.assertTrue(bcast.broadcast)
        # round-trips through the DB
        rows = self.store.for_chat(1)
        by_id = {r.id: r.broadcast for r in rows}
        self.assertFalse(by_id[plain.id])
        self.assertTrue(by_id[bcast.id])

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
        self.store.create(111, "XAUUSD", 2450.0,
                          alerts.CROSSING_UP, "note")
        run(self.engine.on_tick("XAUUSD", 2449.0, 2449.4))   # mid 2449.2
        self.assertEqual(self.sent, [])
        run(self.engine.on_tick("XAUUSD", 2449.9, 2450.3))   # mid 2450.1
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], 111)
        self.assertEqual(self.sent[0][1], "note")   # notes-only message
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


class BroadcastEngineTests(unittest.TestCase):
    def setUp(self):
        self.store = alerts.AlertStore(":memory:")
        self.sent = []
        self.broadcast = []

        async def notify(chat_id, text):
            self.sent.append((chat_id, text))

        async def broadcast(text):
            self.broadcast.append(text)

        self.engine = alerts.AlertEngine(self.store, notify, fmt.alert_fired,
                                         on_broadcast=broadcast)

    def tearDown(self):
        self.store.close()

    def test_broadcast_alert_goes_to_all_not_owner(self):
        self.store.create(111, "XAUUSD", 2450.0, alerts.CROSSING_UP,
                          "note", broadcast=True)
        run(self.engine.on_tick("XAUUSD", 2450.0, 2450.4))
        self.assertEqual(len(self.broadcast), 1)
        self.assertEqual(self.sent, [])           # owner NOT notified
        self.assertEqual(self.broadcast[0], "note")   # notes-only message
        self.assertEqual(self.store.for_chat(111), [])   # deleted

    def test_broadcast_failure_drops_alert(self):
        # broadcast has no single owner to retry for; a failed broadcast is
        # dropped (fire-and-forget) and the alert deleted.
        self.store.create(111, "XAUUSD", 2450.0, alerts.CROSSING_UP,
                          "note", broadcast=True)

        async def broken_broadcast(text):
            raise RuntimeError("telegram down")

        engine = alerts.AlertEngine(self.store,
                                    lambda c, t: None,
                                    fmt.alert_fired,
                                    on_broadcast=broken_broadcast)
        run(engine.on_tick("XAUUSD", 2450.0, 2450.4))
        self.assertEqual(self.store.for_chat(111), [])        # dropped


class CandleBroadcastEngineTests(unittest.TestCase):
    def setUp(self):
        self.store = alerts.CandleAlertStore(":memory:")
        self.sent = []
        self.broadcast = []

        async def notify(chat_id, text):
            self.sent.append((chat_id, text))

        async def broadcast(text):
            self.broadcast.append(text)

        self.engine = alerts.CandleAlertEngine(
            self.store, notify, fmt.candle_alert_fired,
            on_broadcast=broadcast)

    def tearDown(self):
        self.store.close()

    def test_broadcast_candle_alert_goes_to_all_not_owner(self):
        self.store.create(111, "XAUUSD", "M15", 2450.0,
                          alerts.CANDLE_ABOVE, "note", broadcast=True)
        run(self.engine.on_closed_bar("XAUUSD", "M15", 2450.5, 100))
        self.assertEqual(len(self.broadcast), 1)
        self.assertEqual(self.sent, [])                  # owner NOT notified
        self.assertEqual(self.store.for_chat(111), [])   # deleted

    def test_broadcast_failure_keeps_candle_alert(self):
        self.store.create(111, "XAUUSD", "M15", 2450.0,
                          alerts.CANDLE_ABOVE, "note", broadcast=True)

        async def broken_broadcast(text):
            raise RuntimeError("telegram down")

        engine = alerts.CandleAlertEngine(
            self.store, lambda c, t: None, fmt.candle_alert_fired,
            on_broadcast=broken_broadcast)
        run(engine.on_closed_bar("XAUUSD", "M15", 2450.5, 100))
        self.assertEqual(len(self.store.for_chat(111)), 1)   # retained


class DirectionInference(unittest.TestCase):
    def test_bash_parity(self):
        self.assertEqual(alerts.infer_direction(2400.0, 2450.0),
                         alerts.CROSSING_UP)      # live below target
        self.assertEqual(alerts.infer_direction(2500.0, 2450.0),
                         alerts.CROSSING_DOWN)    # live above target
        self.assertEqual(alerts.infer_direction(2450.0, 2450.0),
                         alerts.CROSSING_DOWN)    # tie: bash "else" branch


class CandleStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = alerts.CandleAlertStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_create_and_list(self):
        a = self.store.create(111, "xauusd", "M15", 2450.0,
                              alerts.CANDLE_ABOVE, "breakout")
        self.assertEqual(len(a.id), 4)
        self.assertEqual(a.symbol, "XAUUSD")
        self.assertEqual(a.timeframe, "M15")
        rows = self.store.for_chat(111)
        self.assertEqual([r.id for r in rows], [a.id])
        self.assertEqual(self.store.for_chat(222), [])

    def test_for_key(self):
        self.store.create(1, "XAUUSD", "M15", 2450.0,
                          alerts.CANDLE_ABOVE, "a")
        self.store.create(1, "XAUUSD", "H1", 2400.0,
                          alerts.CANDLE_BELOW, "b")
        self.store.create(1, "EURUSD", "M15", 1.1,
                          alerts.CANDLE_ABOVE, "c")
        self.assertEqual(len(self.store.for_key("XAUUSD", "M15")), 1)
        self.assertEqual(len(self.store.for_key("XAUUSD", "H1")), 1)
        self.assertEqual(len(self.store.for_key("EURUSD", "M15")), 1)

    def test_active_keys(self):
        self.store.create(1, "XAUUSD", "M15", 2450.0,
                          alerts.CANDLE_ABOVE, "a")
        self.store.create(1, "EURUSD", "H1", 1.1,
                          alerts.CANDLE_BELOW, "b")
        self.assertEqual(self.store.active_keys(),
                         {("XAUUSD", "M15"), ("EURUSD", "H1")})

    def test_cancel_ownership(self):
        a = self.store.create(111, "XAUUSD", "M15", 2450.0,
                              alerts.CANDLE_ABOVE, "x")
        self.assertFalse(self.store.cancel(a.id, 999))
        self.assertTrue(self.store.cancel(a.id.lower(), 111))
        self.assertFalse(self.store.cancel(a.id, 111))


class CandleEngineTests(unittest.TestCase):
    def setUp(self):
        self.store = alerts.CandleAlertStore(":memory:")
        self.sent = []

        async def notify(chat_id, text):
            self.sent.append((chat_id, text))

        self.engine = alerts.CandleAlertEngine(
            self.store, notify, fmt.candle_alert_fired)

    def tearDown(self):
        self.store.close()

    def test_above_fires_when_close_above_target(self):
        self.store.create(111, "XAUUSD", "M15", 2450.0,
                          alerts.CANDLE_ABOVE, "note")
        run(self.engine.on_closed_bar("XAUUSD", "M15", 2449.0, 100))
        self.assertEqual(self.sent, [])
        run(self.engine.on_closed_bar("XAUUSD", "M15", 2450.5, 101))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1], "note")   # notes-only message
        self.assertEqual(self.store.for_chat(111), [])   # deleted

    def test_below_fires_when_close_below_target(self):
        self.store.create(111, "XAUUSD", "H1", 2400.0,
                          alerts.CANDLE_BELOW, "note")
        run(self.engine.on_closed_bar("XAUUSD", "H1", 2400.5, 100))
        self.assertEqual(self.sent, [])
        run(self.engine.on_closed_bar("XAUUSD", "H1", 2399.5, 101))
        self.assertEqual(len(self.sent), 1)

    def test_other_key_untouched(self):
        self.store.create(111, "EURUSD", "M15", 1.1,
                          alerts.CANDLE_ABOVE, "x")
        run(self.engine.on_closed_bar("XAUUSD", "M15", 2000.0, 100))
        self.assertEqual(self.sent, [])

    def test_guard_removal_callback_fires(self):
        removed = []

        async def notify(chat_id, text):
            pass

        async def close_hit(alert):
            pass

        engine = alerts.CandleAlertEngine(
            self.store, notify, fmt.candle_alert_fired,
            on_close_hit=close_hit,
            on_alert_removed=lambda: removed.append(True))
        self.store.create(111, "XAUUSD", "M15", 2450.0,
                          alerts.CANDLE_ABOVE, "guard",
                          action="close", position_id=42)
        run(engine.on_closed_bar("XAUUSD", "M15", 2450.5, 101))
        self.assertEqual(removed, [True])
        self.assertEqual(self.store.for_key("XAUUSD", "M15"), [])

    def test_failed_notify_keeps_alert(self):
        self.store.create(111, "XAUUSD", "M15", 2450.0,
                          alerts.CANDLE_ABOVE, "note")

        async def broken_notify(chat_id, text):
            raise RuntimeError("telegram down")

        engine = alerts.CandleAlertEngine(self.store, broken_notify,
                                          fmt.candle_alert_fired)
        run(engine.on_closed_bar("XAUUSD", "M15", 2450.5, 100))
        self.assertEqual(len(self.store.for_chat(111)), 1)   # retained


class ManualEngineTests(unittest.TestCase):
    def test_manual_still_notifies_owning_chat(self):
        store = alerts.AlertStore(":memory:")
        sent = []

        async def notify(chat_id, text):
            sent.append((chat_id, text))

        engine = alerts.AlertEngine(store, notify, fmt.alert_fired)
        store.create(111, "XAUUSD", 2450.0, alerts.CROSSING_UP, "note")
        run(engine.on_tick("XAUUSD", 2450.0, 2450.4))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], 111)
        store.close()


class AutoAlertStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = alerts.AlertStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_startup_purges_legacy_auto_rows(self):
        """v2.0.31 removed the trade auto-alert chain: legacy entry/tp/sl
        rows must be purged on the first open so they can never fire again."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "alerts.db")
            s = alerts.AlertStore(path)
            # raw SQL: the KIND_* constants no longer exist in the module
            s._db.execute(
                "INSERT INTO alerts VALUES ('ZZZ9', 1, 'XAUUSD', 2450.0, "
                "'CROSSING_UP', 'entry', strftime('%s','now'), 'entry', "
                "'9', 'demo', '', 0, 0)")
            s._db.commit()
            self.assertEqual(len(s.for_chat(1)), 1)   # present before reopen
            s.close()
            s2 = alerts.AlertStore(path)              # _migrate runs here
            self.assertEqual(s2.for_chat(1), [])      # purged
            s2.close()

    def test_migration_adds_columns(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "alerts.db")
            conn = sqlite3.connect(path)
            # simulate an old table without the new columns
            conn.execute(
                "CREATE TABLE alerts (id TEXT PRIMARY KEY, chat_id INTEGER, "
                "symbol TEXT, target REAL, direction TEXT, message TEXT, "
                "created_at INTEGER)")
            conn.execute(
                "INSERT INTO alerts VALUES ('AAAA', 1, 'XAUUSD', 2450.0, "
                "'CROSSING_UP', 'note', 0)")
            conn.commit()
            conn.close()
            s = alerts.AlertStore(path)
            cols = {r[1] for r in s._db.execute("PRAGMA table_info(alerts)")}
            self.assertIn("kind", cols)
            self.assertIn("trade_id", cols)
            self.assertIn("account", cols)
            self.assertIn("cc_timeframe", cols)
            self.assertIn("cc_price", cols)
            self.assertEqual(len(s.for_chat(1)), 1)   # legacy row readable
            s.close()


class CandleGuardStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = alerts.CandleAlertStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_create_guard_stores_fields(self):
        a = self.store.create(111, "XAUUSD", "M15", 4080.0,
                              alerts.CANDLE_BELOW, "guard",
                              action="close", position_id=42,
                              account="demo")
        rows = self.store.for_chat(111)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].action, "close")
        self.assertEqual(rows[0].position_id, 42)
        self.assertEqual(rows[0].account, "demo")

    def test_guard_in_for_key(self):
        self.store.create(111, "XAUUSD", "M15", 4080.0,
                          alerts.CANDLE_BELOW, "guard",
                          action="close", position_id=42,
                          account="demo")
        rows = self.store.for_key("XAUUSD", "M15")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].action, "close")
        self.assertEqual(rows[0].position_id, 42)

    def test_cancel_for_position(self):
        self.store.create(111, "XAUUSD", "M15", 4080.0,
                          alerts.CANDLE_BELOW, "guard",
                          action="close", position_id=42,
                          account="demo")
        self.store.create(111, "XAUUSD", "M15", 2450.0,
                          alerts.CANDLE_ABOVE, "notify alert")
        n = self.store.cancel_for_position(42)
        self.assertEqual(n, 1)
        rows = self.store.for_chat(111)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].action, "notify")

    def test_cancel_for_position_wrong_id(self):
        self.store.create(111, "XAUUSD", "M15", 4080.0,
                          alerts.CANDLE_BELOW, "guard",
                          action="close", position_id=42,
                          account="demo")
        n = self.store.cancel_for_position(99)
        self.assertEqual(n, 0)

    def test_notify_defaults(self):
        a = self.store.create(111, "XAUUSD", "M15", 2450.0,
                              alerts.CANDLE_ABOVE, "note")
        self.assertEqual(a.action, "notify")
        self.assertEqual(a.position_id, 0)
        self.assertEqual(a.account, "")

    def test_candle_migration_adds_columns(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "candles.db")
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE candle_alerts (id TEXT PRIMARY KEY, "
                "chat_id INTEGER, symbol TEXT, timeframe TEXT, "
                "target REAL, direction TEXT, message TEXT, "
                "created_at INTEGER)")
            conn.execute(
                "INSERT INTO candle_alerts VALUES ('AAAA', 1, 'XAUUSD', "
                "'M15', 2450.0, 'ABOVE', 'note', 0)")
            conn.commit()
            conn.close()
            s = alerts.CandleAlertStore(path)
            cols = {r[1] for r in s._db.execute(
                "PRAGMA table_info(candle_alerts)")}
            self.assertIn("action", cols)
            self.assertIn("position_id", cols)
            self.assertIn("account", cols)
            rows = s.for_chat(1)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].action, "notify")
            self.assertEqual(rows[0].position_id, 0)
            self.assertEqual(rows[0].account, "")
            s.close()


class CandleGuardEngineTests(unittest.TestCase):
    def setUp(self):
        self.store = alerts.CandleAlertStore(":memory:")
        self.sent = []
        self.close_hits = []

        async def notify(chat_id, text):
            self.sent.append((chat_id, text))

        async def on_close_hit(alert):
            self.close_hits.append(alert)

        self.engine = alerts.CandleAlertEngine(
            self.store, notify, fmt.candle_alert_fired,
            on_close_hit=on_close_hit)

    def tearDown(self):
        self.store.close()

    def test_guard_calls_on_close_hit_not_notify(self):
        self.store.create(111, "XAUUSD", "M15", 4080.0,
                          alerts.CANDLE_BELOW, "guard",
                          action="close", position_id=42,
                          account="demo")
        run(self.engine.on_closed_bar("XAUUSD", "M15", 4079.0, 100))
        self.assertEqual(len(self.close_hits), 1)
        self.assertEqual(self.close_hits[0].position_id, 42)
        self.assertEqual(self.sent, [])   # no notify
        self.assertEqual(self.store.for_chat(111), [])   # deleted

    def test_guard_kept_on_transient_error(self):
        self.store.create(111, "XAUUSD", "M15", 4080.0,
                          alerts.CANDLE_BELOW, "guard",
                          action="close", position_id=42,
                          account="demo")

        async def noop(chat_id, text):
            pass

        async def broken_close(alert):
            raise RuntimeError("link down")

        engine = alerts.CandleAlertEngine(
            self.store, noop, fmt.candle_alert_fired,
            on_close_hit=broken_close)
        run(engine.on_closed_bar("XAUUSD", "M15", 4079.0, 100))
        self.assertEqual(len(self.store.for_chat(111)), 1)   # retained

    def test_guard_not_crossed_stays(self):
        self.store.create(111, "XAUUSD", "M15", 4080.0,
                          alerts.CANDLE_BELOW, "guard",
                          action="close", position_id=42,
                          account="demo")
        run(self.engine.on_closed_bar("XAUUSD", "M15", 4081.0, 100))
        self.assertEqual(self.close_hits, [])
        self.assertEqual(len(self.store.for_chat(111)), 1)

    def test_notify_unaffected_by_guard(self):
        self.store.create(111, "XAUUSD", "M15", 2450.0,
                          alerts.CANDLE_ABOVE, "note")
        run(self.engine.on_closed_bar("XAUUSD", "M15", 2450.5, 100))
        self.assertEqual(self.close_hits, [])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.store.for_chat(111), [])

    def test_no_on_close_hook_falls_through_to_notify(self):
        self.store.create(111, "XAUUSD", "M15", 4080.0,
                          alerts.CANDLE_BELOW, "guard",
                          action="close", position_id=42,
                          account="demo")

        async def notify(chat_id, text):
            self.sent.append((chat_id, text))

        engine = alerts.CandleAlertEngine(
            self.store, notify, fmt.candle_alert_fired)
        run(engine.on_closed_bar("XAUUSD", "M15", 4079.0, 100))
        self.assertEqual(len(self.sent), 1)   # fell through to notify
        self.assertEqual(self.store.for_chat(111), [])


class AlertStoreGuardFieldTests(unittest.TestCase):
    def setUp(self):
        self.store = alerts.AlertStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_cc_fields_round_trip(self):
        a = self.store.create(111, "XAUUSD", 2450.0,
                              alerts.CROSSING_UP, "note",
                              cc_timeframe="M15", cc_price=4080.0)
        rows = self.store.for_symbol("XAUUSD")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].cc_timeframe, "M15")
        self.assertEqual(rows[0].cc_price, 4080.0)

    def test_alert_migration_adds_cc_columns(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "alerts.db")
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE alerts (id TEXT PRIMARY KEY, chat_id INTEGER, "
                "symbol TEXT, target REAL, direction TEXT, message TEXT, "
                "created_at INTEGER)")
            conn.execute(
                "INSERT INTO alerts VALUES ('AAAA', 1, 'XAUUSD', 2450.0, "
                "'CROSSING_UP', 'note', 0)")
            conn.commit()
            conn.close()
            s = alerts.AlertStore(path)
            cols = {r[1] for r in s._db.execute("PRAGMA table_info(alerts)")}
            self.assertIn("cc_timeframe", cols)
            self.assertIn("cc_price", cols)
            rows = s.for_chat(1)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].cc_timeframe, "")
            self.assertEqual(rows[0].cc_price, 0.0)
            s.close()


if __name__ == "__main__":
    unittest.main()
