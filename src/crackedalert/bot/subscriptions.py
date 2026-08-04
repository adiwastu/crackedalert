"""Dynamic chat allow-list backed by SQLite.

At startup the static ALLOWED_CHAT_IDS from the environment are seeded
into the table.  /subscribe and /unsubscribe add/remove chats at runtime
so the owner never has to SSH in to grant access.
"""

import logging
import sqlite3
from typing import List, Set

log = logging.getLogger("crackedalert.subscriptions")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS allowed_chats (
    chat_id INTEGER PRIMARY KEY
);
"""


class SubscriptionStore:
    def __init__(self, db_path: str):
        self._db = sqlite3.connect(db_path)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # ------------------------------------------------------------------
    # seed
    # ------------------------------------------------------------------
    def seed(self, chat_ids: List[int]) -> None:
        """Insert the static env list once at startup. Idempotent."""
        with self._db:
            for cid in chat_ids:
                self._db.execute(
                    "INSERT OR IGNORE INTO allowed_chats VALUES (?)",
                    (int(cid),))

    # ------------------------------------------------------------------
    # add / remove
    # ------------------------------------------------------------------
    def add(self, chat_id: int) -> bool:
        """Return True if this is new, False if already present."""
        cur = self._db.execute(
            "INSERT OR IGNORE INTO allowed_chats VALUES (?)",
            (int(chat_id),))
        self._db.commit()
        return cur.rowcount > 0

    def remove(self, chat_id: int) -> bool:
        """Return True if a row was deleted, False if it didn't exist."""
        cur = self._db.execute(
            "DELETE FROM allowed_chats WHERE chat_id = ?",
            (int(chat_id),))
        self._db.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # lookup
    # ------------------------------------------------------------------
    def is_allowed(self, chat_id: int) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM allowed_chats WHERE chat_id = ?",
            (int(chat_id),)).fetchone()
        return row is not None

    def all_ids(self) -> Set[int]:
        rows = self._db.execute("SELECT chat_id FROM allowed_chats").fetchall()
        return {r[0] for r in rows}