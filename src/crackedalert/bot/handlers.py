"""Telegram command handlers.

Parsing lives in pure functions (unit-testable, no I/O). The Handlers
class binds them to the live services. Every handler is gated on
ALLOWED_CHAT_IDS + the dynamic subscription store -- unknown chats are
logged and ignored. /subscribe and /unsubscribe are NOT gated so anyone
can join.
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from .. import alerts as alerts_mod
from ..config import Settings
from ..ctrader import candles as candles_mod
from ..ctrader.client import CTraderError
from ..ctrader.trading import TradeRejected
from . import formatting as fmt
from .subscriptions import SubscriptionStore

log = logging.getLogger("crackedalert.bot")

SYMBOL_RE = re.compile(r"^[A-Z0-9._]{3,12}$")
DEFAULT_ALERT_MESSAGE = "Price target reached."


class ParseError(Exception):
    pass


@dataclass(frozen=True)
class AlertArgs:
    target: float
    symbol: str
    message: str
    broadcast: bool = False


@dataclass(frozen=True)
class TradeArgs:
    entry: Optional[float]     # None for market orders
    sl: float
    widen: bool
    rr: float
    risk_pct: float
    account: str
    risk_usd: Optional[float] = None   # dollar amount, $ prefix; else pct
    cc_timeframe: Optional[str] = None   # e.g. "M15"; None = no guard
    cc_price: Optional[float] = None
    smart_sl: Optional[float] = None      # exact smart-stop price; None = stop at original SL
    broadcast: bool = False


def parse_alert(text: str, default_symbol: str,
                known_symbols=None) -> AlertArgs:
    """/alert <target> [symbol] [message...]

    The bash parser treated the 2nd token as a symbol unconditionally,
    which broke the /help example ("/alert 2450.00 approaching demand").
    Here the 2nd token is a symbol only if the account actually offers it
    (known_symbols); before the first connection, only an ALL-CAPS token
    is accepted as a symbol.
    """
    tokens = text.split()[1:]
    if not tokens:
        raise ParseError("usage")
    try:
        target = float(tokens[0])
    except ValueError:
        raise ParseError("usage")

    rest = tokens[1:]
    broadcast = _pop_broadcast(rest)
    symbol = default_symbol
    if rest and not _is_number(rest[0]) and SYMBOL_RE.match(rest[0].upper()):
        candidate = rest[0].upper()
        looks_known = known_symbols and candidate in known_symbols
        looks_deliberate = not known_symbols and rest[0].isupper()
        if looks_known or looks_deliberate:
            symbol = candidate
            rest = rest[1:]
    message = " ".join(rest) if rest else DEFAULT_ALERT_MESSAGE
    return AlertArgs(target=target, symbol=symbol, message=message,
                     broadcast=broadcast)


def parse_trade(text: str, is_market: bool) -> TradeArgs:
    """/m <sl> <widen> <rr> <risk%> <account> [--smart-sl <price>] [<tf> <guard_price>] [--all]
       /p <entry> <sl> <widen> <rr> <risk%> <account> [--smart-sl <price>] [<tf> <guard_price>] [--all]
    --smart-sl <price> places the stop exactly at <price> (must sit between
    the fill and the original SL). Lots always anchor to the original SL, so
    the risk at the smart stop is the stated risk% scaled by the distance
    ratio. It may appear before the optional CC guard pair / --all.
    """
    tokens = text.split()[1:]
    broadcast = _pop_broadcast(tokens)
    base = 5 if is_market else 6
    if len(tokens) not in (base, base + 1, base + 2, base + 3, base + 4):
        raise ParseError(
            "expected %d arguments (or %d with --smart-sl / %d with CC guard "
            "/ %d with both / %d with both+smart-sl), got %d"
            % (base, base + 1, base + 2, base + 3, base + 4, len(tokens)))
    try:
        if is_market:
            entry = None
            sl, widen_raw, rr_raw, risk_raw, account = (
                float(tokens[0]), tokens[1], tokens[2], tokens[3], tokens[4])
        else:
            entry = float(tokens[0])
            sl, widen_raw, rr_raw, risk_raw, account = (
                float(tokens[1]), tokens[2], tokens[3], tokens[4], tokens[5])
        rr = float(rr_raw)
    except ValueError:
        raise ParseError("numeric argument is not a number")

    # Risk: "$50" means a dollar amount, "0.5" means a percentage.
    risk_usd = None
    if risk_raw.startswith("$"):
        try:
            risk_usd = float(risk_raw[1:])
        except ValueError:
            raise ParseError("numeric argument is not a number")
        risk_pct = 0.0
    else:
        try:
            risk_pct = float(risk_raw)
        except ValueError:
            raise ParseError("numeric argument is not a number")
        if risk_pct <= 0:
            raise ParseError("rr and risk%% must be positive")

    if widen_raw.lower() not in ("y", "n"):
        raise ParseError("widen must be y or n")
    if rr <= 0:
        raise ParseError("rr and risk%% must be positive")
    if risk_usd is not None and risk_usd <= 0:
        raise ParseError("risk amount must be positive")

    # Optional exact smart-SL price: --smart-sl <price> (or -ss <price>).
    rest = tokens[base:]
    smart_sl = None
    if rest and rest[0].lower() in ("--smart-sl", "-ss", "--smartsl"):
        if len(rest) < 2:
            raise ParseError("expected a price after --smart-sl")
        try:
            smart_sl = float(rest[1])
        except ValueError:
            raise ParseError("smart SL price is not a number")
        rest = rest[2:]

    # CC guard (optional)
    cc_timeframe = None
    cc_price = None
    if rest:
        if len(rest) != 2:
            raise ParseError("expected optional CC guard as a tf+price pair")
        cc_timeframe = rest[0].upper()
        if cc_timeframe not in candles_mod.TIMEFRAMES:
            raise ParseError(
                "cc guard timeframe '%s' is not valid. Use: %s"
                % (cc_timeframe, " ".join(candles_mod.TIMEFRAMES)))
        try:
            cc_price = float(rest[1])
        except ValueError:
            raise ParseError("cc guard price is not a number")

    return TradeArgs(entry=entry, sl=sl, widen=widen_raw.lower() == "y",
                     rr=rr, risk_pct=risk_pct, account=account,
                     risk_usd=risk_usd, smart_sl=smart_sl,
                     cc_timeframe=cc_timeframe, cc_price=cc_price,
                     broadcast=broadcast)


def _is_number(token: str) -> bool:
    try:
        float(token)
        return True
    except ValueError:
        return False


def _pop_broadcast(tokens: list) -> bool:
    """Strip a trailing --all / -all flag from the token list.

    Returns True if the flag was present. Mutates `tokens` in place so
    callers can build the message from the remaining words.
    """
    if not tokens:
        return False
    last = tokens[-1]
    if last.lower() in ("--all", "-all", "--broadcast", "-a"):
        tokens.pop()
        return True
    return False


@dataclass(frozen=True)
class CandleAlertArgs:
    timeframe: str
    target: float
    direction: str     # ABOVE | BELOW
    symbol: str
    message: str
    broadcast: bool = False


def parse_cc_alert(text: str, default_symbol: str) -> CandleAlertArgs:
    """/ccalert <tf> <price> <above|below> [symbol] [notes...]

    Timeframe must be a known cTrader period. Direction is above|below.
    The 4th token is a symbol only if it looks like one (ALL-CAPS or a
    known symbol); otherwise it's folded into the notes.
    """
    tokens = text.split()[1:]
    if len(tokens) < 3:
        raise ParseError("usage")
    timeframe = tokens[0].upper()
    if timeframe not in candles_mod.TIMEFRAMES:
        raise ParseError("timeframe")
    try:
        target = float(tokens[1])
    except ValueError:
        raise ParseError("usage")
    direction = tokens[2].upper()
    if direction not in (alerts_mod.CANDLE_ABOVE, alerts_mod.CANDLE_BELOW):
        raise ParseError("direction")

    rest = tokens[3:]
    broadcast = _pop_broadcast(rest)
    symbol = default_symbol
    if rest and SYMBOL_RE.match(rest[0].upper()) and rest[0].isupper():
        symbol = rest[0].upper()
        rest = rest[1:]
    message = " ".join(rest) if rest else "timeframe candle target reached."
    return CandleAlertArgs(timeframe=timeframe, target=target,
                           direction=direction, symbol=symbol,
                           message=message, broadcast=broadcast)


class Handlers:
    """Binds commands to services. Trading arrives with Phase 3; /m and /p
    reply with a migration notice until then."""

    def __init__(self, settings: Settings, store: alerts_mod.AlertStore,
                 feed, trade_symbol: str, trader=None,
                 candle_store=None, candle_feed=None,
                 subscription_store: Optional[SubscriptionStore] = None):
        # feed: FeedService -- async ensure(symbol) -> Optional[(bid, ask)]
        # trader: TradingService (None only in Phase-2-era wiring/tests)
        # candle_store: CandleAlertStore; candle_feed: CandleFeed
        # subscription_store: dynamic chat allow-list
        self._settings = settings
        self._store = store
        self._feed = feed
        self._symbol = trade_symbol
        self._trader = trader
        self._candle_store = candle_store
        self._candle_feed = candle_feed
        self._subscriptions = subscription_store

    def register(self, app: Application) -> None:
        # un-gated commands
        app.add_handler(CommandHandler("subscribe", self.subscribe))
        app.add_handler(CommandHandler("unsubscribe", self.unsubscribe))
        # gated commands
        app.add_handler(CommandHandler("alert", self.alert))
        app.add_handler(CommandHandler("list", self.list_))
        app.add_handler(CommandHandler("cancel", self.cancel))
        app.add_handler(CommandHandler("help", self.help_))
        app.add_handler(CommandHandler("m", self.market_order))
        app.add_handler(CommandHandler("p", self.pending_order))
        app.add_handler(CommandHandler("orders", self.orders))
        app.add_handler(CommandHandler("positions", self.positions))
        app.add_handler(CommandHandler("close_all", self.close_all))
        app.add_handler(CommandHandler("close", self.close_position))
        app.add_handler(CommandHandler("cancel_order", self.cancel_order))
        app.add_handler(CommandHandler("be", self.breakeven))
        app.add_handler(CommandHandler("ccalert", self.cc_alert))
        app.add_handler(CommandHandler("cclist", self.cc_list))
        app.add_handler(CommandHandler("cccancel", self.cc_cancel))

    def _allowed(self, update: Update) -> bool:
        chat = update.effective_chat
        if chat is None:
            log.warning("ignored command from chat with no effective_chat")
            return False
        # static env list
        if chat.id in self._settings.allowed_chat_ids:
            return True
        # dynamic subscription store
        if self._subscriptions is not None \
                and self._subscriptions.is_allowed(chat.id):
            return True
        log.warning("ignored command from unauthorized chat %s", chat.id)
        return False

    @staticmethod
    async def _reply(update: Update, text: str) -> None:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML)

    # ------------------------------------------------------------------
    # subscribe / unsubscribe (NOT gated -- anyone can call)
    # ------------------------------------------------------------------
    async def subscribe(self, update: Update,
                        _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self._subscriptions is None:
            await self._reply(update, "subscriptions are not wired up.")
            return
        chat = update.effective_chat
        if chat is None:
            return
        if self._subscriptions.add(chat.id):
            log.info("chat %d subscribed via /subscribe", chat.id)
            await self._reply(update, fmt.subscribed(chat.id))
        else:
            await self._reply(update, fmt.already_subscribed(chat.id))

    async def unsubscribe(self, update: Update,
                          _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self._subscriptions is None:
            await self._reply(update, "subscriptions are not wired up.")
            return
        chat = update.effective_chat
        if chat is None:
            return
        if self._subscriptions.remove(chat.id):
            log.info("chat %d unsubscribed via /unsubscribe", chat.id)
            await self._reply(update, fmt.unsubscribed(chat.id))
        else:
            await self._reply(update, fmt.not_subscribed(chat.id))

    # ------------------------------------------------------------------
    async def alert(self, update: Update,
                    _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        try:
            args = parse_alert(update.effective_message.text, self._symbol,
                               self._feed.known_symbols())
        except ParseError:
            await self._reply(update, fmt.alert_usage())
            return

        quote = await self._feed.ensure(args.symbol)
        if quote is None:
            await self._reply(update, fmt.price_fetch_error(args.symbol))
            return
        bid, ask = quote
        live_mid = (bid + ask) / 2.0

        direction = alerts_mod.infer_direction(live_mid, args.target)
        alert = self._store.create(update.effective_chat.id, args.symbol,
                                   args.target, direction, args.message,
                                   broadcast=args.broadcast)
        log.info("alert %s created: %s %s %s (broadcast=%s)", alert.id,
                 alert.symbol, alert.target, alert.direction,
                 alert.broadcast)
        await self._reply(update, fmt.alert_set(alert, live_mid))

    async def list_(self, update: Update,
                    _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        rows = self._store.for_chat(update.effective_chat.id)
        await self._reply(update,
                          fmt.alert_list(rows) if rows else fmt.no_alerts())

    async def cancel(self, update: Update,
                     _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        tokens = update.effective_message.text.split()
        if len(tokens) < 2:
            await self._reply(update, fmt.cancel_usage())
            return
        alert_id = tokens[1]
        if self._store.cancel(alert_id, update.effective_chat.id):
            await self._reply(update, fmt.cancelled(alert_id.upper()))
        else:
            await self._reply(update, fmt.cancel_not_found(alert_id.upper()))

    async def help_(self, update: Update,
                    _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        balances = {}
        if self._trader is not None:
            for shortcode in self._settings.accounts:
                try:
                    balances[shortcode] = await self._trader.balance(
                        shortcode)
                except (TradeRejected, CTraderError, Exception):
                    balances[shortcode] = None
        await self._reply(
            update, fmt.help_text(self._settings.accounts.keys(), balances))

    async def orders(self, update: Update,
                     _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._positions_or_orders(update, is_positions=False)

    async def positions(self, update: Update,
                        _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._positions_or_orders(update, is_positions=True)

    async def _positions_or_orders(self, update: Update,
                                   is_positions: bool) -> None:
        if not self._allowed(update):
            return
        tokens = update.effective_message.text.split()
        if len(tokens) < 2:
            await self._reply(
                update, fmt.positions_usage(is_positions))
            return
        account = tokens[1]
        try:
            rows = await self._trader.positions_or_orders(
                account, is_positions)
        except TradeRejected as e:
            await self._reply(update, str(e))
            return
        except CTraderError as e:
            await self._reply(update, fmt.positions_error(
                account, e.description))
            return
        except Exception:
            log.exception("positions/orders fetch failed")
            await self._reply(update, fmt.positions_error(
                account, "internal error"))
            return
        await self._reply(update, fmt.positions_list(rows, is_positions))

    async def close_all(self, update: Update,
                        _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        tokens = update.effective_message.text.split()
        if len(tokens) < 2:
            await self._reply(update, fmt.close_all_usage())
            return
        account = tokens[1]
        try:
            results = await self._trader.close_all(account)
        except TradeRejected as e:
            await self._reply(update, str(e))
            return
        except CTraderError as e:
            await self._reply(update, fmt.positions_error(
                account, e.description))
            return
        except Exception:
            log.exception("close_all failed")
            await self._reply(update, fmt.positions_error(
                account, "internal error"))
            return
        # cancel auto TP/SL alerts for every closed position
        for r in results:
            if r.get("ok") and r.get("id") is not None:
                self._store.cancel_auto(str(r["id"]))
                if self._candle_store is not None:
                    self._candle_store.cancel_for_position(int(r["id"]))
        await self._reply(update, fmt.close_all_result(account, results))

    async def close_position(self, update: Update,
                             _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        tokens = update.effective_message.text.split()
        if len(tokens) < 3:
            await self._reply(update, fmt.close_usage())
            return
        try:
            position_id = int(tokens[1])
        except ValueError:
            await self._reply(update, fmt.close_usage())
            return
        account = tokens[2]
        try:
            await self._trader.close_position(account, position_id)
        except TradeRejected as e:
            await self._reply(update, str(e))
            return
        except CTraderError as e:
            await self._reply(update, fmt.close_error(
                account, position_id, e.description))
            return
        except Exception:
            log.exception("close position failed")
            await self._reply(update, fmt.close_error(
                account, position_id, "internal error"))
            return
        # cancel auto TP/SL alerts tied to the closed position
        self._store.cancel_auto(str(position_id))
        if self._candle_store is not None:
            n = self._candle_store.cancel_for_position(position_id)
            if n:
                log.info("cancelled %d cc guard(s) for position %d", n, position_id)
        await self._reply(update, fmt.close_success(account, position_id))

    async def cancel_order(self, update: Update,
                           _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        tokens = update.effective_message.text.split()
        if len(tokens) < 3:
            await self._reply(update, fmt.cancel_order_usage())
            return
        try:
            order_id = int(tokens[1])
        except ValueError:
            await self._reply(update, fmt.cancel_order_usage())
            return
        account = tokens[2]
        try:
            await self._trader.cancel_order(account, order_id)
        except TradeRejected as e:
            await self._reply(update, str(e))
            return
        except CTraderError as e:
            await self._reply(update, fmt.cancel_order_error(
                account, order_id, e.description))
            return
        except Exception:
            log.exception("cancel order failed")
            await self._reply(update, fmt.cancel_order_error(
                account, order_id, "internal error"))
            return
        # cancel the auto entry alert tied to the cancelled pending order
        self._store.cancel_auto(str(order_id))
        await self._reply(update, fmt.cancel_order_success(account, order_id))

    async def breakeven(self, update: Update,
                        _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        tokens = update.effective_message.text.split()
        if len(tokens) < 2:
            await self._reply(update, fmt.breakeven_usage())
            return
        account = tokens[1]
        try:
            results = await self._trader.breakeven(account)
        except TradeRejected as e:
            await self._reply(update, str(e))
            return
        except CTraderError as e:
            await self._reply(update, fmt.positions_error(
                account, e.description))
            return
        except Exception:
            log.exception("breakeven failed")
            await self._reply(update, fmt.positions_error(
                account, "internal error"))
            return
        await self._reply(update, fmt.breakeven_result(account, results))

    async def cc_alert(self, update: Update,
                       _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        if self._candle_store is None or self._candle_feed is None:
            await self._reply(update, "candle alerts are not wired up.")
            return
        try:
            args = parse_cc_alert(update.effective_message.text, self._symbol)
        except ParseError:
            await self._reply(update, fmt.candle_alert_usage())
            return

        last_close = await self._candle_feed.last_close(
            args.symbol, args.timeframe)
        if last_close is None:
            await self._reply(update, fmt.price_fetch_error(args.symbol))
            return

        alert = self._candle_store.create(
            update.effective_chat.id, args.symbol, args.timeframe,
            args.target, args.direction, args.message,
            broadcast=args.broadcast)
        log.info("candle alert %s created: %s %s %s %s (broadcast=%s)",
                 alert.id, alert.symbol, alert.timeframe, alert.direction,
                 alert.target, alert.broadcast)
        await self._reply(update, fmt.candle_alert_set(alert, last_close))

    async def cc_list(self, update: Update,
                      _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        if self._candle_store is None:
            await self._reply(update, "candle alerts are not wired up.")
            return
        rows = self._candle_store.for_chat(update.effective_chat.id)
        await self._reply(update, fmt.candle_alert_list(rows))

    async def cc_cancel(self, update: Update,
                        _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        if self._candle_store is None:
            await self._reply(update, "candle alerts are not wired up.")
            return
        tokens = update.effective_message.text.split()
        if len(tokens) < 2:
            await self._reply(update, fmt.candle_cancel_usage())
            return
        alert_id = tokens[1]
        if self._candle_store.cancel(alert_id, update.effective_chat.id):
            await self._reply(update, fmt.candle_cancelled(alert_id.upper()))
        else:
            await self._reply(
                update, fmt.candle_cancel_not_found(alert_id.upper()))

    async def market_order(self, update: Update,
                           _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._trade(update, is_market=True)

    async def pending_order(self, update: Update,
                            _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._trade(update, is_market=False)

    async def _trade(self, update: Update, is_market: bool) -> None:
        if not self._allowed(update):
            return
        try:
            args = parse_trade(update.effective_message.text, is_market)
        except ParseError:
            await self._reply(update, fmt.trade_usage(is_market))
            return

        kind_label = "MARKET" if is_market else "PENDING"
        try:
            plan, symbol, result, lots = await self._trader.execute(
                args, is_market, risk_usd=args.risk_usd)
        except TradeRejected as e:
            await self._reply(update, str(e))
            return
        except CTraderError as e:
            await self._reply(update, fmt.order_failed(
                self._symbol, "?", kind_label, args.account,
                e.description, e.error_code))
            return
        except Exception as e:
            log.exception("order flow failed")
            await self._reply(update, fmt.order_failed(
                self._symbol, "?", kind_label, args.account,
                str(e), "unknown"))
            return

        if is_market:
            entry_label = fmt.entry_label_market(plan.entry_ref,
                                                 symbol.digits)
        else:
            entry_label = fmt.entry_label_pending(
                plan.entry_ref, plan.placement_price, symbol.digits)

        await self._reply(update, fmt.order_success(
            ticket=result.order_id, symbol=symbol.name,
            direction=plan.direction, kind_label=kind_label,
            account=args.account, lots=lots, risk_pct=args.risk_pct,
            risk_usd=plan.risk_usd, entry_label=entry_label,
            sl=plan.sl, tp=plan.tp, rr=args.rr,
            widen_label=plan.widen_label, digits=symbol.digits,
            dollar_risk=args.risk_usd is not None,
            smart_sl=plan.smart_sl, smart_risk_pct=plan.smart_risk_pct))

        if is_market:
            # Market fills are already in: the engine's entry alert would
            # never fire on MID (entry = ask for BUY / bid for SELL), so
            # skip it and create the TP/SL alerts for the open position now.
            trade_id = str(result.position_id or result.order_id) \
                if (result.position_id or result.order_id) else ""
            self._create_tp_sl_alerts(update, plan, symbol.name, trade_id,
                                      args.account,
                                      broadcast=args.broadcast)
            if args.cc_timeframe and args.cc_price is not None:
                if self._candle_store is not None and self._candle_feed is not None:
                    await self._attach_cc_guard(update, plan, args, result)
                else:
                    await self._reply(update,
                        "warning: cc guard requested but candle feed is not available.")
        else:
            # Pending: store guard params on entry alert; guard created in on_entry_hit
            self._create_entry_alert(update, plan, symbol.name, result,
                                     args.account,
                                     cc_timeframe=args.cc_timeframe,
                                     cc_price=args.cc_price,
                                     broadcast=args.broadcast)
            if args.cc_timeframe:
                await self._reply(update, fmt.cc_guard_pending(args.cc_timeframe, args.cc_price))

    def _create_entry_alert(self, update: Update, plan, symbol_name: str,
                            result, account: str,
                            cc_timeframe=None, cc_price=None,
                            broadcast=False) -> None:
        """Create an 'entry' auto-alert for a just-placed order. The engine
        fires it when the entry is hit, confirms the position, then creates
        TP/SL alerts broadcast to all subscribers."""
        trade_id = str(result.position_id or result.order_id)
        if not trade_id or trade_id == "0":
            trade_id = ""
        direction = alerts_mod.direction_for_side(plan.direction, "entry")
        target = plan.placement_price if plan.placement_price is not None else plan.entry_ref
        alert = self._store.create(
            update.effective_chat.id, symbol_name, target, direction,
            "auto entry %s %s" % (plan.direction, symbol_name),
            kind=alerts_mod.KIND_ENTRY, trade_id=trade_id,
            account=account,
            cc_timeframe=cc_timeframe or "",
            cc_price=cc_price or 0.0,
            broadcast=broadcast)
        log.info("auto entry alert %s created: %s %s trade=%s",
                 alert.id, alert.symbol, alert.target, trade_id)

    def _create_tp_sl_alerts(self, update: Update, plan, symbol_name: str,
                             trade_id: str, account: str,
                             broadcast=False) -> None:
        """Create TP and SL auto-alerts for an already-open position
        (market orders). Mirrors the engine's on_entry_hit flow so the
        alert chain is identical whether the fill is instant (market) or
        happens later on a pending entry."""
        if not trade_id:
            log.warning("tp/sl alerts skipped: no trade id for %s %s",
                        plan.direction, symbol_name)
            return
        tp = alerts_mod.direction_for_side(plan.direction, "tp")
        sl = alerts_mod.direction_for_side(plan.direction, "sl")
        self._store.create(
            update.effective_chat.id, symbol_name, plan.tp, tp,
            "auto TP hit for trade %s" % trade_id,
            kind=alerts_mod.KIND_TP, trade_id=trade_id,
            account=account, broadcast=broadcast)
        self._store.create(
            update.effective_chat.id, symbol_name, plan.sl, sl,
            "auto SL hit for trade %s" % trade_id,
            kind=alerts_mod.KIND_SL, trade_id=trade_id,
            account=account, broadcast=broadcast)
        log.info("auto TP/SL alerts %s/%s created for %s %s trade=%s",
                 tp, sl, plan.direction, symbol_name, trade_id)

    async def _attach_cc_guard(self, update: Update, plan,
                               args: TradeArgs, result) -> None:
        """Create the CC guard for a market order right after confirming the
        position exists."""
        # Fast path: the fill's positionId is already known (ExecutionEvent).
        position = None
        if result.position is not None and result.position_id:
            position = result.position
            position["positionId"] = result.position_id
        if position is None:
            try:
                position = await self._trader.confirm_position(
                    args.account, self._symbol, plan.direction,
                    plan.entry_ref,
                    str(result.order_id) if result.order_id else "")
            except Exception:
                log.exception("cc guard: confirm_position failed for %s", args.account)
                await self._reply(update,
                    "trade placed, but cc guard could not be set "
                    "(position confirm failed \u2014 use /ccalert manually).")
                return

        if position is None:
            await self._reply(update,
                "trade placed, but cc guard could not be set "
                "(position not found after fill \u2014 use /ccalert manually).")
            return

        position_id = int(position.get("positionId", 0))
        if position_id == 0:
            await self._reply(update,
                "trade placed, but cc guard could not be set (no positionId).")
            return

        # BUY: guard fires if candle closes BELOW threshold
        # SELL: guard fires if candle closes ABOVE threshold
        cc_direction = (alerts_mod.CANDLE_BELOW
                        if plan.direction == "BUY"
                        else alerts_mod.CANDLE_ABOVE)

        alert = self._candle_store.create(
            update.effective_chat.id, self._symbol, args.cc_timeframe,
            args.cc_price, cc_direction,
            "cc guard for position %d" % position_id,
            action="close", position_id=position_id, account=args.account)

        self._candle_feed.add_symbol(self._symbol, args.cc_timeframe)
        log.info("cc guard %s: pos=%d %s %s close-%s %.5f",
                 alert.id, position_id, self._symbol, args.cc_timeframe,
                 cc_direction, args.cc_price)
        await self._reply(update, fmt.cc_guard_set(alert))
