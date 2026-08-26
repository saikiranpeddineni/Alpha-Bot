# Trade Loss Notes — 2026-08-24 Session

## Trade 1: LRCX gap_and_go SHORT — −$118.95

| | |
|---|---|
| Entry | 61 sh @ **299.70** at 13:53:08 UTC |
| Exit | PIS covered @ **301.65** at 13:55:17 UTC (held 2m09s) |
| P&L | **−$118.95 (−0.59R)** vs planned risk $3.28/sh to stop 302.98 |
| MFE / MAE | −1.95 / +1.95 — never in profit for a single tick |
| Exit reason | PROTECTION_RATCHET, THESIS_DEGRADED, EARLY_THESIS_INVALIDATED |

### What happened

LRCX opened ~313.8, flushed −4.9% to the 298.44 low-of-day by 13:40 UTC,
bounced to ~303.5, faded into 13:52. The strategy read the fade as resuming
breakdown and shorted it — but entry landed within cents of the low-of-day.
Price V-bottomed there and mean-reverted up. Market-order fill at 299.70 came
on the stop-run print (adverse selection). PIS cut it in ~2 min, containing
the loss to −0.59R instead of riding to the −1R stop. Correct exit side:
price kept grinding up to ~302 after the cover.

### Gate audit — why nothing caught it (verified against live data)

1. **Opening blackout**: expired 13:40; entry was 13:53 → not applicable.
2. **Chase veto**: requires decision-bar TR ≥ 1.8×ATR *and* close ≥65% at
   extreme. Bar was 96% extreme but TR = **0.90×ATR** (quiet fade bar) →
   correctly did not fire. The real vertical bar was 13:46 at 1.76×ATR —
   just under threshold.
3. **VWAP location veto**: price already below VWAP post-flush → directional
   stretch negative → did not fire.
4. **ML gate**: passed on BASE global model — no specialist artifact exists
   for LRCX; meta-label BYPASS (no gap_and_go-short meta model). Only the
   base NN + TradeScore gated this trade.

### Root cause

Exhaustion short of an already-completed flush. All current vetoes measure
the single decision bar; this failure was a multi-bar condition: ~15 points
of prior decline plus entry at the session extreme.

---

## Trade 2: INTC gap_and_go SHORT — −$63.28

| | |
|---|---|
| Entry | 214 sh @ **85.67** at 14:00:12 UTC (~$18.3k notional) |
| Exit | PIS covered @ **85.97** at 14:17:09 (THESIS_DEGRADED), held ~17m |
| P&L | **−$63.28 (−0.54R)** |
| Context | Session HOD 90.48 → LOD 85.15 (−5.9% grind-down all morning); shorted 0.52 above LOD |
| Decision bar 13:59 | TR = **0.73×ATR**, close at 100% short-extreme → chase veto blind again |
| ML path | Specialist **feature-version mismatch** → BASE fallback; meta-label BYPASS |
| Governor | ×0.49 recent-performance scaling |

Same failure class: shorting near session lows into mean reversion.

---

## Trade 3: AMD gap_and_go SHORT — −$48.33

| | |
|---|---|
| Entry | 40 sh @ **453.64** at 14:19:17 UTC |
| Exit | PIS covered @ **454.85** at 14:21:18 (held 2m01s) |
| P&L | **−$48.33** |
| Context | Entry 2.2 pts above session LOD 451.42 after −4.3% decline from HOD 474 |
| Decision bar 14:18 | Closed AT its low (100% short-extreme) but TR = **0.11×ATR** → chase veto blind again |
| ML path | Specialist REJECTED_LOW_AUC (0.5044 < 0.53) → BASE fallback; meta-label BYPASS |

---

## Day summary (as of 14:22 UTC): three shorts, three losses

| Time | Symbol | Entry vs session low | Prior decline | Result |
|---|---|---|---|---|
| 13:53 | LRCX | cents above LOD | −4.9% | −$118.95 (−0.59R) |
| 14:00 | INTC | 0.52 above LOD | −5.9% | −$63.28 (−0.54R) |
| 14:19 | AMD | 2.2 above LOD | −4.3% | −$48.33 |

**Total: −$230.56.**

## Systemic findings

1. **Confirmed failure class**: every loss today is a gap_and_go short
   entered within ~2 pts of session lows after >4% declines, caught by the
   ensuing mean reversion. No gate sees multi-bar extension or
   session-extreme proximity — the per-bar vetoes are structurally blind to
   this class.
2. **Both ML safety layers were inert all day**: specialists failed on every
   symbol (missing artifact / feature-version mismatch / low AUC) and
   meta-labels have no gap_and_go-short artifact → every entry gated by BASE
   NN + TradeScore only. Silent degradation — needs an alert when specialist
   fallback rate spikes.
3. **Sizing binds on max_notional_cap (~$18.2–18.4k), not risk budget**:
   governor scaling ×0.62 → ×0.49 → ×0.43 had almost no size effect across
   the three entries. Risk-based sizing intent is being masked by the
   notional cap.
4. **What worked**: PIS cut all three early and on the right side;
   behavioral loop detector blocked correlated entries (AMD vs LRCX 0.97,
   MRVL/AAOI vs INTC 0.96/0.91, NOK vs INTC 0.70).

## BUG found during analysis — cancel verification is broken

`backend/app/brokers/alpaca.py:846`: `cancelled_ids` is declared as a
**list** (line 813) but intersected with a **set** (`list & set`) →
TypeError on every poll. The stop-leg-race safety check before market-close
can therefore NEVER succeed; every close logs "cancel confirmation
incomplete ... race window remains open". Fix: declare
`cancelled_ids: set[str] = set()` (or use `open_ids.isdisjoint(cancelled_ids)`).

## Action items

1. **Session-extreme proximity guard**: block new shorts within X ATR of LOD
   (longs near HOD) when the move is > N ATR old. Would have flagged all
   three losses today.
2. **Fix alpaca.py:846** list/set bug (see above).
3. **Alert on specialist-fallback rate** so silent ML degradation is visible.
4. **Reconcile sizing authority**: max_notional_cap overriding governor
   risk scaling defeats recent-performance throttling.
5. Data hygiene (LRCX): fill 299.70 lies below the low of both adjacent
   1-min bars (300.27 / 300.82) — broker fill vs bar feed inconsistent.
