"""Persistence for the H1 imbalance gate's seen-marker.

The gate (fvg.ImbalanceGate) is in-memory: a restart forgets which
imbalance was last announced. Forgetting is the safe way to be wrong
about duplicates -- but the real danger is the missed boundary: when a
deploy or crash eats the hourly tick, the newest candle's imbalance is
never evaluated, and the next tick's window has already moved past it.
The watcher now catches up at startup, and this one-row marker makes
that catch-up duplicate-safe: an imbalance that was already announced
before the restart is skipped; one that was missed is announced.
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS imbalance_seen (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    newest_ts INTEGER NOT NULL
);
"""


class ImbalanceSeenStore:
    """The utcTimestampInMinutes of the last announced H1 imbalance."""

    def __init__(self, db_path: str):
        self._db = sqlite3.connect(db_path)
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def last_seen(self):
        row = self._db.execute(
            "SELECT newest_ts FROM imbalance_seen WHERE id = 1").fetchone()
        return row[0] if row else None

    def save(self, newest_ts: int) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO imbalance_seen (id, newest_ts) "
            "VALUES (1, ?)", (int(newest_ts),))
        self._db.commit()
