"""Candle polling feed for candle-close alerts.

Polls ProtoOAGetTrendbars (2137) every few seconds and detects newly
closed bars via utcTimestampInMinutes advance. Trendbar prices arrive as
scaled integers (a low plus unsigned deltas) -- divide by PRICE_SCALE
(same as spot). The first bar is logged so the scaling can be eyeballed
during demo testing (mirrors the spot-price scaling check in market.py).

This module is the adapter between the gateway's wire format and the
plain OHLC bars the engines consume: it decodes a trendbar into a Bar
and hands the whole thing over, one closed candle at a time.
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ..wave import Bar
from . import client as ct
from .market import MarketData

log = logging.getLogger("crackedalert.candles")

PRICE_SCALE = 100000.0
POLL_INTERVAL = 10.0
HISTORY_COUNT = 3

# Valid ProtoOATrendbarPeriod values (JSON mode uses these strings).
TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1")

# Timeframes offered for the soft candle-close stop (--smart-sl).
SMART_SL_TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1")


class CandleFeed:
    """Polls closed candles for a set of (symbol, timeframe) keys.

    On each poll it fetches the latest completed bars and, when a new bar
    timestamp appears, treats the previous bar (the one whose timestamp is
    now in the past) as closed and dispatches it to the engine.
    """

    def __init__(self, cli: ct.CTraderClient, market: MarketData,
                 account_id: int, engines: Sequence,
                 poll_interval: float = POLL_INTERVAL,
                 history_count: int = HISTORY_COUNT):
        self._cli = cli
        self._market = market
        self._account_id = account_id
        self._engines = list(engines)
        self._poll_interval = poll_interval
        self._history_count = history_count
        self._keys: Set[Tuple[str, str]] = set()
        self._last_ts: Dict[Tuple[str, str], int] = {}
        self._last_close: Dict[Tuple[str, str], Optional[float]] = {}
        self._task: Optional[asyncio.Task] = None
        self._first_logged = False

    # ------------------------------------------------------------------
    # subscription management
    # ------------------------------------------------------------------
    def add_symbol(self, symbol: str, timeframe: str) -> None:
        self._keys.add((symbol.upper(), timeframe.upper()))

    def remove_symbol(self, symbol: str, timeframe: str) -> None:
        self._keys.discard((symbol.upper(), timeframe.upper()))

    def sync_keys(self, wanted) -> None:
        """Keep only the (symbol, timeframe) keys that are still wanted
        (from the candle-alert store). Stops polling stale keys after
        guards fire, get cancelled, or their position closes."""
        self._keys &= {(str(s).upper(), str(t).upper()) for s, t in wanted}

    def symbols(self) -> set:
        return set(self._keys)

    async def last_close(self, symbol: str, timeframe: str) -> Optional[float]:
        """Latest closed-bar close for a key, or None. Also registers the
        key so future polls keep it fresh."""
        key = (symbol.upper(), timeframe.upper())
        self._keys.add(key)
        try:
            bars = await self._fetch_bars(symbol.upper(), timeframe.upper())
        except (ct.CTraderError, ct.NotConnected):
            return None
        if not bars:
            return None
        close = self._unpack(bars[-1]).close
        self._last_close[key] = close
        return close

    def last_close_cached(self, symbol: str, timeframe: str) -> Optional[float]:
        return self._last_close.get((symbol.upper(), timeframe.upper()))

    async def history(self, symbol: str, timeframe: str,
                      count: int) -> List[Bar]:
        """The last `count` completed bars, oldest first, decoded.

        This is what the warm-up scan fetches. Order matters: state is
        path dependent, so the scan replays from the oldest bar.
        """
        raw = await self._fetch_bars(symbol.upper(), timeframe.upper(),
                                     count)
        return [self._unpack(bar) for bar in raw]

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:
                log.exception("candle poll failed")
            await asyncio.sleep(self._poll_interval)

    async def poll_once(self) -> None:
        for symbol, timeframe in sorted(self._keys):
            try:
                await self._poll_key(symbol, timeframe)
            except (ct.CTraderError, ct.NotConnected):
                log.warning("candle poll %s %s: link down or error",
                            symbol, timeframe)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    async def _fetch_bars(self, symbol: str, timeframe: str,
                          count: Optional[int] = None) -> list:
        """Newest-last completed trendbars, still in wire format.

        count overrides the feed's usual window, which is what the
        warm-up scan widens as it retries.
        """
        info = await self._market.ensure_symbol(self._account_id, symbol)
        _, payload = await self._cli.request(ct.PT_GET_TRENDBARS_REQ, {
            "ctidTraderAccountId": self._account_id,
            "symbolId": info.symbol_id,
            "period": timeframe,
            "toTimestamp": int(time.time() * 1000),
            "count": self._history_count if count is None else count,
        })
        bars = payload.get("trendbar", []) or []
        bars = sorted(bars, key=lambda b: int(b.get("utcTimestampInMinutes", 0) or 0))
        if bars and not self._first_logged:
            self._first_logged = True
            log.info("first trendbar frame (verify price scaling!): %r", bars[-1])
        return bars

    async def _poll_key(self, symbol: str, timeframe: str) -> None:
        bars = await self._fetch_bars(symbol, timeframe)
        if not bars:
            return
        latest_ts = int(bars[-1].get("utcTimestampInMinutes", 0) or 0)
        if latest_ts <= 0:
            return
        key = (symbol, timeframe)
        prev_ts = self._last_ts.get(key)
        if prev_ts is not None and latest_ts > prev_ts:
            # A newer bar appeared => the newest bar (bars[-1]) just closed.
            # GetTrendbars returns only completed bars (no forming candle),
            # so the just-closed bar is the new latest, not the one at
            # prev_ts (which is already a candle older).
            bar = self._unpack(bars[-1])
            self._last_close[key] = bar.close
            log.info("candle closed %s %s ts=%d o=%.5f h=%.5f l=%.5f c=%.5f",
                     symbol, timeframe, latest_ts,
                     bar.open, bar.high, bar.low, bar.close)
            await self._dispatch(symbol, timeframe, bar)
        self._last_ts[key] = latest_ts

    async def _dispatch(self, symbol: str, timeframe: str,
                        bar: Bar) -> None:
        """Hand the closed bar to every engine, in order.

        Each is isolated: engines do their own I/O (SQLite writes, and
        history fetches on a cold warm-up), so one failing must not stop
        the bar reaching the others. A bar is only delivered once, so a
        engine that raises has missed it -- that is why the wave engine
        persists its own progress rather than relying on redelivery.
        """
        for engine in self._engines:
            try:
                await engine.on_closed_bar(symbol, timeframe, bar)
            except Exception:
                log.exception("closed-bar handler failed for %s %s ts=%d",
                              symbol, timeframe, bar.ts)

    @staticmethod
    def _unpack(raw: dict) -> Bar:
        """Decode one completed trendbar into a plain OHLC bar.

        The wire format is a low plus unsigned deltas, per the official
        proto (ProtoOATrendbar): open = low + deltaOpen, and likewise
        deltaHigh and deltaClose. Everything is scaled by PRICE_SCALE.
        """
        low = int(raw.get("low", 0) or 0)
        return Bar(
            ts=int(raw.get("utcTimestampInMinutes", 0) or 0),
            open=(low + int(raw.get("deltaOpen", 0) or 0)) / PRICE_SCALE,
            high=(low + int(raw.get("deltaHigh", 0) or 0)) / PRICE_SCALE,
            low=low / PRICE_SCALE,
            close=(low + int(raw.get("deltaClose", 0) or 0)) / PRICE_SCALE,
        )