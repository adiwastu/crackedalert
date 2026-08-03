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

from telegram.constants import ParseMode
from telegram.ext import Application

from .alerts import (AlertEngine, AlertStore, CandleAlertEngine,
                     CandleAlertStore)
from .bot import formatting as fmt
from .bot.handlers import Handlers
from .config import ConfigError, Settings, load_settings
from .ctrader import client as ct
from .ctrader.candles import CandleFeed
from .ctrader.market import MarketData
from .ctrader.tokens import TokenError, TokenStore
from .ctrader.trading import TradingService

log = logging.getLogger("crackedalert.main")


class _ConflictFilter(logging.Filter):
    """Collapse Telegram's 409 Conflict traceback to a single actionable
    line. It repeats every few seconds while two bots poll one token, and
    the 30-line traceback buries everything else in the log."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc_type = record.exc_info[0] if record.exc_info else None
        if exc_type is not None and exc_type.__name__ == "Conflict":
            record.exc_info = None
            record.msg = ("Telegram 409: another bot instance is polling this "
                          "token -- use bin/use_new.sh to switch cleanly")
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
    imported = store.import_tsv(settings.legacy_tsv)
    if imported:
        log.info("migrated %d alerts from the bash bot", imported)

    candle_store = CandleAlertStore(settings.db_file)

    app = (Application.builder()
           .token(settings.telegram_bot_token).build())

    async def notify(chat_id: int, text: str) -> None:
        await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)

    engine = AlertEngine(store, notify, fmt.alert_fired)
    feed_account = settings.accounts[settings.price_feed_account]

    # one client + market-data cache per environment in use
    clients: Dict[str, ct.CTraderClient] = {}
    markets: Dict[str, MarketData] = {}
    for env in settings.environments_in_use():
        cli = ct.CTraderClient(env, settings.ctrader_client_id,
                               settings.ctrader_client_secret)
        clients[env] = cli
        markets[env] = MarketData(cli)

    feed = FeedService(markets[feed_account.environment],
                       feed_account.ctid_account_id, engine)
    markets[feed_account.environment].add_tick_listener(feed.on_tick)

    candle_engine = CandleAlertEngine(candle_store, notify, fmt.candle_alert_fired)
    candle_feed = CandleFeed(clients[feed_account.environment],
                             markets[feed_account.environment],
                             feed_account.ctid_account_id, candle_engine)

    def make_on_connected(env: str):
        async def on_connected() -> None:
            cli = clients[env]
            for acc in settings.accounts.values():
                if acc.environment != env:
                    continue
                await cli.request(ct.PT_ACCOUNT_AUTH_REQ, {
                    "ctidTraderAccountId": acc.ctid_account_id,
                    "accessToken": tokens.access_token,
                })
                log.info("[%s] account %s (%d) authenticated", env,
                         acc.shortcode, acc.ctid_account_id)
            if env == feed_account.environment:
                await feed.after_connect(settings.trade_symbol,
                                         store.active_symbols())
                for sym, tf in candle_store.active_keys():
                    candle_feed.add_symbol(sym, tf)
        return on_connected

    for env, cli in clients.items():
        cli.set_on_connected(make_on_connected(env))

    trader = TradingService(clients, markets, settings)
    handlers = Handlers(settings, store, feed, settings.trade_symbol,
                        trader=trader, candle_store=candle_store,
                        candle_feed=candle_feed)
    handlers.register(app)

    async def on_token_failure(reason: str) -> None:
        await notify(settings.allowed_chat_ids[0],
                     "⚠️ cTrader token refresh failed: %s\n"
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
        await app.updater.start_polling(allowed_updates=["message"])
        try:
            await stop.wait()
        finally:
            await app.updater.stop()
            await app.stop()

    refresh_task.cancel()
    await candle_feed.stop()
    for cli in clients.values():
        await cli.stop()
    store.close()
    candle_store.close()
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
