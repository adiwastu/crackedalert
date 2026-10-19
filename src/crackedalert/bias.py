"""The operator's regime model: a bias with a two-strike flip rule.

While bullish, demand zones are home and supply is context:
  - a fresh supply imbalance is announced but framed ("still bullish");
  - a supply zone flipped (closed above) is absorbed -- the flipped
    supply now acts as a demand area, and its level is re-armed as a
    DH1-style touch alert;
  - a DEMAND zone flipped (closed below) is a strike. Two strikes flip
    the regime bearish. Mirrored exactly while bearish.

The strike counter comes from the imbalance watcher's own flip alerts
(notes like "strike 1 of FLIP to the DOWNSIDE") -- this module adds the
memory those alerts never had. State is one row in SQLite; the operator
can override the bias at any time (/bias in chat), which resets strikes.
"""

import sqlite3
import time
from dataclasses import dataclass
from typing import Optional, Tuple

BULLISH = "bullish"
BEARISH = "bearish"
DEMAND = "demand"
SUPPLY = "supply"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bias_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    bias        TEXT NOT NULL,
    strikes     INTEGER NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT '',
    updated_at  INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class BiasState:
    bias: str
    strikes: int
    note: str
    updated_at: int


@dataclass(frozen=True)
class FlipOutcome:
    """What one zone flip did to the regime.

    kind: 'strike' (counted, regime unchanged), 'regime_flip' (strikes
    hit 2 -- the bias flipped), 'absorbed' (a flip in the bias's favor:
    the old zone is dead, but nothing transforms -- a proper new zone
    has to form before the area is treated as the bias's own kind),
    'seeded' (there was no bias yet; the flip founded one).
    """
    kind: str
    state: BiasState
    flipped: str          # 'demand' | 'supply' -- the zone that flipped


def flipped_kind_from_note(note: str) -> Optional[str]:
    """Map a flip alert's note to the kind of zone that flipped.

    'strike 1 of FLIP to the DOWNSIDE' -> a DEMAND zone flipped
    (price closed below it); '...UPSIDE' -> a SUPPLY zone flipped.
    """
    upper = note.upper()
    if "FLIP TO THE DOWNSIDE" in upper:
        return DEMAND
    if "FLIP TO THE UPSIDE" in upper:
        return SUPPLY
    return None


def zone_label(kind: str) -> str:
    return "DH1" if kind == DEMAND else "S H1"


def _other(bias: str) -> str:
    return BEARISH if bias == BULLISH else BULLISH


def on_zone_flip(bias: Optional[BiasState], flipped: str,
                 now: Optional[int] = None) -> FlipOutcome:
    """Fold one zone flip into the regime. Pure.

    With no bias yet, the flip founds one pointing in the flip's own
    direction (a demand zone flipping is bearish evidence), strikes at
    1 -- the next flip of the SAME kind against that bias is strike 2.

    A flip in the bias's favor only kills the old zone. It does NOT
    transform it: the area becomes demand/supply only when a proper new
    imbalance forms there, which the hourly watcher announces on its own.
    """
    now = int(time.time()) if now is None else now
    if bias is None or not bias.bias:
        new_bias = BEARISH if flipped == DEMAND else BULLISH
        return FlipOutcome(
            kind="seeded",
            state=BiasState(new_bias, 1, "seeded by %s flip" % flipped,
                            now),
            flipped=flipped)
    if flipped == zone_kind(bias.bias):
        # A flip against the bias: demand died while bullish.
        strikes = bias.strikes + 1
        if strikes >= 2:
            new_bias = _other(bias.bias)
            return FlipOutcome(
                kind="regime_flip",
                state=BiasState(new_bias, 0,
                                "%s flipped twice" % zone_label(flipped),
                                now),
                flipped=flipped)
        return FlipOutcome(
            kind="strike",
            state=BiasState(bias.bias, strikes, "strike %d" % strikes,
                            now),
            flipped=flipped)
    # A flip in the bias's favor: supply dying while bullish. The old
    # zone is gone; no transformation until a proper one forms.
    return FlipOutcome(
        kind="absorbed",
        state=BiasState(bias.bias, bias.strikes,
                        "%s absorbed" % zone_label(flipped), now),
        flipped=flipped)


def zone_kind(bias: str) -> str:
    """The zone kind whose flip strikes against this bias."""
    return DEMAND if bias == BULLISH else SUPPLY


def strike_message(outcome: FlipOutcome) -> str:
    label = zone_label(outcome.flipped)
    return ("%s flipped \u2014 <b>strike %d of 2</b> against the %s bias. "
            "One more flips the regime %s."
            % (label, outcome.state.strikes, outcome.state.bias,
               _other(outcome.state.bias)))


def regime_flip_message(outcome: FlipOutcome) -> str:
    return ("regime flipped: <b>%s</b> (%s flipped twice). "
            "Looking for %s zones now \u2014 past ones included."
            % (outcome.state.bias.upper(), zone_label(outcome.flipped),
               zone_label(zone_kind(outcome.state.bias))))


def absorbed_message(outcome: FlipOutcome) -> str:
    # The kind a proper new zone would need to form as, to serve the
    # current bias: demand while bullish, supply while bearish.
    needed = zone_kind(outcome.state.bias)
    return ("%s flipped \u2014 still <b>%s</b>. A flip alone doesn't "
            "make it %s: waiting for a proper %s to form there first."
            % (zone_label(outcome.flipped), outcome.state.bias,
               needed, needed))


def seeded_message(outcome: FlipOutcome) -> str:
    return ("no bias on record, so this founds one: <b>%s</b> "
            "(%s flipped), strike 1. Correct it with /bias if wrong."
            % (outcome.state.bias, zone_label(outcome.flipped)))


class BiasStore:
    """The single regime row. None until the first flip or /bias set."""

    def __init__(self, db_path: str):
        self._db = sqlite3.connect(db_path)
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def get(self) -> Optional[BiasState]:
        row = self._db.execute(
            "SELECT bias, strikes, note, updated_at FROM bias_state "
            "WHERE id = 1").fetchone()
        if row is None:
            return None
        return BiasState(*row)

    def put(self, state: BiasState) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO bias_state "
            "(id, bias, strikes, note, updated_at) "
            "VALUES (1, ?, ?, ?, ?)",
            (state.bias, state.strikes, state.note, state.updated_at))
        self._db.commit()


class BiasMachine:
    """Store + logic behind the /bias command and the flip alerts."""

    def __init__(self, store: BiasStore):
        self._store = store

    def status(self) -> Optional[BiasState]:
        return self._store.get()

    def set(self, bias: str, note: str = "set by operator") -> BiasState:
        state = BiasState(bias, 0, note, int(time.time()))
        self._store.put(state)
        return state

    def flip(self, flipped: str) -> FlipOutcome:
        outcome = on_zone_flip(self._store.get(), flipped)
        self._store.put(outcome.state)
        return outcome
