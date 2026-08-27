"""Fresh fair-value-gap (imbalance) detection on completed candles.

A fair value gap is a 3-candle pattern: the newest closed candle's
extreme does not overlap the candle two bars back, leaving a price
void (an imbalance) on the way. Detection only needs the last three
completed trendbars:

    bullish: bar3.low  > bar1.high   -> zone (bar1.high, bar3.low)
    bearish: bar3.high < bar1.low    -> zone (bar3.high, bar1.low)

The pattern is "fresh" when it completes on the newest closed candle,
which is exactly what this module checks. Bars must be consecutive
(same timeframe) and newest-last.
"""

from typing import List, Optional, Tuple

from .alerts import CANDLE_ABOVE, CANDLE_BELOW, CROSSING_DOWN, CROSSING_UP

# Trendbar prices are ints scaled by PRICE_SCALE (see ctrader/candles.py).
PRICE_SCALE = 100000

# Auto-alerts created when a fresh imbalance forms. Each entry:
# (kind, candle-1 level to watch, alert direction, note).
# Bullish (1->2->3 up): enter the demand zone at candle1.high, and the
# flip trigger is an H1 close below candle1.low.
# Bearish: enter the supply zone at candle1.low, and the flip trigger is
# an H1 close above candle1.high.
IMBALANCE_ALERT_SPECS: dict = {
    "bullish": (
        ("price", "high1", CROSSING_DOWN, "masuk DH1. WATCH!"),
        ("candle", "low1", CANDLE_BELOW,
         "strike 1 of FLIP to the DOWNSIDE. WATCH!"),
    ),
    "bearish": (
        ("price", "low1", CROSSING_UP, "masuk S H1. WATCH!"),
        ("candle", "high1", CANDLE_ABOVE,
         "strike 1 of FLIP to the UPSIDE. WATCH!"),
    ),
}


def candle_high(bar: dict) -> float:
    """Absolute high of one completed trendbar."""
    return _high(bar)


def candle_low(bar: dict) -> float:
    """Absolute low of one completed trendbar."""
    return _low(bar)


def fresh_imbalance(bars: List[dict]) -> Optional[str]:
    """Return 'bullish' or 'bearish' when the newest of the last three
    completed trendbars completes an FVG, else None.

    Each bar needs 'utcTimestampInMinutes', 'low' and 'deltaHigh'.
    """
    if bars is None or len(bars) < 3:
        return None
    c1, _c2, c3 = bars[-3], bars[-2], bars[-1]

    ts1 = int(c1.get("utcTimestampInMinutes", 0) or 0)
    ts3 = int(c3.get("utcTimestampInMinutes", 0) or 0)
    if ts1 <= 0 or ts3 - ts1 != 2 * 60:      # H1: two 60-min steps apart
        return None

    h1 = _high(c1)
    l1 = _low(c1)
    h3 = _high(c3)
    l3 = _low(c3)

    if l3 > h1:
        return "bullish"
    if h3 < l1:
        return "bearish"
    return None


def _low(bar: dict) -> float:
    return int(bar.get("low", 0) or 0) / PRICE_SCALE


def _high(bar: dict) -> float:
    low = int(bar.get("low", 0) or 0)
    delta_high = int(bar.get("deltaHigh", 0) or 0)
    return (low + delta_high) / PRICE_SCALE
