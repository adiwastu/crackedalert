"""Quiet hours: window boundaries, the hold store, and digest rendering
(see quiet_hours.py). All times are fixed UTC timestamps; offset +7 is
the operator's WIB clock."""

import calendar
import shutil
import tempfile
import unittest

from crackedalert.quiet_hours import (HeldMessageStore, build_digest,
                                      is_quiet_now,
                                      seconds_until_window_end)


def utc(year, month, day, hour, minute=0, second=0):
    return calendar.timegm((year, month, day, hour, minute, second))


class IsQuietNowTests(unittest.TestCase):
    OFF = 7.0    # WIB: quiet 22:00-04:59 local == 15:00-21:59 UTC

    def test_window_boundaries_local(self):
        cases = [
            (utc(2026, 10, 17, 14, 59), False),  # 21:59 local
            (utc(2026, 10, 17, 15, 0), True),    # 22:00 local
            (utc(2026, 10, 17, 18, 0), True),    # 01:00 local (next day)
            (utc(2026, 10, 17, 21, 59), True),   # 04:59 local
            (utc(2026, 10, 17, 22, 0), False),   # 05:00 local
            (utc(2026, 10, 17, 12, 0), False),   # 19:00 local
        ]
        for now, expected in cases:
            self.assertEqual(is_quiet_now(now, self.OFF), expected,
                             "utc %s" % time_str(now))

    def test_zero_offset(self):
        self.assertTrue(is_quiet_now(utc(2026, 10, 17, 22, 30), 0.0))
        self.assertFalse(is_quiet_now(utc(2026, 10, 17, 21, 59), 0.0))
        self.assertFalse(is_quiet_now(utc(2026, 10, 17, 5, 0), 0.0))
        self.assertTrue(is_quiet_now(utc(2026, 10, 17, 4, 59), 0.0))

    def test_negative_offset(self):
        # UTC-5 (EST): quiet 22:00-04:59 local == 03:00-09:59 UTC.
        self.assertTrue(is_quiet_now(utc(2026, 10, 17, 4, 0), -5.0))
        self.assertFalse(is_quiet_now(utc(2026, 10, 17, 10, 0), -5.0))


def time_str(ts):
    import time
    return time.strftime("%H:%M:%S", time.gmtime(ts))


class SecondsUntilWindowEndTests(unittest.TestCase):
    OFF = 7.0

    def test_next_end_is_always_05_local(self):
        # 23:00 local Sat (16:00 UTC) -> 05:00 local Sun (22:00 UTC Sun).
        self.assertEqual(
            seconds_until_window_end(utc(2026, 10, 17, 16, 0), self.OFF),
            utc(2026, 10, 17, 22, 0) - utc(2026, 10, 17, 16, 0))

    def test_inside_window_counts_to_todays_end(self):
        # 02:00 local Sat (19:00 UTC Fri... keep Sat) -> 05:00 local Sat.
        self.assertEqual(
            seconds_until_window_end(utc(2026, 10, 17, 19, 0), self.OFF),
            utc(2026, 10, 17, 22, 0) - utc(2026, 10, 17, 19, 0))

    def test_positive_value_always(self):
        for hour in range(24):
            secs = seconds_until_window_end(
                utc(2026, 10, 17, hour, 0), self.OFF)
            self.assertGreater(secs, 0)
            self.assertLessEqual(secs, 86400)


class HeldMessageStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = HeldMessageStore(self.dir + "/test.db")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_roundtrip_ordering(self):
        self.store.add(1, "later msg", 200)
        self.store.add(1, "early msg", 100)
        self.store.add(2, "other chat", 150)
        rows = self.store.all()
        self.assertEqual([r[2] for r in rows],
                         ["early msg", "other chat", "later msg"])
        self.assertEqual(rows[0][0], 1)

    def test_clear(self):
        self.store.add(1, "x", 100)
        self.store.clear()
        self.assertEqual(self.store.all(), [])


class BuildDigestTests(unittest.TestCase):
    OFF = 7.0

    def test_groups_by_chat_with_local_stamps(self):
        rows = [
            (1, utc(2026, 10, 17, 16, 14), "alert A"),   # 23:14 local
            (1, utc(2026, 10, 17, 18, 37), "alert B"),   # 01:37 local
            (2, utc(2026, 10, 17, 19, 0), "alert C"),    # 02:00 local
        ]
        digests = build_digest(rows, self.OFF)
        chats = [chat for chat, _ in digests]
        self.assertEqual(sorted(chats), [1, 2])
        first = [text for chat, text in digests if chat == 1][0]
        self.assertIn("quiet-hours digest", first)
        self.assertIn("2 held", first)
        self.assertIn("-- 23:14 --\nalert A", first)
        self.assertIn("-- 01:37 --\nalert B", first)

    def test_chunks_before_telegram_limit(self):
        rows = [(1, utc(2026, 10, 17, 16, i), "x" * 900)
                for i in range(6)]
        digests = build_digest(rows, self.OFF)
        self.assertGreater(len(digests), 1)
        for _, text in digests:
            self.assertLess(len(text), 4096)
        total_items = sum(text.count("-- ") for _, text in digests)
        self.assertEqual(total_items, 6)

    def test_empty_rows(self):
        self.assertEqual(build_digest([], self.OFF), [])


if __name__ == "__main__":
    unittest.main()
