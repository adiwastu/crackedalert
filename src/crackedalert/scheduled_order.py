"""One-shot scheduled pending orders: fire a /p at a set time if price
sits inside an absolute band.

The operator arms a rule from chat (see /later in bot/handlers.py): a
time of day, two absolute band prices, and a pending-order spec in
exactly the /p syntax. At the next occurrence of that time the current
bid is checked against the band: inside, the order is placed exactly as
if the operator had typed /p themselves; outside, the skip is reported.
Either way the rule is consumed -- it never places twice on its own. A
rule survives restarts (SQLite) and is removed only by its fire, a
failed-fire give-up, or the operator's /later off.

Downtime handling: if the bot comes back up within GRACE_SECONDS of the
fire time the check runs late at the then-current price; past that the
rule re-arms for the next occurrence and says so. A fire that errors
(link down, balance fetch) retries up to MAX_ATTEMPTS total evaluations
RETRY_DELAY_SECONDS apart, re-checking the band each time -- so a retry
that finds price has left the band skips like any other.
"""

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from .bot import formatting as fmt

log = logging.getLogger("crackedalert.scheduled")

GRACE_SECONDS = 900          # fire late after downtime within 15 minutes
RETRY_DELAY_SECONDS = 600    # next try 10 minutes after a failed fire
MAX_ATTEMPTS = 3             # total band-passed fire evaluations

STATUS_ARMED = "armed"
STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_orders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    account    TEXT NOT NULL,
    fire_time  TEXT NOT NULL,
    band_low   REAL NOT NULL,
    band_high  REAL NOT NULL,
    spec       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'armed',
    next_fire  INTEGER NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    settled_at INTEGER
);
"""

_COLS = ("id, chat_id, account, fire_time, band_low, band_high, spec, "
         "status, next_fire, attempts, created_at, settled_at")


@dataclass(frozen=True)
class ScheduledRule:
    id: int
    chat_id: int
    account: str
    fire_time: str      # "HH:MM" in the operator's display clock
    band_low: float
    band_high: float
    spec: str           # pending-order args in /p syntax (after the 'p')
    status: str
    next_fire: int      # unix UTC the band is next checked
    attempts: int
    created_at: int
    settled_at: Optional[int] = None


def parse_fire_time(text: str) -> Optional[str]:
    """'6:00' -> '06:00'; None when it is not a valid 24h HH:MM."""
    parts = text.split(":")
    if len(parts) != 2 or not parts[0].isdigit() \
            or len(parts[1]) != 2 or not parts[1].isdigit():
        return None
    hour, minute = int(parts[0]), int(parts[1])
    if hour > 23 or minute > 59:
        return None
    return "%02d:%02d" % (hour, minute)


def next_occurrence(now_utc: float, fire_time: str,
                    utc_offset: float) -> int:
    """Unix timestamp of the next HH:MM on the operator's display clock.

    Strictly more than 30s from now, so arming a rule at exactly its own
    fire time aims it at tomorrow, while arming it the evening before
    catches the morning. utc_offset is hours from UTC
    (settings.display_utc_offset); the bot itself only ever works in UTC.
    """
    hour, minute = (int(part) for part in fire_time.split(":"))
    local_now = now_utc + utc_offset * 3600.0
    local_midnight = int(local_now // 86400) * 86400
    target = local_midnight + hour * 3600 + minute * 60
    if target <= local_now + 30:
        target += 86400
    return int(target - utc_offset * 3600.0)


def in_band(price: float, band_low: float, band_high: float) -> bool:
    """Inclusive on both ends: 'not higher nor lower than the band'."""
    return band_low <= price <= band_high


def _rule(row) -> ScheduledRule:
    return ScheduledRule(*row)


class ScheduledOrderStore:
    """Rules in SQLite, so a restart never forgets an armed order."""

    def __init__(self, db_path: str):
        self._db = sqlite3.connect(db_path)
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def arm(self, chat_id: int, account: str, fire_time: str,
            band_low: float, band_high: float, spec: str,
            next_fire: int, created_at: int) -> ScheduledRule:
        cur = self._db.execute(
            "INSERT INTO scheduled_orders (chat_id, account, fire_time, "
            "band_low, band_high, spec, status, next_fire, attempts, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (int(chat_id), account, fire_time, float(band_low),
             float(band_high), spec, STATUS_ARMED, int(next_fire),
             int(created_at)))
        self._db.commit()
        return self.get(cur.lastrowid)

    def get(self, rule_id: int) -> Optional[ScheduledRule]:
        row = self._db.execute(
            "SELECT %s FROM scheduled_orders WHERE id = ?" % _COLS,
            (int(rule_id),)).fetchone()
        return _rule(row) if row else None

    def armed(self) -> List[ScheduledRule]:
        rows = self._db.execute(
            "SELECT %s FROM scheduled_orders WHERE status = ? "
            "ORDER BY next_fire" % _COLS, (STATUS_ARMED,)).fetchall()
        return [_rule(row) for row in rows]

    def recent(self, limit: int = 10) -> List[ScheduledRule]:
        """Armed rules first, then the newest settled ones (audit trail)."""
        rows = self._db.execute(
            "SELECT %s FROM scheduled_orders ORDER BY "
            "CASE WHEN status = 'armed' THEN 0 ELSE 1 END, id DESC "
            "LIMIT ?" % _COLS, (int(limit),)).fetchall()
        return [_rule(row) for row in rows]

    def reschedule(self, rule_id: int, next_fire: int,
                   attempts: int) -> None:
        self._db.execute(
            "UPDATE scheduled_orders SET next_fire = ?, attempts = ? "
            "WHERE id = ?",
            (int(next_fire), int(attempts), int(rule_id)))
        self._db.commit()

    def settle(self, rule_id: int, status: str) -> None:
        self._db.execute(
            "UPDATE scheduled_orders SET status = ?, settled_at = ? "
            "WHERE id = ?",
            (status, int(time.time()), int(rule_id)))
        self._db.commit()

    def cancel(self, rule_id: int, chat_id: int) -> bool:
        """Cancel an armed rule owned by the chat. True on success."""
        cur = self._db.execute(
            "UPDATE scheduled_orders SET status = ?, settled_at = ? "
            "WHERE id = ? AND chat_id = ? AND status = ?",
            (STATUS_CANCELLED, int(time.time()), int(rule_id),
             int(chat_id), STATUS_ARMED))
        self._db.commit()
        return cur.rowcount > 0


class ScheduledOrderRunner:
    """Checks armed rules against the clock and runs their fire path.

    Polling (rather than sleeping to the next deadline) so rules that
    are armed, cancelled or re-armed mid-flight are all seen on the next
    tick, with no event plumbing between the handler and this loop.
    """

    def __init__(self, store: ScheduledOrderStore, trader, settings,
                 notify: Callable[[int, str], object],
                 get_bid: Callable[[str], object],
                 watch_subscribe=None, pending_cc=None,
                 order_cancel_watch=None, parse_spec=None,
                 format_success=None, clock: Callable[[], float] = time.time,
                 poll_seconds: float = 30.0,
                 grace_seconds: float = GRACE_SECONDS,
                 retry_seconds: float = RETRY_DELAY_SECONDS,
                 max_attempts: int = MAX_ATTEMPTS):
        # notify: async (chat_id, text) -> None (main.py's notify)
        # get_bid: async (account shortcode) -> float or None
        # watch_subscribe: main.py hook ensuring ticks flow for an account
        # pending_cc / order_cancel_watch: main.py's shared registries, so
        #   --smart-sl and --cancel on the stored spec behave like a
        #   manually typed /p
        # parse_spec: parse_trade; format_success: handlers.order_success_text
        self._store = store
        self._trader = trader
        self._settings = settings
        self._notify = notify
        self._get_bid = get_bid
        self._watch_subscribe = watch_subscribe
        self._pending_cc = pending_cc if pending_cc is not None else {}
        self._order_cancel_watch = (order_cancel_watch
                                    if order_cancel_watch is not None
                                    else {})
        self._parse_spec = parse_spec
        self._format_success = format_success
        self._clock = clock
        self._poll = poll_seconds
        self._grace = grace_seconds
        self._retry = retry_seconds
        self._max_attempts = max_attempts

    @property
    def utc_offset(self) -> float:
        return getattr(self._settings, "display_utc_offset", 0.0)

    # ------------------------------------------------------------------
    # chat-facing operations (used by the /later handler)
    # ------------------------------------------------------------------
    def arm(self, chat_id: int, account: str, fire_time: str,
            band_low: float, band_high: float, spec: str,
            next_fire: int) -> ScheduledRule:
        rule = self._store.arm(chat_id, account, fire_time, band_low,
                               band_high, spec, next_fire,
                               int(self._clock()))
        log.info("scheduled order %s armed: %s %s band %.2f-%.2f spec: %s",
                 rule.id, fire_time, account, band_low, band_high, spec)
        return rule

    def rules(self, limit: int = 10) -> List[ScheduledRule]:
        return self._store.recent(limit)

    def cancel(self, rule_id: int, chat_id: int) -> bool:
        return self._store.cancel(rule_id, chat_id)

    # ------------------------------------------------------------------
    # the watcher loop
    # ------------------------------------------------------------------
    async def run(self) -> None:
        log.info("scheduled order watcher running (poll %.0fs)",
                 self._poll)
        while True:
            try:
                now = self._clock()
                for rule in self._store.armed():
                    if rule.next_fire > now:
                        continue
                    try:
                        await self._evaluate(rule, now)
                    except Exception:
                        log.exception("scheduled order %s evaluation "
                                      "failed", rule.id)
            except Exception:
                log.exception("scheduled order watcher tick failed")
            await asyncio.sleep(self._poll)

    async def _evaluate(self, rule: ScheduledRule, now: float) -> None:
        if now - rule.next_fire > self._grace:
            nxt = next_occurrence(now, rule.fire_time, self.utc_offset)
            self._store.reschedule(rule.id, nxt, rule.attempts)
            log.info("scheduled order %s: missed %s by %ds -- re-armed "
                     "for %s", rule.id, rule.fire_time,
                     int(now - rule.next_fire),
                     fmt.fire_label(nxt, self.utc_offset))
            await self._safe_notify(
                rule.chat_id,
                fmt.scheduled_missed(rule, fmt.fire_label(nxt,
                                                          self.utc_offset)))
            return

        bid = await self._get_bid(rule.account)
        if bid is None:
            await self._retry_or_fail(rule, now, "could not fetch live price")
            return

        if not in_band(bid, rule.band_low, rule.band_high):
            self._store.settle(rule.id, STATUS_SKIPPED)
            log.info("scheduled order %s skipped: price %.2f outside band "
                     "%.2f-%.2f", rule.id, bid, rule.band_low, rule.band_high)
            await self._safe_notify(rule.chat_id, fmt.scheduled_skipped(
                rule, bid))
            return

        try:
            args = self._parse_spec("/p " + rule.spec, False)
            plan, symbol, result, lots = await self._trader.execute(
                args, False, risk_usd=args.risk_usd)
        except Exception as e:
            await self._retry_or_fail(rule, now, str(e))
            return

        self._store.settle(rule.id, STATUS_DONE)
        log.info("scheduled order %s fired at %.2f (band %.2f-%.2f): %s",
                 rule.id, bid, rule.band_low, rule.band_high, rule.spec)
        notes = await self._attach_followups(rule, args, plan, result)
        await self._safe_notify(
            rule.chat_id,
            fmt.scheduled_fired(rule, bid,
                                self._format_success(plan, symbol, result,
                                                     lots, args),
                                notes))

    async def _retry_or_fail(self, rule: ScheduledRule, now: float,
                             reason: str) -> None:
        """A fire that errored: retry with the band re-checked, then give
        up. Nothing was placed in any of these cases."""
        if rule.attempts + 1 >= self._max_attempts:
            self._store.settle(rule.id, STATUS_FAILED)
            log.warning("scheduled order %s failed after %d attempts: %s",
                        rule.id, self._max_attempts, reason)
            await self._safe_notify(
                rule.chat_id,
                fmt.scheduled_failed(rule, reason, self._max_attempts))
            return
        nxt = int(now + self._retry)
        self._store.reschedule(rule.id, nxt, rule.attempts + 1)
        log.warning("scheduled order %s: attempt %d failed (%s) -- "
                    "retrying at %s", rule.id, rule.attempts + 1, reason,
                    fmt.fire_label(nxt, self.utc_offset))
        await self._safe_notify(
            rule.chat_id,
            fmt.scheduled_retry(rule, reason,
                                fmt.fire_label(nxt, self.utc_offset),
                                rule.attempts + 2))

    async def _attach_followups(self, rule: ScheduledRule, args, plan,
                                result) -> str:
        """Register --smart-sl and --cancel exactly like a manually typed
        /p would. Best effort, after the order is live; returns extra
        lines for the fired message."""
        oid = result.order_id if result is not None else None
        if oid is None:
            return ""
        notes = []
        if args.smart_sl is not None:
            self._pending_cc[str(oid)] = {
                "chat_id": rule.chat_id,
                "symbol": self._settings.trade_symbol,
                "timeframe": args.smart_sl_tf,
                "cc_price": args.smart_sl,
                "direction": plan.direction,
                "account": args.account,
                "broadcast": args.broadcast,
            }
            log.info("scheduled order %s: cc guard queued on fill (%s "
                     "close %s)", oid, args.smart_sl_tf, args.smart_sl)
            notes.append(fmt.cc_guard_pending(args.smart_sl_tf,
                                              args.smart_sl))
        if args.cancel_price is not None:
            account = self._settings.accounts.get(args.account)
            if account is not None:
                spec = {
                    "level": args.cancel_price,
                    "env": account.environment,
                    "account_id": account.ctid_account_id,
                    "chat_id": rule.chat_id,
                    "symbol": self._settings.trade_symbol,
                    "symbol_id": None,
                }
                if self._watch_subscribe is not None:
                    try:
                        info = await self._watch_subscribe(
                            args.account, self._settings.trade_symbol)
                        if info is not None:
                            spec["symbol_id"] = info.symbol_id
                    except Exception as e:
                        log.warning("scheduled order %s: cancel-watch "
                                    "subscribe failed: %s", oid, e)
                self._order_cancel_watch[str(oid)] = spec
                log.info("scheduled order %s: cancel condition set at %.2f",
                         oid, args.cancel_price)
                notes.append(fmt.cancel_condition_set(oid, args.cancel_price))
        return "\n".join(notes)

    async def _safe_notify(self, chat_id: int, text: str) -> None:
        try:
            await self._notify(chat_id, text)
        except Exception:
            log.exception("scheduled order notify to chat %s failed",
                          chat_id)
