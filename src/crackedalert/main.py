"""Entrypoint.

  python -m crackedalert           run the bot (alerts live; trading Phase 3)
  python -m crackedalert --smoke   connect, auth every account, print balances
"""

import argparse
import asyncio
import logging
import signal
import sys
import time
from typing import Dict, Optional, Set, Tuple

from telegram import BotCommand
from telegram.constants import ParseMode
from telegram.ext import Application

from .alerts import (AlertEngine, AlertStore, CandleAlertEngine,
                     CandleAlertStore, CANDLE_ABOVE, CANDLE_BELOW,
                     CROSSING_DOWN, CROSSING_UP, KIND_ZONE)
from .alert_status import ActiveAlert, AlertStatusServer
from .bias import (BiasMachine, BiasStore, absorbed_message,
                   flipped_kind_from_note, regime_flip_message,
                   seeded_message, strike_message, zone_label)
from .bot import formatting as fmt
from .bot.formatting import BOT_COMMANDS
from .bot.handlers import Handlers, order_success_text, parse_trade
from .bot.subscriptions import SubscriptionStore
from .config import ConfigError, Settings, load_settings, fcm_service_account
from .ctrader import client as ct
from .ctrader.candles import CandleFeed
from .ctrader.market import MarketData
from .ctrader.tokens import (TokenError, TokenStore, authorize_url,
                             make_selfheal_refresh)
from .ctrader.trading import TradingService, TradeRejected
from .fcm import FcmSender, FCMTokenStore, load_service_account
from .fvg import (IMBALANCE_ALERT_SPECS, ImbalanceGate, back_to_back,
                  candle_high, candle_low, fresh_imbalance, gap_bounds,
                  just_closed_ts, zone_name)
from .nested_fvg import nested_entries
from .quiet_hours import (HeldMessageStore, build_digest, is_quiet_now,
                          seconds_until_window_end)
from .scheduled_order import ScheduledOrderRunner, ScheduledOrderStore
from .wave_state import WaveService, WaveStateStore, bar_close_time
from .zone_watch import (WATCH_TIMEFRAMES, ZoneWatch, ZoneWatchStore,
                         zone_label)

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

    # FCM push: registered phones get a high-priority data message the
    # moment an alert goes active (delivered through Doze, wakes the
    # app), instead of waiting for the app's next poll. Disabled unless
    # a service-account file is configured; the app's poll loop stays
    # as the fallback either way.
    fcm_store = FCMTokenStore(settings.db_file)
    fcm_sender = None
    fcm_sa_path = fcm_service_account()
    if fcm_sa_path:
        try:
            fcm_sender = FcmSender(load_service_account(fcm_sa_path),
                                   fcm_store)
            log.info("fcm push enabled (service account: %s)", fcm_sa_path)
        except Exception as e:
            log.warning("fcm push disabled: bad service account: %s", e)

    async def register_fcm(device_token: str) -> bool:
        return fcm_store.add(device_token)

    status_server.set_register_fcm(register_fcm)

    # Quiet hours (22:00-05:00 display clock): alert messages are held
    # and flushed as one digest at the window's end. Urgent messages
    # (the token-dead alarm) bypass the hold AND ring the phone through
    # its own quiet window via the push `urgent` flag.
    held_store = HeldMessageStore(settings.db_file)

    def in_quiet_hours() -> bool:
        return is_quiet_now(time.time(), settings.display_utc_offset)

    async def push_alert(text: str, urgent: bool = False) -> None:
        if fcm_sender is None:
            return
        try:
            await fcm_sender.push_all("Cracked Alert", text,
                                      active_alert.since, urgent=urgent)
        except Exception:
            log.exception("fcm push failed")

    async def deliver(chat_id: int, text: str,
                      urgent: bool = False) -> None:
        if urgent or not in_quiet_hours():
            await app.bot.send_message(chat_id, text,
                                       parse_mode=ParseMode.HTML)
        else:
            held_store.add(chat_id, text, int(time.time()))

    async def notify(chat_id: int, text: str, urgent: bool = False) -> None:
        active_alert.set(text)
        await push_alert(text, urgent)
        await deliver(chat_id, text, urgent)

    async def flush_held() -> None:
        rows = held_store.all()
        if not rows:
            return
        for chat_id, message in build_digest(rows,
                                             settings.display_utc_offset):
            try:
                await app.bot.send_message(chat_id, message,
                                           parse_mode=ParseMode.HTML)
            except Exception:
                log.warning("quiet-hours digest to chat %s failed",
                            chat_id)
        held_store.clear()
        log.info("quiet-hours digest flushed: %d held message(s)",
                 len(rows))

    async def digest_watcher() -> None:
        # A restart after the window ended with unflushed holds: flush now.
        if held_store.all() and not in_quiet_hours():
            await flush_held()
        while True:
            now = time.time()
            await asyncio.sleep(max(
                5.0, seconds_until_window_end(now,
                                              settings.display_utc_offset))
                + 2)
            if held_store.all():
                await flush_held()

    feed_account = settings.accounts[settings.price_feed_account]

    # Self-heal an expired access token: the daily refresh loop can miss
    # the expiry window unannounced, and without this the reconnect loop
    # would auth-reject forever (link "down", no live prices). When the
    # refresh itself is rejected -- a revoked grant, which refresh cannot
    # fix -- ping the operator at a sane rate with the consent link; the
    # pasted-redirect handler below completes the recovery.
    async def token_dead_alert(e: Exception) -> None:
        # urgent: bypasses the quiet-hours hold AND rings the phone
        # through its own sleep window -- the bot cannot trade without
        # the operator re-authorizing, whatever the hour.
        await notify(
            settings.allowed_chat_ids[0],
            fmt.token_dead_alert(
                str(e),
                authorize_url(settings.ctrader_client_id,
                              settings.ctrader_redirect_uri)),
            urgent=True)

    selfheal_refresh = make_selfheal_refresh(tokens, token_dead_alert)

    # one client + market-data cache per environment in use
    clients: Dict[str, ct.CTraderClient] = {}
    markets: Dict[str, MarketData] = {}
    for env in settings.environments_in_use():
        cli = ct.CTraderClient(env, settings.ctrader_client_id,
                               settings.ctrader_client_secret)
        cli.set_token_refresher(selfheal_refresh)
        clients[env] = cli
        markets[env] = MarketData(cli)

    trader = TradingService(clients, markets, settings)

    # GET /orders data for the command-builder UI: one reconcile per
    # account, cached so a polling page never hammers the cTrader link.
    # Read-only: cancel/close stays on the Telegram side.
    _orders_cache: Dict[str, object] = {"at": 0.0, "payload": None}
    _orders_lock = asyncio.Lock()

    async def orders_provider() -> dict:
        """Working orders per account, for the UI's /orders endpoint."""
        now = time.monotonic()
        cached = _orders_cache
        if now - cached["at"] < 8.0 and cached["payload"] is not None:
            return cached["payload"]
        async with _orders_lock:
            now = time.monotonic()
            if now - cached["at"] < 8.0 and cached["payload"] is not None:
                return cached["payload"]
            accounts = {}
            for shortcode in settings.accounts:
                try:
                    rows = await trader.positions_or_orders(
                        shortcode, is_positions=False)
                except Exception as e:
                    log.warning("orders fetch failed for %s: %s",
                                shortcode, e)
                    if isinstance(e, ct.CTraderError):
                        msg = e.description
                    else:
                        msg = str(e)
                    accounts[shortcode] = {"orders": [], "error": msg}
                else:
                    # Tag rows that carry an active cancel-condition watch
                    # so the UI can show/amend the level.
                    enriched = []
                    for r in rows:
                        rid = r.get("id")
                        watch = (order_cancel_watch.get(str(rid))
                                 if rid is not None else None)
                        if watch is not None:
                            r = dict(r)
                            r["cancel_level"] = watch["level"]
                        enriched.append(r)
                    accounts[shortcode] = {"orders": enriched,
                                           "error": None}
            payload = {"accounts": accounts}
            cached["at"] = time.monotonic()
            cached["payload"] = payload
            return payload

    status_server.set_orders_provider(orders_provider)

    # Pending-order cc guards: params are registered here at placement time
    # (Handlers._trade) and materialized when the fill's ExecutionEvent
    # arrives on the stream. In-memory only: a restart between placement
    # and fill drops the guard registration (the order itself still lives
    # broker-side with its real SL/TP -- re-attach via /ccalert).
    pending_cc: Dict[str, dict] = {}

    # Pending-order cancel conditions: str(order_id) -> watch spec.
    # Registered by handlers (/p --cancel and /ocancel); this module's
    # tick listener cancels the order when price touches the level before
    # the fill, and on_execution drops the watch once the order fills or
    # is cancelled. In-memory: a restart drops watches (re-arm with
    # /ocancel), like the pending_cc registry.
    order_cancel_watch: Dict[str, dict] = {}

    async def watch_subscribe(shortcode: str, symbol: str):
        """Ensure ticks stream for the order's account/symbol so the
        cancel-condition listener can see price action. Returns the
        resolved SymbolInfo, or None when the account/env is unknown."""
        acc = settings.accounts.get(shortcode)
        if acc is None:
            return None
        market = markets.get(acc.environment)
        if market is None:
            return None
        info = await market.ensure_symbol(acc.ctid_account_id, symbol)
        await market.subscribe(acc.ctid_account_id, info.symbol_id)
        return info

    def _cancel_watch_gone(message: str) -> bool:
        """True when a failed cancel response means the order itself is no
        longer open (already filled/cancelled/rejected elsewhere), so the
        watch should die instead of retrying against a dead order."""
        m = str(message).lower()
        for token in ("not found", "does not exist", "no longer",
                      "already", "filled", "cancelled", "canceled",
                      "rejected", "expired", "not open"):
            if token in m:
                return True
        return False

    async def on_tick_cancel(account_id: int, symbol_id: int,
                             bid: float, ask: float) -> None:
        """Cancel unfilled orders whose cancel level price just traded.

        A watch's level is hit on the first tick that trades at or beyond
        it (bid <= level for a level below mid, ask >= level for one
        above). Filling first wins: on_execution drops the watch when the
        order's fill event arrives."""
        if not order_cancel_watch:
            return
        mid = (bid + ask) / 2.0
        now = time.monotonic()
        for oid, w in list(order_cancel_watch.items()):
            if w["account_id"] != account_id:
                continue
            if w.get("symbol_id") != symbol_id:
                continue    # watch without a resolvable symbol never fires
            if now < w.get("retry_after", 0):
                continue
            level = w["level"]
            hit = (level < mid and bid <= level) or \
                  (level >= mid and ask >= level)
            if not hit:
                continue
            try:
                await clients[w["env"]].request(ct.PT_CANCEL_ORDER_REQ, {
                    "ctidTraderAccountId": w["account_id"],
                    "orderId": int(oid),
                })
                order_cancel_watch.pop(oid, None)
                log.info("order %s cancelled: cancel condition %.2f hit",
                         oid, level)
                await notify(
                    w["chat_id"],
                    "order %s cancelled \u2014 price hit your cancel "
                    "level %.2f before the fill." % (oid, level))
            except Exception as e:
                if _cancel_watch_gone(str(e)):
                    order_cancel_watch.pop(oid, None)
                    log.info("cancel watch %s dropped: order is gone (%s)",
                             oid, e)
                    await notify(
                        w["chat_id"],
                        "order %s is no longer open (filled or cancelled "
                        "already) \u2014 nothing to do for the cancel "
                        "condition at %.2f." % (oid, level))
                else:
                    log.warning("cancel condition %s hit but cancel "
                                "failed: %s -- retrying later", oid, e)
                    w["retry_after"] = time.monotonic() + 10

    async def broadcast(text: str) -> None:
        """Send a message to every subscriber (auto trade alerts)."""
        active_alert.set(text)
        await push_alert(text)
        for cid in subscription_store.all_ids():
            try:
                await deliver(cid, text)
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
        if order_id is not None and position_id is not None:
            # A fill event for this order means it is no longer pending:
            # the fill won, so drop any cancel-condition watch on it.
            if order_cancel_watch.pop(str(order_id), None) is not None:
                log.info("order %s filled: cancel-condition watch dropped",
                         order_id)
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

    # After a DH1 / S H1 zone is touched, broadcast every break on the
    # watched timeframes until the first CHoCH.
    zone_store = ZoneWatchStore(settings.db_file)
    zone_watch = ZoneWatch(
        zone_store, broadcast, utc_offset=settings.display_utc_offset,
        # Late bound: wave_service is built below. Only read on a touch.
        structure=lambda symbol, timeframe: wave_service.state(
            symbol, timeframe))

    async def on_zone_touch(alert) -> None:
        await zone_watch.on_touch(
            alert.symbol, zone_label(alert.direction, alert.cc_timeframe),
            alert.target)

    engine = AlertEngine(store, notify, fmt.alert_fired,
                         on_broadcast=broadcast,
                         on_zone_touch=on_zone_touch)
    feed = FeedService(markets[feed_account.environment],
                       feed_account.ctid_account_id, engine)
    markets[feed_account.environment].add_tick_listener(feed.on_tick)
    # Cancel-condition watcher: listens on every env's spot stream (the
    # watcher itself routes by account/symbol, so one shared listener).
    for mkt in markets.values():
        mkt.add_tick_listener(on_tick_cancel)

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

    # ------------------------------------------------------------------
    # The operator's regime: a bias with a two-strike flip rule. Zone
    # flips are counted from the imbalance watcher's own flip alerts
    # ("strike 1 of FLIP ..."); the counter is persisted, so restarts
    # never lose it. /bias in chat views or overrides the state.
    # ------------------------------------------------------------------
    bias_machine = BiasMachine(BiasStore(settings.db_file))

    async def on_candle_fired(alert) -> None:
        flipped = flipped_kind_from_note(alert.note or "")
        if flipped is None:
            return
        outcome = bias_machine.flip(flipped)
        log.info("bias: %s zone flipped -> %s (bias=%s, strikes=%d)",
                 flipped, outcome.kind, outcome.state.bias,
                 outcome.state.strikes)
        if outcome.kind == "strike":
            await broadcast(strike_message(outcome))
        elif outcome.kind == "regime_flip":
            await broadcast(regime_flip_message(outcome))
        elif outcome.kind == "seeded":
            await broadcast(seeded_message(outcome))
        elif outcome.kind == "absorbed":
            # The flipped zone changes sides and keeps serving the bias:
            # re-arm its origin as a touch alert, watched like any other
            # H1 zone (touch -> M5 structure until CHoCH).
            store.create(
                settings.allowed_chat_ids[0], settings.trade_symbol,
                alert.target,
                CROSSING_DOWN if outcome.flipped_into == "demand"
                else CROSSING_UP,
                "flipped %s, now %s" % (zone_label(outcome.flipped),
                                        zone_label(outcome.flipped_into)),
                kind=KIND_ZONE, cc_timeframe="H1", broadcast=True)
            await broadcast(absorbed_message(outcome))

    candle_engine = CandleAlertEngine(candle_store, notify, fmt.candle_alert_fired,
                                      on_close_hit=on_cc_close,
                                      on_broadcast=broadcast,
                                      on_alert_removed=_sync_candle_feed,
                                      on_fired=on_candle_fired)
    # Market structure engine. Nothing acts on its calls yet: it only
    # logs, so the operator can compare what it reports against what
    # they would have called by eye before anything depends on it.
    wave_store = WaveStateStore(settings.db_file)

    async def on_wave_event(event) -> None:
        # The bar's close, in the operator's own clock: that is when the
        # break happened, and what a chart shows. event.ts is the bar's
        # open, a whole period earlier.
        when = (bar_close_time(event.timeframe, event.ts,
                               settings.display_utc_offset)
                or "ts=%d" % event.ts)
        if event.kind == "doji":
            log.info("wave doji %s %s at %s",
                     event.symbol, event.timeframe, when)
            return
        log.info("wave %s %s %s %s broke %.5f at %s "
                 "(valid high %s, valid low %s)",
                 event.kind, event.direction, event.symbol,
                 event.timeframe, event.level, when,
                 event.valid_high, event.valid_low)
        await zone_watch.on_wave_event(event)

    wave_service = WaveService(
        wave_store,
        # Late bound: candle_feed owns the fetch and is built just below.
        # Warm-up only runs on a closed bar, long after startup wiring.
        lambda symbol, timeframe, count: candle_feed.history(
            symbol, timeframe, count),
        on_event=on_wave_event,
        utc_offset=settings.display_utc_offset)

    # Candle alerts first: they are the live path, the wave engine only
    # observes. The feed isolates each engine, so order is about
    # latency, not safety.
    candle_feed = CandleFeed(clients[feed_account.environment],
                             markets[feed_account.environment],
                             feed_account.ctid_account_id,
                             [candle_engine, wave_service])
    # Zone watches need structure on these timeframes whether or not any
    # candle alert happens to be using them.
    for timeframe in WATCH_TIMEFRAMES:
        candle_feed.pin(settings.trade_symbol, timeframe)

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
            # Server-side spot subscriptions are gone after a reconnect:
            # re-arm streams for symbols with live cancel-condition
            # watches on this environment's accounts.
            mkt = markets[env]
            for oid, w in list(order_cancel_watch.items()):
                if w.get("env") != env:
                    continue
                try:
                    info = await mkt.ensure_symbol(
                        w["account_id"], w["symbol"])
                    await mkt.subscribe(w["account_id"], info.symbol_id)
                    w["symbol_id"] = info.symbol_id
                except Exception as e:
                    log.warning("resubscribe cancel watch %s (%s) "
                                "failed: %s", oid, w.get("symbol"), e)
        return on_connected

    for env, cli in clients.items():
        cli.set_on_connected(make_on_connected(env))

    # Dead-grant recovery: the operator pastes the OAuth redirect URL
    # (linked in the token-dead alert) into the chat. Exchange the one-
    # time code for fresh tokens, drop the live sessions so the runners
    # reconnect with them, and report whether the feed is already back.
    async def token_recovery(code: str) -> str:
        try:
            await tokens.exchange_code(code, settings.ctrader_redirect_uri)
        except Exception as e:
            log.warning("token recovery exchange failed: %s", e)
            return fmt.token_recover_failed(e)
        kicks = await asyncio.gather(
            *(cli.force_reconnect() for cli in clients.values()),
            return_exceptions=True)
        for r in kicks:
            if isinstance(r, Exception):
                log.warning("token recovery session kick failed: %s", r)
        ready = await asyncio.gather(
            *(cli.wait_ready(90) for cli in clients.values()),
            return_exceptions=True)
        if all(not isinstance(r, Exception) for r in ready):
            return fmt.token_recover_ok()
        return fmt.token_recover_reconnecting()

    # One-shot scheduled pending orders (/later): at the rule's time the
    # bid is checked against the rule's absolute band; inside it, the
    # stored /p spec is placed exactly as if the operator had typed it.
    scheduled_store = ScheduledOrderStore(settings.db_file)

    async def scheduled_bid(shortcode: str) -> Optional[float]:
        """Current bid for a rule's band check, or None when unavailable."""
        acc = settings.accounts.get(shortcode)
        if acc is None:
            return None
        market = markets.get(acc.environment)
        if market is None:
            return None
        try:
            _info, quote = await market.ensure_quote(
                acc.ctid_account_id, settings.trade_symbol)
        except (ct.CTraderError, ct.NotConnected) as e:
            log.warning("scheduled bid fetch failed for %s: %s",
                        shortcode, e)
            return None
        return quote.bid if quote is not None else None

    scheduled_runner = ScheduledOrderRunner(
        scheduled_store, trader, settings, notify, scheduled_bid,
        watch_subscribe=watch_subscribe, pending_cc=pending_cc,
        order_cancel_watch=order_cancel_watch,
        parse_spec=parse_trade, format_success=order_success_text)

    handlers = Handlers(settings, store, feed, settings.trade_symbol,
                        trader=trader, candle_store=candle_store,
                        candle_feed=candle_feed,
                        subscription_store=subscription_store,
                        pending_cc=pending_cc,
                        cancel_watch=order_cancel_watch,
                        watch_subscribe=watch_subscribe,
                        imbalance_checker=lambda: imbalance_verdict(),
                        token_recovery=token_recovery,
                        scheduled=scheduled_runner,
                        bias=bias_machine)
    handlers.register(app)

    # P2: native command menu
    await app.bot.set_my_commands(
        [BotCommand(c, d) for c, d in BOT_COMMANDS])

    async def on_token_failure(reason: str) -> None:
        await notify(settings.allowed_chat_ids[0],
                     fmt.token_dead_alert(
                         reason,
                         authorize_url(settings.ctrader_client_id,
                                       settings.ctrader_redirect_uri)),
                     urgent=True)

    refresh_task = asyncio.get_running_loop().create_task(
        tokens.refresh_loop(on_failure=on_token_failure))

    # ------------------------------------------------------------------
    # H1 fair-value-gap watch: once per closed H1 candle, broadcast a
    # full --all alert (all subscribers + the alarm app) when the newest
    # completed candle forms a fresh imbalance. One trendbar fetch/hour.
    # ------------------------------------------------------------------
    # Remember which imbalance each check last saw, so a continuation is
    # only skipped if its first candle was really handled.
    imbalance_gate = ImbalanceGate(period=60)

    async def recent_bars(period: str, count: int = 6) -> list:
        """The latest completed bars on `period`, oldest first."""
        env = feed_account.environment
        info = await markets[env].ensure_symbol(
            feed_account.ctid_account_id, settings.trade_symbol)
        _, payload = await clients[env].request(ct.PT_GET_TRENDBARS_REQ, {
            "ctidTraderAccountId": feed_account.ctid_account_id,
            "symbolId": info.symbol_id,
            "period": period,
            "toTimestamp": int(time.time() * 1000),
            # Ask for more than we need: the gateway counts the forming
            # candle in the limit but returns only completed bars, so
            # count=3 can yield just 2 completed candles and the FVG
            # check never sees a full triplet.
            "count": count,
        })
        bars = payload.get("trendbar", []) or []
        return sorted(
            bars, key=lambda b: int(b.get("utcTimestampInMinutes", 0) or 0))

    def warn_if_stale(timeframe: str, newest_ts: int, period: int) -> None:
        """The bar that just closed should be the newest one returned. If
        the gateway has not published it yet, this boundary's triplet is
        never evaluated, and an imbalance completing on it is missed."""
        expected = just_closed_ts(time.time(), period)
        if newest_ts < expected:
            log.warning("%s imbalance check: newest completed bar is ts=%d "
                        "but the bar that just closed is ts=%d -- gateway "
                        "data is stale, this boundary was not evaluated",
                        timeframe, newest_ts, expected)

    async def imbalance_verdict() -> Optional[dict]:
        """Fetch the last 3 completed H1 bars and evaluate them.

        Returns {'which': 'bullish'|'bearish'|None, 'high1': float,
        'low1': float, 'bars': [bar...]} so callers (the hourly watcher
        and the /imbalance debug command) share one implementation.
        """
        bars = await recent_bars("H1")
        which = fresh_imbalance(bars)
        verdict = {"which": which, "bars": bars, "high1": None,
                   "low1": None,
                   "back_to_back": which is not None and back_to_back(bars)}
        if which is not None and len(bars) >= 3:
            c1 = bars[-3]
            verdict["high1"] = candle_high(c1)
            verdict["low1"] = candle_low(c1)
        return verdict

    async def check_h1_imbalance() -> None:
        verdict = await imbalance_verdict()
        which = verdict["which"]
        bars = verdict["bars"]
        log.info("imbalance check: %d completed bar(s) evaluated",
                 len(bars))
        for b in bars[-3:]:
            log.info("  bar ts=%s low=%.5f high=%.5f",
                     b.get("utcTimestampInMinutes"),
                     candle_low(b), candle_high(b))
        newest_ts = (int(bars[-1].get("utcTimestampInMinutes", 0) or 0)
                     if bars else 0)
        warn_if_stale("H1", newest_ts, 60)
        if which is None:
            return
        previous = imbalance_gate.last_seen
        if not imbalance_gate.admit(newest_ts):
            # Continues an impulse whose first imbalance this watcher
            # already saw (or is that same imbalance again), so it would
            # alert at near-duplicate levels a candle after the last.
            log.info("H1 imbalance: %s on bar ts=%d, but the one on bar "
                     "ts=%s was already seen -- skipping as back to back",
                     which, newest_ts, previous)
            return
        text = "new %s imbalance on H1" % which
        # An imbalance against the bias is context, not a regime change:
        # "supply created -- but we're still bullish."
        bias_state = bias_machine.status()
        if bias_state is not None and which != bias_state.bias:
            text += (" \u2014 but we're still %s" % bias_state.bias)
        log.info("H1 imbalance: %s", text)
        await broadcast(text)
        # Auto-create the two --all alerts for this setup: a price alert
        # on candle 1's entry level and an H1 close alert for the flip.
        owner = settings.allowed_chat_ids[0]
        for kind, level_key, direction, note in IMBALANCE_ALERT_SPECS[which]:
            level = verdict[level_key]
            if kind == "price":
                store.create(owner, settings.trade_symbol, level,
                             direction, note, kind=KIND_ZONE,
                             cc_timeframe="H1", broadcast=True)
            else:
                candle_store.create(owner, settings.trade_symbol, "H1",
                                    level, direction, note, broadcast=True)
                candle_feed.add_symbol(settings.trade_symbol, "H1")
            log.info("imbalance %s: %s alert at %.2f (%s)",
                     which, kind, level, note)
        # The H1 alerts are already armed, so a failure finding M15 gaps
        # must not undo or hide them.
        try:
            await arm_nested_m15(which, bars)
        except Exception:
            log.exception("M15 imbalance scan failed")

    async def arm_nested_m15(which: str, h1_bars: list) -> None:
        """M15 gaps on the H1 imbalance's own move that overlap it.

        The three H1 candles are twelve M15 candles, all closed by now,
        so this runs right after the H1 alerts rather than on a schedule
        of its own. An M15 gap that lines up with an H1 gap later on is a
        different move and is never looked at.
        """
        m15_bars = await recent_bars("M15", count=16)
        newest_ts = (int(m15_bars[-1].get("utcTimestampInMinutes", 0) or 0)
                     if m15_bars else 0)
        warn_if_stale("M15", newest_ts, 15)
        entries = nested_entries(m15_bars, h1_bars, which)
        if not entries:
            log.info("M15: no %s imbalance on this H1 move overlaps it",
                     which)
            return
        h1_bottom, h1_top = gap_bounds(h1_bars, which)
        name = zone_name(which, "H1")
        owner = settings.allowed_chat_ids[0]
        for entry in entries:
            text = ("new %s imbalance on M15, inside %s (%.2f-%.2f)"
                    % (which, name, h1_bottom, h1_top))
            log.info("M15 imbalance: %s", text)
            await broadcast(text)
            store.create(owner, settings.trade_symbol, entry.level,
                         entry.direction, entry.note, kind=KIND_ZONE,
                         cc_timeframe="M15", broadcast=True)
            log.info("imbalance M15 %s: price alert at %.2f (%s)",
                     which, entry.level, entry.note)

    async def imbalance_watcher() -> None:
        """Check shortly after every UTC hour boundary."""
        while True:
            now = time.time()
            next_hour = (int(now // 3600) + 1) * 3600
            await asyncio.sleep(next_hour - now + 5)
            try:
                await check_h1_imbalance()
            except Exception:
                log.exception("H1 imbalance check failed")

    imbalance_task = asyncio.get_running_loop().create_task(
        imbalance_watcher())

    scheduled_task = asyncio.get_running_loop().create_task(
        scheduled_runner.run())

    digest_task = asyncio.get_running_loop().create_task(digest_watcher())

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
    scheduled_task.cancel()
    digest_task.cancel()
    await candle_feed.stop()
    for cli in clients.values():
        await cli.stop()
    store.close()
    candle_store.close()
    wave_store.close()
    zone_store.close()
    subscription_store.close()
    scheduled_store.close()
    fcm_store.close()
    held_store.close()
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