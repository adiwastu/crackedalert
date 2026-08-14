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
    """Return the raw floored lots. No min-lot clamp here: enforcing the
    floor is the caller's job (trading.py rejects sub-minimum sizes)."""
    if dist <= 0:
        return 0.0
    raw = risk_usd / (dist * usd_per_point_per_lot)
    floored = math.floor(raw / lot_step + 1e-9) * lot_step
    return round(floored, 8)


def _build(direction: str, order_kind: str, entry_ref: float,
           placement_price: Optional[float], sl: float, widen: bool,
           rr: float, risk_pct: float, balance: float, spread: float,
           usd_per_point_per_lot: float, min_lots: float,
           lot_step: float, risk_usd_override: Optional[float] = None,
           basis: Optional[float] = None
           ) -> TradePlan:
    widen_label = ""
    if widen:
        sl = sl - WIDEN_AMOUNT if direction == BUY else sl + WIDEN_AMOUNT
        widen_label = WIDEN_LABEL

    # Pending orders fill at the spread-adjusted placement price, so their
    # SL/TP/dist/lots are computed off that actual fill (basis). Market
    # orders: basis == entry_ref (the fill price).
    if basis is None:
        basis = entry_ref
    dist = abs(basis - sl)
    tp = basis + dist * rr if direction == BUY else basis - dist * rr

    if risk_usd_override is not None:
        risk_usd = risk_usd_override
    else:
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
                lot_step: float = 0.01,
                risk_usd: Optional[float] = None) -> TradePlan:
    """Market order: entry reference is the side MT5/cTrader actually fills.

    Direction is inferred against the mid-price (bash parity), then the
    math entry becomes ask for BUY, bid for SELL.

    risk_usd overrides the percentage-based risk (dollar mode): when set,
    risk_pct is ignored and the exact dollar amount is used.
    """
    mid = (bid + ask) / 2.0
    direction = BUY if sl < mid else SELL
    entry_ref = ask if direction == BUY else bid
    return _build(direction, MARKET, entry_ref, None, sl, widen, rr,
                  risk_pct, balance, ask - bid,
                  usd_per_point_per_lot, min_lots, lot_step,
                  risk_usd_override=risk_usd)


def plan_pending(bid: float, ask: float, entry: float, sl: float, widen: bool,
                 rr: float, risk_pct: float, balance: float,
                 usd_per_point_per_lot: float = 100.0,
                 min_lots: float = 0.01,
                 lot_step: float = 0.01,
                 risk_usd: Optional[float] = None) -> TradePlan:
    """Pending order: placement price is spread-offset (BUY above, SELL
    below); LIMIT/STOP inferred from where the placement price sits relative
    to the current book. SL/TP/dist/lots are computed off the placement
    price (the actual fill), not the raw entry.

    risk_usd overrides the percentage-based risk (dollar mode): when set,
    risk_pct is ignored and the exact dollar amount is used.
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
                  usd_per_point_per_lot, min_lots, lot_step,
                  risk_usd_override=risk_usd, basis=placement)
