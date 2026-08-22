"""Order placement: volume conversion, payload building, execution.

Volume encoding (the migration's highest-risk conversion, PLAN.md risk #1):
ProtoOANewOrderReq.volume and ProtoOASymbol.lotSize/minVolume/stepVolume
are all in 0.01-unit encoding. protocol_volume = lots * lotSize.
XAUUSD sanity: lotSize 10000 (100 oz), so 0.04 lots -> volume 400.
Verified empirically on demo before any live order (Phase 4 gate).

LIVE_TRADING_ENABLED is the Phase 3 hard guard: orders on live accounts
are refused until the Phase 5 cutover flips it.
"""

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Optional

from .. import risk
from . import client as ct
from .market import MarketData, SymbolInfo

log = logging.getLogger("crackedalert.trading")

LIVE_TRADING_ENABLED = True           # Phase 5 cutover: live trading is on
ORDER_TIMEOUT_SECONDS = 10.0
MAGIC_LABEL = "crackedalert"          # cTrader has no magic numbers; label instead
POSITION_CONFIRM_RETRIES = 3
POSITION_CONFIRM_RETRY_DELAY = 0.5   # seconds between reconcile attempts


class TradeRejected(Exception):
    """User-facing refusal (guard rails, math produced nothing tradable)."""


@dataclass(frozen=True)
class OrderResult:
    order_id: Optional[int]
    execution_type: str
    position: Optional[dict] = None
    position_id: Optional[int] = None


def lots_to_volume(lots: float, symbol: SymbolInfo) -> int:
    """Convert decimal lots to protocol volume, snapped to broker limits.

    Rejects sub-minimum sizes instead of clamping up: a $10 risk calc that
    floors to 0.003 lots must NOT silently become 0.01 lots (10x risk).
    """
    if symbol.lot_size <= 0:
        raise TradeRejected("symbol lotSize unknown -- cannot size order")
    raw = lots * symbol.lot_size
    # Reject below the broker minimum BEFORE step-flooring, so a tiny calc
    # reports "below minimum" rather than a confusing "volume rounds to zero".
    if symbol.min_volume > 0 and raw < symbol.min_volume:
        min_lots = volume_to_lots(symbol.min_volume, symbol)
        raise TradeRejected(
            "error: calculated %.3f lots below the %.2f minimum -- "
            "not placing the order." % (lots, min_lots))
    if symbol.step_volume > 0:
        raw = math.floor(raw / symbol.step_volume + 1e-9) * symbol.step_volume
    volume = int(round(raw))
    if volume <= 0:
        raise TradeRejected("volume rounds to zero")
    return volume


def volume_to_lots(volume: int, symbol: SymbolInfo) -> float:
    return volume / symbol.lot_size if symbol.lot_size else 0.0


def build_order_payload(account_id: int, symbol: SymbolInfo,
                        plan: risk.TradePlan, volume: int) -> dict:
    digits = symbol.digits
    payload = {
        "ctidTraderAccountId": account_id,
        "symbolId": symbol.symbol_id,
        "orderType": plan.order_kind,          # MARKET | LIMIT | STOP
        "tradeSide": plan.direction,           # BUY | SELL
        "volume": volume,
        "stopLoss": round(plan.sl, digits),
        "takeProfit": round(plan.tp, digits),
        "label": MAGIC_LABEL,
    }
    if plan.order_kind == risk.LIMIT:
        payload["limitPrice"] = round(plan.placement_price, digits)
        payload["timeInForce"] = "GOOD_TILL_CANCEL"
    elif plan.order_kind == risk.STOP:
        payload["stopPrice"] = round(plan.placement_price, digits)
        payload["timeInForce"] = "GOOD_TILL_CANCEL"
    return payload


async def fetch_balance(cli: ct.CTraderClient, account_id: int) -> float:
    _, payload = await cli.request(ct.PT_TRADER_REQ, {
        "ctidTraderAccountId": account_id,
    })
    trader = payload.get("trader", {})
    digits = int(trader.get("moneyDigits", 2))
    return trader.get("balance", 0) / (10 ** digits)


async def place_order(cli: ct.CTraderClient, payload: dict) -> OrderResult:
    """Send the order; the correlated response is an ExecutionEvent
    (errors surface as CTraderError via the client)."""
    pt, resp = await cli.request(ct.PT_NEW_ORDER_REQ, payload,
                                 timeout=ORDER_TIMEOUT_SECONDS)
    if pt != ct.PT_EXECUTION_EVENT:
        log.warning("unexpected order response pt=%s payload=%r", pt, resp)
    order = resp.get("order", {}) or {}
    order_id = order.get("orderId")
    position = resp.get("position") or {}
    # Market orders return the opened position in the same event; pending
    # orders don't (they fill later), so position stays None for those.
    position_id = position.get("positionId")
    if position_id is None:
        position_id = order.get("positionId")
    return OrderResult(
        order_id=int(order_id) if order_id is not None else None,
        execution_type=str(resp.get("executionType", "UNKNOWN")),
        position=position or None,
        position_id=int(position_id) if position_id is not None else None)


class TradingService:
    """Full /m and /p flow against one account. Pure orchestration --
    all math lives in risk.py, all formatting in bot/formatting.py."""

    def __init__(self, clients: dict, markets: dict, settings):
        self._clients = clients      # env -> CTraderClient
        self._markets = markets      # env -> MarketData
        self._settings = settings
        self._filled = {}            # orderId -> positionId (pending fills)

    async def execute(self, args, is_market: bool,
                      risk_usd: Optional[float] = None):
        """Returns (plan, symbol, result, lots_final). Raises TradeRejected
        with a user-facing message, or CTraderError from the wire."""
        account = self._settings.accounts.get(args.account)
        if account is None:
            raise TradeRejected("error: account '%s' not found." % args.account)

        if account.environment != "demo" and not LIVE_TRADING_ENABLED:
            raise TradeRejected(
                "error: live trading is locked until the Phase 5 cutover. "
                "use the demo account.")

        cli = self._clients[account.environment]
        market: MarketData = self._markets[account.environment]
        if not cli.connected:
            raise TradeRejected(
                "error: cTrader %s link is down, try again shortly."
                % account.environment)

        symbol_name = self._settings.trade_symbol
        try:
            symbol, quote = await market.ensure_quote(
                account.ctid_account_id, symbol_name)
        except ct.CTraderError as e:
            raise TradeRejected("error: %s" % e.description)
        if quote is None:
            raise TradeRejected(
                "error: could not fetch live price for %s." % symbol_name)

        try:
            balance = await fetch_balance(cli, account.ctid_account_id)
        except (ct.CTraderError, ct.NotConnected):
            raise TradeRejected(
                "error: could not fetch balance for %s." % args.account)
        if balance <= 0:
            raise TradeRejected(
                "error: could not fetch balance for %s." % args.account)

        usd_per_point = symbol.lot_size / 100.0 if symbol.lot_size else 100.0

        smart_sl = getattr(args, "smart_sl", None)
        # Validate the exact smart-SL price against the actual fill before
        # planning: it must sit strictly between the fill (bid for SELL /
        # ask for BUY market; the placement price for pending) and the
        # original (widen-adjusted) SL, so it genuinely tightens the stop.
        if smart_sl is not None:
            if is_market:
                mid = (quote.bid + quote.ask) / 2.0
                fill_side = risk.BUY if args.sl < mid else risk.SELL
                fill_price = quote.ask if fill_side == risk.BUY else quote.bid
            else:
                fill_side = risk.BUY if args.sl < args.entry else risk.SELL
                fill_price = args.entry
            sl_bound = args.sl - (risk.WIDEN_AMOUNT if args.widen else 0) \
                if fill_side == risk.BUY else \
                args.sl + (risk.WIDEN_AMOUNT if args.widen else 0)
            lo = min(fill_price, sl_bound)
            hi = max(fill_price, sl_bound)
            if not (lo < smart_sl < hi):
                raise TradeRejected(
                    "error: smart SL %.2f must sit between the fill (%.2f) "
                    "and the original SL (%.2f)."
                    % (smart_sl, fill_price, sl_bound))
        if is_market:
            plan = risk.plan_market(
                quote.bid, quote.ask, args.sl, args.widen, args.rr,
                args.risk_pct, balance, usd_per_point_per_lot=usd_per_point,
                risk_usd=risk_usd, smart_sl=smart_sl)
        else:
            plan = risk.plan_pending(
                quote.bid, quote.ask, args.entry, args.sl, args.widen,
                args.rr, args.risk_pct, balance,
                usd_per_point_per_lot=usd_per_point, risk_usd=risk_usd,
                smart_sl=smart_sl)

        if plan.lots <= 0:
            raise TradeRejected(
                "error: lot size calculated to 0. check parameters.")

        volume = lots_to_volume(plan.lots, symbol)
        payload = build_order_payload(account.ctid_account_id, symbol,
                                      plan, volume)
        log.info("placing order: %r", payload)
        result = await place_order(cli, payload)
        # Record the fill mapping when the order response already carries
        # both ids (some brokers echo positionId on the order event). The
        # PT_EXECUTION_EVENT stream handler in main.py covers the rest.
        if result.order_id is not None and result.position_id is not None:
            self.note_fill(result.order_id, result.position_id)
        return plan, symbol, result, volume_to_lots(volume, symbol)

    # ------------------------------------------------------------------
    # pending-fill tracking (orderId -> positionId)
    # ------------------------------------------------------------------
    def note_fill(self, order_id, position_id) -> None:
        """Record which position a pending order filled into. Called from
        the bot event loop when an ExecutionEvent carries both ids."""
        if order_id is None or position_id is None:
            return
        self._filled[int(order_id)] = int(position_id)

    def filled_position_id(self, order_id) -> Optional[int]:
        """Look up the positionId a pending order filled into, if seen."""
        if not order_id:
            return None
        return self._filled.get(int(order_id))

    # ------------------------------------------------------------------
    # entry confirmation (for trade auto-alerts)
    # ------------------------------------------------------------------
    async def confirm_position(self, account_code: str, symbol_name: str,
                               side: str, entry_target: float,
                               trade_id: str):
        """Return the open position matching symbol/side (+ id or entry
        proximity), or None. Used to confirm an 'entry' auto-alert before
        creating TP/SL alerts. Raises TradeRejected for account/link errors.

        Matching is hardened for the auto-alert flow:
        - trade_id may be an orderId (pending fills): resolve it via
          self._filled (orderId -> positionId) before comparing.
        - positionId equality is compared as ints so an orderId string can
          never false-match a positionId.
        - entry-price tolerance is derived from the symbol's digits
          (~1.5 pips), not a hardcoded 0.05.
        - the reconcile is retried a few times: a just-filled pending order
          may not appear in the very first snapshot.
        """
        account = self._settings.accounts.get(account_code)
        if account is None:
            raise TradeRejected(
                "error: account '%s' not found." % account_code)
        cli = self._clients[account.environment]
        market: MarketData = self._markets[account.environment]
        if not cli.connected:
            raise TradeRejected(
                "error: cTrader %s link is down, try again shortly."
                % account.environment)

        info = await market.ensure_symbol(account.ctid_account_id,
                                          symbol_name)
        symbol_id = info.symbol_id
        # Pip size for typical metals (digits=2 -> 0.1), cushioned 1.5x and
        # floored at a cent so exotic symbols never get a sub-tick tolerance.
        tick = 10.0 ** -max(info.digits - 1, 1)
        tolerance = max(1.5 * tick, 1.5 * 0.0003 * entry_target, 0.05)

        want_position = self.filled_position_id(trade_id)
        try:
            trade_id_int = int(trade_id)
        except (TypeError, ValueError):
            trade_id_int = None

        for attempt in range(POSITION_CONFIRM_RETRIES):
            if attempt:
                await asyncio.sleep(POSITION_CONFIRM_RETRY_DELAY)
            _, payload = await cli.request(ct.PT_RECONCILE_REQ, {
                "ctidTraderAccountId": account.ctid_account_id,
            })
            for item in payload.get("position", []):
                trade = item.get("tradeData", {}) or {}
                if int(trade.get("symbolId", 0)) != symbol_id:
                    continue
                if trade.get("tradeSide") != side:
                    continue
                pos_id = item.get("positionId")
                try:
                    pos_int = int(pos_id)
                except (TypeError, ValueError):
                    pos_int = None
                if want_position is not None and pos_int == want_position:
                    return item
                if trade_id_int is not None and pos_int == trade_id_int:
                    return item
                entry = item.get("price")
                if entry is not None and abs(entry - entry_target) <= tolerance:
                    return item
        return None

    # ------------------------------------------------------------------
    # account balance (for /help)
    # ------------------------------------------------------------------
    async def balance(self, shortcode: str) -> float:
        account = self._settings.accounts.get(shortcode)
        if account is None:
            raise TradeRejected("error: account '%s' not found." % shortcode)
        cli = self._clients[account.environment]
        if not cli.connected:
            raise TradeRejected("connection down")
        return await fetch_balance(cli, account.ctid_account_id)

    # ------------------------------------------------------------------
    # positions / working orders (ProtoOAReconcileReq)
    # ------------------------------------------------------------------
    async def positions_or_orders(self, account_code: str,
                                  is_positions: bool) -> list:
        """Fetch open positions (or working orders) for one account via
        ProtoOAReconcileReq, which returns both position[] and order[].

        Returns a list of dicts ready for formatting:
          {id, symbol, side, volume, price, sl, tp, extra,
           current_price, contract_size}
        Raises TradeRejected for account/link errors, CTraderError from
        the wire.
        """
        account = self._settings.accounts.get(account_code)
        if account is None:
            raise TradeRejected(
                "error: account '%s' not found." % account_code)

        cli = self._clients[account.environment]
        market: MarketData = self._markets[account.environment]
        if not cli.connected:
            raise TradeRejected(
                "error: cTrader %s link is down, try again shortly."
                % account.environment)

        _, payload = await cli.request(ct.PT_RECONCILE_REQ, {
            "ctidTraderAccountId": account.ctid_account_id,
        })

        source = payload.get("position", []) if is_positions \
            else payload.get("order", [])
        rows = []
        for item in source:
            trade = item.get("tradeData", {}) or {}
            symbol_id = int(trade.get("symbolId", 0))
            symbol = market.symbol_name(account.ctid_account_id, symbol_id)
            if symbol is None:
                symbol = "#%d" % symbol_id
            info = market._symbols.get(account.ctid_account_id, {}).get(
                symbol.upper()) if symbol_id else None

            # P3: current_price and contract_size for live PnL cards
            quote = market.quote(account.ctid_account_id, symbol_id)
            side = trade.get("tradeSide", "")
            current_price = None
            if quote is not None:
                if side == "BUY":
                    current_price = quote.ask
                elif side == "SELL":
                    current_price = quote.bid
                elif quote.bid is not None and quote.ask is not None:
                    current_price = (quote.bid + quote.ask) / 2.0
            contract_size = info.contract_size if info is not None else 100.0

            if is_positions:
                rows.append({
                    "id": item.get("positionId"),
                    "symbol": symbol,
                    "side": side,
                    "volume": _volume_to_lots(trade.get("volume", 0), info),
                    "price": item.get("price"),
                    "sl": item.get("stopLoss"),
                    "tp": item.get("takeProfit"),
                    "extra": _swap_label(item.get("swap"),
                                         item.get("moneyDigits")),
                    "current_price": current_price,
                    "contract_size": contract_size,
                })
            else:
                rows.append({
                    "id": item.get("orderId"),
                    "symbol": symbol,
                    "side": side,
                    "volume": _volume_to_lots(trade.get("volume", 0), info),
                    "price": item.get("limitPrice") or item.get(
                        "stopPrice") or item.get("executionPrice"),
                    "sl": item.get("stopLoss"),
                    "tp": item.get("takeProfit"),
                    "extra": item.get("orderType"),
                    "current_price": current_price,
                    "contract_size": contract_size,
                })
        return rows

    # ------------------------------------------------------------------
    # close all positions (ProtoOAClosePositionReq)
    # ------------------------------------------------------------------
    async def close_all(self, account_code: str) -> list:
        """Close every open position on the account.

        Returns a list of result dicts:
          {id, symbol, side, volume, ok, message}
        Raises TradeRejected for account/link errors.
        """
        account = self._settings.accounts.get(account_code)
        if account is None:
            raise TradeRejected(
                "error: account '%s' not found." % account_code)

        if account.environment != "demo" and not LIVE_TRADING_ENABLED:
            raise TradeRejected(
                "error: live trading is locked until the Phase 5 cutover. "
                "use the demo account.")

        cli = self._clients[account.environment]
        market: MarketData = self._markets[account.environment]
        if not cli.connected:
            raise TradeRejected(
                "error: cTrader %s link is down, try again shortly."
                % account.environment)

        _, payload = await cli.request(ct.PT_RECONCILE_REQ, {
            "ctidTraderAccountId": account.ctid_account_id,
        })
        positions = payload.get("position", [])
        results = []
        for item in positions:
            trade = item.get("tradeData", {}) or {}
            position_id = item.get("positionId")
            volume = int(trade.get("volume", 0))
            symbol_id = int(trade.get("symbolId", 0))
            symbol = market.symbol_name(account.ctid_account_id, symbol_id) \
                or "#%d" % symbol_id
            info = market.symbol_info(account.ctid_account_id, symbol_id)
            try:
                await cli.request(ct.PT_CLOSE_POSITION_REQ, {
                    "ctidTraderAccountId": account.ctid_account_id,
                    "positionId": position_id,
                    "volume": volume,
                })
                results.append({
                    "id": position_id, "symbol": symbol,
                    "side": trade.get("tradeSide"),
                    "volume": _volume_to_lots(volume, info),
                    "ok": True, "message": "closed",
                })
            except ct.CTraderError as e:
                results.append({
                    "id": position_id, "symbol": symbol,
                    "side": trade.get("tradeSide"),
                    "volume": _volume_to_lots(volume, info),
                    "ok": False, "message": e.description,
                })
        return results

    # ------------------------------------------------------------------
    # breakeven with spread buffer (ProtoOAAmendPositionSLTPReq)
    # ------------------------------------------------------------------
    async def breakeven(self, account_code: str) -> list:
        """Move SL to breakeven + spread buffer on every open position.

        BE_SL = entry + spread (BUY) / entry - spread (SELL). Only amended
        when the market has actually moved past the BE level (BUY: bid >=
        BE_SL; SELL: ask <= BE_SL). Existing TP is preserved.

        Returns a list of result dicts:
          {id, symbol, side, volume, ok, message, be_sl}
        Raises TradeRejected for account/link errors.
        """
        account = self._settings.accounts.get(account_code)
        if account is None:
            raise TradeRejected(
                "error: account '%s' not found." % account_code)

        if account.environment != "demo" and not LIVE_TRADING_ENABLED:
            raise TradeRejected(
                "error: live trading is locked until the Phase 5 cutover. "
                "use the demo account.")

        cli = self._clients[account.environment]
        market: MarketData = self._markets[account.environment]
        if not cli.connected:
            raise TradeRejected(
                "error: cTrader %s link is down, try again shortly."
                % account.environment)

        _, payload = await cli.request(ct.PT_RECONCILE_REQ, {
            "ctidTraderAccountId": account.ctid_account_id,
        })
        positions = payload.get("position", [])
        results = []
        for item in positions:
            trade = item.get("tradeData", {}) or {}
            position_id = item.get("positionId")
            symbol_id = int(trade.get("symbolId", 0))
            side = trade.get("tradeSide")
            entry = item.get("price")
            symbol = market.symbol_name(account.ctid_account_id, symbol_id) \
                or "#%d" % symbol_id
            info = market.symbol_info(account.ctid_account_id, symbol_id)

            if entry is None or side not in (risk.BUY, risk.SELL):
                results.append({
                    "id": position_id, "symbol": symbol, "side": side,
                    "volume": _volume_to_lots(trade.get("volume", 0), info),
                    "ok": False, "message": "missing entry/side",
                    "be_sl": None,
                })
                continue

            quote = market.quote(account.ctid_account_id, symbol_id)
            if quote is None:
                results.append({
                    "id": position_id, "symbol": symbol, "side": side,
                    "volume": _volume_to_lots(trade.get("volume", 0), info),
                    "ok": False, "message": "no fresh quote",
                    "be_sl": None,
                })
                continue

            spread = quote.ask - quote.bid
            if side == risk.BUY:
                be_sl = entry + spread
                ready = quote.bid >= be_sl
            else:
                be_sl = entry - spread
                ready = quote.ask <= be_sl

            if not ready:
                results.append({
                    "id": position_id, "symbol": symbol, "side": side,
                    "volume": _volume_to_lots(trade.get("volume", 0), info),
                    "ok": False,
                    "message": "not in profit by spread yet",
                    "be_sl": be_sl,
                })
                continue

            try:
                await cli.request(ct.PT_AMEND_POSITION_SLTP_REQ, {
                    "ctidTraderAccountId": account.ctid_account_id,
                    "positionId": position_id,
                    "stopLoss": be_sl,
                })
                results.append({
                    "id": position_id, "symbol": symbol, "side": side,
                    "volume": _volume_to_lots(trade.get("volume", 0), info),
                    "ok": True, "message": "breakeven set",
                    "be_sl": be_sl,
                })
            except ct.CTraderError as e:
                results.append({
                    "id": position_id, "symbol": symbol, "side": side,
                    "volume": _volume_to_lots(trade.get("volume", 0), info),
                    "ok": False, "message": e.description,
                    "be_sl": be_sl,
                })
        return results

    # ------------------------------------------------------------------
    # cancel a single pending order (ProtoOACancelOrderReq)
    # ------------------------------------------------------------------
    async def cancel_order(self, account_code: str, order_id: int) -> None:
        """Cancel one working order. Raises TradeRejected for account/link
        errors, CTraderError from the wire."""
        account = self._settings.accounts.get(account_code)
        if account is None:
            raise TradeRejected(
                "error: account '%s' not found." % account_code)

        if account.environment != "demo" and not LIVE_TRADING_ENABLED:
            raise TradeRejected(
                "error: live trading is locked until the Phase 5 cutover. "
                "use the demo account.")

        cli = self._clients[account.environment]
        if not cli.connected:
            raise TradeRejected(
                "error: cTrader %s link is down, try again shortly."
                % account.environment)

        await cli.request(ct.PT_CANCEL_ORDER_REQ, {
            "ctidTraderAccountId": account.ctid_account_id,
            "orderId": order_id,
        })

    # ------------------------------------------------------------------
    # close a single position (ProtoOAClosePositionReq)
    # ------------------------------------------------------------------
    async def close_position(self, account_code: str, position_id: int) -> None:
        """Close one open position at its full volume. Raises TradeRejected
        for account/link/not-found errors, CTraderError from the wire."""
        account = self._settings.accounts.get(account_code)
        if account is None:
            raise TradeRejected(
                "error: account '%s' not found." % account_code)

        if account.environment != "demo" and not LIVE_TRADING_ENABLED:
            raise TradeRejected(
                "error: live trading is locked until the Phase 5 cutover. "
                "use the demo account.")

        cli = self._clients[account.environment]
        if not cli.connected:
            raise TradeRejected(
                "error: cTrader %s link is down, try again shortly."
                % account.environment)

        _, payload = await cli.request(ct.PT_RECONCILE_REQ, {
            "ctidTraderAccountId": account.ctid_account_id,
        })
        for item in payload.get("position", []):
            if int(item.get("positionId", 0)) == position_id:
                volume = int((item.get("tradeData", {}) or {}).get(
                    "volume", 0))
                if volume <= 0:
                    raise TradeRejected(
                        "error: position %d has no volume to close."
                        % position_id)
                await cli.request(ct.PT_CLOSE_POSITION_REQ, {
                    "ctidTraderAccountId": account.ctid_account_id,
                    "positionId": position_id,
                    "volume": volume,
                })
                return
        raise TradeRejected(
            "error: position %d not found on %s." % (position_id, account_code))


def _volume_to_lots(volume, info) -> float:
    """Convert protocol volume (0.01 units) to lots using symbol lotSize.
    Falls back to raw volume/10000 if symbol metadata is unknown."""
    if info is not None and info.lot_size:
        return volume / info.lot_size
    return volume / 10000.0


def _swap_label(swap, money_digits) -> str:
    """Format swap (moneyDigits-scaled int) to a signed currency string."""
    if swap is None:
        return ""
    digits = int(money_digits or 2)
    return "swap %+.2f" % (swap / (10 ** digits))