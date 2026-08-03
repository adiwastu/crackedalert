"""Symbol metadata cache, spot subscriptions, and the live price store.

Spot prices arrive as uint64 scaled by 100000 (per ProtoOASpotEvent spec).
A SpotEvent may carry only bid or only ask -- sides are merged into the
stored quote. The first raw spot frame is logged at INFO so the scaling
assumption can be eyeballed during the demo smoke test (PLAN.md risk #2).
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from . import client as ct

log = logging.getLogger("crackedalert.market")

PRICE_SCALE = 100000.0
STALE_AFTER_SECONDS = 10.0


@dataclass(frozen=True)
class SymbolInfo:
    symbol_id: int
    name: str
    digits: int
    lot_size: int        # in 0.01 units
    min_volume: int      # in 0.01 units
    step_volume: int     # in 0.01 units
    pip_position: int


@dataclass
class Quote:
    bid: Optional[float] = None
    ask: Optional[float] = None
    updated_at: float = 0.0

    @property
    def complete(self) -> bool:
        return self.bid is not None and self.ask is not None

    def fresh(self, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        return self.complete and (now - self.updated_at) <= STALE_AFTER_SECONDS


TickListener = Callable[[int, int, float, float], Awaitable[None]]
# args: account_id, symbol_id, bid, ask (only fired on complete quotes)


class MarketData:
    def __init__(self, cli: ct.CTraderClient):
        self._client = cli
        self._symbols: Dict[int, Dict[str, SymbolInfo]] = {}
        self._all_names: Dict[int, set] = {}    # every symbol name per account
        self._quotes: Dict[Tuple[int, int], Quote] = {}
        self._subscriptions: set = set()
        self._listeners: List[TickListener] = []
        self._first_spot_logged = False
        cli.add_event_handler(ct.PT_SPOT_EVENT, self._on_spot)

    # ------------------------------------------------------------------
    # symbols
    # ------------------------------------------------------------------
    async def ensure_symbol(self, account_id: int, name: str) -> SymbolInfo:
        """Resolve a symbol name to full metadata for one account, cached."""
        cached = self._symbols.get(account_id, {}).get(name.upper())
        if cached is not None:
            return cached

        _, listing = await self._client.request(ct.PT_SYMBOLS_LIST_REQ, {
            "ctidTraderAccountId": account_id,
        })
        symbol_id = None
        names = set()
        for light in listing.get("symbol", []):
            light_name = str(light.get("symbolName", "")).upper()
            names.add(light_name)
            if light_name == name.upper():
                symbol_id = int(light["symbolId"])
        self._all_names[account_id] = names
        if symbol_id is None:
            raise ct.CTraderError(
                "SYMBOL_NOT_FOUND",
                "symbol '%s' not offered on account %d" % (name, account_id))

        _, detail = await self._client.request(ct.PT_SYMBOL_BY_ID_REQ, {
            "ctidTraderAccountId": account_id,
            "symbolId": [symbol_id],
        })
        entries = detail.get("symbol", [])
        if not entries:
            raise ct.CTraderError(
                "SYMBOL_NOT_FOUND",
                "no detail for symbol id %d on account %d"
                % (symbol_id, account_id))
        raw = entries[0]
        info = SymbolInfo(
            symbol_id=symbol_id,
            name=name.upper(),
            digits=int(raw.get("digits", 2)),
            lot_size=int(raw.get("lotSize", 0)),
            min_volume=int(raw.get("minVolume", 0)),
            step_volume=int(raw.get("stepVolume", 0)),
            pip_position=int(raw.get("pipPosition", 0)),
        )
        self._symbols.setdefault(account_id, {})[info.name] = info
        log.info("symbol %s on account %d: id=%d digits=%d lotSize=%d "
                 "minVol=%d stepVol=%d", info.name, account_id, symbol_id,
                 info.digits, info.lot_size, info.min_volume, info.step_volume)
        return info

    def forget_account(self, account_id: int) -> None:
        """Drop caches after a reconnect (symbol ids are stable, but be safe)."""
        self._symbols.pop(account_id, None)

    def known_symbols(self, account_id: int) -> set:
        """Every symbol name the account offers (empty before first fetch)."""
        return self._all_names.get(account_id, set())

    def symbol_name(self, account_id: int, symbol_id: int) -> Optional[str]:
        """Reverse lookup: symbolId -> name, from the cached metadata."""
        for name, info in self._symbols.get(account_id, {}).items():
            if info.symbol_id == symbol_id:
                return name
        return None

    def symbol_info(self, account_id: int, symbol_id: int) -> Optional[SymbolInfo]:
        """Reverse lookup: symbolId -> full SymbolInfo, from the cache."""
        for info in self._symbols.get(account_id, {}).values():
            if info.symbol_id == symbol_id:
                return info
        return None

    # ------------------------------------------------------------------
    # subscriptions & quotes
    # ------------------------------------------------------------------
    async def subscribe(self, account_id: int, symbol_id: int) -> None:
        await self._client.request(ct.PT_SUBSCRIBE_SPOTS_REQ, {
            "ctidTraderAccountId": account_id,
            "symbolId": [symbol_id],
        })
        self._subscriptions.add((account_id, symbol_id))
        log.info("subscribed to spots: account=%d symbol=%d",
                 account_id, symbol_id)

    def reset_subscriptions(self, account_id: int) -> None:
        """Call after reconnect: server-side subscriptions are gone."""
        self._subscriptions = {(acc, sym) for acc, sym in self._subscriptions
                               if acc != account_id}

    async def ensure_quote(self, account_id: int, symbol_name: str,
                           wait_seconds: float = 3.0):
        """Resolve + subscribe (idempotent) + wait for a fresh quote.

        Returns (SymbolInfo, Quote-or-None). None quote = market closed,
        dead feed, or subscription not yet flowing.
        """
        info = await self.ensure_symbol(account_id, symbol_name)
        key = (account_id, info.symbol_id)

        quote = self.quote(account_id, info.symbol_id)
        if quote is not None:
            return info, quote

        # (Re)subscribe: either never subscribed, or the quote went stale
        # (possibly a silently dropped subscription) -- resubscribing is safe.
        try:
            await self.subscribe(account_id, info.symbol_id)
        except ct.CTraderError as e:
            if key not in self._subscriptions:
                raise
            log.warning("resubscribe %s: %s", symbol_name, e)

        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            quote = self.quote(account_id, info.symbol_id)
            if quote is not None:
                return info, quote
        return info, None

    def quote(self, account_id: int, symbol_id: int) -> Optional[Quote]:
        """Latest quote, or None if absent/stale/incomplete."""
        q = self._quotes.get((account_id, symbol_id))
        return q if q is not None and q.fresh() else None

    def add_tick_listener(self, listener: TickListener) -> None:
        self._listeners.append(listener)

    async def _on_spot(self, payload: dict) -> None:
        if not self._first_spot_logged:
            self._first_spot_logged = True
            log.info("first spot frame (verify price scaling!): %r", payload)

        try:
            account_id = int(payload["ctidTraderAccountId"])
            symbol_id = int(payload["symbolId"])
        except (KeyError, TypeError, ValueError):
            return

        key = (account_id, symbol_id)
        q = self._quotes.get(key)
        if q is None:
            q = self._quotes[key] = Quote()

        raw_bid = payload.get("bid")
        raw_ask = payload.get("ask")
        if raw_bid is not None:
            q.bid = int(raw_bid) / PRICE_SCALE
        if raw_ask is not None:
            q.ask = int(raw_ask) / PRICE_SCALE
        q.updated_at = time.monotonic()

        if q.complete and self._listeners:
            await asyncio.gather(
                *(listener(account_id, symbol_id, q.bid, q.ask)
                  for listener in self._listeners),
                return_exceptions=True)
