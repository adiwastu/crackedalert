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
from .fvg import zone_name
from .wave import BEARISH, BOS, BULLISH, CHOCH, WaveEvent, WaveState
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
Structure = Callable[[str, str], Optional[WaveState]]
Watch = Tuple[str, float, int]          # label, zone level, touched at (s)


def zone_label(direction: str, timeframe: str = "H1") -> str:
    """Demand is reached by price coming down into it, supply coming up;
    the FVG watchers arm them that way round. A zone alert's timeframe
    rides in its cc_timeframe, and zones armed before it was recorded
    are H1, so an empty timeframe means H1."""
    return zone_name("bullish" if direction == CROSSING_DOWN else "bearish",
                     timeframe or "H1")


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
                 clock: Callable[[], float] = time.time,
                 structure: Optional[Structure] = None):
        self._store = store
        self._broadcast = broadcast
        self._utc_offset = utc_offset
        self._timeframes = tuple(tf.upper() for tf in timeframes)
        self._clock = clock
        # Current wave state for (symbol, timeframe), to say where the
        # CHoCH that ends a watch sits. None until that key has primed.
        self._structure = structure
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
        text = ("%s touched on %s at %s -- watching %s structure until the "
                "first CHoCH." % (label, symbol, _price(level),
                                  "/".join(self._timeframes)))
        if self._structure is not None:
            for timeframe in self._timeframes:
                text += " " + _where_choch(
                    timeframe, self._structure(symbol, timeframe))
        await self._send(text)

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
        text = ("%s %s %s broke %s at %s -- %s after %s touch."
                % (event.timeframe, event.kind, event.direction,
                   _price(event.level), when,
                   _elapsed(max(0, closed_at - touched_at)), label))
        if ended:
            text += " Watch ended."
        else:
            # A BOS re-commits the opposite level, so the CHoCH announced
            # at the touch is stale now. The event carries the new one.
            threshold = _choch_threshold(event.direction, event.valid_high,
                                         event.valid_low)
            if threshold:
                text += " CHoCH now on %s." % threshold
        await self._send(text)

    async def _send(self, text: str) -> None:
        try:
            await self._broadcast(text)
        except Exception:
            log.exception("zone watch broadcast failed: %s", text)


def _choch_threshold(direction: Optional[str], valid_high: Optional[float],
                     valid_low: Optional[float]) -> Optional[str]:
    """The close that would be a CHoCH: against the current direction.

    Bullish, it is a close below the valid low; bearish, a close above
    the valid high. That level is always armed while its direction
    holds -- only a break on that side could disarm it, and that break
    is the CHoCH itself. With no direction yet there is no CHoCH to
    name: the first break either way is a BOS.
    """
    if direction == BULLISH and valid_low is not None:
        return "a close below %s" % _price(valid_low)
    if direction == BEARISH and valid_high is not None:
        return "a close above %s" % _price(valid_high)
    return None


def _where_choch(timeframe: str, state: Optional[WaveState]) -> str:
    if state is None:
        return ("%s structure is still warming up, so the CHoCH level "
                "is not known yet." % timeframe)
    threshold = _choch_threshold(state.direction, state.valid_high,
                                 state.valid_low)
    if threshold is None:
        return ("%s has no direction yet: the first break either way is "
                "a BOS, and the CHoCH comes after it." % timeframe)
    return "%s is %s: CHoCH on %s." % (timeframe, state.direction, threshold)


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
