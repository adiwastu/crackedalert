"""Broadcast market structure after a zone is touched.

When a DH1 or S H1 zone alert fires, every break on the watched
timeframes is broadcast to all subscribers until the first CHoCH ends
the watch. It exists to see how structure actually unfolds off a zone,
as it happens, rather than reconstructing it from the log afterwards.

Watches persist in SQLite, keyed by symbol, so a restart or deploy in
the middle of one does not silently end it.
"""

import logging
import sqlite3
import time
from typing import Awaitable, Callable, Dict, Optional, Sequence, Tuple

from .alerts import CROSSING_DOWN
from .wave import BOS, CHOCH, WaveEvent
from .wave_state import PERIOD_MINUTES, bar_close_time

log = logging.getLogger("crackedalert.zone")

WATCH_TIMEFRAMES = ("M5",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS zone_watch (
    symbol     TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    level      REAL NOT NULL,
    touched_at INTEGER NOT NULL
);
"""

Broadcast = Callable[[str], Awaitable[None]]
Watch = Tuple[str, float, int]          # label, zone level, touched at (s)


def zone_label(direction: str) -> str:
    """DH1 is demand, reached by price coming down into it; S H1 is
    supply, reached coming up. The FVG watcher arms them that way round
    (see IMBALANCE_ALERT_SPECS)."""
    return "DH1" if direction == CROSSING_DOWN else "S H1"


class ZoneWatchStore:
    """At most one open watch per symbol."""

    def __init__(self, db_path: str):
        self._db = sqlite3.connect(db_path)
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def load(self) -> Dict[str, Watch]:
        rows = self._db.execute(
            "SELECT symbol, label, level, touched_at FROM zone_watch")
        return {symbol: (label, level, touched_at)
                for symbol, label, level, touched_at in rows}

    def start(self, symbol: str, label: str, level: float,
              touched_at: int) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO zone_watch VALUES (?, ?, ?, ?)",
            (symbol.upper(), label, level, touched_at))
        self._db.commit()

    def end(self, symbol: str) -> None:
        self._db.execute("DELETE FROM zone_watch WHERE symbol = ?",
                         (symbol.upper(),))
        self._db.commit()


class ZoneWatch:
    """Opens a watch on a zone touch and narrates breaks until a CHoCH.

    A touch while a watch is already open restarts it: the newer zone is
    the one price is reacting to. The CHoCH that ends a watch is itself
    broadcast -- it is the most informative line of the lot.
    """

    def __init__(self, store: ZoneWatchStore, broadcast: Broadcast,
                 utc_offset: float = 0.0,
                 timeframes: Sequence[str] = WATCH_TIMEFRAMES,
                 clock: Callable[[], float] = time.time):
        self._store = store
        self._broadcast = broadcast
        self._utc_offset = utc_offset
        self._timeframes = tuple(tf.upper() for tf in timeframes)
        self._clock = clock
        self._watches: Dict[str, Watch] = store.load()
        for symbol, (label, _, _) in self._watches.items():
            log.info("zone watch resumed: %s %s", symbol, label)

    def watching(self, symbol: str) -> Optional[Watch]:
        return self._watches.get(symbol.upper())

    async def on_touch(self, symbol: str, label: str, level: float) -> None:
        symbol = symbol.upper()
        touched_at = int(self._clock())
        self._watches[symbol] = (label, level, touched_at)
        self._store.start(symbol, label, level, touched_at)
        log.info("zone watch opened: %s %s at %s", symbol, label,
                 _price(level))
        await self._send(
            "%s touched on %s at %s -- watching %s structure until the "
            "first CHoCH." % (label, symbol, _price(level),
                              "/".join(self._timeframes)))

    async def on_wave_event(self, event: WaveEvent) -> None:
        symbol = event.symbol.upper()
        watch = self._watches.get(symbol)
        if (watch is None or event.kind not in (BOS, CHOCH)
                or event.timeframe.upper() not in self._timeframes):
            return
        label, level, touched_at = watch

        ended = event.kind == CHOCH
        if ended:
            # State first, so a failed broadcast cannot leave a watch open.
            del self._watches[symbol]
            self._store.end(symbol)
            log.info("zone watch ended by CHoCH: %s %s", symbol, label)

        period = PERIOD_MINUTES.get(event.timeframe.upper(), 0)
        closed_at = (event.ts + period) * 60
        when = (bar_close_time(event.timeframe, event.ts, self._utc_offset)
                or "ts=%d" % event.ts)
        await self._send(
            "%s %s %s broke %s at %s -- %s after %s touch.%s"
            % (event.timeframe, event.kind, event.direction,
               _price(event.level), when,
               _elapsed(max(0, closed_at - touched_at)), label,
               " Watch ended." if ended else ""))

    async def _send(self, text: str) -> None:
        try:
            await self._broadcast(text)
        except Exception:
            log.exception("zone watch broadcast failed: %s", text)


def _price(value: float) -> str:
    """Enough precision for any symbol, without trailing zeros."""
    return ("%.5f" % value).rstrip("0").rstrip(".")


def _elapsed(seconds: int) -> str:
    hours, minutes = divmod(seconds // 60, 60)
    if hours and minutes:
        return "%dh%02dm" % (hours, minutes)
    if hours:
        return "%dh" % hours
    return "%dm" % minutes
