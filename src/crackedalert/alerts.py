"""Price alerts: SQLite-backed store + tick-driven crossing engine.

Replaces cracked_alerts.tsv and the 5-second polling checker. Alerts fire
off the live tick stream (bid/ask midpoint) and are deleted after firing.

Manual /alert alerts notify the owning chat; --all alerts broadcast to
every subscriber.

The former trade auto-alert chain (entry/tp/sl + reconcile-based position
confirmation) was REMOVED in v2.0.31: it produced only Telegram
notifications while its per-tick reconcile retries starved the recv loop
and deadlocked request correlation on the feed connection (HANDOFF.md 2.x).
Broker-side SL/TP never depended on it -- results are visible in the
cTrader app. Legacy entry/tp/sl rows are purged at startup.
"""

import logging
import random
import sqlite3
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional, Set, Tuple

log = logging.getLogger("crackedalert.alerts")

CROSSING_UP = "CROSSING_UP"
CROSSING_DOWN = "CROSSING_DOWN"
ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

# alert kinds (legacy 'entry'/'tp'/'sl' rows are purged at startup)
KIND_MANUAL = "manual"

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id         TEXT PRIMARY KEY,
    chat_id    INTEGER NOT NULL,
    symbol     TEXT NOT NULL,
    target     REAL NOT NULL,
    direction  TEXT NOT NULL,
    message    TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'manual',
    trade_id   TEXT NOT NULL DEFAULT '',
    account    TEXT NOT NULL DEFAULT '',
    cc_timeframe TEXT NOT NULL DEFAULT '',
    cc_price     REAL NOT NULL DEFAULT 0,
    broadcast    INTEGER NOT NULL DEFAULT 0
);
"""

# Created AFTER _migrate() so legacy DBs (no kind/trade_id yet) don't fail.
SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_alerts_symbol ON alerts(symbol);
CREATE INDEX IF NOT EXISTS idx_alerts_chat ON alerts(chat_id);
CREATE INDEX IF NOT EXISTS idx_alerts_trade ON alerts(trade_id, kind);
"""


@dataclass(frozen=True)
class Alert:
    id: str
    chat_id: int
    symbol: str
    target: float
    direction: str
    message: str
    kind: str = KIND_MANUAL
    trade_id: str = ""
    account: str = ""
    cc_timeframe: str = ""
    cc_price: float = 0.0
    broadcast: bool = False


class AlertStore:
    def __init__(self, db_path: str):
        self._db = sqlite3.connect(db_path)
        self._db.executescript(SCHEMA)
        self._migrate()
        self._db.executescript(SCHEMA_INDEXES)
        self._db.commit()

    def _migrate(self) -> None:
        """Add newer columns to pre-existing DBs; purge legacy auto rows."""
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(alerts)")}
        for col, ddl in (("kind", "TEXT NOT NULL DEFAULT 'manual'"),
                         ("trade_id", "TEXT NOT NULL DEFAULT ''"),
                         ("account", "TEXT NOT NULL DEFAULT ''"),
                         ("cc_timeframe", "TEXT NOT NULL DEFAULT ''"),
                         ("cc_price", "REAL NOT NULL DEFAULT 0"),
                         ("broadcast", "INTEGER NOT NULL DEFAULT 0")):
            if col not in cols:
                self._db.execute(
                    "ALTER TABLE alerts ADD COLUMN %s %s" % (col, ddl))
        # v2.0.31: the trade auto-alert chain is gone -- drop any legacy
        # entry/tp/sl rows so they can never fire again.
        cur = self._db.execute("DELETE FROM alerts WHERE kind != ?",
                               (KIND_MANUAL,))
        if cur.rowcount:
            log.info("purged %d legacy auto trade alert(s)", cur.rowcount)

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
               direction: str, message: str,
               kind: str = KIND_MANUAL, trade_id: str = "",
               account: str = "",
               cc_timeframe: str = "", cc_price: float = 0.0,
               broadcast: bool = False) -> Alert:
        alert = Alert(self._gen_id(), chat_id, symbol.upper(), target,
                      direction, message, kind, trade_id, account,
                      cc_timeframe, cc_price, broadcast)
        self._db.execute(
            "INSERT INTO alerts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (alert.id, alert.chat_id, alert.symbol, alert.target,
             alert.direction, alert.message, int(time.time()),
             alert.kind, alert.trade_id, alert.account,
             alert.cc_timeframe, alert.cc_price, int(alert.broadcast)))
        self._db.commit()
        return alert

    def for_chat(self, chat_id: int) -> List[Alert]:
        rows = self._db.execute(
            "SELECT id, chat_id, symbol, target, direction, message, "
            "kind, trade_id, account, cc_timeframe, cc_price, broadcast "
            "FROM alerts WHERE chat_id = ? ORDER BY created_at",
            (chat_id,)).fetchall()
        return [Alert(*row) for row in rows]

    def for_symbol(self, symbol: str) -> List[Alert]:
        rows = self._db.execute(
            "SELECT id, chat_id, symbol, target, direction, message, "
            "kind, trade_id, account, cc_timeframe, cc_price, broadcast "
            "FROM alerts WHERE symbol = ?",
            (symbol.upper(),)).fetchall()
        return [Alert(*row) for row in rows]

    def active_symbols(self) -> Set[str]:
        rows = self._db.execute("SELECT DISTINCT symbol FROM alerts").fetchall()
        return {row[0] for row in rows}

    def cancel(self, alert_id: str, chat_id: int) -> bool:
        """Delete ANY alert owned by this chat (manual or auto entry/tp/sl).

        Auto trade alerts used to be system-managed here, which stranded
        stale entry alerts (e.g. an unfilled pending order) forever: the
        only other removal paths (/close, /cancel_order) require a
        successful broker call first. Auto alerts are notification/guard
        chains only -- the real SL/TP live broker-side -- so the owning
        chat may remove them with /cancel.
        """
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
                        "INSERT OR IGNORE INTO alerts "
                        "(id, chat_id, symbol, target, direction, message, "
                        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
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
Broadcaster = Callable[[str], Awaitable[None]]    # (text)


class AlertEngine:
    """Consumes ticks, fires crossed alerts, deletes them.

    Manual alerts notify the owning chat; --all (broadcast) alerts go to
    every subscriber.
    """

    def __init__(self, store: AlertStore, notify: Notifier,
                 format_fired: Formatter,
                 on_broadcast: Optional[Broadcaster] = None):
        self._store = store
        self._notify = notify
        self._format = format_fired
        self._on_broadcast = on_broadcast

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
            if alert.broadcast:
                await self._fire_broadcast(alert)
            else:  # KIND_MANUAL
                await self._fire_manual(alert)

    async def _fire_manual(self, alert: Alert) -> None:
        try:
            await self._notify(alert.chat_id, self._format(alert))
        except Exception:
            log.exception("failed to notify chat %d for alert %s -- "
                          "keeping alert", alert.chat_id, alert.id)
            return
        self._store.delete(alert.id)

    async def _fire_broadcast(self, alert: Alert) -> None:
        try:
            await self._broadcast(alert)
        except Exception:
            log.exception("broadcast failed for alert %s -- keeping alert",
                          alert.id)
            return
        self._store.delete(alert.id)

    async def _broadcast(self, alert: Alert) -> None:
        if self._on_broadcast is None:
            return
        try:
            await self._on_broadcast(self._format(alert))
        except Exception:
            log.exception("broadcast failed for alert %s -- dropping "
                          "notification", alert.id)


def infer_direction(live_price: float, target: float) -> str:
    """Bash parity: live below target means we wait for an upward cross."""
    return CROSSING_UP if live_price < target else CROSSING_DOWN


# ----------------------------------------------------------------------
# candle-close alerts (separate store + engine)
# ----------------------------------------------------------------------

CANDLE_ABOVE = "ABOVE"
CANDLE_BELOW = "BELOW"

CANDLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS candle_alerts (
    id         TEXT PRIMARY KEY,
    chat_id    INTEGER NOT NULL,
    symbol     TEXT NOT NULL,
    timeframe  TEXT NOT NULL,
    target     REAL NOT NULL,
    direction  TEXT NOT NULL,
    message    TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    action      TEXT NOT NULL DEFAULT 'notify',
    position_id INTEGER NOT NULL DEFAULT 0,
    account     TEXT NOT NULL DEFAULT '',
    broadcast   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_candle_alerts_key
    ON candle_alerts(symbol, timeframe);
"""

CANDLE_SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_candle_alerts_position
    ON candle_alerts(position_id);
"""


@dataclass(frozen=True)
class CandleAlert:
    id: str
    chat_id: int
    symbol: str
    timeframe: str
    target: float
    direction: str
    message: str
    action: str = "notify"     # "notify" | "close"
    position_id: int = 0
    account: str = ""
    broadcast: bool = False


class CandleAlertStore:
    def __init__(self, db_path: str):
        self._db = sqlite3.connect(db_path)
        self._db.executescript(CANDLE_SCHEMA)
        self._migrate()
        self._db.executescript(CANDLE_SCHEMA_INDEXES)
        self._db.commit()

    def _migrate(self) -> None:
        """Add newer columns to pre-existing DBs (idempotent)."""
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(candle_alerts)")}
        for col, ddl in (
            ("action",      "TEXT NOT NULL DEFAULT 'notify'"),
            ("position_id", "INTEGER NOT NULL DEFAULT 0"),
            ("account",     "TEXT NOT NULL DEFAULT ''"),
            ("broadcast",   "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in cols:
                self._db.execute(
                    "ALTER TABLE candle_alerts ADD COLUMN %s %s" % (col, ddl))

    def close(self) -> None:
        self._db.close()

    def _gen_id(self) -> str:
        for _ in range(50):
            candidate = "".join(random.choices(ID_ALPHABET, k=4))
            row = self._db.execute(
                "SELECT 1 FROM candle_alerts WHERE id = ?",
                (candidate,)).fetchone()
            if row is None:
                return candidate
        raise RuntimeError("could not generate a unique candle alert id")

    def create(self, chat_id: int, symbol: str, timeframe: str,
               target: float, direction: str, message: str,
               action: str = "notify", position_id: int = 0,
               account: str = "", broadcast: bool = False) -> CandleAlert:
        alert = CandleAlert(self._gen_id(), chat_id, symbol.upper(),
                            timeframe.upper(), target, direction, message,
                            action, position_id, account, broadcast)
        self._db.execute(
            "INSERT INTO candle_alerts "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (alert.id, alert.chat_id, alert.symbol, alert.timeframe,
             alert.target, alert.direction, alert.message,
             int(time.time()), alert.action, alert.position_id,
             alert.account, int(alert.broadcast)))
        self._db.commit()
        return alert

    def for_chat(self, chat_id: int) -> List[CandleAlert]:
        rows = self._db.execute(
            "SELECT id, chat_id, symbol, timeframe, target, direction, "
            "message, action, position_id, account, broadcast "
            "FROM candle_alerts WHERE chat_id = ? ORDER BY created_at",
            (chat_id,)).fetchall()
        return [CandleAlert(*row) for row in rows]

    def for_key(self, symbol: str, timeframe: str) -> List[CandleAlert]:
        rows = self._db.execute(
            "SELECT id, chat_id, symbol, timeframe, target, direction, "
            "message, action, position_id, account, broadcast "
            "FROM candle_alerts WHERE symbol = ? AND timeframe = ?",
            (symbol.upper(), timeframe.upper())).fetchall()
        return [CandleAlert(*row) for row in rows]

    def active_keys(self) -> Set[Tuple[str, str]]:
        rows = self._db.execute(
            "SELECT DISTINCT symbol, timeframe FROM candle_alerts").fetchall()
        return {(r[0], r[1]) for r in rows}

    def cancel(self, alert_id: str, chat_id: int) -> bool:
        cur = self._db.execute(
            "DELETE FROM candle_alerts WHERE id = ? AND chat_id = ?",
            (alert_id.upper(), chat_id))
        self._db.commit()
        return cur.rowcount > 0

    def delete(self, alert_id: str) -> None:
        self._db.execute(
            "DELETE FROM candle_alerts WHERE id = ?", (alert_id,))
        self._db.commit()

    def cancel_for_position(self, position_id: int) -> int:
        """Delete all CC guard alerts linked to a position. Returns count."""
        cur = self._db.execute(
            "DELETE FROM candle_alerts WHERE position_id = ? AND action = 'close'",
            (position_id,))
        self._db.commit()
        return cur.rowcount


class CandleAlertEngine:
    """Consumes closed-bar events, fires crossed candle alerts, deletes them.

    If the alert has action='close', invokes the on_close_hit callback instead
    of a notify. If on_close_hit raises, the alert is kept for retry.
    """

    def __init__(self, store: CandleAlertStore, notify: Notifier,
                 format_fired: Formatter,
                 on_close_hit: Optional[Callable] = None,
                 on_broadcast: Optional[Broadcaster] = None,
                 on_alert_removed: Optional[Callable[[], None]] = None):
        self._store = store
        self._notify = notify
        self._format = format_fired
        self._on_close_hit = on_close_hit
        self._on_broadcast = on_broadcast
        self._on_alert_removed = on_alert_removed

    def _removed(self) -> None:
        if self._on_alert_removed is not None:
            try:
                self._on_alert_removed()
            except Exception:
                log.exception("on_alert_removed callback failed")

    async def on_closed_bar(self, symbol: str, timeframe: str,
                            close: float, ts_minutes: int) -> None:
        for alert in self._store.for_key(symbol, timeframe):
            crossed = (
                (alert.direction == CANDLE_ABOVE and close >= alert.target) or
                (alert.direction == CANDLE_BELOW and close <= alert.target))
            if not crossed:
                continue
            log.info("candle alert %s fired: %s %s closed %.5f vs %s",
                     alert.id, alert.symbol, alert.timeframe, close,
                     alert.target)

            if alert.action == "close" and self._on_close_hit is not None:
                try:
                    await self._on_close_hit(alert)
                except Exception:
                    log.exception("cc guard close failed for %s -- keeping",
                                  alert.id)
                    continue
                self._store.delete(alert.id)
                self._removed()
                continue

            if alert.broadcast:
                try:
                    await self._broadcast(alert)
                except Exception:
                    log.exception("broadcast failed for candle alert %s -- "
                                  "keeping alert", alert.id)
                    continue
                self._store.delete(alert.id)
                self._removed()
                continue

            # existing notify path
            try:
                await self._notify(alert.chat_id, self._format(alert))
            except Exception:
                log.exception("failed to notify chat %d for candle alert %s "
                              "-- keeping alert", alert.chat_id, alert.id)
                continue
            self._store.delete(alert.id)
            self._removed()

    async def _broadcast(self, alert: CandleAlert) -> None:
        if self._on_broadcast is None:
            return
        await self._on_broadcast(self._format(alert))
