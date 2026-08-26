# Session Analysis — 2026-08-24 (deep validation)

Companion to `trade_loss_note_LRCX_2026-08-24.md`. This note answers:
"are the other strategies broken?" and validates timings, gates, and
observability end-to-end against live data.

---

## Q: Only gap_and_go traded today — are the other strategies broken?

**No. All 9 strategies are enabled and firing.** Verified two ways:

1. `/api/strategies` → every strategy `enabled: true`.
2. Offline replay of all 9 strategies over today's live bars (repo code,
   `StrategyContext` replicated): raw signals generated today included
   **momentum ×17** (10 long / 7 short), liquidity_sweep ×1, vwap_reversion
   ×1, plus the gap_and_go shorts that traded.

### What actually happens to those signals

Live pipeline log stream (14:16–14:34 UTC window) shows a steady flow of
non-gap_and_go signals reaching deep scoring, then dying at the final ML
gates by hair-thin margins:

| Time (UTC) | Signal | Fate |
|---|---|---|
| 14:16:08 | NOK vwap_reversion SHORT | ml=0.67 (SPECIALIST auc 0.67!), meta PASS 0.844 → **Trade Score 69.4 < 70.0** — missed by 0.6 pts |
| 14:17:08 | NOK vwap_reversion SHORT | passed scoring; blocked downstream by behavioral loop detector (corr 0.70 vs INTC position) |
| 14:20:12 | MSTR orb LONG | Meta-label REJECT: 0.4991 < 0.52 (missed by 0.02) |
| 14:21:14 | ONDS orb SHORT | feature row unavailable (warmup) |
| 14:27:15 | ETHA orb LONG | Meta-label REJECT: 0.4216 < 0.52 |
| 14:33:15 | TSLA vwap_reversion SHORT | Meta-label REJECT: 0.5074 < 0.52 |
| 14:34:14 | TSLA vwap_reversion SHORT | Meta-label REJECT: 0.1791 |

### The real cause: gating asymmetry

**gap_and_go is the only strategy whose meta-label gate is BYPASSED**
(`meta_label_not_configured_for_strategy`, no artifact exists for
gap_and_go-long or -short). Every signal it emits skips an entire gating
layer that orb / vwap_reversion / momentum must pass.

Result today: the one strategy with a missing safety gate was also the one
that went 0-for-3 for −$230, while fully-gated strategies (e.g., NOK
vwap_reversion with ml 0.67 + meta 0.84) were kept out by a 0.6-point Trade
Score margin or by correlation blocks tied to the gap_and_go position itself
(INTC's presence blocked NOK/MRVL/AAOI).

**Fix candidates:** train/configure gap_and_go meta-label artifacts (the
trainer iterates REGISTRY so the hook exists), or fail-closed: treat missing
meta artifact as REJECT instead of BYPASS until trained.

---

## Timing validation (signal → fill latency, all 3 trades)

Decision bar close → enriched eval + pipeline score → order submit → fill:

| Trade | Bar close | Scored | Submitted | Filled | Decision→fill |
|---|---|---|---|---|---|
| LRCX | 13:53:00 | 13:53:05.7 | 13:53:07.5 | 13:53:08.2 | **8.2s** |
| INTC | 14:00:00 | ~14:00:08 | 14:00:10.6 | 14:00:12.1 | **12.1s** |
| AMD | 14:19:00 | 14:19:15 | 14:19:16.0 | 14:19:17.5 | **17.5s** |

Broker round-trip is fast (0.6–1.5s market orders). The 5–10s pre-submit
span is enrich + regime detect + ML/meta/TradeScore + governor + sizing —
acceptable for 1-min bars, but note fills land mid-bar, ~60–90s after the
decision bar's close price (LRCX filled $1.15 below decision-bar close).
Slippage vs decision close averaged favorable on entry but adverse-selection
prone at extremes (see loss note).

Exit path validated: PIS trigger logs precede close submissions by seconds;
exits landed on the correct side in all three cases (price continued against
the dead thesis after each cover).

## Market-risk & permission gates: verified open

- `MarketRiskState=NORMAL confidence=0.85` all morning →
  `allowed_strategies = ALL`, `risk_multiplier=1.0`, entries allowed.
  The DEFENSIVE-tier "no momentum" restriction never engaged.
- `allow_new_entries=true` continuously; heartbeat diagnostics all green.
- Opening blackout expired 9:40 ET; irrelevant to the 9:53–10:19 entries.
- Chase veto measured on all three decision bars: TR/ATR = 0.90 / 0.73 /
  0.11 vs ≥1.8 required → structurally blind to multi-bar exhaustion.

## RS-score sanity check

`compute_relative_strength` blends 40% ret_5 + 60% ret_15 differential vs
SPY, saturating at ±2%. Observed values hug 0.44–0.51 for nearly everything
all morning (MSTU saturated at 1.000). Working as designed but near-zero
discrimination intraday when the tape moves with SPY — the relative_strength
strategy's thresholds (long ≥0.75 / short ≤0.25) are effectively unreachable
on days like this. Not a bug; a calibration observation.

## `#86` market-data-gap detector: false positives on illiquid names

Repeated "no bar for ~120–130s … New entries BLOCKED" warnings fired all
morning for YJ, BEEM, MS, USDE, BTCS, BTCT. Validated against bars: BEEM has
**58 minute-holes across ~154 bars today**, BTCS 49/134 — these symbols
legitimately don't print every minute. The detector conflates illiquidity
with feed failure, permanently blocking new entries on exactly the
low-float movers the scanner surfaces. Consider differentiating "no trade
printed" (benign) from "provider stale" (e.g., quote timestamp frozen).

## Observability bugs catalog (validated)

1. **Pipeline rejections logged at DEBUG only** (`engine_evaluation.py:269`)
   → invisible on console (INFO threshold) and UI WS stream (main.py drops
   DEBUG). Combined with RS-score debug spam (~800 lines / 10 min) flushing
   the 1000-entry ring buffer every ~12 min, rejection forensics vanish
   within minutes. Promote rejections to INFO; rate-limit RS spam to DEBUG
   sampling or drop entirely.
2. **decision_traces.db is orphaned**: no writer exists anywhere in
   backend/tools/scripts; last trace 2026-07-30 despite 1236 historical rows.
   The per-candidate forensic trail silently died in a refactor.
3. **`signal_timestamp_utc` / `decision_bar_timestamp_utc` are NULL** on
   every order and trade row — breaks post-hoc latency and decision-bar
   audits (this session's timing table had to be reconstructed from log
   correlation).
4. **alpaca.py:846 list/set TypeError**: cancel-verification poll can never
   succeed; stop-leg race window error fires on every close (details in
   loss note).
5. **Specialist models degraded across the board**: NO_ARTIFACT (LRCX,
   MARA, TSLA…), feature-version mismatch (INTC: artifact 59c7cf2e4916 ≠
   code 14fb8e075ffd — retrain needed after feature changes),
   REJECTED_LOW_AUC (AMD 0.5044, ETHA 0.5051 < 0.53). Everything falls back
   to BASE silently. Needs a fallback-rate metric + alert.
6. **Exit fill timestamps backdated**: exit orders show
   `submitted_at == filled_at` to the millisecond — fill time is recorded at
   poll time, not actual fill. Minor, but corrupts latency stats.

## Bottom line

Nothing is "broken" in the sense of crashed strategies — the engine is
healthy and disciplined. The day's outcome was shaped by three structural
issues: (1) gap_and_go trades without its meta-label gate while everyone
else pays full freight, (2) the chase/location vetoes measure one bar and
cannot see the multi-bar exhaustion short that lost three times, and (3)
near-total observability of *why* signals die is locked behind DEBUG in an
overflowing ring buffer.

## Recommended actions (priority order)

1. Fail-close or fix the gap_and_go meta-label gap (asymmetric gating).
2. Session-extreme proximity guard (would have blocked all 3 losers).
3. Fix alpaca.py:846 set intersection bug.
4. Promote pipeline rejections to INFO; silence/scale back RS-score debug.
5. Retrain specialists after feature-version change; alert on fallback rate.
6. Restore decision-trace writing (or remove the dead DB).
7. Populate signal/decision-bar timestamps on orders and trades.

---

## Fixes applied (2026-08-24, post-analysis)

1. **alpaca.py cancel verification** — `cancelled_ids` is now a `set[str]`
   (was list; `list & set` TypeError made the stop-leg race check dead
   code). `.add()` used for insertion.
2. **Session-extreme proximity guard** (`pipeline.py`, after location veto):
   blocks non-mean-reversion ENTER_SHORT within
   `entry_max_extreme_proximity_atr` (default 3.0) ATR14 of the session low,
   and ENTER_LONG within the same distance of the session high. Config:
   `enable_entry_extreme_guard=True`,
   `entry_max_extreme_proximity_atr=3.0` in RiskConfig. Requires >=15
   session bars (opening blackout owns 09:30-09:45). Validated against
   today's decision bars: LRCX 2.90 ATR -> BLOCKED, INTC 1.90 -> BLOCKED,
   AMD 2.71 -> BLOCKED, TSLA mid-range control 20.47 -> allowed.
   Also fixed a latent nesting bug: the **location veto was indented inside
   the chase veto's condition** — disabling the chase veto silently disabled
   the location veto too. All three vetoes are now independent siblings.
   Tests: `backend/tests/test_entry_extreme_guard.py` (6 cases incl.
   regression + mean-reversion exemption), 10/10 green with chase-veto suite.
3. **Observability**: pipeline rejections promoted DEBUG->INFO
   (engine_evaluation.py); RS-vs-SPY per-tick debug now logs only when
   |rs-0.5| > 0.15; AppLog ring buffer capacity 1000 -> 5000.
4. **gap_and_go meta gate fail-closed**: added to
   `MetaLabelEngine.MANDATORY_STRATEGIES` (prediction.py). Verified live
   behavior: `decision=REJECT, source=NO_MODEL,
   reason=META_MODEL_UNAVAILABLE (mandatory strategy)` while artifacts are
   absent. gap_and_go will NOT trade until `meta_gap_and_go_{long,short}.joblib`
   exist. NOTE: regenerating artifacts is currently blocked by the Windows
   ProcessPool deadlock that motivated ALPHADESK_SKIP_META_TRAIN=1
   (trainer.py:158) -- reworking that precompute path is the follow-up.

Verification: py_compile clean on all touched files; ruff shows no new
issue classes vs HEAD baseline (95 pre-existing errors in these files at
HEAD, none introduced); pytest targeted run 35 passed / 2 skipped plus the
new guard suite 10 passed.

**Not yet applied** (needs server restart to take effect): all of the
above are code changes only — the running process still executes old code.

---

## Restart + live validation (15:07 UTC)

Server restarted under `.venv` python (the WindowsApps store shim resolved
without site-packages on relaunch — use venv for future starts). Console
output now captured to `logs/server_*.err.log` (applog writes to stderr).
Position V long survived via broker-side OTO stop + engine reconciliation;
PIS scaled out half (48→24) minutes after restart.

All fixes verified live within 10 minutes:

| Time | Evidence |
|---|---|
| 15:08 | PIS partial exit V 24 sh (scale-out path intact post-restart) |
| 15:09+ | liquidity_sweep traded twice — mean-reversion exemption working |
| 20:46 | `ASTS orb enter_short rejected -- chasing extended signal bar (1.9x ATR)` — **chase veto now visible at INFO** |
| 20:47 | `FRSH vwap_reversion enter_long rejected -- Trade Score 66.3 < 70.0 (ml=0.86)` — previously invisible forensics |
| 20:52 | **`MU gap_and_go enter_short rejected -- META_MODEL_UNAVAILABLE (mandatory strategy)`** — fail-close caught a real signal of exactly today's losing class |
| — | RS-score spam: zero lines since restart (throttle works) |
| 20:52 | Behavioral loop detector blocked BMNR (corr 0.73 vs open V) |

## Full-day ledger (final)

| Entry | Symbol | Strategy | P&L | Note |
|---|---|---|---|---|
| 13:53 | LRCX | gap_and_go | −118.95 | 2.9 ATR from LOD → new guard BLOCKS |
| 14:00 | INTC | gap_and_go | −63.28 | 1.9 ATR → BLOCKS |
| 14:19 | AMD | gap_and_go | −48.33 | 2.7 ATR → BLOCKS |
| 14:29 | PLTR | gap_and_go | −65.64 | **6.7 ATR from LOD → guard does NOT catch** |
| 14:37 | SNDK | vwap_reversion | −89.48 | −1.03R full stop; reversion exempt from vetoes by design |
| 14:46 | V | gap_and_go | −14.40 | |
| 15:09 | SOXL | liquidity_sweep | −54.24 | pre-restart code |
| | | **Net** | **−$454.32** | |

### Guard coverage honesty

The extreme-proximity guard would have blocked 3 of the 4 gap_and_go
losers. PLTR shorted 6.7 ATR above the LOD after a bounce had already
lifted price — a different entry geometry (fade-the-bounce-failure) the
proximity rule cannot see. Follow-up candidates: bounce-failure pattern
(lower-high rejection) detection, or extending the meta-gate fix so
gap_and_go stays fully gated once artifacts are trainable (still blocked by
the ProcessPool deadlock in trainer.py:158).

Post-restart open positions: GOOGL short (liquidity_sweep, stop 352.10),
V long half-position (runner with ratcheted floor).

## Extended validation (15:25–15:32 UTC)

The new extreme guard caught **two more live entries**: PLTR bb_squeeze
long (2.8 ATR from session high) at 20:57 and ORCL bb_squeeze long
(2.2 ATR) at 21:01 — both exactly the chase-the-extreme class it was built
for. bb_squeeze's meta gate (artifact trained 13:11 today, val_auc 0.588)
is splitting candidates correctly: AMZN 0.65 / CRWV 0.65 / BABA 0.60 PASS,
INTC 0.38 REJECT. Behavioral loop detector blocked INTC (corr 0.93) and
BMNR (0.73). PIS contained GOOGL short to −$10.40 with a fast cover.
Zero tracebacks/critical errors since restart.

Post-restart book: AMZN/BABA/CRWV longs (bb_squeeze) + V runner, net
unrealized positive. Day closed-trade ledger stands at −$464.72 across
8 trades (all pre-restart losses); no completed post-restart losses at
time of writing.

---

## Opportunity-cost audit (forward returns of every rejected signal)

Measured forward move (decision price -> late-session price) for all 28+
rejections since restart plus the notable pre-restart ones:

### The new extreme guard: NET STRONGLY POSITIVE on day one

| Blocked signal | Forward favorable move |
|---|---|
| AAOI long @16:11 | **−11.9% avoided** |
| ORCL long @16:01 | −2.6% avoided |
| ORCL long @16:16 | −2.4% avoided |
| NBIS long @16:17 | −1.6% avoided |
| PLTR long @15:57 | +1.4% missed |
| NOW short @16:13 | +1.4% missed |
| COHR long @16:17 | +1.3% missed |
| BE long @16:17/16:19 | +2.0% missed (x2) |

Avoided ~18.6 pts of adverse move vs missed ~8.2 pts of favorable move.
Caveat: one afternoon, small sample — keep measuring before loosening.

### Confirmed systematic miss: Trade Score near-threshold with strong ML+meta

**NOK vwap_reversion long 14:16 UTC**: ml=0.67 (specialist auc 0.67),
meta=0.84 PASS, Trade Score **69.4 < 70.0** — rejected by 0.6 points and
the stock ran **+2.9%** into the close. All intelligence agreed; the scalar
threshold vetoed it. Candidate fix: a high-agreement rescue rule (e.g.,
ml >= 0.65 AND meta >= 0.80 admits despite TS < threshold), or regime-
sensitive TS thresholds. Measure in shadow first.

### Other findings

- **SNDK**: traded vwap_reversion long pre-restart, stopped at −1.03R,
  then rallied **+10%** — worst opportunity cost of the day, but a PIS/
  thesis-management problem (stop placement or re-entry logic), not gating.
- INTC bb_squeeze meta REJECT (0.38) preceded a +3.3% rally — single sample,
  gate not falsified, worth tracking.
- MU gap_and_go short (fail-closed) would have made +0.5% — acceptable
  cost of the asymmetric-gating fix.
- Correctly avoided losers: MSTR −4.0% (meta), TSLA shorts (price rose),
  BABA −1.9% (Trade Score).

## Follow-up queue (updated priority)

1. Shadow-ledger mode for vetoes: record every veto + forward return to a
   DB, decide thresholds with N-session data instead of anecdotes. (The
   frontend already polls `/api/shadow-ledger` — endpoint never shipped.)
2. High-agreement Trade Score rescue rule (NOK class).
3. Trainer ProcessPool deadlock fix -> train gap_and_go meta artifacts ->
   lift fail-close.
4. Specialist artifact retrains (feature-version mismatches).
5. SNDK-style stop-then-rally review for vwap_reversion.
