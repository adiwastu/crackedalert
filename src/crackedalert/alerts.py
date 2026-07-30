"""Price alerts: SQLite-backed store + tick-driven crossing engine.

Replaces cracked_alerts.tsv and the 5-second polling checker. Alerts fire
off the live tick stream (bid/ask midpoint, the closest analog of the old
M1 close) and are deleted after firing, matching the bash behavior.
"""

import logging
import random
import sqlite3
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional, Set

log = logging.getLogger("crackedalert.alerts")

CROSSING_UP = "CROSSING_UP"
CROSSING_DOWN = "CROSSING_DOWN"
ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id         TEXT PRIMARY KEY,
    chat_id    INTEGER NOT NULL,
    symbol     TEXT NOT NULL,
    target     REAL NOT NULL,
    direction  TEXT NOT NULL,
    message    TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_symbol ON alerts(symbol);
CREATE INDEX IF NOT EXISTS idx_alerts_chat ON alerts(chat_id);
"""


@dataclass(frozen=True)
class Alert:
    id: str
    chat_id: int
    symbol: str
    target: float
    direction: str
    message: str


class AlertStore:
    def __init__(self, db_path: str):
        self._db = sqlite3.connect(db_path)
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def _gen_id(self) -> str:
        for _ in range(50):
            candidate = "".join(random.choices(ID_ALPHABET, k=4))
            row = self._db.execute(
                "SELECT 1 FROM alerts WHERE id = ?", (candidate,)).fetchone()
            if row is None:
                return candidate
        raise RuntimeError("could not generate a unique alert id")

    def create(self, chat_id: int, symbol: str, target: float,
               direction: str, message: str) -> Alert:
        alert = Alert(self._gen_id(), chat_id, symbol.upper(), target,
                      direction, message)
        self._db.execute(
            "INSERT INTO alerts VALUES (?, ?, ?, ?, ?, ?, ?)",
            (alert.id, alert.chat_id, alert.symbol, alert.target,
             alert.direction, alert.message, int(time.time())))
        self._db.commit()
        return alert

    def for_chat(self, chat_id: int) -> List[Alert]:
        rows = self._db.execute(
            "SELECT id, chat_id, symbol, target, direction, message "
            "FROM alerts WHERE chat_id = ? ORDER BY created_at",
            (chat_id,)).fetchall()
        return [Alert(*row) for row in rows]

    def for_symbol(self, symbol: str) -> List[Alert]:
        rows = self._db.execute(
            "SELECT id, chat_id, symbol, target, direction, message "
            "FROM alerts WHERE symbol = ?", (symbol.upper(),)).fetchall()
        return [Alert(*row) for row in rows]

    def active_symbols(self) -> Set[str]:
        rows = self._db.execute("SELECT DISTINCT symbol FROM alerts").fetchall()
        return {row[0] for row in rows}

    def cancel(self, alert_id: str, chat_id: int) -> bool:
        """Delete only if the alert belongs to this chat (bash parity)."""
        cur = self._db.execute(
            "DELETE FROM alerts WHERE id = ? AND chat_id = ?",
            (alert_id.upper(), chat_id))
        self._db.commit()
        return cur.rowcount > 0

    def delete(self, alert_id: str) -> None:
        self._db.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
        self._db.commit()

    # ------------------------------------------------------------------
    # legacy migration
    # ------------------------------------------------------------------
    def import_tsv(self, tsv_path: str) -> int:
        """One-shot import of the bash bot's TSV; renames it .imported."""
        import os
        if not os.path.exists(tsv_path):
            return 0
        imported = 0
        with open(tsv_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 6:
                    continue
                alert_id, chat_id, symbol, target, direction, message = parts[:6]
                if not alert_id or direction not in (CROSSING_UP, CROSSING_DOWN):
                    continue
                try:
                    self._db.execute(
                        "INSERT OR IGNORE INTO alerts VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (alert_id.upper(), int(chat_id), symbol.upper(),
                         float(target), direction, message, int(time.time())))
                    imported += 1
                except (ValueError, sqlite3.Error):
                    log.warning("skipping malformed TSV row: %r", line.strip())
        self._db.commit()
        os.replace(tsv_path, tsv_path + ".imported")
        log.info("imported %d legacy alerts from %s", imported, tsv_path)
        return imported


Notifier = Callable[[int, str], Awaitable[None]]  # (chat_id, text)
Formatter = Callable[[Alert], str]


class AlertEngine:
    """Consumes ticks, fires crossed alerts, deletes them."""

    def __init__(self, store: AlertStore, notify: Notifier,
                 format_fired: Formatter):
        self._store = store
        self._notify = notify
        self._format = format_fired

    async def on_tick(self, symbol: str, bid: float, ask: float) -> None:
        mid = (bid + ask) / 2.0
        for alert in self._store.for_symbol(symbol):
            crossed = (
                (alert.direction == CROSSING_UP and mid >= alert.target) or
                (alert.direction == CROSSING_DOWN and mid <= alert.target))
            if not crossed:
                continue
            log.info("alert %s fired: %s crossed %s (mid %.5f)",
                     alert.id, alert.symbol, alert.target, mid)
            try:
                await self._notify(alert.chat_id, self._format(alert))
            except Exception:
                log.exception("failed to notify chat %d for alert %s -- "
                              "keeping alert", alert.chat_id, alert.id)
                continue
            self._store.delete(alert.id)


def infer_direction(live_price: float, target: float) -> str:
    """Bash parity: live below target means we wait for an upward cross."""
    return CROSSING_UP if live_price < target else CROSSING_DOWN
