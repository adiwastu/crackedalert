"""Quiet hours for Telegram delivery and the overnight digest.

Between 22:00 and 05:00 on the operator's display clock, alert
messages are HELD instead of sent; when the window ends they go out as
one chronological digest. Urgent messages (the token-dead alarm, the
one thing that needs the operator even asleep) bypass the hold -- both
here and, via the push `urgent` flag, in the phone app's own quiet
window.

The phone app has its own 22:00-05:00 ring suppression; this module
only governs Telegram delivery. A held message is not lost on restart:
holds live in SQLite and the digest flushes on the next window end (or
immediately at startup if the window already passed).
"""

import sqlite3
import time
from typing import List, Tuple

QUIET_FROM_HOUR = 22
QUIET_TO_HOUR = 5

# Telegram's hard message limit is 4096 chars; stay well under it so a
# header + bullets always fit.
DIGEST_CHUNK_CHARS = 3500


def is_quiet_now(now_utc: float, utc_offset: float = 0.0) -> bool:
    """True between 22:00 (inclusive) and 05:00 (exclusive) local."""
    local_hour = int(((now_utc + utc_offset * 3600.0) % 86400) // 3600)
    return local_hour >= QUIET_FROM_HOUR or local_hour < QUIET_TO_HOUR


def seconds_until_window_end(now_utc: float, utc_offset: float = 0.0) -> float:
    """Seconds from now until the next 05:00 local."""
    off = utc_offset * 3600.0
    local = now_utc + off
    target = (local // 86400) * 86400 + QUIET_TO_HOUR * 3600
    if target <= local:
        target += 86400
    return target - local


SCHEMA = """
CREATE TABLE IF NOT EXISTS held_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    text       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""


class HeldMessageStore:
    """Messages held during quiet hours, in SQLite (survives restarts)."""

    def __init__(self, db_path: str):
        self._db = sqlite3.connect(db_path)
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def add(self, chat_id: int, text: str, created_at: int) -> None:
        self._db.execute(
            "INSERT INTO held_messages (chat_id, text, created_at) "
            "VALUES (?, ?, ?)",
            (int(chat_id), text, int(created_at)))
        self._db.commit()

    def all(self) -> List[Tuple[int, int, str]]:
        """(chat_id, created_at, text), oldest first."""
        return self._db.execute(
            "SELECT chat_id, created_at, text FROM held_messages "
            "ORDER BY created_at, id").fetchall()

    def clear(self) -> None:
        self._db.execute("DELETE FROM held_messages")
        self._db.commit()


def build_digest(rows: List[Tuple[int, int, str]],
                 utc_offset: float = 0.0) -> List[Tuple[int, str]]:
    """Render held rows into per-chat digest messages.

    rows: (chat_id, created_at, text), oldest first. Returns
    (chat_id, text) pairs; each text starts with a count header and
    carries `-- HH:MM --` time separators, chunked under Telegram's
    size limit. The texts are already HTML fragments (escaped at render
    time), so the digest is sent with parse_mode HTML.
    """
    out: List[Tuple[int, str]] = []
    by_chat = {}
    for chat_id, created_at, text in rows:
        by_chat.setdefault(chat_id, []).append((created_at, text))
    for chat_id, items in by_chat.items():
        header = ("\U0001F319 quiet-hours digest \u2014 %d held overnight"
                  % len(items))
        chunks: List[str] = []
        current = header
        first_in_chunk = True
        for created_at, text in items:
            stamp = time.strftime(
                "%H:%M", time.gmtime(created_at + utc_offset * 3600.0))
            block = "\n\n-- %s --\n%s" % (stamp, text)
            if len(current) + len(block) > DIGEST_CHUNK_CHARS \
                    and current != header:
                chunks.append(current)
                current = header + " (continued)"
                first_in_chunk = True
            current += block
            first_in_chunk = False
        chunks.append(current)
        for chunk in chunks:
            out.append((chat_id, chunk))
    return out
