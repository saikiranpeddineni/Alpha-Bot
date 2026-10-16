# =============================================================================
# [MODIFIED 2026-10-16]: Deprecate Unused SlotVolumeEngine
# ORIGINAL INTENT: Time-normalized relative volume calculator by 5-minute slots.
# REASON FOR CHANGE: Unused speculative indicator; 0 callers repo-wide. Canonical volume
# features are computed directly in app.indicators.ta and app.ai.features.
# SAFE ROLLBACK: Entire previous implementation is preserved below as comments.
# -----------------------------------------------------------------------------
# [PREVIOUS CODE - PRESERVED]:
# """Causal Time-Normalised Relative Volume (SLOT_RVOL) calculator.
# """
# 
# from __future__ import annotations
# 
# from datetime import datetime
# from zoneinfo import ZoneInfo
# 
# import numpy as np
# import pandas as pd
# 
# ET = ZoneInfo("America/New_York")
# 
# 
# class SlotVolumeEngine:
#     """Pre-calculates and fetches historical median volumes per 5-minute slot
# 
#     to prevent look-ahead bias and calculate leakage-free SLOT_RVOL.
#     """
# 
#     def __init__(self, df: pd.DataFrame, lookback_sessions: int = 20, min_observations: int = 5) -> None:
#         self.lookback_sessions = lookback_sessions
#         self.min_observations = min_observations
# 
#         # Calculate rolling 5m volume for the entire dataset
#         # rolling(5) sum of 1Min volume represents the volume of the 5m bar ending at that minute.
#         self.rolling_5m = df["volume"].rolling(5, min_periods=5).sum()
#         self.df_index = df.index.tz_convert(ET)
# 
#         # Create a helper structure: maps (date, slot_idx) -> rolling 5m volume
#         self.slot_map = {}
#         for idx, ts in enumerate(self.df_index):
#             val = self.rolling_5m.iloc[idx]
#             if pd.notna(val):
#                 minute_of_day = ts.hour * 60 + ts.minute
#                 slot_idx = minute_of_day // 5
#                 d = ts.date()
#                 self.slot_map[(d, slot_idx)] = float(val)
# 
#         # Get sorted unique dates
#         self.unique_dates = sorted(list({ts.date() for ts in self.df_index}))
# 
#     def get_slot_rvol(self, ts: datetime) -> float | None:
#         """Returns the relative volume for the 5-minute bar ending at `ts`.
# 
#         Strictly causal: baseline is computed using only sessions prior to ts.date().
#         """
#         ts_et = ts.astimezone(ET)
#         current_date = ts_et.date()
#         minute_of_day = ts_et.hour * 60 + ts_et.minute
#         slot_idx = minute_of_day // 5
# 
#         # Get current 5m volume
#         # We look up the value of rolling 5m volume at ts (which matches current_date, slot_idx)
#         current_val = self.slot_map.get((current_date, slot_idx))
#         if current_val is None or current_val <= 0:
#             # Fallback to direct slice sum if missing from precomputed map
#             # e.g. during warmup or edge cases
#             return None
# 
#         # Filter prior dates
#         prior_dates = [d for d in self.unique_dates if d < current_date]
#         if not prior_dates:
#             return None
# 
#         # Take the last `lookback_sessions` prior dates
#         prior_dates = prior_dates[-self.lookback_sessions:]
# 
#         # Extract values for the same slot index on those prior dates
#         prior_vals = []
#         for d in prior_dates:
#             v = self.slot_map.get((d, slot_idx))
#             if v is not None and v > 0:
#                 prior_vals.append(v)
# 
#         if len(prior_vals) < self.min_observations:
#             return None  # UNKNOWN
# 
#         median_vol = float(np.median(prior_vals))
#         if median_vol <= 0:
#             return None
# 
#         return current_val / median_vol
# -----------------------------------------------------------------------------
# [NEW CODE]:
# (Module deprecated. Volume indicators in app.indicators.ta are canonical.)
# =============================================================================
