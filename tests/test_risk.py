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
        self.assertAlmostEqual(p.entry_ref, 2400.0)          # raw entry kept
        self.assertAlmostEqual(p.dist, 5.2)                 # fill 2400.2 - sl
        self.assertAlmostEqual(p.tp, 2415.8)                # 2400.2 + 5.2*3
        self.assertAlmostEqual(p.lots, 0.19)                # 100/(5.2*100)

    def test_pending_buy_tp_based_on_placement(self):
        # RR applies to the real distance from the actual fill (placement)
        # to SL, not the raw entry. spread 0.2 -> fill 2400.2, real risk 5.2.
        p = risk.plan_pending(self.BID, self.ASK, entry=2400.0, sl=2395.0,
                              widen=False, rr=3, risk_pct=1, balance=10000)
        placement = p.placement_price
        self.assertAlmostEqual(placement, 2400.2)
        self.assertAlmostEqual(p.dist, placement - p.sl)
        self.assertAlmostEqual(p.tp, placement + (placement - p.sl) * 3)

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
        self.assertAlmostEqual(p.dist, 10.2)                # sl - fill 2499.8
        self.assertAlmostEqual(p.tp, 2489.6)                # 2499.8 - 10.2*1
        self.assertAlmostEqual(p.lots, 0.09)                # 100/(10.2*100)

    def test_sell_below_market_is_stop(self):
        p = risk.plan_pending(self.BID, self.ASK, entry=2400.0, sl=2410.0,
                              widen=False, rr=2, risk_pct=1, balance=10000)
        self.assertEqual(p.direction, risk.SELL)
        self.assertEqual(p.order_kind, risk.STOP)
        self.assertAlmostEqual(p.placement_price, 2399.8)


class LotEdgeCases(unittest.TestCase):
    def test_minimum_lot_not_clamped(self):
        # 100 * 0.1% = 0.1 usd risk, dist 10 -> raw 0.0001. Floored to the
        # 0.01 step this becomes 0.0 (not clamped up to 0.01). trading.py
        # rejects sub-minimum sizes instead of clamping.
        p = risk.plan_market(bid=2449.8, ask=2450.0, sl=2440.0, widen=False,
                             rr=1, risk_pct=0.1, balance=100)
        self.assertEqual(p.lots, 0.0)

    def test_zero_distance_yields_zero_lots(self):
        # SL == fill price -> dist 0 -> lots 0 (caller must reject).
        # Zero spread: SL == entry_ref == fill -> dist 0 -> lots 0.
        p = risk.plan_market(bid=2450.0, ask=2450.0, sl=2450.0,
                             widen=False, rr=1, risk_pct=1, balance=10000)
        self.assertEqual(p.dist, 0.0)
        self.assertEqual(p.lots, 0.0)

    def test_floor_never_exceeds_risk(self):
        # raw 0.0454 lots: bash printf -> 0.05 (over-risk); we floor to 0.04
        p = risk.plan_market(bid=2449.8, ask=2450.0, sl=2439.0, widen=False,
                             rr=2, risk_pct=0.5, balance=10000)
        risk_at_sl = p.lots * p.dist * 100
        self.assertLessEqual(risk_at_sl, p.risk_usd + 1e-9)


class SmartSL(unittest.TestCase):
    """Exact smart-SL price: stop placed at the price, lots anchored to the
    original SL, risk at the smart stop = risk_pct * smart_dist/dist."""

    def test_pending_buy_smart_stop_exact_and_risk_scaled(self):
        # fill 2400.2, sl 2395 (dist 5.2), smart 2398.5 -> smart_dist 1.7
        p = risk.plan_pending(2449.8, 2450.0, entry=2400.0, sl=2395.0,
                              widen=False, rr=3, risk_pct=1, balance=10000,
                              smart_sl=2398.5)
        self.assertAlmostEqual(p.sl, 2398.5)          # stop exactly at price
        self.assertAlmostEqual(p.dist, 5.2)           # original SL distance
        # lots anchored to the ORIGINAL SL distance (== no-mult x1 sizing)
        p_plain = risk.plan_pending(2449.8, 2450.0, entry=2400.0, sl=2395.0,
                                    widen=False, rr=3, risk_pct=1,
                                    balance=10000)
        self.assertAlmostEqual(p.lots, p_plain.lots)
        self.assertAlmostEqual(p.lots, 0.19)
        # risk at the smart stop = 1% * 1.7/5.2
        self.assertAlmostEqual(p.smart_risk_pct, 1 * (1.7 / 5.2))
        # TP still anchored to the original SL distance * RR
        self.assertAlmostEqual(p.tp, 2400.2 + 5.2 * 3)

    def test_pending_sell_smart_stop(self):
        # entry 2500, sl 2510, spread 0.2 -> fill 2499.8 (sell below), so
        # original dist = |2499.8 - 2510| = 10.2; smart 2505 -> smart_dist 5.2
        p = risk.plan_pending(2449.8, 2450.0, entry=2500.0, sl=2510.0,
                              widen=False, rr=1, risk_pct=2, balance=10000,
                              smart_sl=2505.0)
        self.assertAlmostEqual(p.sl, 2505.0)
        smart_dist = abs(2499.8 - 2505.0)
        orig_dist = abs(2499.8 - 2510.0)
        self.assertAlmostEqual(p.smart_risk_pct, 2 * (smart_dist / orig_dist))

    def test_market_buy_smart_stop_uses_ask(self):
        # bid/ask 2449.8/2450.0, SL 2440 -> fill 2450 (ask), smart 2445
        p = risk.plan_market(bid=2449.8, ask=2450.0, sl=2440.0, widen=False,
                             rr=2, risk_pct=0.5, balance=10000,
                             smart_sl=2445.0)
        self.assertEqual(p.direction, risk.BUY)
        self.assertAlmostEqual(p.entry_ref, 2450.0)
        self.assertAlmostEqual(p.sl, 2445.0)          # exact smart stop
        self.assertAlmostEqual(p.dist, 10.0)
        self.assertAlmostEqual(p.smart_risk_pct, 0.5 * (2450.0 - 2445.0) / 10.0)

    def test_smart_equals_original_sl_report_no_division_err(self):
        # smart at the original SL (edge allowed by math but meaningless):
        # lots still anchor to original SL distance.
        p = risk.plan_pending(2449.8, 2450.0, entry=2400.0, sl=2395.0,
                              widen=False, rr=3, risk_pct=1, balance=10000,
                              smart_sl=2395.0)
        self.assertAlmostEqual(p.sl, 2395.0)
        self.assertAlmostEqual(p.smart_risk_pct, 1.0, delta=1e-9)

    def test_no_smart_sl_fields_none(self):
        p = risk.plan_market(bid=2449.8, ask=2450.0, sl=2440.0, widen=False,
                             rr=2, risk_pct=0.5, balance=10000)
        self.assertIsNone(p.smart_sl)
        self.assertIsNone(p.smart_risk_usd)
        self.assertIsNone(p.smart_risk_pct)

    def test_smart_sl_dollar_mode_reports_dollars(self):
        # $50 risk (dollar mode), risk_pct=0; smart stop at half the
        # original distance -> $25 exposure at the smart stop.
        p = risk.plan_market(bid=2449.8, ask=2450.0, sl=2440.0, widen=False,
                             rr=2, risk_pct=0, balance=10000,
                             risk_usd=50.0, smart_sl=2445.0)
        self.assertAlmostEqual(p.sl, 2445.0)
        self.assertAlmostEqual(p.smart_risk_usd, 25.0)   # 50 * (5/10)
        # % of balance: 25 / 10000 * 100
        self.assertAlmostEqual(p.smart_risk_pct, 0.25)

    def test_smart_sl_zero_dist_no_division_error(self):
        # dist == 0 with a smart_sl must not raise a ZeroDivisionError; the
        # pure function reports no smart-risk instead.
        p = risk.plan_market(bid=2450.0, ask=2450.0, sl=2450.0, widen=False,
                             rr=1, risk_pct=1, balance=10000, smart_sl=2450.0)
        self.assertEqual(p.dist, 0.0)
        self.assertIsNone(p.smart_risk_usd)
        self.assertIsNone(p.smart_risk_pct)
        self.assertAlmostEqual(p.smart_sl, 2450.0)


class DollarRisk(unittest.TestCase):
    def test_dollar_override_ignores_balance(self):
        # $50 risk on a $1M balance: same lots as $50 on $10k balance
        p = risk.plan_market(bid=2449.8, ask=2450.0, sl=2440.0, widen=False,
                             rr=2, risk_pct=99, balance=1000000,
                             risk_usd=50.0)
        self.assertAlmostEqual(p.risk_usd, 50.0)
        self.assertAlmostEqual(p.lots, 0.05)   # 50/(10*100), dist=10

    def test_dollar_override_pending(self):
        # real distance is placement 2400.2 - sl 2395 = 5.2
        p = risk.plan_pending(2449.8, 2450.0, entry=2400.0, sl=2395.0,
                              widen=False, rr=3, risk_pct=99, balance=1,
                              risk_usd=100.0)
        self.assertAlmostEqual(p.risk_usd, 100.0)
        self.assertAlmostEqual(p.lots, 0.19)    # 100/(5.2*100)

    def test_dollar_override_zero_balance_still_works(self):
        # balance is irrelevant in dollar mode
        p = risk.plan_market(bid=2449.8, ask=2450.0, sl=2440.0, widen=False,
                             rr=1, risk_pct=0, balance=0, risk_usd=25.0)
        self.assertAlmostEqual(p.risk_usd, 25.0)
        self.assertGreater(p.lots, 0.0)


if __name__ == "__main__":
    unittest.main()