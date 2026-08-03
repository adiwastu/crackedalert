"""Golden tests: risk.py must reproduce the bash/AWK engine's numbers.

Hand-computed against bin/cracked_listener.sh logic (widen = 1.00 as of the
10-pip change). The single sanctioned difference is D9: lots floor to the
step instead of printf %.2f rounding.
"""

import unittest

from crackedalert import risk


class MarketOrders(unittest.TestCase):
    def test_buy_uses_ask_and_widen(self):
        # bid/ask 2449.8/2450.0 -> mid 2449.9; SL below mid -> BUY at ask
        p = risk.plan_market(bid=2449.8, ask=2450.0, sl=2440.0, widen=True,
                             rr=2, risk_pct=0.5, balance=10000)
        self.assertEqual(p.direction, risk.BUY)
        self.assertEqual(p.order_kind, risk.MARKET)
        self.assertEqual(p.entry_ref, 2450.0)
        self.assertIsNone(p.placement_price)
        self.assertAlmostEqual(p.sl, 2439.0)          # widened by 1.00
        self.assertAlmostEqual(p.dist, 11.0)
        self.assertAlmostEqual(p.tp, 2472.0)          # 2450 + 11*2
        self.assertAlmostEqual(p.risk_usd, 50.0)      # 10000 * 0.5%
        # raw 50/(11*100)=0.04545 -> floor to 0.04 (bash rounded to 0.05)
        self.assertAlmostEqual(p.lots, 0.04)
        self.assertEqual(p.widen_label, " (tambah 10 pips)")

    def test_sell_uses_bid(self):
        p = risk.plan_market(bid=2449.8, ask=2450.0, sl=2460.0, widen=True,
                             rr=2, risk_pct=0.5, balance=10000)
        self.assertEqual(p.direction, risk.SELL)
        self.assertEqual(p.entry_ref, 2449.8)
        self.assertAlmostEqual(p.sl, 2461.0)          # widened upward
        self.assertAlmostEqual(p.dist, 11.2)
        self.assertAlmostEqual(p.tp, 2427.4)          # 2449.8 - 11.2*2
        self.assertAlmostEqual(p.lots, 0.04)          # 50/1120 -> floor

    def test_no_widen_has_empty_label(self):
        p = risk.plan_market(bid=2449.8, ask=2450.0, sl=2440.0, widen=False,
                             rr=1, risk_pct=1, balance=5000)
        self.assertAlmostEqual(p.sl, 2440.0)
        self.assertEqual(p.widen_label, "")


class PendingOrders(unittest.TestCase):
    BID, ASK = 2449.8, 2450.0   # spread 0.2

    def test_buy_below_market_is_limit_offset_up(self):
        p = risk.plan_pending(self.BID, self.ASK, entry=2400.0, sl=2395.0,
                              widen=False, rr=3, risk_pct=1, balance=10000)
        self.assertEqual(p.direction, risk.BUY)
        self.assertEqual(p.order_kind, risk.LIMIT)
        self.assertAlmostEqual(p.placement_price, 2400.2)   # entry + spread
        self.assertAlmostEqual(p.entry_ref, 2400.0)          # math on raw
        self.assertAlmostEqual(p.dist, 5.0)
        self.assertAlmostEqual(p.tp, 2415.0)                 # 2400 + 5*3
        self.assertAlmostEqual(p.lots, 0.2)                  # 100/(5*100)

    def test_buy_above_market_is_stop(self):
        p = risk.plan_pending(self.BID, self.ASK, entry=2500.0, sl=2490.0,
                              widen=False, rr=2, risk_pct=1, balance=10000)
        self.assertEqual(p.direction, risk.BUY)
        self.assertEqual(p.order_kind, risk.STOP)
        self.assertAlmostEqual(p.placement_price, 2500.2)

    def test_sell_above_market_is_limit_offset_down(self):
        p = risk.plan_pending(self.BID, self.ASK, entry=2500.0, sl=2510.0,
                              widen=False, rr=1, risk_pct=1, balance=10000)
        self.assertEqual(p.direction, risk.SELL)
        self.assertEqual(p.order_kind, risk.LIMIT)
        self.assertAlmostEqual(p.placement_price, 2499.8)   # entry - spread
        self.assertAlmostEqual(p.tp, 2490.0)                # 2500 - 10*1

    def test_sell_below_market_is_stop(self):
        p = risk.plan_pending(self.BID, self.ASK, entry=2400.0, sl=2410.0,
                              widen=False, rr=2, risk_pct=1, balance=10000)
        self.assertEqual(p.direction, risk.SELL)
        self.assertEqual(p.order_kind, risk.STOP)
        self.assertAlmostEqual(p.placement_price, 2399.8)


class LotEdgeCases(unittest.TestCase):
    def test_minimum_lot_clamp(self):
        # 100 * 0.1% = 0.1 usd risk, dist 10 -> raw 0.0001 -> clamp 0.01
        p = risk.plan_market(bid=2449.8, ask=2450.0, sl=2440.0, widen=False,
                             rr=1, risk_pct=0.1, balance=100)
        self.assertAlmostEqual(p.lots, 0.01)

    def test_zero_distance_yields_zero_lots(self):
        # SL == entry ref -> dist 0 -> lots 0 (caller must reject)
        p = risk.plan_pending(2449.8, 2450.0, entry=2400.0, sl=2400.0,
                              widen=False, rr=1, risk_pct=1, balance=10000)
        self.assertEqual(p.lots, 0.0)

    def test_floor_never_exceeds_risk(self):
        # raw 0.0454 lots: bash printf -> 0.05 (over-risk); we floor to 0.04
        p = risk.plan_market(bid=2449.8, ask=2450.0, sl=2439.0, widen=False,
                             rr=2, risk_pct=0.5, balance=10000)
        risk_at_sl = p.lots * p.dist * 100
        self.assertLessEqual(risk_at_sl, p.risk_usd + 1e-9)


class DollarRisk(unittest.TestCase):
    def test_dollar_override_ignores_balance(self):
        # $50 risk on a $1M balance: same lots as $50 on $10k balance
        p = risk.plan_market(bid=2449.8, ask=2450.0, sl=2440.0, widen=False,
                             rr=2, risk_pct=99, balance=1000000,
                             risk_usd=50.0)
        self.assertAlmostEqual(p.risk_usd, 50.0)
        self.assertAlmostEqual(p.lots, 0.05)   # 50/(10*100), dist=10

    def test_dollar_override_pending(self):
        p = risk.plan_pending(2449.8, 2450.0, entry=2400.0, sl=2395.0,
                              widen=False, rr=3, risk_pct=99, balance=1,
                              risk_usd=100.0)
        self.assertAlmostEqual(p.risk_usd, 100.0)
        self.assertAlmostEqual(p.lots, 0.2)    # 100/(5*100)

    def test_dollar_override_zero_balance_still_works(self):
        # balance is irrelevant in dollar mode
        p = risk.plan_market(bid=2449.8, ask=2450.0, sl=2440.0, widen=False,
                             rr=1, risk_pct=0, balance=0, risk_usd=25.0)
        self.assertAlmostEqual(p.risk_usd, 25.0)
        self.assertGreater(p.lots, 0.0)


if __name__ == "__main__":
    unittest.main()
