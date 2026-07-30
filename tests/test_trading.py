"""Trading path tests: volume conversion, payload building, and the
TradingService orchestration against fakes (no network)."""

import asyncio
import time
import unittest

from crackedalert import risk
from crackedalert.bot import formatting as fmt
from crackedalert.bot.handlers import TradeArgs
from crackedalert.config import Account, Settings
from crackedalert.ctrader import client as ct
from crackedalert.ctrader import trading
from crackedalert.ctrader.market import Quote, SymbolInfo

XAU = SymbolInfo(symbol_id=41, name="XAUUSD", digits=2, lot_size=10000,
                 min_volume=100, step_volume=100, pip_position=1)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class VolumeConversion(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(trading.lots_to_volume(0.04, XAU), 400)
        self.assertEqual(trading.lots_to_volume(1.0, XAU), 10000)

    def test_floors_to_step(self):
        # 0.045 lots -> 450 raw -> floored to step 100 -> 400
        self.assertEqual(trading.lots_to_volume(0.045, XAU), 400)

    def test_clamps_up_to_min(self):
        # 0.001 lots -> 10 raw -> below min 100 -> clamped to 100
        self.assertEqual(trading.lots_to_volume(0.001, XAU), 100)

    def test_unknown_lot_size_rejected(self):
        broken = SymbolInfo(1, "X", 2, 0, 0, 0, 1)
        with self.assertRaises(trading.TradeRejected):
            trading.lots_to_volume(0.04, broken)

    def test_roundtrip(self):
        self.assertAlmostEqual(
            trading.volume_to_lots(trading.lots_to_volume(0.04, XAU), XAU),
            0.04)


class PayloadBuilding(unittest.TestCase):
    def _plan(self, kind, placement=None):
        return risk.TradePlan(
            direction=risk.BUY, order_kind=kind, entry_ref=2450.0,
            placement_price=placement, sl=2439.004, tp=2472.006,
            dist=11.0, lots=0.04, risk_usd=50.0, widen_label="", spread=0.2)

    def test_market_payload(self):
        p = trading.build_order_payload(111, XAU, self._plan(risk.MARKET), 400)
        self.assertEqual(p["orderType"], "MARKET")
        self.assertEqual(p["tradeSide"], "BUY")
        self.assertEqual(p["volume"], 400)
        self.assertEqual(p["stopLoss"], 2439.0)      # rounded to digits=2
        self.assertEqual(p["takeProfit"], 2472.01)
        self.assertNotIn("limitPrice", p)
        self.assertNotIn("stopPrice", p)

    def test_limit_payload(self):
        p = trading.build_order_payload(
            111, XAU, self._plan(risk.LIMIT, placement=2400.204), 400)
        self.assertEqual(p["orderType"], "LIMIT")
        self.assertEqual(p["limitPrice"], 2400.2)
        self.assertEqual(p["timeInForce"], "GOOD_TILL_CANCEL")
        self.assertNotIn("stopPrice", p)

    def test_stop_payload(self):
        p = trading.build_order_payload(
            111, XAU, self._plan(risk.STOP, placement=2500.2), 400)
        self.assertEqual(p["orderType"], "STOP")
        self.assertEqual(p["stopPrice"], 2500.2)
        self.assertNotIn("limitPrice", p)


class FakeClient:
    def __init__(self):
        self.connected = True
        self.orders = []

    def add_event_handler(self, *_):
        pass

    async def request(self, payload_type, payload, timeout=None):
        if payload_type == ct.PT_TRADER_REQ:
            return ct.PT_TRADER_RES, {
                "trader": {"balance": 1000000, "moneyDigits": 2}}  # 10000.00
        if payload_type == ct.PT_NEW_ORDER_REQ:
            self.orders.append(payload)
            return ct.PT_EXECUTION_EVENT, {
                "executionType": "ORDER_ACCEPTED",
                "order": {"orderId": 555}}
        raise AssertionError("unexpected request %s" % payload_type)


class FakeMarket:
    def __init__(self, quote):
        self._quote = quote

    async def ensure_quote(self, account_id, symbol_name):
        return XAU, self._quote


def make_settings():
    return Settings(
        telegram_bot_token="t", allowed_chat_ids=[1],
        ctrader_client_id="id", ctrader_client_secret="sec",
        accounts={
            "demo": Account("demo", 111, "demo"),
            "5k": Account("5k", 222, "live"),
        },
        price_feed_account="demo", trade_symbol="XAUUSD")


class TradingServiceTests(unittest.TestCase):
    def setUp(self):
        self.cli = FakeClient()
        self.quote = Quote(bid=2449.8, ask=2450.0,
                           updated_at=time.monotonic())
        self.service = trading.TradingService(
            clients={"demo": self.cli, "live": self.cli},
            markets={"demo": FakeMarket(self.quote),
                     "live": FakeMarket(self.quote)},
            settings=make_settings())

    def test_market_happy_path(self):
        args = TradeArgs(entry=None, sl=2440.0, widen=True, rr=2,
                         risk_pct=0.5, account="demo")
        plan, symbol, result, lots = run(self.service.execute(args, True))
        self.assertEqual(result.order_id, 555)
        self.assertEqual(plan.direction, risk.BUY)
        self.assertAlmostEqual(lots, 0.04)
        sent = self.cli.orders[0]
        self.assertEqual(sent["volume"], 400)
        self.assertEqual(sent["stopLoss"], 2439.0)
        self.assertEqual(sent["takeProfit"], 2472.0)

    def test_live_account_is_locked(self):
        args = TradeArgs(entry=None, sl=2440.0, widen=False, rr=2,
                         risk_pct=0.5, account="5k")
        with self.assertRaises(trading.TradeRejected) as cm:
            run(self.service.execute(args, True))
        self.assertIn("locked", str(cm.exception))

    def test_unknown_account(self):
        args = TradeArgs(entry=None, sl=2440.0, widen=False, rr=2,
                         risk_pct=0.5, account="nope")
        with self.assertRaises(trading.TradeRejected) as cm:
            run(self.service.execute(args, True))
        self.assertIn("not found", str(cm.exception))

    def test_stale_quote_rejected(self):
        service = trading.TradingService(
            clients={"demo": self.cli},
            markets={"demo": FakeMarket(None)},
            settings=make_settings())
        args = TradeArgs(entry=None, sl=2440.0, widen=False, rr=2,
                         risk_pct=0.5, account="demo")
        with self.assertRaises(trading.TradeRejected) as cm:
            run(service.execute(args, True))
        self.assertIn("live price", str(cm.exception))

    def test_pending_places_spread_offset_price(self):
        args = TradeArgs(entry=2400.0, sl=2395.0, widen=False, rr=3,
                         risk_pct=1, account="demo")
        plan, symbol, result, lots = run(self.service.execute(args, False))
        sent = self.cli.orders[0]
        self.assertEqual(sent["orderType"], "LIMIT")
        self.assertEqual(sent["limitPrice"], 2400.2)   # entry + 0.2 spread
        self.assertEqual(sent["stopLoss"], 2395.0)
        self.assertEqual(sent["takeProfit"], 2415.0)   # math on raw entry


class SuccessMessageFormat(unittest.TestCase):
    def test_matches_bash_template(self):
        text = fmt.order_success(
            ticket=555, symbol="XAUUSD", direction="BUY",
            kind_label="MARKET", account="demo", lots=0.04, risk_pct=0.5,
            risk_usd=50.0, entry_label="2450.00", sl=2439.0, tp=2472.0,
            rr=2.0, widen_label=" (tambah 10 pips)", digits=2)
        expected = ("order placed (ticket: #555)\n"
                    "XAUUSD - BUY MARKET (demo)\n"
                    "lots: 0.04 (0.5% risk = $50.00)\n\n"
                    "entry: 2450.00\n"
                    "sl: 2439.00 (tambah 10 pips)\n"
                    "tp: 2472.00 (1:2 RR)")
        self.assertEqual(text, expected)


if __name__ == "__main__":
    unittest.main()
