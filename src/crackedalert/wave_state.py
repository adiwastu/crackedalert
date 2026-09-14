"""Persistence and warm-up for the market structure engine.

wave.py is pure: it folds one closed bar at a time and knows nothing
about where bars come from or how state survives a restart. This module
is the other half. It keeps one engine state per (symbol, timeframe) in
SQLite, rebuilds state that is missing or stale by replaying history,
and folds live bars in as they close.

State is path dependent and cannot be computed backwards, so every
rebuild replays from the oldest bar of its window.
"""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from .wave import (BOS, CHOCH, Bar, WaveEvent, WaveState, apply_bar,
                   new_state, replay)

log = logging.getLogger("crackedalert.wave")

# Warm-up replays one wide window. It is reconstructing what the engine
# would hold had it never stopped, and more history reproduces that more
# closely: a wider window corrects breaks a narrow one got wrong rather
# than corrupting ones it had right. Against a continuous run, a 100-bar
# window matched direction ~91% of the time and 1000 bars ~100%.
WARMUP_BARS = 1000

# Minutes per timeframe, for the staleness check. MN1 is absent on
# purpose: months are not a fixed number of minutes, so state on that
# timeframe can never be resumed and is always rebuilt.
PERIOD_MINUTES = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 60, "H4": 240, "D1": 1440, "W1": 10080,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS wave_state (
    symbol      TEXT NOT NULL,
    timeframe   TEXT NOT NULL,
    direction   TEXT,
    up_top      REAL,
    up_line     REAL,
    down_bottom REAL,
    down_line   REAL,
    valid_high  REAL,
    valid_low   REAL,
    window_low  REAL,
    window_high REAL,
    last_ts     INTEGER,
    PRIMARY KEY (symbol, timeframe)
);
"""

# Column order matches WaveState's fields by name, so a row round-trips
# through the dataclass without a hand-written mapping.
_COLUMNS = ("symbol", "timeframe", "direction", "up_top", "up_line",
            "down_bottom", "down_line", "valid_high", "valid_low",
            "window_low", "window_high", "last_ts")


class WaveStateStore:
    """One row per (symbol, timeframe).

    Every engine field is stored, not just the headline levels. The
    state is path dependent, so a partial row could not be resumed.
    """

    def __init__(self, db_path: str):
        self._db = sqlite3.connect(db_path)
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def load(self, symbol: str, timeframe: str) -> Optional[WaveState]:
        row = self._db.execute(
            "SELECT %s FROM wave_state WHERE symbol = ? AND timeframe = ?"
            % ", ".join(_COLUMNS),
            (symbol.upper(), timeframe.upper())).fetchone()
        if row is None:
            return None
        return WaveState(**dict(zip(_COLUMNS, row)))

    def save(self, state: WaveState) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO wave_state (%s) VALUES (%s)"
            % (", ".join(_COLUMNS), ", ".join("?" * len(_COLUMNS))),
            tuple(getattr(state, column) for column in _COLUMNS))
        self._db.commit()

    def delete(self, symbol: str, timeframe: str) -> None:
        self._db.execute(
            "DELETE FROM wave_state WHERE symbol = ? AND timeframe = ?",
            (symbol.upper(), timeframe.upper()))
        self._db.commit()


def bar_close_time(timeframe: str, ts: int,
                   utc_offset: float = 0.0) -> Optional[str]:
    """When the bar closed, at the given offset from UTC.

    ts is the bar's OPEN: the proto defines utcTimestampInMinutes as the
    timestamp of the open tick. The close is one period later, which on
    H4 is four hours -- a long way to misread a log line by, and the
    close is the moment a break actually happened.

    None for a timeframe whose period is not a fixed number of minutes.
    """
    period = PERIOD_MINUTES.get(timeframe.upper())
    if period is None:
        return None
    closed = datetime.fromtimestamp(
        (ts + period) * 60, tz=timezone(timedelta(hours=utc_offset)))
    return "%s %s" % (closed.strftime("%Y-%m-%d %H:%M"),
                      _offset_label(utc_offset))


def _offset_label(utc_offset: float) -> str:
    if not utc_offset:
        return "UTC"
    return "UTC%s%g" % ("+" if utc_offset > 0 else "-", abs(utc_offset))


def resumable(state: WaveState, next_ts: int) -> bool:
    """True only when next_ts is the bar immediately after the stored one.

    Staleness is strict. Any other gap means bars were missed, and state
    cannot be computed backwards, so the only correct move is to rebuild.
    Rescanning is cheap; wrong state is not.
    """
    period = PERIOD_MINUTES.get(state.timeframe.upper())
    if period is None or state.last_ts is None:
        return False
    return state.last_ts + period == next_ts


Fetch = Callable[[str, str, int], Awaitable[List[Bar]]]
EventHandler = Callable[[WaveEvent], Awaitable[None]]


class WaveService:
    """Drives one engine state per (symbol, timeframe), persisted.

    On the first bar for a key it loads the stored state, resumes it if
    that bar follows it exactly, and otherwise rebuilds by warm-up.
    Every folded bar is written back, so a restart resumes rather than
    rescans whenever the process comes back within one bar.

    Warm-up events are not emitted. Replaying history reconstructs where
    structure already stands; only live bars announce a break.
    """

    def __init__(self, store: WaveStateStore, fetch: Fetch,
                 on_event: Optional[EventHandler] = None,
                 warmup_bars: int = WARMUP_BARS,
                 utc_offset: float = 0.0):
        self._store = store
        self._fetch = fetch
        self._on_event = on_event
        self._warmup_bars = warmup_bars
        self._utc_offset = utc_offset
        self._states: Dict[Tuple[str, str], WaveState] = {}

    def state(self, symbol: str, timeframe: str) -> Optional[WaveState]:
        return self._states.get((symbol.upper(), timeframe.upper()))

    async def on_closed_bar(self, symbol: str, timeframe: str,
                            bar: Bar) -> Tuple[WaveEvent, ...]:
        key = (symbol.upper(), timeframe.upper())
        state = self._states.get(key)
        if state is None:
            state = await self._prime(key[0], key[1], bar.ts)

        events: Tuple[WaveEvent, ...] = ()
        # A warm-up window ends at the newest completed bar, which may
        # already be this one.
        if state.last_ts is None or state.last_ts < bar.ts:
            state, events = apply_bar(state, bar)

        self._states[key] = state
        self._store.save(state)
        for event in events:
            await self._emit(event)
        return events

    async def _prime(self, symbol: str, timeframe: str,
                     next_ts: int) -> WaveState:
        stored = self._store.load(symbol, timeframe)
        if stored is not None and resumable(stored, next_ts):
            log.info("wave %s %s: resuming stored state at ts=%d",
                     symbol, timeframe, stored.last_ts)
            return stored
        if stored is not None:
            log.info("wave %s %s: stored state is stale (last_ts=%s, "
                     "next bar %d), rebuilding",
                     symbol, timeframe, stored.last_ts, next_ts)
        return await self._warm_up(symbol, timeframe)

    async def _warm_up(self, symbol: str, timeframe: str) -> WaveState:
        """Rebuild state by replaying one wide window from its oldest bar.

        State cannot be computed backwards, so this replays forwards
        from the start of the window. One window, not a widening scan:
        see WARMUP_BARS for why more history is more faithful, not less.

        The gateway truncates to its own chunk size rather than failing,
        so the bar count is logged -- a short window is a quiet loss of
        accuracy that an error would not announce.
        """
        bars = await self._fetch(symbol, timeframe, self._warmup_bars)
        if not bars:
            log.info("wave %s %s: warm-up found no history, staying cold",
                     symbol, timeframe)
            return new_state(symbol, timeframe)
        state, events = replay(bars, new_state(symbol, timeframe))
        self._log_warm_up(symbol, timeframe, len(bars), state, events)
        return state

    def _log_warm_up(self, symbol: str, timeframe: str, count: int,
                     state: WaveState,
                     events: Tuple[WaveEvent, ...]) -> None:
        """Report the state warm-up inherited, including the break that
        set the direction.

        Warm-up does not emit that break as an event -- it fired hours
        ago and announcing it would read as live -- but the operator
        still needs to know which one the engine is working from, since
        it decides BOS from CHoCH on everything that follows.
        """
        breaks = [event for event in events if event.kind in (BOS, CHOCH)]
        if not breaks:
            log.info("wave %s %s: warm-up replayed %d bars (asked %d), "
                     "no break in window, staying undirected",
                     symbol, timeframe, count, self._warmup_bars)
            return
        last = breaks[-1]
        log.info("wave %s %s: warm-up replayed %d bars (asked %d), "
                 "direction %s, last break %s %s at %.5f (%s)",
                 symbol, timeframe, count, self._warmup_bars,
                 state.direction, last.kind, last.direction, last.level,
                 bar_close_time(timeframe, last.ts, self._utc_offset)
                 or "ts=%d" % last.ts)

    async def _emit(self, event: WaveEvent) -> None:
        if self._on_event is None:
            return
        try:
            await self._on_event(event)
        except Exception:
            log.exception("wave event handler failed for %s %s",
                          event.kind, event.ts)
