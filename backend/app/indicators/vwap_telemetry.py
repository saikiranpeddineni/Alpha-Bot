# =============================================================================
# [MODIFIED 2026-10-16]: Deprecate Unused compute_vwap_telemetry
# ORIGINAL INTENT: Calculate granular VWAP reclaim/rejection telemetry facts.
# REASON FOR CHANGE: Unused speculative indicator; 0 callers repo-wide. Live strategies
# and pipeline location vetoes consume session_vwap directly from app.indicators.ta.
# SAFE ROLLBACK: Entire previous implementation is preserved below as comments.
# -----------------------------------------------------------------------------
# [PREVIOUS CODE - PRESERVED]:
# """VWAP Reclaim/Rejection Event Telemetry.
# """
# 
# from __future__ import annotations
# 
# from datetime import datetime
# 
# import numpy as np
# import pandas as pd
# 
# 
# def compute_vwap_telemetry(
#     df: pd.DataFrame,
#     ts: datetime,
#     slot_rvol_val: float | None = None,
# ) -> dict:
#     """Calculate causal VWAP cross telemetry facts for the symbol up to `ts`."""
#     telemetry = {
#         "vwap_state": "unknown",
#         "vwap_distance_atr": 0.0,
#         "vwap_slope": 0.0,
#         "vwap_cross_count": 0,
#         "distance_before_cross_atr": None,
#         "cross_velocity": None,
#         "close_distance_after_cross_atr": None,
#         "bars_held_after_cross": 0,
#         "slot_rvol_on_cross": None,
#         "volume_acceleration": None,
#         "body_pct": None,
#         "upper_wick_pct": None,
#         "lower_wick_pct": None,
#         "retest_occurred": False,
#         "retest_depth_atr": None,
#         "retest_hold_bars": 0,
#         "cross_count_prior_30m": 0,
#         "vwap_event_type": "none",
#     }
# 
#     if df.empty or "vwap" not in df.columns:
#         return telemetry
# 
#     sub = df[df.index <= ts]
#     if len(sub) < 30:
#         return telemetry
# 
#     close = sub["close"].to_numpy()
#     open_prices = sub["open"].to_numpy()
#     high = sub["high"].to_numpy()
#     low = sub["low"].to_numpy()
#     volume = sub["volume"].to_numpy()
#     vwap = sub["vwap"].to_numpy()
#     atr = sub["atr14"].to_numpy() if "atr14" in sub.columns else np.ones(len(sub))
# 
#     # Basic state metrics
#     curr_close = close[-1]
#     curr_vwap = vwap[-1]
#     curr_atr = atr[-1]
# 
#     telemetry["vwap_distance_atr"] = round(abs(curr_close - curr_vwap) / curr_atr, 4) if curr_atr > 0 else 0.0
#     telemetry["vwap_state"] = "above_control" if curr_close > curr_vwap else "below_control"
# 
#     # VWAP slope over last 10 bars
#     if len(vwap) >= 10 and pd.notna(vwap[-1]) and pd.notna(vwap[-10]):
#         telemetry["vwap_slope"] = round((vwap[-1] / vwap[-10] - 1.0) * 100.0, 4)
# 
#     # Cross counts prior 30 bars
#     cross_indices = []
#     cross_count_30 = 0
#     diffs = close - vwap
#     n = len(sub)
#     for k in range(max(1, n - 30), n):
#         if pd.notna(diffs[k]) and pd.notna(diffs[k-1]):
#             # Check for cross
#             if diffs[k] * diffs[k-1] < 0:
#                 cross_count_30 += 1
#                 cross_indices.append(k)
# 
#     telemetry["vwap_cross_count"] = cross_count_30
# 
#     # Locate the most recent cross
#     if cross_indices:
#         c_idx = cross_indices[-1]
#         telemetry["bars_held_after_cross"] = n - 1 - c_idx
# 
#         # Compute cross metrics
#         atr_before = atr[c_idx - 1] if c_idx > 0 else curr_atr
#         atr_after = atr[c_idx]
# 
#         if atr_before > 0:
#             telemetry["distance_before_cross_atr"] = round(abs(close[c_idx - 1] - vwap[c_idx - 1]) / atr_before, 4)
#             telemetry["cross_velocity"] = round((close[c_idx] - close[c_idx - 1]) / atr_before, 4)
# 
#         if atr_after > 0:
#             telemetry["close_distance_after_cross_atr"] = round(abs(close[c_idx] - vwap[c_idx]) / atr_after, 4)
# 
#         telemetry["slot_rvol_on_cross"] = slot_rvol_val  # Pass slot RVOL if available at cross
# 
#         if c_idx > 0 and volume[c_idx - 1] > 0:
#             telemetry["volume_acceleration"] = round(volume[c_idx] / volume[c_idx - 1], 4)
# 
#         # Bar wick statistics on the crossing bar
#         c_range = high[c_idx] - low[c_idx]
#         c_body = abs(close[c_idx] - open_prices[c_idx])
#         if c_range > 0:
#             telemetry["body_pct"] = round(c_body / c_range, 4)
#             upper = high[c_idx] - max(open_prices[c_idx], close[c_idx])
#             telemetry["upper_wick_pct"] = round(upper / c_range, 4)
#             lower = min(open_prices[c_idx], close[c_idx]) - low[c_idx]
#             telemetry["lower_wick_pct"] = round(lower / c_range, 4)
# 
#         # Retest tracking (price approaches or tests VWAP from post-cross side)
#         post_cross_diffs = diffs[c_idx:]
#         post_cross_lows = low[c_idx:]
#         post_cross_highs = high[c_idx:]
#         post_cross_vwap = vwap[c_idx:]
#         post_cross_atr = atr[c_idx:]
# 
#         crossed_above = diffs[c_idx] > 0
#         retest_detected = False
#         min_dist_atr = float("inf")
#         retest_hold = 0
# 
#         for idx_offset in range(len(post_cross_diffs)):
#             k = c_idx + idx_offset
#             # Check if we crossed back (violated cross side)
#             if crossed_above and diffs[k] < 0:
#                 break
#             if not crossed_above and diffs[k] > 0:
#                 break
# 
#             retest_hold += 1
#             # Distance from low (if long) or high (if short) to VWAP
#             dist_to_vwap = abs(post_cross_lows[idx_offset] - post_cross_vwap[idx_offset]) if crossed_above else abs(post_cross_highs[idx_offset] - post_cross_vwap[idx_offset])
#             dist_atr = dist_to_vwap / post_cross_atr[idx_offset] if post_cross_atr[idx_offset] > 0 else 0.0
# 
#             min_dist_atr = min(min_dist_atr, dist_atr)
#             if dist_atr <= 0.15:  # Touched/approached VWAP within 0.15 ATR
#                 retest_detected = True
# 
#         telemetry["retest_occurred"] = retest_detected
#         telemetry["retest_depth_atr"] = round(min_dist_atr, 4) if min_dist_atr != float("inf") else None
#         telemetry["retest_hold_bars"] = retest_hold
# 
#         # Derive event type
#         if crossed_above:
#             telemetry["vwap_event_type"] = "reclaim" if retest_detected else "failed_rejection"
#         else:
#             telemetry["vwap_event_type"] = "rejection" if retest_detected else "failed_reclaim"
# 
#         # Check prior 30 crosses before the cross index
#         cross_count_prior = 0
#         for k in range(max(1, c_idx - 30), c_idx):
#             if pd.notna(diffs[k]) and pd.notna(diffs[k-1]) and (diffs[k] * diffs[k-1] < 0):
#                 cross_count_prior += 1
#         telemetry["cross_count_prior_30m"] = cross_count_prior
# 
#     return telemetry
# -----------------------------------------------------------------------------
# [NEW CODE]:
# (Module deprecated. Canonical session VWAP is in app.indicators.ta.)
# =============================================================================
