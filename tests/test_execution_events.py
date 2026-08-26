"""Unit tests for ExecutionEvent position-close detection (main.py).

The bot drops cc guards for a position the moment an ExecutionEvent shows
it closed (broker-side SL/TP hit or manual close): the closing order trades
the OPPOSITE side of the position, while open fills carry the same side on
both. Guards must never linger for dead positions.
"""

import unittest

from crackedalert.main import _execution_closes_position


class ExecutionCloseDetectionTests(unittest.TestCase):

    def test_close_order_opposite_side(self):
        # BUY position closed by its SL: the closing market order is SELL.
        order = {"orderId": 1, "orderType": "MARKET",
                 "tradeData": {"tradeSide": "SELL"}}
        position = {"positionId": 9, "tradeData": {"tradeSide": "BUY"}}
        self.assertTrue(_execution_closes_position(order, position))

    def test_tp_close_opposite_side(self):
        order = {"orderId": 2, "orderType": "MARKET",
                 "tradeData": {"tradeSide": "BUY"}}
        position = {"positionId": 9, "tradeData": {"tradeSide": "SELL"}}
        self.assertTrue(_execution_closes_position(order, position))

    def test_open_fill_same_side_not_close(self):
        order = {"orderId": 3, "orderType": "MARKET",
                 "tradeData": {"tradeSide": "BUY"}}
        position = {"positionId": 9, "tradeData": {"tradeSide": "BUY"}}
        self.assertFalse(_execution_closes_position(order, position))

    def test_missing_trade_data_not_close(self):
        self.assertFalse(_execution_closes_position({}, {"positionId": 9}))
        self.assertFalse(_execution_closes_position(None, None))
        self.assertFalse(_execution_closes_position(
            {"orderId": 1}, {"positionId": 9, "tradeData": {}}))


if __name__ == "__main__":
    unittest.main()
