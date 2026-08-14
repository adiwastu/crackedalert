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

    def test_sub_minimum_rejected(self):
        # 0.001 lots -> 10 raw -> below min 100 -> rejected, not clamped
        with self.assertRaises(trading.TradeRejected) as cm:
            trading.lots_to_volume(0.001, XAU)
        self.assertIn("minimum", str(cm.exception))

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
        self.closes = []
        self.amends = []
        self.cancels = []
        self.reconcile_payload = {
            "position": [
                {"positionId": 1,
                 "tradeData": {"symbolId": 41, "volume": 400,
                               "tradeSide": "BUY"},
                 "price": 2450.0, "stopLoss": 2439.0, "takeProfit": 2472.0,
                 "swap": -120, "moneyDigits": 2},
            ],
            "order": [
                {"orderId": 7,
                 "tradeData": {"symbolId": 41, "volume": 2000,
                               "tradeSide": "BUY"},
                 "orderType": "LIMIT", "limitPrice": 2400.2,
                 "stopLoss": 2395.0, "takeProfit": 2415.0},
            ],
        }

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
        if payload_type == ct.PT_RECONCILE_REQ:
            return ct.PT_RECONCILE_RES, self.reconcile_payload
        if payload_type == ct.PT_CLOSE_POSITION_REQ:
            self.closes.append(payload)
            return ct.PT_EXECUTION_EVENT, {
                "executionType": "ORDER_FILLED",
                "order": {"orderId": 999}}
        if payload_type == ct.PT_AMEND_POSITION_SLTP_REQ:
            self.amends.append(payload)
            return ct.PT_EXECUTION_EVENT, {
                "executionType": "ORDER_ACCEPTED",
                "order": {"orderId": 888}}
        if payload_type == ct.PT_CANCEL_ORDER_REQ:
            self.cancels.append(payload)
            return ct.PT_EXECUTION_EVENT, {
                "executionType": "ORDER_CANCELLED",
                "order": {"orderId": payload["orderId"]}}
        raise AssertionError("unexpected request %s" % payload_type)


class FakeMarket:
    def __init__(self, quote):
        self._quote = quote
        self._symbols = {111: {"XAUUSD": XAU}}

    async def ensure_quote(self, account_id, symbol_name):
        return XAU, self._quote

    def symbol_name(self, account_id, symbol_id):
        for name, info in self._symbols.get(account_id, {}).items():
            if info.symbol_id == symbol_id:
                return name
        return None

    def symbol_info(self, account_id, symbol_id):
        for info in self._symbols.get(account_id, {}).values():
            if info.symbol_id == symbol_id:
                return info
        return None

    def quote(self, account_id, symbol_id):
        return self._quote


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

    def test_live_account_works(self):
        # LIVE_TRADING_ENABLED is True after the Phase 5 cutover.
        args = TradeArgs(entry=None, sl=2440.0, widen=False, rr=2,
                         risk_pct=0.5, account="5k")
        plan, symbol, result, lots = run(self.service.execute(args, True))
        self.assertEqual(result.order_id, 555)
        self.assertEqual(plan.direction, risk.BUY)

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
        self.assertEqual(sent["takeProfit"], 2415.8)   # 2400.2 + 5.2*3

    def test_dollar_risk_executes_exact_amount(self):
        args = TradeArgs(entry=None, sl=2440.0, widen=False, rr=2,
                         risk_pct=0.0, account="demo", risk_usd=50.0)
        plan, symbol, result, lots = run(self.service.execute(
            args, True, risk_usd=args.risk_usd))
        self.assertAlmostEqual(plan.risk_usd, 50.0)
        self.assertAlmostEqual(lots, 0.05)   # 50/(10*100), dist=10

    def test_balance(self):
        bal = run(self.service.balance("demo"))
        self.assertAlmostEqual(bal, 10000.0)   # 1000000 / 10^2

    def test_balance_unknown_account(self):
        with self.assertRaises(trading.TradeRejected):
            run(self.service.balance("nope"))

    def test_positions(self):
        rows = run(self.service.positions_or_orders("demo", True))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], 1)
        self.assertEqual(row["symbol"], "XAUUSD")
        self.assertEqual(row["side"], "BUY")
        self.assertAlmostEqual(row["volume"], 0.04)   # 400 / 10000
        self.assertAlmostEqual(row["price"], 2450.0)
        self.assertAlmostEqual(row["sl"], 2439.0)
        self.assertAlmostEqual(row["tp"], 2472.0)
        self.assertIn("swap", row["extra"])

    def test_orders(self):
        rows = run(self.service.positions_or_orders("demo", False))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], 7)
        self.assertEqual(row["symbol"], "XAUUSD")
        self.assertAlmostEqual(row["volume"], 0.2)   # 2000 / 10000
        self.assertAlmostEqual(row["price"], 2400.2)
        self.assertEqual(row["extra"], "LIMIT")

    def test_positions_unknown_account(self):
        with self.assertRaises(trading.TradeRejected):
            run(self.service.positions_or_orders("nope", True))

    def test_close_all(self):
        results = run(self.service.close_all("demo"))
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        self.assertEqual(results[0]["message"], "closed")
        sent = self.cli.closes[0]
        self.assertEqual(sent["positionId"], 1)
        self.assertEqual(sent["volume"], 400)   # full volume

    def test_close_all_live_works(self):
        # LIVE_TRADING_ENABLED is True after the cutover.
        results = run(self.service.close_all("5k"))
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])

    def test_close_position(self):
        run(self.service.close_position("demo", 1))
        sent = self.cli.closes[0]
        self.assertEqual(sent["positionId"], 1)
        self.assertEqual(sent["volume"], 400)   # full volume

    def test_close_position_not_found(self):
        with self.assertRaises(trading.TradeRejected) as cm:
            run(self.service.close_position("demo", 999))
        self.assertIn("not found", str(cm.exception))

    def test_cancel_order(self):
        run(self.service.cancel_order("demo", 7))
        sent = self.cli.cancels[0]
        self.assertEqual(sent["orderId"], 7)
        self.assertEqual(sent["ctidTraderAccountId"], 111)

    def test_breakeven_buy_ready(self):
        # BUY entry 2450, spread 0.2 -> BE at 2450.2; bid 2449.8 < 2450.2
        # so NOT ready yet -> skipped
        results = run(self.service.breakeven("demo"))
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertIn("not in profit", results[0]["message"])
        self.assertAlmostEqual(results[0]["be_sl"], 2450.2)
        self.assertEqual(self.cli.amends, [])   # nothing sent

    def test_breakeven_buy_ready_when_price_moved(self):
        # Move bid above BE: bid 2450.5, ask 2450.7 -> spread 0.2
        quote = Quote(bid=2450.5, ask=2450.7, updated_at=time.monotonic())
        service = trading.TradingService(
            clients={"demo": self.cli},
            markets={"demo": FakeMarket(quote)},
            settings=make_settings())
        results = run(service.breakeven("demo"))
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        self.assertAlmostEqual(results[0]["be_sl"], 2450.2)
        sent = self.cli.amends[0]
        self.assertEqual(sent["positionId"], 1)
        self.assertAlmostEqual(sent["stopLoss"], 2450.2)
        self.assertNotIn("takeProfit", sent)   # TP preserved (not sent)


class SuccessMessageFormat(unittest.TestCase):
    def test_matches_bash_template(self):
        text = fmt.order_success(
            ticket=555, symbol="XAUUSD", direction="BUY",
            kind_label="MARKET", account="demo", lots=0.04, risk_pct=0.5,
            risk_usd=50.0, entry_label="2450.00", sl=2439.0, tp=2472.0,
            rr=2.0, widen_label=" (tambah 10 pips)", digits=2)
        # New HTML template includes side glyph and <code> wrappers
        self.assertIn("BUY XAUUSD", text)
        self.assertIn("MARKET", text)
        self.assertIn("demo", text)
        self.assertIn("2450.00", text)
        self.assertIn("2439.00 (tambah 10 pips)", text)
        self.assertIn("2472.00", text)
        self.assertIn("0.04", text)
        self.assertIn("0.5% risk", text)
        self.assertIn("$50.00", text)
        self.assertIn("RR 1:2", text)
        self.assertIn("ticket #555", text)
        self.assertIn("<code>", text)


if __name__ == "__main__":
    unittest.main()
