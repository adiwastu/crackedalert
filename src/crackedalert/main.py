"""Entrypoint.

  python -m crackedalert           run the bot (alerts live; trading Phase 3)
  python -m crackedalert --smoke   connect, auth every account, print balances
"""

import argparse
import asyncio
import logging
import signal
import sys
from typing import Dict, Optional, Set, Tuple

from telegram import BotCommand
from telegram.constants import ParseMode
from telegram.ext import Application

from .alerts import (AlertEngine, AlertStore, CandleAlertEngine,
                     CandleAlertStore, CANDLE_ABOVE, CANDLE_BELOW)
from .alert_status import ActiveAlert, AlertStatusServer
from .bot import formatting as fmt
from .bot.formatting import BOT_COMMANDS
from .bot.handlers import Handlers
from .bot.subscriptions import SubscriptionStore
from .config import ConfigError, Settings, load_settings
from .ctrader import client as ct
from .ctrader.candles import CandleFeed
from .ctrader.market import MarketData
from .ctrader.tokens import TokenError, TokenStore
from .ctrader.trading import TradingService, TradeRejected

log = logging.getLogger("crackedalert.main")


def _execution_closes_position(order: dict, position: dict) -> bool:
    """True when an ExecutionEvent closes a position.

    A close (broker-side SL/TP hit or manual) arrives as an ExecutionEvent
    whose closing order trades the OPPOSITE side of the position; open
    fills carry the same side on both.
    """
    if not isinstance(order, dict) or not isinstance(position, dict):
        return False
    order_td = order.get("tradeData")
    pos_td = position.get("tradeData")
    if not isinstance(order_td, dict) or not isinstance(pos_td, dict):
        return False
    o_side = order_td.get("tradeSide")
    p_side = pos_td.get("tradeSide")
    return bool(o_side and p_side and o_side != p_side)


class _ConflictFilter(logging.Filter):
    """Collapse Telegram's 409 Conflict traceback to a single actionable
    line. It repeats every few seconds while two bots poll one token, and
    the 30-line traceback buries everything else in the log."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc_type = record.exc_info[0] if record.exc_info else None
        if exc_type is not None and exc_type.__name__ == "Conflict":
            record.exc_info = None
            record.msg = ("Telegram 409: another bot instance is polling this "
                          "token -- stop that instance "
                          "(service: systemctl stop cracked-bot)")
            record.args = ()
        return True


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if verbose:
        return
    # httpx logs every Telegram long-poll (~1 line/10s, forever) and puts
    # the bot token in the URL; httpcore/websockets are chattier still.
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.Updater").addFilter(_ConflictFilter())


class FeedService:
    """Price feed for alerts: one account's spot stream, symbols added
    on demand, resubscribed after every reconnect."""

    def __init__(self, market: MarketData, account_id: int,
                 engine: AlertEngine):
        self._market = market
        self._account_id = account_id
        self._engine = engine
        self._subscribed: Dict[int, str] = {}   # symbol_id -> name

    async def ensure(self, symbol_name: str) -> Optional[Tuple[float, float]]:
        """Subscribe if needed; return a fresh (bid, ask) or None."""
        try:
            info, quote = await self._market.ensure_quote(
                self._account_id, symbol_name)
        except (ct.CTraderError, ct.NotConnected) as e:
            log.warning("feed: cannot resolve/subscribe %s: %s",
                        symbol_name, e)
            return None
        self._subscribed[info.symbol_id] = info.name   # for tick routing
        if quote is None:
            return None                          # market closed or dead feed
        return (quote.bid, quote.ask)

    async def after_connect(self, default_symbol: str,
                            alert_symbols: Set[str]) -> None:
        """(Re)establish subscriptions after connect/reconnect."""
        self._market.forget_account(self._account_id)
        self._market.reset_subscriptions(self._account_id)
        wanted = {default_symbol.upper()}
        wanted.update(s.upper() for s in alert_symbols)
        wanted.update(self._subscribed.values())
        self._subscribed = {}
        for name in sorted(wanted):
            await self.ensure(name)

    async def on_tick(self, account_id: int, symbol_id: int,
                      bid: float, ask: float) -> None:
        if account_id != self._account_id:
            return
        name = self._subscribed.get(symbol_id)
        if name is not None:
            await self._engine.on_tick(name, bid, ask)

    def known_symbols(self) -> set:
        return self._market.known_symbols(self._account_id)


async def _run_bot(settings: Settings) -> None:
    tokens = TokenStore(settings.tokens_file, settings.ctrader_client_id,
                        settings.ctrader_client_secret)

    store = AlertStore(settings.db_file)

    candle_store = CandleAlertStore(settings.db_file)

    # dynamic chat allow-list: seed static IDs, then /subscribe takes over
    subscription_store = SubscriptionStore(settings.db_file)
    subscription_store.seed(settings.allowed_chat_ids)

    app = (Application.builder()
           .token(settings.telegram_bot_token).build())

    active_alert = ActiveAlert()
    status_server = AlertStatusServer(
        token=settings.alert_status_token, active=active_alert,
        port=settings.alert_status_port)

    async def notify(chat_id: int, text: str) -> None:
        active_alert.set(text)
        await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)

    feed_account = settings.accounts[settings.price_feed_account]

    # one client + market-data cache per environment in use
    clients: Dict[str, ct.CTraderClient] = {}
    markets: Dict[str, MarketData] = {}
    for env in settings.environments_in_use():
        cli = ct.CTraderClient(env, settings.ctrader_client_id,
                               settings.ctrader_client_secret)
        clients[env] = cli
        markets[env] = MarketData(cli)

    trader = TradingService(clients, markets, settings)

    # Pending-order cc guards: params are registered here at placement time
    # (Handlers._trade) and materialized when the fill's ExecutionEvent
    # arrives on the stream. In-memory only: a restart between placement
    # and fill drops the guard registration (the order itself still lives
    # broker-side with its real SL/TP -- re-attach via /ccalert).
    pending_cc: Dict[str, dict] = {}

    async def broadcast(text: str) -> None:
        """Send a message to every subscriber (auto trade alerts)."""
        active_alert.set(text)
        for cid in subscription_store.all_ids():
            try:
                await app.bot.send_message(cid, text,
                                           parse_mode=ParseMode.HTML)
            except Exception:
                log.warning("broadcast to chat %s failed", cid)

    def _materialize_pending_cc(order_id, position_id) -> None:
        spec = pending_cc.pop(str(order_id), None)
        if spec is None or position_id is None:
            return
        try:
            pos_id = int(position_id)
        except (TypeError, ValueError):
            log.warning("cc guard: fill for order %s has no usable "
                        "positionId (%r)", order_id, position_id)
            return
        side = spec["direction"]
        # BUY: guard fires if candle closes BELOW threshold; SELL: ABOVE.
        cc_direction = CANDLE_BELOW if side == "BUY" else CANDLE_ABOVE
        alert = candle_store.create(
            spec["chat_id"], spec["symbol"], spec["timeframe"],
            spec["cc_price"], cc_direction,
            "cc guard for position %d" % pos_id,
            action="close", position_id=pos_id,
            account=spec["account"], broadcast=spec["broadcast"])
        candle_feed.add_symbol(spec["symbol"], spec["timeframe"])
        log.info("cc guard %s created on fill: pos=%d %s %s",
                 alert.id, pos_id, spec["symbol"], spec["timeframe"])

    def on_execution(payload: dict) -> None:
        """Create pending-order cc guards as fills stream in; drop guards
        for positions the event shows as closed (broker-side SL/TP hits
        or manual closes), so stale guards die the moment the position
        closes instead of lingering until a candle crosses the level."""
        order = payload.get("order") or {}
        position = payload.get("position") or {}
        order_id = order.get("orderId")
        position_id = position.get("positionId")
        if position_id is None:
            position_id = order.get("positionId")
        if order_id is not None:
            _materialize_pending_cc(order_id, position_id)
        if position_id is not None \
                and _execution_closes_position(order, position):
            try:
                pos_int = int(position_id)
            except (TypeError, ValueError):
                return
            n = candle_store.cancel_for_position(pos_int)
            if n:
                log.info("position %s closed on stream: cancelled %d "
                         "cc guard(s)", position_id, n)
                _sync_candle_feed()

    for _env, cli in clients.items():
        cli.add_event_handler(ct.PT_EXECUTION_EVENT, on_execution)

    engine = AlertEngine(store, notify, fmt.alert_fired,
                         on_broadcast=broadcast)
    feed = FeedService(markets[feed_account.environment],
                       feed_account.ctid_account_id, engine)
    markets[feed_account.environment].add_tick_listener(feed.on_tick)

    async def on_cc_close(alert) -> None:
        try:
            await trader.close_position(alert.account, alert.position_id)
            await broadcast(fmt.cc_guard_fired(alert))
        except TradeRejected as e:
            if "not found" in str(e).lower():
                log.info("cc guard %s: position %d already gone: %s",
                         alert.id, alert.position_id, e)
                await broadcast(fmt.cc_guard_position_gone(alert))
            else:
                raise

    def _sync_candle_feed() -> None:
        """Drop feed keys the store no longer needs (guards fired,
        cancelled, or their position closed on the stream)."""
        candle_feed.sync_keys(candle_store.active_keys())

    candle_engine = CandleAlertEngine(candle_store, notify, fmt.candle_alert_fired,
                                      on_close_hit=on_cc_close,
                                      on_broadcast=broadcast,
                                      on_alert_removed=_sync_candle_feed)
    candle_feed = CandleFeed(clients[feed_account.environment],
                             markets[feed_account.environment],
                             feed_account.ctid_account_id, candle_engine)

    def make_on_connected(env: str):
        async def on_connected() -> None:
            cli = clients[env]
            for acc in settings.accounts.values():
                if acc.environment != env:
                    continue
                _pt, auth_resp = await cli.request(ct.PT_ACCOUNT_AUTH_REQ, {
                    "ctidTraderAccountId": acc.ctid_account_id,
                    "accessToken": tokens.access_token,
                })
                # Note: ProtoOAAccountAuthRes has NO isAuthorized field
                # (verified against the official proto) -- a response at
                # all means the account authenticated; failures arrive as
                # ProtoOAErrorRes. Log the raw response for diagnostics.
                log.info("[%s] account %s (%d) auth res: %r",
                         env, acc.shortcode, acc.ctid_account_id, auth_resp)
            if env == feed_account.environment:
                await feed.after_connect(settings.trade_symbol,
                                         store.active_symbols())
                for sym, tf in candle_store.active_keys():
                    candle_feed.add_symbol(sym, tf)
        return on_connected

    for env, cli in clients.items():
        cli.set_on_connected(make_on_connected(env))

    handlers = Handlers(settings, store, feed, settings.trade_symbol,
                        trader=trader, candle_store=candle_store,
                        candle_feed=candle_feed,
                        subscription_store=subscription_store,
                        pending_cc=pending_cc)
    handlers.register(app)

    # P2: native command menu
    await app.bot.set_my_commands(
        [BotCommand(c, d) for c, d in BOT_COMMANDS])

    async def on_token_failure(reason: str) -> None:
        await notify(settings.allowed_chat_ids[0],
                     "\u26a0\ufe0f cTrader token refresh failed: %s\n"
                     "re-run auth_setup.py on the VPS." % reason)

    refresh_task = asyncio.get_running_loop().create_task(
        tokens.refresh_loop(on_failure=on_token_failure))

    for cli in clients.values():
        cli.start()
    candle_feed.start()

    stop = asyncio.Event()
    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
    except NotImplementedError:
        pass    # Windows dev box: Ctrl+C raises KeyboardInterrupt instead

    log.info("cracked alert v2 starting: envs=%s feed=%s symbol=%s",
             ",".join(clients), settings.price_feed_account,
             settings.trade_symbol)

    async with app:
        await app.start()
        await status_server.start()
        await app.updater.start_polling(allowed_updates=["message"])
        try:
            await stop.wait()
        finally:
            await app.updater.stop()
            await status_server.stop()
            await app.stop()

    refresh_task.cancel()
    await candle_feed.stop()
    for cli in clients.values():
        await cli.stop()
    store.close()
    candle_store.close()
    subscription_store.close()
    log.info("shut down cleanly")


async def _smoke(settings: Settings) -> int:
    tokens = TokenStore(settings.tokens_file, settings.ctrader_client_id,
                        settings.ctrader_client_secret)

    print("environments in use: %s" % ", ".join(settings.environments_in_use()))
    failures = 0

    for env in settings.environments_in_use():
        cli = ct.CTraderClient(env, settings.ctrader_client_id,
                               settings.ctrader_client_secret)
        cli.start()
        try:
            await cli.wait_ready(timeout=30)
            print("\n[%s] app authenticated" % env)
            for acc in settings.accounts.values():
                if acc.environment != env:
                    continue
                try:
                    await cli.request(ct.PT_ACCOUNT_AUTH_REQ, {
                        "ctidTraderAccountId": acc.ctid_account_id,
                        "accessToken": tokens.access_token,
                    })
                    _, payload = await cli.request(ct.PT_TRADER_REQ, {
                        "ctidTraderAccountId": acc.ctid_account_id,
                    })
                    trader = payload.get("trader", {})
                    digits = int(trader.get("moneyDigits", 2))
                    balance = trader.get("balance", 0) / (10 ** digits)
                    print("  %-8s (id %s): balance %.2f" % (
                        acc.shortcode, acc.ctid_account_id, balance))
                except ct.CTraderError as e:
                    failures += 1
                    print("  %-8s (id %s): FAILED -- %s" % (
                        acc.shortcode, acc.ctid_account_id, e))
        except asyncio.TimeoutError:
            failures += 1
            print("\n[%s] could not connect/authenticate within 30s" % env)
        finally:
            await cli.stop()

    print("\nsmoke test %s" % ("PASSED" if failures == 0 else
                               "FAILED (%d problems)" % failures))
    return 0 if failures == 0 else 1


def cli() -> None:
    parser = argparse.ArgumentParser(prog="crackedalert")
    parser.add_argument("--smoke", action="store_true",
                        help="connect, authenticate, print balances, exit")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="keep third-party HTTP/websocket logs (noisy; "
                             "prints the bot token in Telegram URLs)")
    args = parser.parse_args()
    _setup_logging(verbose=args.verbose)

    try:
        settings = load_settings()
    except ConfigError as e:
        sys.exit("config error: %s" % e)

    try:
        if args.smoke:
            sys.exit(asyncio.run(_smoke(settings)))
        asyncio.run(_run_bot(settings))
    except TokenError as e:
        sys.exit("token error: %s" % e)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()