# Wave analysis: BOS and CHoCH

Spec for a market structure engine. Runs on closed candles only. One
instance per (symbol, timeframe). Timeframes never talk to each other.

This describes both sides explicitly where they differ in a way that
matters, and says "mirrored" where the bearish side is the exact
reflection of the bullish one: swap high for low, above for below, top
for bottom, bullish for bearish.

## Notation

Candles are written `open high low close`. Prices are small integers so
the shapes are easy to read. A candle is **bullish** when close is above
open, **bearish** when close is below open, and a **doji** when they are
equal.

All comparisons are strict. Nowhere in this spec does a level get beaten
by equalling it.

## State

The engine carries eleven values between bars. Naming them up front
matters, because two pairs are easy to confuse and are not the same
thing.

| Field | Meaning |
|---|---|
| `direction` | `bullish`, `bearish`, or none before the first break |
| `up_top` | highest high of the live up move |
| `up_line` | low of the last candle that extended the up move |
| `down_bottom` | lowest low of the live down move |
| `down_line` | high of the last candle that extended the down move |
| `valid_high` | the level a close must beat for a bullish break |
| `valid_low` | the level a close must beat for a bearish break |
| `window_low` | running minimum low since `valid_high` was armed |
| `window_high` | running maximum high since `valid_low` was armed |
| `last_ts` | timestamp of the last bar folded in |

Any of these may be absent.

**`window_low` is not `valid_low`.** `valid_low` is an armed level that a
close can break. `window_low` is a running extreme that has not become a
level yet: it records how deep price went while the engine waited, and it
is committed as the new `valid_low` at the moment of a bullish break.
They move independently and usually hold different numbers. Mirrored for
`window_high` and `valid_high`.

There is a standing invariant: `valid_high` is armed exactly when
`window_low` is open, and `valid_low` exactly when `window_high` is open.
Every rule below preserves it.

## Moves

**Up move.** A run of candles going up. It has a top, the highest high in
the run, and a line, the low of the last candle that extended it.

**Down move.** The mirror: a bottom, the lowest low, and a line, the high
of the last candle that extended it.

**Seeding.** When there is no move at all, a candle seeds both: an up
move with its high as the top and its low as the line, and a down move
with its low as the bottom and its high as the line. Seeding takes a high
and a low and nothing else, so it needs no direction and any candle can
do it, doji included.

Seeding is what extending does when there is nothing to extend. It is not
a first-candle special case, though in practice it can only ever happen
on the first candle, because from then on at least one move is always
live. It never revives a move that a break killed or that its opposite
displaced: those are dead, not absent.

**Extending.** A candle extends the up move only if it closes bullish AND
its high beats the current top. The top becomes its high and the line
becomes its low. A candle that does neither is ignored completely and has
no effect on any level.

A bullish candle may dip below the previous candle's low and still be
part of the up move, as long as it closes bullish and beats the top. The
line then moves down to that candle's low.

Mirrored: a candle extends the down move only if it closes bearish AND
its low beats the current bottom.

**Qualifying.** A candle whose low breaks the up move's line AND which
closes bearish is a down move. A bearish candle that does not break the
line is nothing. A candle that breaks the line but closes bullish is not
a down move.

Mirrored: a candle whose high breaks the down move's line AND which
closes bullish is an up move.

Qualifying is structural and always happens when the conditions are met.
The opposite move comes into existence, taking the candle's own extremes,
and the move it qualified against dies. There is only ever one live move,
except during cold start (below).

## Arming a level

When a down move qualifies, the up move's top becomes the **valid high**,
and the engine is waiting for a break on that side.

If the qualifying candle's own high is above that top, the level is the
candle's high instead. It closed against the move, so its high was never
folded into the top; arming at the top would leave the level below ground
the qualifying candle itself had already covered, and the next close
above it would break price that had already traded. So the rule is not
"the top" and not "the candle's high", it is the outermost of the two.

Mirrored: an up move arms the **valid low** at the lower of the down
move's bottom and the qualifying candle's own low.

**Levels only move outward.** A lower high that gets validated later does
not replace a higher valid high. If the candidate level is not further
out than the level already armed, it is rejected — but the move still
flips, because qualifying is structural and adopting the level is not.

**Arming opens the opposite window.** Arming `valid_high` opens
`window_low` at the qualifying candle's low. Arming `valid_low` opens
`window_high` at its high. A window resets only when its level is freshly
armed, never when an already-armed level is merely widened by a wick.

## While a level is armed

- Any candle whose wick goes above the valid high raises it to that
  candle's high, bullish or bearish or doji.
- Any candle whose wick goes below the valid low lowers it to that
  candle's low.
- Every candle deepens the open windows: `window_low` takes the lowest
  low seen, `window_high` the highest high.

Both sides can update on the same candle. A candle can lower the valid
low with its wick and arm a new valid high at the same time.

This widening is the same operation that produces the outermost-of-two
rule above: arming sets the level to the move's extreme, and then the
candle's own wick widens it if it reaches further.

## Breaking

**Break.** A close above the valid high. Nothing else counts. Wicks do
not break. Mirrored: a close below the valid low.

A break does four things.

1. **Names itself.** A break in the same direction as the current trend
   is a **BOS**. A break against it is a **CHoCH**, and the direction
   flips. With no direction yet, the first break is a BOS whichever side
   it comes on, and it sets the direction. BOS and CHoCH are the same
   detection; only the direction you were already in decides the name.
2. **Disarms the side it broke.** The level just broken is spent. It is
   not carried forward at its old value and not reset to anything else,
   it is simply gone, and it stays gone until a fresh qualification arms
   that side again. This is what stops one level firing twice.
3. **Commits the opposite side from its window.** A bullish break sets
   the valid low to the lowest low from the qualifying candle through the
   breaking candle inclusive — that is, the minimum of `window_low` and
   the breaking candle's own low. If the breaking candle wicks lower than
   anything before it, its low wins. `window_low` then closes, and
   `window_high` opens at the breaking candle's high. Mirrored bearish.
4. **Starts a fresh move.** The breaking candle's high becomes the new
   top and its low the new line, in the break direction. The move on the
   broken side is dead.

Both sides can be armed at once, but only because separate events armed
them.

## Order of operations

Per closed candle, in this order:

1. **Seed.** If there is no move at all, seed both from this candle's
   high and low and stop. Emit it if it is a doji.
2. **Doji.** If open equals close, widen armed levels and deepen the
   windows, emit, and stop.
3. **Break.** Check both sides strictly. If either breaks, do the four
   things above and stop.
4. **Structure.** Extend the live move, or let its opposite qualify.
5. **Price.** Widen armed levels by this candle's wick, then deepen the
   windows.

Step 3 must come before step 5. Reversed, a candle that raises a level
with its wick could then break the level it just raised — and since a
candle's high is never below its close, every new high would fire a false
break.

## Dojis

A candle where open equals close has no direction. Ignore it for anything
that needs one: it cannot extend a move, cannot qualify, cannot arm a
level, and cannot break — not even by closing past an armed level.

Its high and low still count as price. Its low deepens the window, and a
doji wick past an armed level still widens that level. The window is a
fact about how far price went, not about candle direction. Skipping a
doji that printed the window's deepest low would commit a valid low above
where price actually traded, and a later break would then fire off a
level price had already passed through.

A doji can also seed, because seeding needs no direction. A doji that
opens a warm-up window seeds both moves exactly as any other candle of
the same range would. Were it to leave the engine cold instead, a
100-candle scan and a 300-candle scan over the same data could open in
different states purely because one happened to start on a doji, and the
result of a scan must not depend on where its window was sliced.

Emit every doji with its timestamp and timeframe so the caller can log
it. Real dojis are rare and the operator wants to know where they land.

## Cold start

On a fresh feed there is no direction and nothing is armed. The first
candle seeds both an up move and a down move, and both stay live until a
candle qualifies, which kills one. Until then the engine is simply
watching, and it may watch for a long time: no level is armed, so no
break is possible.

Because seeding sets `up_line` and `down_bottom` to the same low, and
`up_top` and `down_line` to the same high, any candle that extends one
move also qualifies against the other. So the two-move phase ends on the
first candle that does anything at all, and candles that do nothing leave
both moves standing.

Tracking both sides is what lets a first break come out bearish. The down
move is the only path to arming a valid low before any direction exists.

## Emitted events

The engine emits on a break and on a doji, and is silent otherwise. Each
event carries the symbol, timeframe and timestamp. A break also carries
its kind (BOS or CHoCH), the side it broke on, the level it broke, and
the levels left armed afterwards.

The engine only reports. It does not alert, trade, or decide anything.

## Test cases

Each block is `open high low close`, oldest first.

**Minimal BOS.**

```
2 6 1 4
4 5 0 2
2 9 1 7
```

Candle 1 seeds. Candle 2 breaks candle 1's low of 1 while closing
bearish, so it qualifies and arms 6. Candle 3 closes at 7, above 6. BOS.
The valid low commits at 0, the deepest point from candle 2 through
candle 3.

**A bullish candle dipping below the previous low still extends.**

```
1 4 0 3
3 6 2 5
5 9 1 8
```

All three are one up move. Candle 3 dips to 1, under candle 2's low of 2,
but closes bullish and beats the top, so it extends. The line is now 1,
candle 3's low.

**Ignored candles.**

```
1 4 0 3
3 6 2 5
5 9 1 8
8 9 6 7
7 11 5 10
```

Candle 4 closes bearish but its low of 6 is above the line of 1, so it is
nothing. Candle 5 extends the move. Top is 11, line is 5.

**A bullish candle that fails to beat the top is also nothing.**

```
13 25 11 22
22 22 18 19
19 23 17 22
```

Candle 3 closes bullish but its high of 23 does not beat 25, so it does
not extend and the line stays at 11.

**A wick raises the armed level.**

```
7 11 5 10
10 10 4 5
5 7 3 6
8 13 4 5
13 23 12 22
```

Candle 2 qualifies and arms 11. Candle 4 is bearish and does not extend,
but it wicks to 13, so the level to beat becomes 13. Candle 5 closes at
22. BOS.

The valid low comes from the deepest point of the window, which ran from
candle 2 to candle 5 and bottomed at candle 3's 3.

**A close above the old level but below the raised one is not a break.**

Same as above, but with a candle closing at 12 after the wick to 13. No
break. It has to close above 13.

**The full run: BOS, second BOS, CHoCH, and a two-sided candle.**

Rows are W1 to W9, then T, then W10 and W11.

```
3 8 2 7
7 7 1 3
3 6 2 5
5 12 4 11
11 13 10 12
12 12 8 9
9 11 7 10
10 16 9 15
15 16 5 6
9 10 7 8
6 7 2 3
3 18 3 15
```

W1 seeds both moves: an up move with top 8 and line 2, and a down move
with bottom 2 and line 8. Nothing is armed.

W2 closes bearish with a low of 1, under the line of 2, so it qualifies
and 8 turns valid. Its own high of 7 is below 8, so the top is the outer
value and the level is 8. The low window opens here at 1. The up move
dies and the down move is now bottom 1, line 7.

W3 does nothing: its high of 6 does not beat the line of 7.

W4 closes at 11, above 8. First BOS, and the direction is now bullish.
The window ran W2 to W4 and bottomed at 1, so the valid low commits at 1.
The valid high is spent and disarms. W4 starts the fresh up move, top 12
and line 4, and the down move dies.

W5 extends the move to a top of 13 with a line of 10.

W6 closes bearish at 9 with a low of 8, under 10, so 13 turns valid and a
new low window opens at 8.

W7 does nothing much, though its low of 7 is the deepest in that window.
Note that the valid low is still 1 and does not move: 7 is nowhere near
below it. Only the window tracks 7.

W8 closes at 15, above 13. Second BOS, same direction, so not a CHoCH.
The valid low moves up to 7, committed from the window.

W9 closes at 6, under 7. CHoCH, and the direction flips bearish. The
valid high commits at 16, the valid low disarms, and W9 starts a fresh
down move with bottom 5 and line 16.

T closes bearish, so it cannot be an up move, and its low of 7 does not
beat the bottom of 5, so it does not extend the down move. Ignored
entirely.

W10 closes bearish with a low of 2, under the bottom of 5, so it extends
the down move: bottom 2, line 7.

W11 wicks to 18, above the armed valid high of 16, but closes at 15, so
there is no break — the break is checked before the wick raises anything
— and the valid high then widens to 18. It also closes bullish and beats
the line of 7, so it qualifies as an up move, which arms the valid low at
2. Both sides are now armed: a close below 2 is a BOS, a close above 18
is a CHoCH.

## Warm-up

The engine has no state on a cold boot and the process restarts under
systemd, so this runs whenever stored state cannot be resumed, not just
the first time.

1. Fetch the last 1000 closed candles.
2. Replay the engine forwards from the oldest.
3. Use the result. If the window contains no break, stay undirected and
   keep watching live.

One wide window, not a widening scan. The gateway caps the response at
its own chunk size rather than failing, so fewer candles than asked for
is normal and is used as-is; log the count, because a short window is a
quiet loss of accuracy that no error would announce.

State cannot be computed backwards. It is path dependent, so the replay
runs forwards from the start of the window.

### Why not a widening scan

An earlier version of this spec started at 100 candles, widened to 300
and 1000 only if no break had fired, and stopped at the first window
that produced one — on the reasoning that a longer window can reclassify
the same break as a CHoCH instead of a BOS, so the first hit is the one
to trust.

The observation is true. The conclusion drawn from it was backwards, and
this section records that so it does not get reintroduced.

Warm-up is reconstructing what the engine would hold had it never
stopped. Measured against a continuous run over synthetic series, wider
windows match that more closely, not less:

| window | same last break | same direction |
|---|---|---|
| 100 | 78% | 91% |
| 300 | 97% | 99% |
| 1000 | 100% | 100% |

The reclassification a longer window performs is a **correction**, not a
corruption. A narrow window forces its first break to BOS because no
direction exists yet, and it arms different levels from a shorter
history, so it can even break on different candles entirely.

The practical cost of getting this wrong was not small. Strict staleness
means warm-up runs after every session gap, so direction was being
re-derived from a narrow window routinely rather than rarely, and
direction is what decides BOS from CHoCH on every break that follows
until the state converges.

The wide window is also less code and the same number of requests, since
the narrow scan's common case was already a single fetch.

### How far a starting point can move the answer

Worth stating plainly, because it bounds what this engine can claim.

Two replays of the same candles from different starting points do
converge, but not immediately — on synthetic series, a median of about
30 candles and one break, with a worst case near 180 candles and nine
breaks. Until they converge they can disagree about labels and about
which candles broke at all.

This is inherent to the definition rather than a defect. Structure is
path dependent by construction, so "the structure" is only well defined
relative to a history. Two observers reading the same chart from
different scroll-back are both right.

The practical consequences: a freshly warmed state is provisional until
it has run a while, and a disagreement between the engine and a human
eye may just be a difference of history rather than an error by either.

Warm-up does not emit. Replaying history reconstructs where structure
already stands; announcing a break that happened hours ago as though it
just fired would be wrong. Only live candles emit.

## Persistence

State is stored per (symbol, timeframe). Every field listed in the state
table is stored, not just the headline levels — the state is path
dependent, so a partial row could not be resumed.

On boot, load the row and compare its `last_ts` against the candle about
to be folded in.

**Staleness is strict.** Resume only when the stored candle is the one
immediately before the incoming candle, by exactly one timeframe period.
Any other gap, or a missing row, and the stored state is thrown out and
warm-up runs. Rescanning is cheap; wrong state is not. There is no
tolerance window, and none is wanted: a gap means candles were missed,
and missed candles cannot be reconstructed after the fact.

A timeframe whose period is not a fixed number of minutes cannot be
checked this way, so state on it never resumes and always rebuilds.

Because market closes and weekends produce gaps, ordinary operation will
rebuild after every session break. That is intended.

Every folded candle is written back, so a process that restarts within
one candle resumes instead of rescanning.

## Fitting it to crackedalert

Three pieces, split along the I/O line.

**`wave.py`** is the engine: `Bar`, `WaveState`, `WaveEvent`, and
`apply_bar(state, bar) -> (state, events)` with `replay` folding a
sequence. Pure, in the same shape as `fvg.py` — no I/O, no fetching, no
knowledge of where candles come from, and no mutation of the state passed
in. Warm-up replay and the live path are the same call, so there is no
branch between them. Same candles in, same state out, always.

**`wave_state.py`** is the other half: `WaveStateStore` for the SQLite
row, `resumable()` for the staleness rule, and `WaveService` to tie them
together — load, resume or warm up, fold, save. It holds one state per
(symbol, timeframe) and is the only place that knows about restarts.

**`ctrader/candles.py`** is the adapter. Trendbars arrive as a low plus
unsigned deltas, so `_unpack` decodes all four prices per the official
proto (`ProtoOATrendbar`: `open = low + deltaOpen`, and likewise
`deltaHigh` and `deltaClose`), scaled by `PRICE_SCALE`. `_poll_key` hands
the whole bar to `on_closed_bar`. The fetch count is a parameter, which
is what warm-up widens through 100, 300 and 1000; `history()` returns
decoded bars oldest first for exactly that.

Feeding one bar at a time means replay and live both use the same path,
with no branching between them.
