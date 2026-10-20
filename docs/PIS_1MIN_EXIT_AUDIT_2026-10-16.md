# PIS 1-Minute Exit Audit — 2026-10-16 UTC

## Scope

This is a read-only forensic reconstruction of the completed trades recorded in:

- `data/alphadesk.db` — entries, exits, fills, reasons, and initial risk
- `data/bars_cache.db` — cached `1Min` OHLC bars

All timestamps below are UTC. The session close used for the audit is 20:00 UTC
(16:00 ET on 2026-10-16).

### Definitions

- **In-trade peak R**: maximum favorable 1-minute high/low from the entry through
  the minute before the exit, divided by the trade's recorded initial risk.
- **EOD peak R**: maximum favorable excursion from entry through 20:00 UTC,
  including bars after the actual exit. This is an opportunity-cost measure, not
  a claim that the closed trade could have captured it without changing policy.
- **Post-exit favorable R**: favorable movement available after the recorded exit,
  measured from the exit price using the original risk unit.
- **Harvest gap**: in-trade peak R minus realized R.

The labels in the final column are forensic classifications, not new exit reason
codes:

- **Correct / contained**: the exit bounded a loss or captured most of the move,
  and there was no material favorable continuation after the exit.
- **Premature / panic-like**: a discretionary thesis or stop exit occurred at a
  low/negative R, followed by a material favorable move later in the session.
  This does not mean the original reason literally says `PANIC`.
- **Low harvest**: the position had a meaningful peak, but the realized result
  surrendered a large portion of it or the runner was stopped well below the
  peak.
- **Safety stop / entry issue**: a broker protective stop, with the result
  judged separately from PIS discretionary harvesting.

## Per-trade reconstruction

| Symbol / side | Entry → exit UTC | In-trade peak R (time) | Realized R | EOD peak R | Post-exit favorable R | Exit reason | Audit result |
|---|---|---:|---:|---:|---:|---|---|
| BABA long* | 19:01 prior day → 13:37 | +2.58 (13:30) | +0.81 | +2.58 | +0.68 | manual close | Low harvest / manual |
| DIA long* | 18:47 prior day → 13:37 | +1.30 (13:36) | +1.19 | +1.61 | +0.43 | manual close | Correct / contained |
| CRWV short | 14:01 → 14:18 | +0.33 (14:10) | -0.77 | **+5.31** | **+6.08** | `THESIS_DEGRADED` | **Premature / panic-like** |
| HIMS short | 14:37 → 14:47 | +0.19 (14:45) | -1.25 | **+8.06** | **+9.31** | `stop_loss` | Safety stop; poor outcome / entry-stop issue |
| PLTR long | 14:52 → 14:55 | +0.12 (14:52) | -0.82 | +0.12 | +0.17 | `THESIS_DEGRADED,EARLY_THESIS_INVALIDATED` | Correct / contained |
| ORCL short | 14:53 → 15:11 | **+2.77 (15:10)** | +1.49 | **+4.54** | **+3.05** | PIS partial, then runner broker fill | Low harvest / runner stop |
| DDOG short | 15:48 → 16:04 | +1.10 (16:02) | +0.59 | +2.35 | +1.76 | PIS partial, then runner fill | Low harvest |
| MARA short | 15:51 → 16:06 | **+2.37 (16:03)** | +0.75 | **+12.13** | **+11.38** | giveback partial, then runner fill | Low harvest / major continuation |
| QQQ long | 16:52 → 17:09 | +0.04 (16:53) | -0.62 | +0.04 | +0.35 | `THESIS_DEGRADED` | Correct / contained |
| PLTR short | 16:34 → 17:11 | +1.10 (17:09) | +0.77 | +2.24 | +1.47 | PIS partial, then runner fill | Moderate harvest loss |
| CRWV short | 16:39 → 17:11 | +1.21 (17:08) | +0.69 | **+5.82** | **+5.13** | PIS partial, then runner fill | Low harvest / major continuation |
| SNDQ long | 18:10 → 18:24 | **+2.29 (18:20)** | +1.64 | **+10.43** | **+8.79** | PIS partial, protective-stop repair | Runner exit was protected; opportunity cost remained |
| AVGO long | 18:22 → 18:48 | +0.51 (18:34) | +0.08 | +0.55 | +0.47 | `PROTECTION_BREACH` | Low harvest / floor breach |
| SPY long | 18:05 → 19:09 | +0.24 (18:08) | -0.41 | +0.31 | +0.72 | `THESIS_DEGRADED` | Correct-ish containment; no large R peak |
| META short | 18:53 → 19:13 | +0.88 (19:08) | +0.19 | +0.88 | +0.66 | floor-tag partial, then `PROTECTION_BREACH` | Low harvest |
| NVDA long | 18:52 → 19:32 | **+1.16 (19:30)** | +0.43 | +1.16 | +0.46 | giveback partial, then runner fill | Low harvest |
| ASTS long | 19:31 → 19:33 | +0.33 (19:32) | -1.08 | +1.00 | +2.08 | `stop_loss` | Safety stop; later continuation |

`*` BABA and DIA were carried from the prior session. The cached-bar coverage
contains overnight gaps, so their session comparison is less reliable than the
same-day entries.

## Findings

### 1. The clearest premature discretionary exit was CRWV, first trade

CRWV was closed at `-0.77R` after only `+0.33R` of in-trade favorable excursion.
The same short later reached approximately `+5.31R` by EOD. The PIS reason was
`THESIS_DEGRADED`, not a literal panic code, but the path is panic-like in
outcome: the exit occurred before the move had developed and the trade then
continued strongly in the intended direction.

### 2. HIMS and ASTS were not PIS panic exits, but their hard stops were costly

Both were broker protective-stop exits:

- HIMS: `-1.25R`, then approximately `+8.06R` by EOD
- ASTS: `-1.08R`, then approximately `+1.00R` by EOD

These are safety-stop/entry-geometry problems, not discretionary PIS panic
exits. The new PIS changes do not veto a broker hard stop.

### 3. The dominant leak was runner harvest, not peak-R detection

PIS did detect the large excursions in the recent trades. Examples:

- ORCL: peak `2.77R`, PIS scale-out happened, aggregate realized `1.49R`
- MARA: peak `2.37R`, giveback guard scaled out, aggregate realized `0.75R`
- SNDQ: peak `2.29R`, aggregate realized `1.64R`
- META: peak `0.88R`, floor-tag partial occurred, aggregate realized `0.19R`
- NVDA: peak `1.16R`, giveback guard scaled out, aggregate realized `0.43R`

The original system did not sell the full position at the peak. It sold a first
tranche and then waited for a later protection breach. The runner therefore
realized a lower price than the peak.

### 4. Several exits were followed by strong continuation after EOD analysis

The largest post-exit favorable opportunities were MARA, SNDQ, CRWV, HIMS,
ORCL, and ASTS. These should not all be treated as PIS bugs: after a position is
closed, the system cannot know that the same move will resume without a new
entry. They do show that the runner trail and re-entry policy are conservative
or poorly timed for these names.

## Code fixes applied

The following fixes were implemented in the PIS/execution path:

1. **R geometry** now uses the actual initial stop distance. Tight stops are no
   longer silently widened to 0.5% of entry, so ledger R and PIS R agree.
2. **Completed-bar extremes** are kept separate from a fresher broker quote;
   broker quotes cannot create lookahead peak R.
3. **Runner protection** is anchored to the completed bar's favorable extreme
   and peak-R memory with an ATR buffer, while preserving monotonic floors.
4. **Partial decisions** return the recomputed post-scale-out runner floor.
5. **Full PIS state** is persisted/restored through the existing JSON payload,
   including floor, lifecycle, runner flags, counters, and health smoothing.
6. **Identity-bound recovery** works even when a broker restart loses
   `opened_at`, provided the persisted entry/side/stop identity matches.
7. **Partial close protection** is repaired immediately for the residual
   runner quantity after the broker cancels the old OTO/stop legs.
8. **Close failures** (`None`, rejected, canceled, or ambiguous) release the
   close latch and revert decision-time scale-out state.
9. **PIS attribution** is carried through broker fills so known runner exits do
   not degrade into `unspecified_exit` merely because the process restarted.

## Validation

Focused regression and neighboring suites after the fixes:

```text
82 passed, 1 skipped
```

The full repository suite was started twice but did not finish within the
available five-minute command windows; it reached approximately 25% without a
reported failure before timeout. The focused PIS/execution suites are the
verified checks for this change.
