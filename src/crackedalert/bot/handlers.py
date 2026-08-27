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
from ..fvg import candle_high, candle_low
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
    smart_sl: Optional[float] = None      # soft candle-close stop level
    smart_sl_tf: Optional[str] = None     # its timeframe (required with smart_sl)
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
    """/m <sl> <widen> <rr> <risk%> <account> [--smart-sl <price> <tf>] [--all]
       /p <entry> <sl> <widen> <rr> <risk%> <account> [--smart-sl <price> <tf>] [--all]
    --smart-sl <price> <tf> arms a SOFT candle-close stop: when a <tf>
    candle CLOSES past <price> (below for longs, above for shorts), the
    position is closed at market. The broker-side SL stays at the original
    level and lots always anchor to it, so the risk at the smart level is
    the stated risk% scaled by the distance ratio. <price> must sit
    between the fill and the original SL (validated pre-placement).
    """
    tokens = text.split()[1:]
    broadcast = _pop_broadcast(tokens)
    base = 5 if is_market else 6
    if len(tokens) not in (base, base + 3):
        raise ParseError(
            "expected %d arguments (or %d with --smart-sl <price> <tf>), "
            "got %d" % (base, base + 3, len(tokens)))
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

    # Optional soft candle-close stop: --smart-sl <price> <tf>.
    rest = tokens[base:]
    smart_sl = None
    smart_sl_tf = None
    if rest and rest[0].lower() in ("--smart-sl", "-ss", "--smartsl"):
        if len(rest) < 3:
            raise ParseError(
                "expected a price AND a timeframe after --smart-sl "
                "(e.g. --smart-sl 4613.23 M5)")
        try:
            smart_sl = float(rest[1])
        except ValueError:
            raise ParseError("smart SL price is not a number")
        smart_sl_tf = rest[2].upper()
        if smart_sl_tf not in candles_mod.SMART_SL_TIMEFRAMES:
            raise ParseError(
                "smart SL timeframe '%s' is not valid. Use: %s"
                % (smart_sl_tf, " ".join(candles_mod.SMART_SL_TIMEFRAMES)))
        rest = rest[3:]
        if rest:
            raise ParseError("unexpected extra arguments after --smart-sl")
    if rest:
        raise ParseError("unexpected trailing arguments")

    return TradeArgs(entry=entry, sl=sl, widen=widen_raw.lower() == "y",
                     rr=rr, risk_pct=risk_pct, account=account,
                     risk_usd=risk_usd, smart_sl=smart_sl,
                     smart_sl_tf=smart_sl_tf,
                     broadcast=broadcast)


def parse_guard(text: str) -> tuple:
    """/guard <position_id> <price> <tf> [--all]

    Attaches a candle-close guard to an EXISTING position: when a <tf>
    candle CLOSES past <price> (below for a BUY, above for a SELL), the
    position is closed at market. Same soft-stop semantics as --smart-sl,
    but for a position that is already open. --all broadcasts the guard.
    """
    tokens = text.split()
    if len(tokens) < 2:
        raise ParseError("expected /guard <position_id> <price> <tf> [--all]")
    rest = tokens[1:]
    broadcast = _pop_broadcast(rest)
    if len(rest) != 3:
        raise ParseError("expected /guard <position_id> <price> <tf> [--all]")
    try:
        position_id = int(rest[0])
    except ValueError:
        raise ParseError("position id is not a number")
    try:
        price = float(rest[1])
    except ValueError:
        raise ParseError("guard price is not a number")
    tf = rest[2].upper()
    if tf not in candles_mod.TIMEFRAMES:
        raise ParseError(
            "guard timeframe '%s' is not valid. Use: %s"
            % (tf, " ".join(candles_mod.TIMEFRAMES)))
    return position_id, price, tf, broadcast


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
                 subscription_store: Optional[SubscriptionStore] = None,
                 pending_cc: Optional[dict] = None,
                 imbalance_checker=None):
        # feed: FeedService -- async ensure(symbol) -> Optional[(bid, ask)]
        # trader: TradingService (None only in Phase-2-era wiring/tests)
        # candle_store: CandleAlertStore; candle_feed: CandleFeed
        # subscription_store: dynamic chat allow-list
        # pending_cc: shared dict (main.py) registering pending-order cc
        #   guard params; materialized on fill by main.on_execution
        # imbalance_checker: main.py's imbalance_verdict() for /imbalance
        self._settings = settings
        self._store = store
        self._feed = feed
        self._symbol = trade_symbol
        self._trader = trader
        self._candle_store = candle_store
        self._candle_feed = candle_feed
        self._subscriptions = subscription_store
        self._pending_cc = pending_cc
        self._imbalance_checker = imbalance_checker

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
        app.add_handler(CommandHandler("guard", self.guard))
        app.add_handler(CommandHandler("imbalance", self.imbalance))
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
        await self._reply(update, fmt.help_text())

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
        # clean up cc guards for every closed position
        for r in results:
            if r.get("ok") and r.get("id") is not None:
                if self._candle_store is not None:
                    self._candle_store.cancel_for_position(int(r["id"]))
        self._sync_candle_feed()
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
        # clean up any cc guard tied to the closed position
        if self._candle_store is not None:
            n = self._candle_store.cancel_for_position(position_id)
            if n:
                log.info("cancelled %d cc guard(s) for position %d", n, position_id)
            self._sync_candle_feed()
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

    def _sync_candle_feed(self) -> None:
        """Drop candle-feed keys the store no longer needs, so stale
        guards (fired, cancelled, or position closed) stop the 10s
        trendbar polling instead of leaking forever."""
        if self._candle_store is not None and self._candle_feed is not None:
            self._candle_feed.sync_keys(self._candle_store.active_keys())

    async def guard(self, update: Update,
                    _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Attach a candle-close guard to an existing open position.

        Finds the position across all configured accounts, derives the
        close direction from its side (BUY closes below, SELL closes
        above), and registers the guard like --smart-sl does at trade
        time. The broker-side SL/TP is untouched.
        """
        if not self._allowed(update):
            return
        if self._candle_store is None or self._candle_feed is None:
            await self._reply(update, "candle alerts are not wired up.")
            return
        try:
            position_id, price, tf, broadcast = parse_guard(
                update.effective_message.text)
        except ParseError:
            await self._reply(update, fmt.guard_usage())
            return
        for shortcode in self._settings.accounts:
            try:
                rows = await self._trader.positions_or_orders(
                    shortcode, is_positions=True)
            except (TradeRejected, CTraderError):
                continue
            for row in rows:
                if int(row.get("id", 0) or 0) != position_id:
                    continue
                side = row.get("side", "")
                symbol = row.get("symbol", "")
                cc_direction = (alerts_mod.CANDLE_BELOW
                                if side == "BUY"
                                else alerts_mod.CANDLE_ABOVE)
                alert = self._candle_store.create(
                    update.effective_chat.id, symbol, tf, price,
                    cc_direction,
                    "cc guard for position %d" % position_id,
                    action="close", position_id=position_id,
                    account=shortcode, broadcast=broadcast)
                self._candle_feed.add_symbol(symbol, tf)
                log.info("cc guard %s attached to position %d (%s %s)",
                         alert.id, position_id, symbol, side)
                await self._reply(update, fmt.cc_guard_set(alert))
                return
        await self._reply(update,
                          "position %d not found." % position_id)

    async def imbalance(self, update: Update,
                        _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Debug: run the H1 imbalance check on demand and report what the
        last three completed candles evaluate to. Read-only -- no alerts."""
        if not self._allowed(update):
            return
        if self._imbalance_checker is None:
            await self._reply(update, "imbalance checker is not wired up.")
            return
        try:
            verdict = await self._imbalance_checker()
        except Exception as e:
            await self._reply(update, "imbalance check failed: %s" % e)
            return
        lines = ["last 3 completed H1 candles (ts / low / high):"]
        for b in verdict["bars"][-3:]:
            lines.append("  %s / %.2f / %.2f"
                         % (b.get("utcTimestampInMinutes"),
                            candle_low(b), candle_high(b)))
        if verdict["which"] is None:
            lines.append("no fresh imbalance on these candles.")
        else:
            lines.append("fresh %s imbalance on H1" % verdict["which"])
            lines.append("candle-1 levels: high1=%.2f low1=%.2f"
                         % (verdict["high1"], verdict["low1"]))
        await self._reply(update, "\n".join(lines))

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
            self._sync_candle_feed()
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
            smart_sl=plan.smart_sl, smart_risk_usd=plan.smart_risk_usd,
            smart_risk_pct=plan.smart_risk_pct))

        if args.smart_sl is not None:
            if self._candle_store is None or self._candle_feed is None:
                await self._reply(update,
                    "warning: smart stop requested but the candle feed "
                    "is not available.")
                return
            if not is_market and self._pending_cc is None:
                await self._reply(update,
                    "warning: guard requested but the fill hook is "
                    "unavailable -- use /guard after the fill.")
                return
            await self._attach_cc_guard(update, plan, args, result, is_market)

    async def _attach_cc_guard(self, update: Update, plan,
                               args: TradeArgs, result,
                               is_market: bool) -> None:
        """Attach the --smart-sl close-guard to a trade.

        --smart-sl <price> <tf> arms a SOFT candle-close stop (broker SL
        stays at the original anchor). Market orders carry their position
        in the placement response, so the guard is created immediately.
        Pending orders register the params in the shared pending_cc
        registry; main.on_execution materializes the guard when the
        fill's ExecutionEvent arrives.
        """
        tf = args.smart_sl_tf
        guard_price = args.smart_sl

        if not is_market:
            self._pending_cc[str(result.order_id)] = {
                "chat_id": update.effective_chat.id,
                "symbol": self._symbol,
                "timeframe": tf,
                "cc_price": guard_price,
                "direction": plan.direction,
                "account": args.account,
                "broadcast": args.broadcast,
            }
            await self._reply(update,
                              fmt.cc_guard_pending(tf, guard_price))
            return

        # Market fast path: the fill's positionId is already known.
        if result.position is None or not result.position_id:
            log.warning("cc guard: no positionId on market fill for %s",
                        args.account)
            await self._reply(update,
                "trade placed, but the guard could not be set "
                "(no positionId on the fill \u2014 use /ccalert manually).")
            return
        position = dict(result.position)
        position["positionId"] = result.position_id

        position_id = int(position.get("positionId", 0))
        if position_id == 0:
            await self._reply(update,
                "trade placed, but the guard could not be set (no positionId).")
            return

        # BUY: guard fires if candle closes BELOW threshold
        # SELL: guard fires if candle closes ABOVE threshold
        cc_direction = (alerts_mod.CANDLE_BELOW
                        if plan.direction == "BUY"
                        else alerts_mod.CANDLE_ABOVE)

        alert = self._candle_store.create(
            update.effective_chat.id, self._symbol, tf,
            guard_price, cc_direction,
            "cc guard for position %d" % position_id,
            action="close", position_id=position_id, account=args.account)

        self._candle_feed.add_symbol(self._symbol, tf)
        log.info("cc guard %s: pos=%d %s %s close-%s %.5f",
                 alert.id, position_id, self._symbol, tf,
                 cc_direction, guard_price)
        await self._reply(update, fmt.cc_guard_set(alert))
