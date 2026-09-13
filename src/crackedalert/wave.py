"""Market structure: BOS and CHoCH detection on closed candles.

See docs/wave-spec.md. Pure logic: no I/O, no fetching, no knowledge of
where candles come from. One instance per (symbol, timeframe).

The cycle is: a move runs, an opposite move qualifies against its line
and validates its extreme as an armed level, price waits, and a close
past that level is the break. The break disarms the side it broke,
commits the opposite side from the window, and starts a fresh move.
Direction alone decides whether a break is called BOS or CHoCH.
"""

from dataclasses import dataclass, replace
from typing import Iterable, NamedTuple, Optional, Tuple

BOS = "BOS"
CHOCH = "CHoCH"
DOJI = "doji"

BULLISH = "bullish"
BEARISH = "bearish"


class Bar(NamedTuple):
    """One closed candle. Prices are absolute, already unscaled."""
    ts: int
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class WaveState:
    """Everything the engine remembers between bars.

    Only one move is live after the first break: the pair for the break
    direction is populated and the other is None. Cold start is the one
    case where both pairs are live at once.

    window_low is the running minimum since valid_high was armed; it
    commits as valid_low at a bullish break. window_high mirrors it.
    """
    symbol: str
    timeframe: str
    direction: Optional[str] = None
    up_top: Optional[float] = None
    up_line: Optional[float] = None
    down_bottom: Optional[float] = None
    down_line: Optional[float] = None
    valid_high: Optional[float] = None
    valid_low: Optional[float] = None
    window_low: Optional[float] = None
    window_high: Optional[float] = None
    last_ts: Optional[int] = None


@dataclass(frozen=True)
class WaveEvent:
    """A break, or a doji worth logging."""
    kind: str
    symbol: str
    timeframe: str
    ts: int
    direction: str = ""
    level: Optional[float] = None
    valid_high: Optional[float] = None
    valid_low: Optional[float] = None


def new_state(symbol: str, timeframe: str) -> WaveState:
    return WaveState(symbol=symbol.upper(), timeframe=timeframe.upper())


def apply_bar(state: WaveState,
              bar: Bar) -> Tuple[WaveState, Tuple[WaveEvent, ...]]:
    """Fold one closed candle into the state.

    Returns the new state and any events it produced. Pure: the input
    state is never mutated.
    """
    # No move at all: seed both and stop. Seeding takes a high and a low
    # and nothing else, so any candle can do it, doji included. This can
    # only be the first bar -- after it, at least one move is always
    # live, and a move killed by a break or displaced by its opposite is
    # dead rather than absent.
    if state.up_top is None and state.down_bottom is None:
        seeded = replace(state,
                         up_top=bar.high, up_line=bar.low,
                         down_bottom=bar.low, down_line=bar.high,
                         last_ts=bar.ts)
        if bar.open == bar.close:
            return seeded, (_doji(seeded, bar),)
        return seeded, ()

    # A doji has no direction, so it cannot extend, qualify, validate or
    # break. Its wick is still price: it widens armed levels and feeds
    # the windows.
    if bar.open == bar.close:
        absorbed = replace(_absorb_price(state, bar), last_ts=bar.ts)
        return absorbed, (_doji(absorbed, bar),)

    # Strict, and before any widening, so a candle can never break a
    # level its own wick just raised.
    if state.valid_high is not None and bar.close > state.valid_high:
        return _break(state, bar, BULLISH)
    if state.valid_low is not None and bar.close < state.valid_low:
        return _break(state, bar, BEARISH)

    structured = _structure(state, bar)
    absorbed = _absorb_price(structured, bar)
    return replace(absorbed, last_ts=bar.ts), ()


def _structure(state: WaveState, bar: Bar) -> WaveState:
    """Extend the live move, or let its opposite qualify against the line.

    Qualifying is structural: the opposite move comes into existence and
    the current one dies. Adopting the validated level is separate and
    subject to outward-only, so a rejected level still flips the move.
    """
    up_top, up_line = state.up_top, state.up_line
    down_bottom, down_line = state.down_bottom, state.down_line
    valid_high, valid_low = state.valid_high, state.valid_low
    window_low, window_high = state.window_low, state.window_high

    if bar.close > bar.open:
        if up_top is not None and bar.high > up_top:
            up_top, up_line = bar.high, bar.low
        if down_line is not None and bar.high > down_line:
            if valid_low is None or down_bottom < valid_low:
                valid_low = down_bottom
                window_high = bar.high      # opens at the validating bar
            up_top, up_line = bar.high, bar.low
            down_bottom = down_line = None
    else:
        if down_bottom is not None and bar.low < down_bottom:
            down_bottom, down_line = bar.low, bar.high
        if up_line is not None and bar.low < up_line:
            if valid_high is None or up_top > valid_high:
                valid_high = up_top
                window_low = bar.low
            down_bottom, down_line = bar.low, bar.high
            up_top = up_line = None

    return replace(state, up_top=up_top, up_line=up_line,
                   down_bottom=down_bottom, down_line=down_line,
                   valid_high=valid_high, valid_low=valid_low,
                   window_low=window_low, window_high=window_high)


def _absorb_price(state: WaveState, bar: Bar) -> WaveState:
    """Widen armed levels by the wick and deepen the open windows.

    Levels only move outward. The windows track how far price went
    regardless of candle direction.
    """
    valid_high, valid_low = state.valid_high, state.valid_low
    window_low, window_high = state.window_low, state.window_high

    if valid_high is not None and bar.high > valid_high:
        valid_high = bar.high
    if valid_low is not None and bar.low < valid_low:
        valid_low = bar.low
    if window_low is not None and bar.low < window_low:
        window_low = bar.low
    if window_high is not None and bar.high > window_high:
        window_high = bar.high

    return replace(state, valid_high=valid_high, valid_low=valid_low,
                   window_low=window_low, window_high=window_high)


def _break(state: WaveState, bar: Bar,
           side: str) -> Tuple[WaveState, Tuple[WaveEvent, ...]]:
    """Commit a break: disarm the broken side, commit the opposite one
    from its window, and start a fresh move on the breaking candle."""
    if side == BULLISH:
        level = state.valid_high
        broken = replace(
            state, direction=BULLISH,
            up_top=bar.high, up_line=bar.low,
            down_bottom=None, down_line=None,
            valid_high=None, valid_low=min(state.window_low, bar.low),
            window_low=None, window_high=bar.high,
            last_ts=bar.ts)
    else:
        level = state.valid_low
        broken = replace(
            state, direction=BEARISH,
            down_bottom=bar.low, down_line=bar.high,
            up_top=None, up_line=None,
            valid_low=None, valid_high=max(state.window_high, bar.high),
            window_high=None, window_low=bar.low,
            last_ts=bar.ts)

    kind = BOS if state.direction in (None, side) else CHOCH
    return broken, (WaveEvent(kind=kind, symbol=state.symbol,
                              timeframe=state.timeframe, ts=bar.ts,
                              direction=side, level=level,
                              valid_high=broken.valid_high,
                              valid_low=broken.valid_low),)


def _doji(state: WaveState, bar: Bar) -> WaveEvent:
    return WaveEvent(kind=DOJI, symbol=state.symbol,
                     timeframe=state.timeframe, ts=bar.ts,
                     valid_high=state.valid_high,
                     valid_low=state.valid_low)


def replay(bars: Iterable[Bar],
           state: WaveState) -> Tuple[WaveState, Tuple[WaveEvent, ...]]:
    """Fold a sequence of closed candles, oldest first."""
    events: list = []
    for bar in bars:
        state, produced = apply_bar(state, bar)
        events.extend(produced)
    return state, tuple(events)
