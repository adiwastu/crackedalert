"""Candle polling feed for candle-close alerts.

Polls ProtoOAGetTrendbars (2137) every few seconds and detects newly
closed bars via utcTimestampInMinutes advance. Trendbar prices arrive as
scaled integers (low + deltas) -- divide by PRICE_SCALE (same as spot).
The first bar is logged so the scaling can be eyeballed during demo
testing (mirrors the spot-price scaling check in market.py).
"""

import asyncio
import logging
import time
from typing import Dict, Optional, Set, Tuple

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
                 account_id: int, engine,
                 poll_interval: float = POLL_INTERVAL):
        self._cli = cli
        self._market = market
        self._account_id = account_id
        self._engine = engine
        self._poll_interval = poll_interval
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
        close = self._bar_close(bars[-1])
        self._last_close[key] = close
        return close

    def last_close_cached(self, symbol: str, timeframe: str) -> Optional[float]:
        return self._last_close.get((symbol.upper(), timeframe.upper()))

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
    async def _fetch_bars(self, symbol: str, timeframe: str) -> list:
        info = await self._market.ensure_symbol(self._account_id, symbol)
        _, payload = await self._cli.request(ct.PT_GET_TRENDBARS_REQ, {
            "ctidTraderAccountId": self._account_id,
            "symbolId": info.symbol_id,
            "period": timeframe,
            "toTimestamp": int(time.time() * 1000),
            "count": HISTORY_COUNT,
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
            close = self._bar_close(bars[-1])
            self._last_close[key] = close
            log.info("candle closed %s %s ts=%d close=%.5f",
                     symbol, timeframe, latest_ts, close)
            await self._engine.on_closed_bar(
                symbol, timeframe, close, latest_ts)
        self._last_ts[key] = latest_ts

    @staticmethod
    def _bar_close(bar: dict) -> float:
        low = int(bar.get("low", 0) or 0)
        delta_close = int(bar.get("deltaClose", 0) or 0)
        return (low + delta_close) / PRICE_SCALE