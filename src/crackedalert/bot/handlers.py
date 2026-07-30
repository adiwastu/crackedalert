"""Telegram command handlers.

Parsing lives in pure functions (unit-testable, no I/O). The Handlers
class binds them to the live services. Every handler is gated on
ALLOWED_CHAT_IDS -- unknown chats are logged and ignored (the bash bot
accepted commands from anyone; that hole is closed here).
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
from ..ctrader.client import CTraderError
from ..ctrader.trading import TradeRejected
from . import formatting as fmt

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


@dataclass(frozen=True)
class TradeArgs:
    entry: Optional[float]     # None for market orders
    sl: float
    widen: bool
    rr: float
    risk_pct: float
    account: str


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
    symbol = default_symbol
    if rest and not _is_number(rest[0]) and SYMBOL_RE.match(rest[0].upper()):
        candidate = rest[0].upper()
        looks_known = known_symbols and candidate in known_symbols
        looks_deliberate = not known_symbols and rest[0].isupper()
        if looks_known or looks_deliberate:
            symbol = candidate
            rest = rest[1:]
    message = " ".join(rest) if rest else DEFAULT_ALERT_MESSAGE
    return AlertArgs(target=target, symbol=symbol, message=message)


def parse_trade(text: str, is_market: bool) -> TradeArgs:
    """/m <sl> <widen> <rr> <risk%> <account>
       /p <entry> <sl> <widen> <rr> <risk%> <account>"""
    tokens = text.split()[1:]
    expected = 5 if is_market else 6
    if len(tokens) != expected:
        raise ParseError("expected %d arguments, got %d"
                         % (expected, len(tokens)))
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
        risk_pct = float(risk_raw)
    except ValueError:
        raise ParseError("numeric argument is not a number")

    if widen_raw.lower() not in ("y", "n"):
        raise ParseError("widen must be y or n")
    if rr <= 0 or risk_pct <= 0:
        raise ParseError("rr and risk%% must be positive")

    return TradeArgs(entry=entry, sl=sl, widen=widen_raw.lower() == "y",
                     rr=rr, risk_pct=risk_pct, account=account)


def _is_number(token: str) -> bool:
    try:
        float(token)
        return True
    except ValueError:
        return False


class Handlers:
    """Binds commands to services. Trading arrives with Phase 3; /m and /p
    reply with a migration notice until then."""

    def __init__(self, settings: Settings, store: alerts_mod.AlertStore,
                 feed, trade_symbol: str, trader=None):
        # feed: FeedService -- async ensure(symbol) -> Optional[(bid, ask)]
        # trader: TradingService (None only in Phase-2-era wiring/tests)
        self._settings = settings
        self._store = store
        self._feed = feed
        self._symbol = trade_symbol
        self._trader = trader

    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler("alert", self.alert))
        app.add_handler(CommandHandler("list", self.list_))
        app.add_handler(CommandHandler("cancel", self.cancel))
        app.add_handler(CommandHandler("help", self.help_))
        app.add_handler(CommandHandler("m", self.market_order))
        app.add_handler(CommandHandler("p", self.pending_order))

    def _allowed(self, update: Update) -> bool:
        chat = update.effective_chat
        if chat is None or chat.id not in self._settings.allowed_chat_ids:
            log.warning("ignored command from unauthorized chat %s",
                        None if chat is None else chat.id)
            return False
        return True

    @staticmethod
    async def _reply(update: Update, text: str) -> None:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML)

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
                                   args.target, direction, args.message)
        log.info("alert %s created: %s %s %s", alert.id, alert.symbol,
                 alert.target, alert.direction)
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
        await self._reply(update,
                          fmt.help_text(self._settings.accounts.keys()))

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
                args, is_market)
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
            widen_label=plan.widen_label, digits=symbol.digits))
