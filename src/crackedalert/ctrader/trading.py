"""Order placement: volume conversion, payload building, execution.

Volume encoding (the migration's highest-risk conversion, PLAN.md risk #1):
ProtoOANewOrderReq.volume and ProtoOASymbol.lotSize/minVolume/stepVolume
are all in 0.01-unit encoding. protocol_volume = lots * lotSize.
XAUUSD sanity: lotSize 10000 (100 oz), so 0.04 lots -> volume 400.
Verified empirically on demo before any live order (Phase 4 gate).

LIVE_TRADING_ENABLED is the Phase 3 hard guard: orders on live accounts
are refused until the Phase 5 cutover flips it.
"""

import logging
import math
from dataclasses import dataclass
from typing import Optional

from .. import risk
from . import client as ct
from .market import MarketData, SymbolInfo

log = logging.getLogger("crackedalert.trading")

LIVE_TRADING_ENABLED = False          # flipped at Phase 5 cutover
ORDER_TIMEOUT_SECONDS = 10.0
MAGIC_LABEL = "crackedalert"          # cTrader has no magic numbers; label instead


class TradeRejected(Exception):
    """User-facing refusal (guard rails, math produced nothing tradable)."""


@dataclass(frozen=True)
class OrderResult:
    order_id: Optional[int]
    execution_type: str


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
    return OrderResult(
        order_id=int(order_id) if order_id is not None else None,
        execution_type=str(resp.get("executionType", "UNKNOWN")))


class TradingService:
    """Full /m and /p flow against one account. Pure orchestration --
    all math lives in risk.py, all formatting in bot/formatting.py."""

    def __init__(self, clients: dict, markets: dict, settings):
        self._clients = clients      # env -> CTraderClient
        self._markets = markets      # env -> MarketData
        self._settings = settings

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

        if is_market:
            plan = risk.plan_market(
                quote.bid, quote.ask, args.sl, args.widen, args.rr,
                args.risk_pct, balance, usd_per_point_per_lot=usd_per_point,
                risk_usd=risk_usd)
        else:
            plan = risk.plan_pending(
                quote.bid, quote.ask, args.entry, args.sl, args.widen,
                args.rr, args.risk_pct, balance,
                usd_per_point_per_lot=usd_per_point, risk_usd=risk_usd)

        if plan.lots <= 0:
            raise TradeRejected(
                "error: lot size calculated to 0. check parameters.")

        volume = lots_to_volume(plan.lots, symbol)
        payload = build_order_payload(account.ctid_account_id, symbol,
                                      plan, volume)
        log.info("placing order: %r", payload)
        result = await place_order(cli, payload)
        return plan, symbol, result, volume_to_lots(volume, symbol)

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
          {id, symbol, side, volume, price, sl, tp, extra}
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
            if is_positions:
                rows.append({
                    "id": item.get("positionId"),
                    "symbol": symbol,
                    "side": trade.get("tradeSide"),
                    "volume": _volume_to_lots(trade.get("volume", 0), info),
                    "price": item.get("price"),
                    "sl": item.get("stopLoss"),
                    "tp": item.get("takeProfit"),
                    "extra": _swap_label(item.get("swap"),
                                         item.get("moneyDigits")),
                })
            else:
                rows.append({
                    "id": item.get("orderId"),
                    "symbol": symbol,
                    "side": trade.get("tradeSide"),
                    "volume": _volume_to_lots(trade.get("volume", 0), info),
                    "price": item.get("limitPrice") or item.get(
                        "stopPrice") or item.get("executionPrice"),
                    "sl": item.get("stopLoss"),
                    "tp": item.get("takeProfit"),
                    "extra": item.get("orderType"),
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
