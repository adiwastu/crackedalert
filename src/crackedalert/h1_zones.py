"""Live H1 imbalance zones, for lower-timeframe gaps to nest in.

An H1 zone is the gap an H1 FVG left behind. It stays live after DH1 or
S H1 is touched -- price coming back into the zone and printing an M15
gap there is the point, and that happens during the touch, not before.
It dies when its flip fires: the H1 close through the far side that the
FVG watcher arms as "strike 1 of FLIP".

A zone is alive exactly as long as that flip candle alert exists. There
is no separate death event to wire up and miss: the flip firing, or
someone cancelling it, deletes the alert, and the zone goes with it the
next time zones are read.
"""

import sqlite3
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from .fvg import (NESTED_ALERT_SPECS, ImbalanceGate, candle_high,
                  candle_low, gap_bounds, overlaps, zone_name)

SCHEMA = """
CREATE TABLE IF NOT EXISTS h1_zones (
    flip_alert_id TEXT PRIMARY KEY,
    symbol        TEXT NOT NULL,
    which         TEXT NOT NULL,
    bottom        REAL NOT NULL,
    top           REAL NOT NULL,
    formed_ts     INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class H1Zone:
    flip_alert_id: str
    symbol: str
    which: str          # bullish | bearish
    bottom: float
    top: float
    formed_ts: int      # UTC minutes the gap completed: close of candle 3

    @property
    def name(self) -> str:
        return zone_name(self.which, "H1")


class H1ZoneStore:

    def __init__(self, db_path: str):
        self._db = sqlite3.connect(db_path)
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def add(self, zone: H1Zone) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO h1_zones VALUES (?, ?, ?, ?, ?, ?)",
            (zone.flip_alert_id, zone.symbol.upper(), zone.which,
             zone.bottom, zone.top, zone.formed_ts))
        self._db.commit()

    def live(self, symbol: str,
             is_alive: Callable[[str], bool]) -> List[H1Zone]:
        """Zones on `symbol` whose flip alert still exists, newest first.
        Dead ones are deleted as they are found."""
        rows = self._db.execute(
            "SELECT flip_alert_id, symbol, which, bottom, top, formed_ts "
            "FROM h1_zones WHERE symbol = ? ORDER BY formed_ts DESC",
            (symbol.upper(),)).fetchall()
        zones, dead = [], []
        for row in rows:
            zone = H1Zone(*row)
            (zones if is_alive(zone.flip_alert_id) else dead).append(zone)
        if dead:
            self._db.executemany(
                "DELETE FROM h1_zones WHERE flip_alert_id = ?",
                [(zone.flip_alert_id,) for zone in dead])
            self._db.commit()
        return zones


@dataclass(frozen=True)
class NestedEntry:
    """What an M15 gap nested in an H1 zone should arm."""
    zone: H1Zone
    bottom: float           # the M15 gap
    top: float
    level: float            # entry alert level: its candle 1
    direction: str          # alert direction
    note: str


def nested_entry(bars: List[dict], which: str,
                 zones: List[H1Zone]) -> Optional[NestedEntry]:
    """The entry alert for a fresh M15 imbalance, if it nests in a zone.

    `bars` must end with the triplet that completed the imbalance and
    `which` is its direction, as fresh_imbalance returned it. None when
    it nests in nothing.
    """
    c1 = bars[-3]
    bottom, top = gap_bounds(bars, which)
    zone = nesting_zone(zones, which, bottom, top,
                        int(c1.get("utcTimestampInMinutes", 0) or 0))
    if zone is None:
        return None
    level_key, direction, note = NESTED_ALERT_SPECS[which]
    level = candle_high(c1) if level_key == "high1" else candle_low(c1)
    return NestedEntry(zone, bottom, top, level, direction, note)


class NestedGate:
    """Which fresh M15 imbalances arm an entry alert.

    Nesting is checked before the back-to-back gate, not after. A gap
    that forms just above the zone arms nothing, so the next candle's gap
    landing in the zone duplicates nothing -- gating first would record
    the outside gap as seen and then skip the only one that matters.
    """

    OUTSIDE = "outside"             # nests in no live zone
    CONTINUATION = "continuation"   # continues a nested gap already armed
    ARMED = "armed"

    def __init__(self, period: int = 15) -> None:
        self._gate = ImbalanceGate(period)

    @property
    def last_seen(self) -> Optional[int]:
        return self._gate.last_seen

    def evaluate(self, bars: List[dict], which: str, zones: List[H1Zone]
                 ) -> Tuple[str, Optional[NestedEntry]]:
        entry = nested_entry(bars, which, zones)
        if entry is None:
            return self.OUTSIDE, None
        newest_ts = int(bars[-1].get("utcTimestampInMinutes", 0) or 0)
        if not self._gate.admit(newest_ts):
            return self.CONTINUATION, entry
        return self.ARMED, entry


def nesting_zone(zones: List[H1Zone], which: str, bottom: float,
                 top: float, first_bar_ts: int) -> Optional[H1Zone]:
    """The H1 zone a lower-timeframe gap nests in, or None.

    It must point the same way, overlap the zone's range at all, and
    have started after the zone formed. That last rule matters: the M15
    candles of the H1 impulse always overlap the gap they themselves
    created, so without it every H1 gap would immediately nest an M15
    one from its own move. `zones` is newest first; the first match wins.
    """
    for zone in zones:
        if (zone.which == which and zone.formed_ts <= first_bar_ts
                and overlaps(bottom, top, zone.bottom, zone.top)):
            return zone
    return None
