"""Pure trade math -- port of the bash/AWK engine, no I/O.

Parity contract (see PLAN.md §5): identical numbers to the bash bot except
lot flooring (D9): lots floor to the volume step instead of printf rounding,
so the stated risk %% is never exceeded.
"""

import math
from dataclasses import dataclass
from typing import Optional

WIDEN_AMOUNT = 1.00           # absolute price units (= 10 pips on XAUUSD)
WIDEN_LABEL = " (tambah 10 pips)"

BUY = "BUY"
SELL = "SELL"
MARKET = "MARKET"
LIMIT = "LIMIT"
STOP = "STOP"


@dataclass(frozen=True)
class TradePlan:
    direction: str            # BUY | SELL
    order_kind: str           # MARKET | LIMIT | STOP
    entry_ref: float          # price all math is based on
    placement_price: Optional[float]  # actual pending price (None for market)
    sl: float
    tp: float
    dist: float
    lots: float
    risk_usd: float
    widen_label: str          # "" or WIDEN_LABEL
    spread: float


def _lots(risk_usd: float, dist: float, usd_per_point_per_lot: float,
          min_lots: float, lot_step: float) -> float:
    if dist <= 0:
        return 0.0
    raw = risk_usd / (dist * usd_per_point_per_lot)
    floored = math.floor(raw / lot_step + 1e-9) * lot_step
    return max(round(floored, 8), min_lots)


def _build(direction: str, order_kind: str, entry_ref: float,
           placement_price: Optional[float], sl: float, widen: bool,
           rr: float, risk_pct: float, balance: float, spread: float,
           usd_per_point_per_lot: float, min_lots: float,
           lot_step: float) -> TradePlan:
    widen_label = ""
    if widen:
        sl = sl - WIDEN_AMOUNT if direction == BUY else sl + WIDEN_AMOUNT
        widen_label = WIDEN_LABEL

    dist = abs(entry_ref - sl)
    tp = entry_ref + dist * rr if direction == BUY else entry_ref - dist * rr

    risk_usd = balance * (risk_pct / 100.0)
    lots = _lots(risk_usd, dist, usd_per_point_per_lot, min_lots, lot_step)

    return TradePlan(
        direction=direction, order_kind=order_kind, entry_ref=entry_ref,
        placement_price=placement_price, sl=sl, tp=tp, dist=dist, lots=lots,
        risk_usd=risk_usd, widen_label=widen_label, spread=spread)


def plan_market(bid: float, ask: float, sl: float, widen: bool, rr: float,
                risk_pct: float, balance: float,
                usd_per_point_per_lot: float = 100.0,
                min_lots: float = 0.01,
                lot_step: float = 0.01) -> TradePlan:
    """Market order: entry reference is the side MT5/cTrader actually fills.

    Direction is inferred against the mid-price (bash parity), then the
    math entry becomes ask for BUY, bid for SELL.
    """
    mid = (bid + ask) / 2.0
    direction = BUY if sl < mid else SELL
    entry_ref = ask if direction == BUY else bid
    return _build(direction, MARKET, entry_ref, None, sl, widen, rr,
                  risk_pct, balance, ask - bid,
                  usd_per_point_per_lot, min_lots, lot_step)


def plan_pending(bid: float, ask: float, entry: float, sl: float, widen: bool,
                 rr: float, risk_pct: float, balance: float,
                 usd_per_point_per_lot: float = 100.0,
                 min_lots: float = 0.01,
                 lot_step: float = 0.01) -> TradePlan:
    """Pending order: math on the user's raw entry; placement price is
    spread-offset (BUY above, SELL below); LIMIT/STOP inferred from where
    the placement price sits relative to the current book.
    """
    direction = BUY if sl < entry else SELL
    spread = ask - bid

    if direction == BUY:
        placement = entry + spread
        order_kind = STOP if placement > ask else LIMIT
    else:
        placement = entry - spread
        order_kind = STOP if placement < bid else LIMIT

    return _build(direction, order_kind, entry, placement, sl, widen, rr,
                  risk_pct, balance, spread,
                  usd_per_point_per_lot, min_lots, lot_step)
