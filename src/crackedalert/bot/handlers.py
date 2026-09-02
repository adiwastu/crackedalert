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
    cancel_price: Optional[float] = None  # /p: cancel if price touches this pre-fill
    broadcast: bool = False


# Named-flag aliases for the positional trade parameters.
_TRADE_FLAG_NAMES = {
    "--entry": "entry", "-e": "entry",
    "--sl": "sl", "--stop": "sl",
    "--widen": "widen", "-w": "widen",
    "--rr": "rr", "--risk-reward": "rr",
    "--risk": "risk",
    "--account": "account", "--acct": "account",
}
_BROADCAST_FLAGS = ("--all", "-all", "--broadcast", "-a")


def parse_trade(text: str, is_market: bool) -> TradeArgs:
    """/m <sl> <widen> <rr> <risk%> <account> [--smart-sl <price> <tf>] [--all]
       /p <entry> <sl> <widen> <rr> <risk%> <account> [--smart-sl <price> <tf>] [--cancel <price>] [--all]
    Named-flag mode (first option starts with '-'): any /m or /p parameter
    by name, e.g.
       /p --entry 2450 --sl 2455 --widen n --rr 3 --risk 1 --account 5k
         [--smart-sl <price> <tf>] [--cancel <price>] [--all]
    --smart-sl <price> <tf> arms a SOFT candle-close stop (see /m docs).
    --cancel <price> (pending orders only) sets a CANCEL CONDITION: if
    price touches <price> before the order fills, the order is cancelled.
    """
    tokens = text.split()[1:]
    broadcast = False
    kept = []
    for tok in tokens:
        if tok.lower() in _BROADCAST_FLAGS:
            broadcast = True
        else:
            kept.append(tok)
    tokens = kept
    if not tokens:
        raise ParseError(
            "expected %s arguments" % ("6" if is_market else "7"))
    if tokens[0].startswith("-"):
        return _parse_trade_flags(tokens, is_market, broadcast)
    return _parse_trade_positional(tokens, is_market, broadcast)


def _parse_trade_positional(tokens, is_market: bool,
                            broadcast: bool) -> TradeArgs:
    base = 5 if is_market else 6
    if len(tokens) < base:
        raise ParseError(
            "expected %d arguments (or %d with --smart-sl <price> <tf>), "
            "got %d" % (base, base + 3, len(tokens)))
    try:
        if is_market:
            entry = None
            sl_raw, widen_raw, rr_raw, risk_raw, account = (
                tokens[0], tokens[1], tokens[2], tokens[3], tokens[4])
        else:
            entry = float(tokens[0])
            sl_raw, widen_raw, rr_raw, risk_raw, account = (
                tokens[1], tokens[2], tokens[3], tokens[4], tokens[5])
    except ValueError:
        raise ParseError("numeric argument is not a number")

    rest = tokens[base:]
    smart_sl = None
    smart_sl_tf = None
    cancel_price = None
    i = 0
    while i < len(rest):
        low = rest[i].lower()
        if low in ("--smart-sl", "-ss", "--smartsl"):
            if i + 2 >= len(rest):
                raise ParseError(
                    "expected a price AND a timeframe after --smart-sl "
                    "(e.g. --smart-sl 4613.23 M5)")
            try:
                smart_sl = float(rest[i + 1])
            except ValueError:
                raise ParseError("smart SL price is not a number")
            smart_sl_tf = rest[i + 2].upper()
            if smart_sl_tf not in candles_mod.SMART_SL_TIMEFRAMES:
                raise ParseError(
                    "smart SL timeframe '%s' is not valid. Use: %s"
                    % (smart_sl_tf,
                       " ".join(candles_mod.SMART_SL_TIMEFRAMES)))
            i += 3
        elif low == "--cancel":
            if i + 1 >= len(rest):
                raise ParseError("expected a price after --cancel")
            try:
                cancel_price = float(rest[i + 1])
            except ValueError:
                raise ParseError("cancel price is not a number")
            i += 2
        else:
            raise ParseError("unexpected argument '%s'" % rest[i])
    return _finalize_trade_args(
        entry=entry, sl_raw=sl_raw, widen_raw=widen_raw, rr_raw=rr_raw,
        risk_raw=risk_raw, account=account, smart_sl=smart_sl,
        smart_sl_tf=smart_sl_tf, cancel_price=cancel_price,
        broadcast=broadcast, is_market=is_market)


def _parse_trade_flags(tokens, is_market: bool,
                       broadcast: bool) -> TradeArgs:
    vals = {}
    smart_sl = None
    smart_sl_tf = None
    cancel_price = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        low = tok.lower()
        if low in ("--smart-sl", "-ss", "--smartsl"):
            if i + 2 >= len(tokens):
                raise ParseError(
                    "expected a price AND a timeframe after --smart-sl "
                    "(e.g. --smart-sl 4613.23 M5)")
            try:
                smart_sl = float(tokens[i + 1])
            except ValueError:
                raise ParseError("smart SL price is not a number")
            smart_sl_tf = tokens[i + 2].upper()
            if smart_sl_tf not in candles_mod.SMART_SL_TIMEFRAMES:
                raise ParseError(
                    "smart SL timeframe '%s' is not valid. Use: %s"
                    % (smart_sl_tf,
                       " ".join(candles_mod.SMART_SL_TIMEFRAMES)))
            i += 3
        elif low == "--cancel":
            if i + 1 >= len(tokens):
                raise ParseError("expected a price after --cancel")
            try:
                cancel_price = float(tokens[i + 1])
            except ValueError:
                raise ParseError("cancel price is not a number")
            i += 2
        elif low in _TRADE_FLAG_NAMES:
            name = _TRADE_FLAG_NAMES[low]
            if i + 1 >= len(tokens):
                raise ParseError("expected a value after %s" % tok)
            vals[name] = tokens[i + 1]
            i += 2
        else:
            raise ParseError("unknown option '%s'" % tok)

    entry_raw = vals.get("entry")
    if is_market and entry_raw is not None:
        raise ParseError("--entry is only valid for pending orders (/p)")
    entry = float(entry_raw) if entry_raw is not None else None
    if not is_market and entry is None:
        raise ParseError("missing --entry (pending order needs an entry)")

    sl_raw = vals.get("sl")
    rr_raw = vals.get("rr")
    risk_raw = vals.get("risk")
    account = vals.get("account")
    missing = [name for name, val in
               (("--sl", sl_raw), ("--rr", rr_raw),
                ("--risk", risk_raw), ("--account", account))
               if val is None]
    if missing:
        raise ParseError("missing option(s): %s" % ", ".join(missing))
    widen_raw = vals.get("widen", "n")
    return _finalize_trade_args(
        entry=entry, sl_raw=sl_raw, widen_raw=widen_raw, rr_raw=rr_raw,
        risk_raw=risk_raw, account=account, smart_sl=smart_sl,
        smart_sl_tf=smart_sl_tf, cancel_price=cancel_price,
        broadcast=broadcast, is_market=is_market)


def _finalize_trade_args(entry, sl_raw, widen_raw, rr_raw, risk_raw,
                         account, smart_sl, smart_sl_tf, cancel_price,
                         broadcast, is_market: bool) -> TradeArgs:
    try:
        sl = float(sl_raw)
        rr = float(rr_raw)
    except (TypeError, ValueError):
        raise ParseError("numeric argument is not a number")
    if widen_raw.lower() not in ("y", "n"):
        raise ParseError("widen must be y or n")
    if rr <= 0:
        raise ParseError("rr and risk%% must be positive")
    if is_market and cancel_price is not None:
        raise ParseError("--cancel is only valid for pending orders (/p)")

    # Risk: "$50" means a dollar amount, "0.5" means a percentage.
    risk_usd = None
    risk_pct = 0.0
    if str(risk_raw).startswith("$"):
        try:
            risk_usd = float(str(risk_raw)[1:])
        except ValueError:
            raise ParseError("numeric argument is not a number")
        if risk_usd <= 0:
            raise ParseError("risk amount must be positive")
    else:
        try:
            risk_pct = float(risk_raw)
        except (TypeError, ValueError):
            raise ParseError("numeric argument is not a number")
        if risk_pct <= 0:
            raise ParseError("rr and risk%% must be positive")

    return TradeArgs(entry=entry, sl=sl, widen=widen_raw.lower() == "y",
                     rr=rr, risk_pct=risk_pct, account=account,
                     risk_usd=risk_usd, smart_sl=smart_sl,
                     smart_sl_tf=smart_sl_tf, cancel_price=cancel_price,
                     broadcast=broadcast)


def parse_alert(text: str, default_symbol: str,
                known_symbols=None) -> AlertArgs:
    """/alert <target> [symbol] [message...]
       /alert --price 2450 [--symbol XAUUSD] [--notes approaching demand] [--all]

    The bash parser treated the 2nd token as a symbol unconditionally,
    which broke the /help example ("/alert 2450.00 approaching demand").
    Here the 2nd token is a symbol only if the account actually offers it
    (known_symbols); before the first connection, only an ALL-CAPS token
    is accepted as a symbol. Flag form (first token starts with '-') is
    strict: every parameter is named, --notes carries the message.
    """
    tokens = text.split()[1:]
    if not tokens:
        raise ParseError("usage")
    if _flag_mode(tokens):
        vals, broadcast = _parse_named(
            tokens, {"--price": "price", "--target": "price",
                     "--symbol": "symbol",
                     "--notes": "notes", "--message": "notes"},
            required=("price",), multiword="notes")
        try:
            target = float(vals["price"])
        except ValueError:
            raise ParseError("alert price is not a number")
        symbol = default_symbol
        if vals.get("symbol"):
            candidate = vals["symbol"].upper()
            if not SYMBOL_RE.match(candidate):
                raise ParseError(
                    "symbol '%s' is not valid" % vals["symbol"])
            symbol = candidate
        message = vals.get("notes") or DEFAULT_ALERT_MESSAGE
        return AlertArgs(target=target, symbol=symbol, message=message,
                         broadcast=broadcast)
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


def parse_ocancel(text: str) -> tuple:
    """/ocancel <order_id> <price>
       /ocancel --id 4467051 --price 4612

    Guards an EXISTING unfilled pending order with a cancel condition:
    if price touches <price> before the order fills, the order is
    cancelled at the broker. Flag form: --id/--order and --price.
    """
    tokens = text.split()
    if not tokens or len(tokens) == 1:
        raise ParseError("expected /ocancel <order_id> <price>")
    rest = tokens[1:]
    if _flag_mode(rest):
        vals, _b = _parse_named(
            rest, {"--id": "id", "--order": "id", "--order-id": "id",
                   "--price": "price", "--cancel": "price"},
            required=("id", "price"))
        raw_id, raw_price = vals["id"], vals["price"]
    else:
        if len(tokens) != 3:
            raise ParseError("expected /ocancel <order_id> <price>")
        raw_id, raw_price = tokens[1], tokens[2]
    try:
        order_id = int(raw_id)
    except ValueError:
        raise ParseError("order id is not a number")
    try:
        price = float(raw_price)
    except ValueError:
        raise ParseError("cancel price is not a number")
    return order_id, price


def parse_guard(text: str) -> tuple:
    """/guard <position_id> <price> <tf> [--all]
       /guard --id 4467051 --price 4080 --tf H1 [--all]

    Attaches a candle-close guard to an EXISTING position: when a <tf>
    candle CLOSES past <price> (below for a BUY, above for a SELL), the
    position is closed at market. Same soft-stop semantics as --smart-sl,
    but for a position that is already open. --all broadcasts the guard.
    """
    tokens = text.split()
    if len(tokens) < 2:
        raise ParseError("expected /guard <position_id> <price> <tf> [--all]")
    rest = tokens[1:]
    if _flag_mode(rest):
        vals, broadcast = _parse_named(
            rest, {"--id": "id", "--position": "id",
                   "--position-id": "id",
                   "--price": "price",
                   "--tf": "tf", "--timeframe": "tf"},
            required=("id", "price", "tf"))
        raw_id, raw_price, raw_tf = vals["id"], vals["price"], vals["tf"]
    else:
        broadcast = _pop_broadcast(rest)
        if len(rest) != 3:
            raise ParseError(
                "expected /guard <position_id> <price> <tf> [--all]")
        raw_id, raw_price, raw_tf = rest
    try:
        position_id = int(raw_id)
    except ValueError:
        raise ParseError("position id is not a number")
    try:
        price = float(raw_price)
    except ValueError:
        raise ParseError("guard price is not a number")
    tf = raw_tf.upper()
    if tf not in candles_mod.TIMEFRAMES:
        raise ParseError(
            "guard timeframe '%s' is not valid. Use: %s"
            % (tf, " ".join(candles_mod.TIMEFRAMES)))
    return position_id, price, tf, broadcast


def _flag_mode(tokens: list) -> bool:
    """True when the first token starts a named-flag form (--name value).

    Broadcast aliases (-all, -a, ...) never count as flag mode, so a
    command like "/m ... -all" still parses positionally.
    """
    if not tokens:
        return False
    low = tokens[0].lower()
    return low.startswith("-") and low not in _BROADCAST_FLAGS


def _parse_named(tokens: list, names: dict, required: tuple = (),
                 multiword: Optional[str] = None) -> tuple:
    """Scan `--name value` pairs into {canonical: raw_value}.

    `names` maps lowercase flag spellings (aliases included) to the
    canonical parameter name. `required` are canonical names that must be
    present. `multiword` (e.g. notes) consumes everything after its flag
    as one value -- except a trailing broadcast flag, which is peeled off
    so "--all" never leaks into a message.

    Broadcast aliases are stripped wherever they appear. Unknown options
    and stray non-flag tokens are errors (flag mode is strict: every
    parameter is named).

    Returns (values, broadcast).
    """
    vals = {}
    broadcast = False
    i = 0
    while i < len(tokens):
        low = tokens[i].lower()
        if low in _BROADCAST_FLAGS:
            broadcast = True
            i += 1
            continue
        if low not in names:
            raise ParseError("unknown option '%s'" % tokens[i])
        canonical = names[low]
        if i + 1 >= len(tokens):
            raise ParseError("expected a value after %s" % tokens[i])
        if canonical == multiword:
            tail = tokens[i + 1:]
            if tail and tail[-1].lower() in _BROADCAST_FLAGS:
                broadcast = True
                tail = tail[:-1]
            vals[canonical] = " ".join(tail)
            i = len(tokens)
        else:
            vals[canonical] = tokens[i + 1]
            i += 2
    missing = [name for name in required if name not in vals]
    if missing:
        raise ParseError("missing option(s): %s" % ", ".join(missing))
    return vals, broadcast


_ACCOUNT_FLAG_NAMES = {"--account": "account", "--acct": "account"}
_ID_ACCOUNT_FLAG_NAMES = {
    "--id": "id", "--order": "id", "--position": "id",
    "--account": "account", "--acct": "account",
}


def parse_account(text: str) -> str:
    """/orders|/positions|/close_all|/be <account>
       or --account <acct> form."""
    tokens = text.split()[1:]
    if not tokens:
        raise ParseError("usage")
    if _flag_mode(tokens):
        vals, _b = _parse_named(tokens, _ACCOUNT_FLAG_NAMES,
                                required=("account",))
        return vals["account"]
    return tokens[0]


def parse_id_account(text: str) -> tuple:
    """/close|/cancel_order <id> <account>
       or --id <id> --account <acct> form."""
    tokens = text.split()[1:]
    if not tokens:
        raise ParseError("usage")
    if _flag_mode(tokens):
        vals, _b = _parse_named(tokens, _ID_ACCOUNT_FLAG_NAMES,
                                required=("id", "account"))
        raw_id, account = vals["id"], vals["account"]
    else:
        if len(tokens) < 2:
            raise ParseError("usage")
        raw_id, account = tokens[0], tokens[1]
    try:
        ident = int(raw_id)
    except ValueError:
        raise ParseError("id is not a number")
    return ident, account


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
       /ccalert --tf M15 --price 2450 --dir above [--symbol XAUUSD]
                [--notes breakout] [--all]

    Timeframe must be a known cTrader period. Direction is above|below.
    The 4th token is a symbol only if it looks like one (ALL-CAPS or a
    known symbol); otherwise it's folded into the notes. Flag form (first
    token starts with '-') is strict: every parameter is named.
    """
    tokens = text.split()[1:]
    if not tokens:
        raise ParseError("usage")
    if _flag_mode(tokens):
        vals, broadcast = _parse_named(
            tokens, {"--tf": "tf", "--timeframe": "tf",
                     "--price": "price",
                     "--dir": "dir", "--direction": "dir",
                     "--symbol": "symbol",
                     "--notes": "notes", "--message": "notes"},
            required=("tf", "price", "dir"), multiword="notes")
        timeframe = vals["tf"].upper()
        if timeframe not in candles_mod.TIMEFRAMES:
            raise ParseError("timeframe")
        try:
            target = float(vals["price"])
        except ValueError:
            raise ParseError("price is not a number")
        direction = vals["dir"].upper()
        if direction not in (alerts_mod.CANDLE_ABOVE,
                             alerts_mod.CANDLE_BELOW):
            raise ParseError("direction")
        symbol = default_symbol
        if vals.get("symbol"):
            candidate = vals["symbol"].upper()
            if not SYMBOL_RE.match(candidate):
                raise ParseError(
                    "symbol '%s' is not valid" % vals["symbol"])
            symbol = candidate
        message = vals.get("notes") or "timeframe candle target reached."
        return CandleAlertArgs(timeframe=timeframe, target=target,
                               direction=direction, symbol=symbol,
                               message=message, broadcast=broadcast)
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
                 imbalance_checker=None,
                 cancel_watch: Optional[dict] = None,
                 watch_subscribe=None):
        # feed: FeedService -- async ensure(symbol) -> Optional[(bid, ask)]
        # trader: TradingService (None only in Phase-2-era wiring/tests)
        # candle_store: CandleAlertStore; candle_feed: CandleFeed
        # subscription_store: dynamic chat allow-list
        # pending_cc: shared dict (main.py) registering pending-order cc
        #   guard params; materialized on fill by main.on_execution
        # imbalance_checker: main.py's imbalance_verdict() for /imbalance
        # cancel_watch: shared dict (main.py) of unfilled-order cancel
        #   conditions; main's tick listener fires the broker cancels.
        # watch_subscribe: main.py hook ensuring ticks flow for an account.
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
        self._cancel_watch = cancel_watch
        self._watch_subscribe = watch_subscribe

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
        app.add_handler(CommandHandler("ocancel", self.ocancel))
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
        try:
            account = parse_account(update.effective_message.text)
        except ParseError:
            await self._reply(
                update, fmt.positions_usage(is_positions))
            return
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
        try:
            account = parse_account(update.effective_message.text)
        except ParseError:
            await self._reply(update, fmt.close_all_usage())
            return
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
        try:
            position_id, account = parse_id_account(
                update.effective_message.text)
        except ParseError:
            await self._reply(update, fmt.close_usage())
            return
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
        try:
            order_id, account = parse_id_account(
                update.effective_message.text)
        except ParseError:
            await self._reply(update, fmt.cancel_order_usage())
            return
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
        try:
            account = parse_account(update.effective_message.text)
        except ParseError:
            await self._reply(update, fmt.breakeven_usage())
            return
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

    async def _set_cancel_watch(self, order_id, price: float, shortcode: str,
                                symbol: str, chat_id: int) -> bool:
        """Register a cancel condition on an unfilled order (shared by the
        /p --cancel flow and /ocancel). Returns False when unwired."""
        if self._cancel_watch is None:
            return False
        account = self._settings.accounts.get(shortcode)
        if account is None:
            return False
        spec = {
            "level": price,
            "env": account.environment,
            "account_id": account.ctid_account_id,
            "chat_id": chat_id,
            "symbol": symbol,
            "symbol_id": None,
        }
        if self._watch_subscribe is not None:
            try:
                info = await self._watch_subscribe(shortcode, symbol)
                if info is not None:
                    spec["symbol_id"] = info.symbol_id
            except Exception as e:
                log.warning("cancel watch subscribe failed: %s", e)
        self._cancel_watch[str(order_id)] = spec
        return True

    async def ocancel(self, update: Update,
                      _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Attach a cancel condition to an existing unfilled order.

        If price touches <price> before the order fills, the order is
        cancelled at the broker and the chat is notified.
        """
        if not self._allowed(update):
            return
        try:
            order_id, price = parse_ocancel(update.effective_message.text)
        except ParseError:
            await self._reply(update, fmt.ocancel_usage())
            return
        for shortcode in self._settings.accounts:
            try:
                rows = await self._trader.positions_or_orders(
                    shortcode, is_positions=False)
            except (TradeRejected, CTraderError):
                continue
            for row in rows:
                if int(row.get("id", 0) or 0) != order_id:
                    continue
                symbol = row.get("symbol", self._symbol)
                if not await self._set_cancel_watch(
                        order_id, price, shortcode, symbol,
                        update.effective_chat.id):
                    await self._reply(update,
                                      "cancel watch is not wired up.")
                    return
                log.info("cancel condition set on order %d at %.2f",
                         order_id, price)
                await self._reply(update,
                    "cancel condition set: order %d will be cancelled if "
                    "price hits %.2f before it fills." % (order_id, price))
                return
        await self._reply(update, "order %d not found." % order_id)

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
        bars = verdict["bars"]
        lines = ["gateway returned %d completed H1 bar(s):" % len(bars)]
        for b in bars[-3:]:
            ts = int(b.get("utcTimestampInMinutes", 0) or 0)
            lines.append("  %02d:%02d UTC / %.2f / %.2f"
                         % ((ts // 60) % 24, ts % 60,
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

        # Cancel condition on a pending order: watch price; if it touches
        # the level before the order fills, cancel at the broker.
        if not is_market and args.cancel_price is not None:
            if result.order_id is None:
                await self._reply(update,
                    "trade placed, but the cancel condition could not be "
                    "set (no orderId on the response).")
            elif not await self._set_cancel_watch(
                    result.order_id, args.cancel_price, args.account,
                    self._symbol, update.effective_chat.id):
                await self._reply(update,
                    "warning: cancel condition requested but the watch "
                    "is not wired up.")
            else:
                await self._reply(update,
                    "cancel condition set: order %s will be cancelled if "
                    "price hits %.2f before it fills."
                    % (result.order_id, args.cancel_price))

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
