# Implementation Plan

## [Overview]
Fix `/p` (pending) take-profit, risk-sizing, and auto entry-alert math to use the actual fill price (`placement = entry +- spread`) instead of the raw entry. Today `_build()` computes `dist`/`tp`/`lots` from the raw `entry`, so BUY TPs come out too low, SELL TPs too high, real risk (fill-to-SL) exceeds the stated risk%, and the entry alert targets the wrong price. `/m` is unaffected: its fill (ask/bid) already equals `entry_ref`.

## [Types]
Add optional `basis: Optional[float] = None` to `_build()`. Defaults to `entry_ref` (unchanged `/m`); pending passes `placement_price`. `dist`, `tp`, `lots` compute from `basis`. `TradePlan` fields unchanged.

## [Files]
- `src/crackedalert/risk.py` - `_build` gains `basis`; `dist = abs(basis-sl)`, `tp = basis + dist*rr` (BUY) / `basis - dist*rr` (SELL); `plan_pending` passes `basis=placement`.
- `src/crackedalert/bot/handlers.py` - `_create_entry_alert` target = `placement_price if not None else entry_ref`.
- `tests/test_risk.py`, `tests/test_trading.py` - update assertions per [Testing].

## [Functions]
- `_build(...)` risk.py: add `basis=None`; use it for dist/TP.
- `plan_pending(...)` risk.py: pass `basis=placement`.
- `Handlers._create_entry_alert(...)`: alert target = placement (fallback entry_ref).
- No functions removed. `plan_market`, `_lots`, `trading.py` payload rounding unchanged.

## [Classes]
No class changes. `TradePlan`, `TradingService`, `Handlers` structures unchanged.

## [Dependencies]
None. No new packages or version changes.

## [Testing]
- `test_risk.py` (spread 0.2): buy-below -> `dist 5.2`, `tp 2415.8`, `lots 0.19`; sell-above -> `tp 2489.6`; zero-distance -> `sl=2400.2`; dollar pending -> `lots 0.19`. Add `test_pending_buy_tp_based_on_placement` asserting `tp == placement + (placement-sl)*rr`.
- `test_trading.py`: `test_pending_places_spread_offset_price` `takeProfit` 2415.0 -> 2415.8.
- Run `python -m unittest discover tests`; confirm `/m` tests green.

## [Implementation Order]
1. `risk.py`: add `basis` to `_build`, switch dist/TP math, pass `basis=placement` from `plan_pending`.
2. `handlers.py`: entry-alert target uses placement price.
3. `test_risk.py`: update pending assertions + add regression test.
4. `test_trading.py`: update expected TP.
5. Run full suite; `/m` unchanged.