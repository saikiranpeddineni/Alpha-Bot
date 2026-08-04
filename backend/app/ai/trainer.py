"""ContinuousTrainer -- daily background model retraining.

Architecture:
  - Fires at ~16:15 ET after market close (configurable)
  - Fetches 60 days of 1-minute data for all active watchlist symbols in parallel
  - Retrains the global SignalModel on combined data
  - Trains per-symbol specialist models in parallel (asyncio.to_thread for CPU work)
  - Only promotes a new model if it shows measurable AUC improvement (>= 0.002)
  - Saves specialist models to data/models/specialists/<SYMBOL>.pkl

Specialist model usage:
  The SignalModel.score_action() method automatically loads the best available
  model for each symbol: specialist if available and better, else global.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from app.core.applog import applog

if TYPE_CHECKING:
    from app.ai.model import SignalModel
    from app.data.manager import DataManager


class _LegacyContinuousTrainer:
    """Parallel daily background trainer with model promotion gating."""

    FETCH_DAYS = 60          # How many days of history to train on
    FETCH_CONCURRENCY = 10   # Parallel API calls during data fetch
    TRAIN_CONCURRENCY = 8    # Parallel specialist model training jobs
    PROMOTE_THRESHOLD = 0.002  # Min AUC improvement required to promote

    def __init__(
        self,
        data: DataManager,
        model: SignalModel,
        model_dir: Path,
        strategy_overrides: dict[str, dict] | None = None,
    ) -> None:
        self.data = data
        self.model = model
        self.model_dir = model_dir
        self.strategy_overrides = strategy_overrides or {}
        self.specialist_dir = model_dir / "specialists"
        self.specialist_dir.mkdir(parents=True, exist_ok=True)

        self.training_in_progress: bool = False
        self.last_trained: datetime | None = None
        self.last_result: dict = {}
        self._last_train_date = None

    # ------------------------------------------------------------------
    # Public trigger
    # ------------------------------------------------------------------

    async def trigger(self, watchlist: list[str], as_of: datetime | None = None) -> None:
        """Schedule a background training cycle if one is not already running."""
        if self.training_in_progress:
            applog.info("trainer", "Training already running — skipping duplicate trigger.")
            return
        asyncio.create_task(self._run_cycle(watchlist, as_of))

    def should_retrain_today(self, now_et_date) -> bool:
        """True if we haven't yet trained today."""
        return self._last_train_date != now_et_date

    def mark_trained(self, date) -> None:
        self._last_train_date = date

    # ------------------------------------------------------------------
    # Full training cycle
    # ------------------------------------------------------------------

    async def _run_cycle(
        self, watchlist: list[str], as_of: datetime | None = None
    ) -> None:
        self.training_in_progress = True
        start_time = datetime.now(UTC)
        try:
            applog.info(
                "trainer",
                f"=== Daily training cycle started | {len(watchlist)} symbols | "
                f"{self.FETCH_DAYS} days of data ===",
            )
            end = as_of or datetime.now(UTC)
            fetch_start = end - timedelta(days=self.FETCH_DAYS)

            # ---- Stage 1: parallel data fetch ----
            dfs = await self._fetch_all(watchlist, fetch_start, end)
            if not dfs:
                applog.warning("trainer", "No usable data fetched. Aborting training cycle.")
                return

            applog.info(
                "trainer",
                f"Data ready for {len(dfs)}/{len(watchlist)} symbols. "
                "Running global + specialist + MetaLabel training...",
            )

            # ---- Stage 2: global + specialists in parallel ----
            global_task = asyncio.create_task(self._train_global(dfs))
            specialist_task = asyncio.create_task(self._train_specialists(dfs))
            global_auc, specialist_results = await asyncio.gather(global_task, specialist_task)

            # ---- Stage 3: MetaLabel training (sequential per strategy) ----
            meta_results = await self._train_meta_labels(dfs)

            elapsed = (datetime.now(UTC) - start_time).total_seconds()
            self.last_trained = datetime.now(UTC)
            self.last_result = {
                "timestamp": self.last_trained.isoformat(),
                "elapsed_sec": round(elapsed, 1),
                "symbols_trained": len(dfs),
                "global_auc": round(global_auc, 4),
                "specialists": specialist_results,
                "meta_labels": meta_results,
            }
            applog.info(
                "trainer",
                f"=== Training complete in {elapsed:.1f}s | "
                f"Global AUC: {global_auc:.3f} | "
                f"Specialists: {sum(1 for v in specialist_results.values() if v.get('decision') == 'PROMOTED')} promoted "
                f"/ {len(specialist_results)} fitted | "
                f"MetaLabel: {sum(1 for v in meta_results.values() if v.get('decision') == 'PROMOTED')} promoted "
                f"/ {len(meta_results)} attempted ===",
            )

            # ---- Stage 4: Post-training readiness audit ----
            await asyncio.to_thread(self._readiness_audit, dfs)

        except Exception as exc:
            applog.error("trainer", f"Training cycle failed: {exc}")
        finally:
            self.training_in_progress = False


    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    async def _fetch_all(
        self, symbols: list[str], start: datetime, end: datetime
    ) -> dict[str, pd.DataFrame]:
        semaphore = asyncio.Semaphore(self.FETCH_CONCURRENCY)

        async def _fetch_one(sym: str) -> tuple[str, pd.DataFrame | None]:
            async with semaphore:
                try:
                    df = await self.data.history(sym, "1Min", start, end, prefer_cache=True)
                    if df.empty or len(df) < 1000:
                        return sym, None
                    return sym, df
                except Exception as exc:
                    applog.warning("trainer", f"Fetch failed for {sym}: {exc}")
                    return sym, None

        results = await asyncio.gather(*[_fetch_one(sym) for sym in symbols])
        return {sym: df for sym, df in results if df is not None}

    # ------------------------------------------------------------------
    # Global model retraining
    # ------------------------------------------------------------------

    async def _train_global(self, dfs: dict[str, pd.DataFrame]) -> float:
        """Retrain and conditionally promote the global SignalModel."""
        auc = await asyncio.to_thread(self._fit_global_sync, dfs)
        return auc

    def _fit_global_sync(self, dfs: dict[str, pd.DataFrame]) -> float:
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.metrics import roc_auc_score
        from app.ai.features import build_training_set
        import joblib
        from datetime import timedelta
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")

        xs_l, ys_l, xs_s, ys_s = [], [], [], []
        for sym, df in dfs.items():
            try:
                X_l, y_l = build_training_set(df, horizon_bars=15, threshold_pct=0.10, side="long")
                X_s, y_s = build_training_set(df, horizon_bars=15, threshold_pct=0.10, side="short")
                if len(X_l) >= 200:
                    xs_l.append(X_l); ys_l.append(y_l)
                if len(X_s) >= 200:
                    xs_s.append(X_s); ys_s.append(y_s)
            except Exception:
                continue

        if not xs_l:
            return 0.5

        # ---- Long Side Data Preparation (Temporal Split & Purge) ----
        X_l = pd.concat(xs_l).sort_index()
        y_l = pd.concat(ys_l).sort_index()

        dates_l = sorted(list(set(X_l.index.tz_convert(ET).date)))
        if len(dates_l) >= 4:
            split_idx = int(len(dates_l) * 0.75)
            boundary_date = dates_l[split_idx]
            train_mask = X_l.index.tz_convert(ET).date < boundary_date
            val_start_dt = X_l[~train_mask].index.min()
            
            # Purge training samples whose outcome window (15 bars = 15 minutes) overlaps with validation
            purge_horizon = timedelta(minutes=15)
            train_filtered_mask = train_mask & (X_l.index < val_start_dt - purge_horizon)
            
            X_tr, y_tr = X_l[train_filtered_mask], y_l[train_filtered_mask]
            X_va, y_va = X_l[~train_mask], y_l[~train_mask]
        else:
            split_l = int(len(X_l) * 0.75)
            val_start_dt = X_l.iloc[split_l:].index.min()
            purge_horizon = timedelta(minutes=15)
            train_mask = X_l.index < val_start_dt - purge_horizon
            X_tr, y_tr = X_l[train_mask], y_l[train_mask]
            X_va, y_va = X_l.iloc[split_l:], y_l.iloc[split_l:]

        if not X_tr.empty and not X_va.empty:
            assert X_tr.index.max() < X_va.index.min(), f"Long temporal split violated: {X_tr.index.max()} >= {X_va.index.min()}"

        # ---- Short Side Data Preparation (Temporal Split & Purge) ----
        X_s = pd.concat(xs_s).sort_index()
        y_s = pd.concat(ys_s).sort_index()

        dates_s = sorted(list(set(X_s.index.tz_convert(ET).date)))
        if len(dates_s) >= 4:
            split_idx_s = int(len(dates_s) * 0.75)
            boundary_date_s = dates_s[split_idx_s]
            train_mask_s = X_s.index.tz_convert(ET).date < boundary_date_s
            val_start_dt_s = X_s[~train_mask_s].index.min()
            
            # Purge training samples whose outcome window (15 bars = 15 minutes) overlaps with validation
            purge_horizon = timedelta(minutes=15)
            train_filtered_mask_s = train_mask_s & (X_s.index < val_start_dt_s - purge_horizon)
            
            X_tr_s, y_tr_s = X_s[train_filtered_mask_s], y_s[train_filtered_mask_s]
            X_va_s, y_va_s = X_s[~train_mask_s], y_s[~train_mask_s]
        else:
            split_s = int(len(X_s) * 0.75)
            val_start_dt_s = X_s.iloc[split_s:].index.min()
            purge_horizon = timedelta(minutes=15)
            train_mask_s = X_s.index < val_start_dt_s - purge_horizon
            X_tr_s, y_tr_s = X_s[train_mask_s], y_s[train_mask_s]
            X_va_s, y_va_s = X_s.iloc[split_s:], y_s.iloc[split_s:]

        if not X_tr_s.empty and not X_va_s.empty:
            assert X_tr_s.index.max() < X_va_s.index.min(), f"Short temporal split violated: {X_tr_s.index.max()} >= {X_va_s.index.min()}"

        # ---- Fit Models ----
        clf_l = HistGradientBoostingClassifier(
            max_iter=300, learning_rate=0.06, max_depth=6,
            l2_regularization=1.0, early_stopping=True, random_state=42,
        )
        clf_l.fit(X_tr.to_numpy(), y_tr.to_numpy())

        clf_s = HistGradientBoostingClassifier(
            max_iter=300, learning_rate=0.06, max_depth=6,
            l2_regularization=1.0, early_stopping=True, random_state=42,
        )
        clf_s.fit(X_tr_s.to_numpy(), y_tr_s.to_numpy())

        # Evaluate
        def _safe_auc(clf, X_val, y_val):
            if y_val.nunique() < 2:
                return 0.5
            proba = clf.predict_proba(X_val.to_numpy())[:, 1]
            return float(roc_auc_score(y_val, proba))

        auc_l = _safe_auc(clf_l, X_va, y_va)
        auc_s = _safe_auc(clf_s, X_va_s, y_va_s)

        # Decoupled Promotion Gate
        current_auc_l = float(self.model.meta.get("validation_auc_long") or self.model.meta.get("validation_auc") or 0.0)
        current_auc_s = float(self.model.meta.get("validation_auc_short") or self.model.meta.get("validation_auc") or 0.0)
        
        promoted_l = False
        promoted_s = False

        if auc_l >= current_auc_l + self.PROMOTE_THRESHOLD:
            joblib.dump(clf_l, self.model.models_dir / "signal_model_long.joblib")
            # F-08 FIX: Removed dual-write to "signal_model.joblib" (legacy path).
            # When only the long model is promoted (short not promoted simultaneously),
            # writing clf_l to signal_model.joblib makes the legacy loader treat a
            # long-only classifier as the universal bidirectional model — silently
            # misapplying long probabilities to short entries. The legacy file is
            # managed exclusively by the generation store on full paired promotions.
            self.model._model_long = clf_l
            self.model.meta.update({
                "validation_auc_long": round(auc_l, 4),
                "trained_at_long": datetime.now(UTC).isoformat(),
            })
            promoted_l = True
            applog.info("trainer", f"Global LONG model PROMOTED: AUC {current_auc_l:.3f} → {auc_l:.3f}")

        else:
            applog.info("trainer", f"Global LONG model NOT promoted: new AUC {auc_l:.3f} vs current {current_auc_l:.3f}")

        if auc_s >= current_auc_s + self.PROMOTE_THRESHOLD:
            joblib.dump(clf_s, self.model.models_dir / "signal_model_short.joblib")
            self.model._model_short = clf_s
            self.model.meta.update({
                "validation_auc_short": round(auc_s, 4),
                "trained_at_short": datetime.now(UTC).isoformat(),
            })
            promoted_s = True
            applog.info("trainer", f"Global SHORT model PROMOTED: AUC {current_auc_s:.3f} → {auc_s:.3f}")
        else:
            applog.info("trainer", f"Global SHORT model NOT promoted: new AUC {auc_s:.3f} vs current {current_auc_s:.3f}")

        if promoted_l or promoted_s:
            self.model.meta.update({
                "trained_at": datetime.now(UTC).isoformat(),
                "validation_auc": round(max(self.model.meta.get("validation_auc_long") or 0.0, self.model.meta.get("validation_auc_short") or 0.0), 4),
                "continuous_learning": True,
            })
            (self.model.models_dir / "signal_model.meta.json").write_text(
                json.dumps(self.model.meta, indent=2)
            )

        return float(self.model.meta.get("validation_auc") or 0.5)

    # ------------------------------------------------------------------
    # Per-symbol specialist training
    # ------------------------------------------------------------------

    async def _train_specialists(
        self, dfs: dict[str, pd.DataFrame]
    ) -> dict[str, dict]:
        """Train a specialist per symbol concurrently.

        Returns {symbol: lifecycle_dict} where lifecycle_dict contains:
          decision: PROMOTED | RETAINED_INCUMBENT | FITTED_NOT_PROMOTED | FAILED
          candidate_auc: newly fitted AUC
          incumbent_auc: previously loaded AUC (if any)
        """
        semaphore = asyncio.Semaphore(self.TRAIN_CONCURRENCY)
        results: dict[str, dict] = {}

        async def _one(sym: str, df: pd.DataFrame) -> tuple[str, dict | None]:
            async with semaphore:
                try:
                    result = await asyncio.to_thread(self._fit_specialist_sync, sym, df)
                    return sym, result
                except Exception as exc:
                    applog.warning("trainer", f"  Specialist [{sym}] failed: {exc}")
                    return sym, {"decision": "FAILED", "reason": str(exc)}

        outcomes = await asyncio.gather(*[_one(sym, df) for sym, df in dfs.items()])
        for sym, result in outcomes:
            if result is not None:
                results[sym] = result
        return results

    def _fit_specialist_sync(self, symbol: str, df: pd.DataFrame) -> dict:
        """Fit a specialist candidate and apply promotion gate vs incumbent.

        Lifecycle states returned:
          PROMOTED           — candidate AUC >= incumbent AUC + threshold; artifact replaced
          RETAINED_INCUMBENT — candidate AUC insufficient; incumbent kept
          FITTED_NOT_PROMOTED — no incumbent; candidate fitted but AUC below min_auc
          INSUFFICIENT_DATA  — not enough samples to fit
        """
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.metrics import roc_auc_score
        from app.ai.features import build_training_set, FEATURE_COLUMNS
        import hashlib
        import joblib

        X_l, y_l = build_training_set(df, horizon_bars=15, threshold_pct=0.10, side="long")
        X_s, y_s = build_training_set(df, horizon_bars=15, threshold_pct=0.10, side="short")
        if len(X_l) < 300:
            applog.info("trainer", f"  Specialist [{symbol}] INSUFFICIENT_DATA n_samples={len(X_l)} < 300")
            return {"decision": "INSUFFICIENT_DATA", "n_samples": len(X_l)}

        split_l = int(len(X_l) * 0.75)
        split_s = int(len(X_s) * 0.75)
        
        from datetime import timedelta
        purge = timedelta(minutes=15)
        
        # Long split with purge
        val_start_l = X_l.iloc[split_l:].index.min()
        mask_l = X_l.index < val_start_l - purge
        X_tr_l, y_tr_l = X_l[mask_l], y_l[mask_l]
        X_va_l, y_va_l = X_l.iloc[split_l:], y_l.iloc[split_l:]
        
        # Short split with purge
        val_start_s = X_s.iloc[split_s:].index.min()
        mask_s = X_s.index < val_start_s - purge
        X_tr_s, y_tr_s = X_s[mask_s], y_s[mask_s]
        X_va_s, y_va_s = X_s.iloc[split_s:], y_s.iloc[split_s:]

        clf_l = HistGradientBoostingClassifier(
            max_iter=150, learning_rate=0.08, max_depth=5,
            l2_regularization=1.0, early_stopping=True, random_state=42,
        )
        if not X_tr_l.empty:
            clf_l.fit(X_tr_l.to_numpy(), y_tr_l.to_numpy())

        clf_s = HistGradientBoostingClassifier(
            max_iter=150, learning_rate=0.08, max_depth=5,
            l2_regularization=1.0, early_stopping=True, random_state=42,
        )
        if not X_tr_s.empty:
            clf_s.fit(X_tr_s.to_numpy(), y_tr_s.to_numpy())

        def _auc(clf, X_val, y_val) -> float:
            if y_val.nunique() < 2:
                return 0.5
            return float(roc_auc_score(y_val, clf.predict_proba(X_val.to_numpy())[:, 1]))

        auc_l = _auc(clf_l, X_va_l, y_va_l) if not X_va_l.empty and not X_tr_l.empty else 0.5
        auc_s = _auc(clf_s, X_va_s, y_va_s) if not X_va_s.empty and not X_tr_s.empty else 0.5
        candidate_auc = max(auc_l, auc_s)

        # Build feature schema fingerprint
        feature_hash = hashlib.md5(",".join(FEATURE_COLUMNS).encode()).hexdigest()[:12]
        artifact_path = self.specialist_dir / f"{symbol}.pkl"

        # Read incumbent AUC from existing artifact (if any)
        incumbent_auc = 0.0
        if artifact_path.exists():
            try:
                incumbent = joblib.load(artifact_path)
                if isinstance(incumbent, dict):
                    incumbent_auc = float(incumbent.get("auc", 0.0))
                    # Check feature version compatibility: if incompatible, treat incumbent_auc as 0.0
                    # to force promotion of the new, compatible candidate.
                    if incumbent.get("feature_version") != feature_hash:
                        applog.warning(
                            "trainer",
                            f"  Specialist [{symbol}] incumbent schema is incompatible: "
                            f"artifact version {incumbent.get('feature_version')} != current version {feature_hash} "
                            f"— ignoring incumbent AUC to force promotion of the new candidate.",
                        )
                        incumbent_auc = 0.0
                else:
                    incumbent_auc = 0.0
            except Exception:
                incumbent_auc = 0.0

        # Promotion gate: candidate must beat incumbent by at least PROMOTE_THRESHOLD
        should_promote = candidate_auc >= incumbent_auc + self.PROMOTE_THRESHOLD
        decision = "PROMOTED" if should_promote else "RETAINED_INCUMBENT"

        applog.info(
            "trainer",
            f"  Specialist [{symbol}] FITTED | candidate_auc={candidate_auc:.4f} "
            f"incumbent_auc={incumbent_auc:.4f} threshold=+{self.PROMOTE_THRESHOLD} "
            f"decision={decision}",
        )

        if should_promote:
            artifact = {
                "long": clf_l,
                "short": clf_s,
                "auc": candidate_auc,
                "auc_long": round(auc_l, 6),
                "auc_short": round(auc_s, 6),
                "trained_at": datetime.now(UTC).isoformat(),
                "symbol": symbol,
                "n_samples": len(X_l),
                "n_positive": int(y_l.sum()),
                "n_negative": int((y_l == 0).sum()),
                "n_train": split_l,
                "n_validation": len(X_l) - split_l,
                "base_rate": round(float(y_l.mean()), 4),
                "feature_version": feature_hash,
                "feature_count": len(FEATURE_COLUMNS),
                "horizon_bars": 15,
                "threshold_pct": 0.10,
                "label_definition": "price_up_threshold_pct_over_horizon_bars",
                "training_window_start": df.index.min().isoformat() if len(df) > 0 else None,
                "training_window_end": df.index.max().isoformat() if len(df) > 0 else None,
            }
            joblib.dump(artifact, artifact_path)
            # Invalidate in-memory specialist cache so score_action() loads the new artifact
            sym_upper = symbol.upper()
            if sym_upper in self.model._specialists:
                del self.model._specialists[sym_upper]
            applog.info("trainer", f"  Specialist [{symbol}] PROMOTED and cache invalidated")

        return {
            "decision": decision,
            "candidate_auc": round(candidate_auc, 4),
            "incumbent_auc": round(incumbent_auc, 4),
            "n_samples": len(X_l),
        }

    # ------------------------------------------------------------------
    # MetaLabel training (Stage 3)
    # ------------------------------------------------------------------

    async def _train_meta_labels(
        self, dfs: dict[str, pd.DataFrame]
    ) -> dict[str, dict]:
        """Train MetaLabel classifiers for all configured strategies and sides.

        Runs sequentially (not parallelised) because each strategy uses
        the full symbol universe and CPU load from specialists already ran.
        Returns {strategy_side: result_dict}.
        """
        from app.ai.prediction import MetaLabelEngine
        from app.config import get_models_dir

        strategies = ["orb", "momentum", "vwap_reversion", "rsi2_reversion", "bb_squeeze"]
        sides = ["long", "short"]
        meta_engine: MetaLabelEngine = getattr(self.model, "_meta_label_engine", None)  # type: ignore[assignment]
        if meta_engine is None:
            # Instantiate standalone engine pointing at same models dir
            meta_engine = MetaLabelEngine(get_models_dir())

        results: dict[str, dict] = {}
        for strategy in strategies:
            for side in sides:
                key = f"{strategy}_{side}"
                try:
                    result = await asyncio.to_thread(
                        meta_engine.train,
                        strategy,
                        dfs,
                        15,    # horizon_bars
                        0.10,  # threshold_pct
                        side,
                        0.52,  # min_validation_auc
                        self.strategy_overrides,
                    )
                    results[key] = result
                    applog.info(
                        "trainer",
                        f"  MetaLabel [{strategy}] ({side}) {result.get('decision', result.get('status', 'unknown'))} "
                        f"val_auc={result.get('val_auc', 'n/a')} "
                        f"n_samples={result.get('n_samples', 'n/a')}",
                    )
                except Exception as exc:
                    applog.warning("trainer", f"  MetaLabel [{strategy}] ({side}) training failed: {exc}")
                    results[key] = {"status": "failed", "strategy": strategy, "side": side, "reason": str(exc)}

        # Sync the pipeline's MetaLabelEngine if it shares the same object
        # (avoids requiring a restart to pick up newly trained artifacts)
        if hasattr(self.model, "_meta_label_engine") and self.model._meta_label_engine is not None:
            promoted = [k for k, r in results.items() if r.get("decision") == "PROMOTED"]
            for cache_key in promoted:
                # Force cache reload by removing cached entry
                if cache_key in self.model._meta_label_engine._classifiers:
                    del self.model._meta_label_engine._classifiers[cache_key]
                    applog.info("trainer", f"  MetaLabel [{cache_key}] inference cache invalidated")

        return results

    # ------------------------------------------------------------------
    # Post-training readiness audit (Stage 4)
    # ------------------------------------------------------------------

    def _readiness_audit(self, dfs: dict[str, pd.DataFrame]) -> None:
        """Enumerate every artifact actually loaded for inference and log provenance.

        This is the single source of truth for 'what will inference use?'
        It distinguishes newly promoted artifacts from retained incumbents.
        """
        import joblib
        import hashlib
        from app.ai.features import FEATURE_COLUMNS
        from app.ai.prediction import MetaLabelEngine
        from app.config import get_models_dir

        runtime_feature_hash = hashlib.md5(",".join(FEATURE_COLUMNS).encode()).hexdigest()[:12]
        applog.info("trainer", "=== POST-TRAINING READINESS AUDIT ===")

        # ---- Global model ----
        applog.info(
            "trainer",
            f"[readiness] GLOBAL | trained_at={self.model.meta.get('trained_at', 'unknown')} "
            f"auc_long={self.model.meta.get('validation_auc_long', 'unknown')} "
            f"auc_short={self.model.meta.get('validation_auc_short', 'unknown')}",
        )

        # ---- Specialists ----
        for sym in sorted(dfs.keys()):
            sym_upper = sym.upper()
            artifact_path = self.specialist_dir / f"{sym}.pkl"
            if artifact_path.exists():
                try:
                    art = joblib.load(artifact_path)
                    if isinstance(art, dict):
                        art_hash = art.get("feature_version", "unknown")
                        schema_ok = art_hash == runtime_feature_hash
                        applog.info(
                            "trainer",
                            f"[readiness] SPECIALIST {sym_upper} | "
                            f"trained_at={art.get('trained_at', 'unknown')} "
                            f"auc={art.get('auc', 'unknown')} "
                            f"n_samples={art.get('n_samples', 'unknown')} "
                            f"n_pos={art.get('n_positive', 'unknown')} "
                            f"n_neg={art.get('n_negative', 'unknown')} "
                            f"feature_version={art_hash} "
                            f"runtime_hash={runtime_feature_hash} "
                            f"schema_compatible={schema_ok}",
                        )
                    else:
                        applog.info("trainer", f"[readiness] SPECIALIST {sym_upper} | LEGACY_FORMAT (no provenance)")
                except Exception as exc:
                    applog.warning("trainer", f"[readiness] SPECIALIST {sym_upper} | load_failed: {exc}")
            else:
                applog.info("trainer", f"[readiness] SPECIALIST {sym_upper} | NO_ARTIFACT")

        # ---- MetaLabel ----
        strategies = ["orb", "momentum", "vwap_reversion", "rsi2_reversion", "bb_squeeze"]
        sides = ["long", "short"]
        meta_dir = get_models_dir() / "meta"
        for strategy in strategies:
            for side in sides:
                artifact_path = meta_dir / f"meta_{strategy}_{side}.joblib"
                if artifact_path.exists():
                    try:
                        art = joblib.load(artifact_path)
                        if isinstance(art, dict) and "model" in art:
                            art_hash = art.get("feature_version", "unknown")
                            schema_ok = art_hash == runtime_feature_hash
                            applog.info(
                                "trainer",
                                f"[readiness] META_LABEL {strategy} ({side}) | "
                                f"trained_at={art.get('trained_at', 'unknown')} "
                                f"val_auc={art.get('val_auc', 'unknown')} "
                                f"n_samples={art.get('n_samples', 'unknown')} "
                                f"n_pos={art.get('n_positive', 'unknown')} "
                                f"n_neg={art.get('n_negative', 'unknown')} "
                                f"feature_version={art_hash} "
                                f"schema_compatible={schema_ok} "
                                f"source=REAL_MODEL",
                            )
                        else:
                            applog.info("trainer", f"[readiness] META_LABEL {strategy} ({side}) | LEGACY_FORMAT")
                    except Exception as exc:
                        applog.warning("trainer", f"[readiness] META_LABEL {strategy} ({side}) | load_failed: {exc}")
                else:
                    applog.info(
                        "trainer",
                        f"[readiness] META_LABEL {strategy} ({side}) | NO_ARTIFACT "
                        f"(decision=BYPASS until trained and promoted)",
                    )

        applog.info("trainer", "=== READINESS AUDIT COMPLETE ===")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def status(self) -> dict:
        return {
            "training_in_progress": self.training_in_progress,
            "last_trained": self.last_trained.isoformat() if self.last_trained else self.model.meta.get("trained_at"),
            "last_result": self.last_result,
        }




import asyncio
import tempfile
import threading
from datetime import datetime
from pathlib import Path

import joblib

from app.ai.generation import AtomicSpecialistStore, TrainingCoordinator
from app.core.applog import applog


class ContinuousTrainer(_LegacyContinuousTrainer):
    """Continuous trainer whose writers share ownership and atomic publication."""

    def __init__(
        self,
        data,
        model,
        model_dir: Path,
        strategy_overrides: dict[str, dict] | None = None,
        training_coordinator: TrainingCoordinator | None = None,
    ) -> None:
        super().__init__(data, model, model_dir, strategy_overrides)
        coordinator = training_coordinator or getattr(model, "_training_coordinator", None)
        if coordinator is None:
            coordinator = TrainingCoordinator()
            setattr(model, "_training_coordinator", coordinator)
        self.training_coordinator = coordinator
        self._training_owner = f"continuous:{id(self)}"
        self._specialist_store = AtomicSpecialistStore(model_dir)
        self._specialist_fit_lock = threading.Lock()

    async def trigger(self, watchlist: list[str], as_of: datetime | None = None) -> None:
        """Claim synchronously before scheduling so duplicate writers cannot queue."""
        owner = self._training_owner
        if not self.training_coordinator.try_claim(owner):
            applog.info(
                "trainer",
                f"Model training already owned by {self.training_coordinator.owner or 'another writer'} "
                "- skipping continuous trigger.",
            )
            return
        try:
            asyncio.create_task(self._run_cycle(watchlist, as_of, training_owner=owner))
        except BaseException:
            self.training_coordinator.release(owner)
            raise

    async def _run_cycle(
        self,
        watchlist: list[str],
        as_of: datetime | None = None,
        *,
        training_owner: str | None = None,
    ) -> None:
        owner = training_owner
        try:
            await super()._run_cycle(watchlist, as_of)
        finally:
            if owner is not None and self.training_coordinator.owner == owner:
                self.training_coordinator.release(owner)

    def _fit_global_sync(self, dfs):
        """Fit candidates off the inference snapshot, then commit one generation."""
        generation_store = getattr(self.model, "_generation_store", None)
        if generation_store is None:
            return super()._fit_global_sync(dfs)
        incumbent = generation_store.current
        incumbent_long = self.model._model_long
        incumbent_short = self.model._model_short
        incumbent_meta = dict(self.model.meta)
        try:
            auc = super()._fit_global_sync(dfs)
            candidate_long = self.model._model_long
            candidate_short = self.model._model_short
            candidate_meta = dict(self.model.meta)
            changed = (
                candidate_long is not incumbent_long
                or candidate_short is not incumbent_short
                or candidate_meta != incumbent_meta
            )
            if changed:
                snapshot = generation_store.publish(candidate_long, candidate_short, candidate_meta)
                self.model._model_long = snapshot.long_model
                self.model._model_short = snapshot.short_model
                self.model.meta = dict(snapshot.meta)
            return auc
        except BaseException:
            if incumbent is not None:
                self.model._model_long = incumbent.long_model
                self.model._model_short = incumbent.short_model
                self.model.meta = dict(incumbent.meta)
            else:
                self.model._model_long = incumbent_long
                self.model._model_short = incumbent_short
                self.model.meta = incumbent_meta
            raise

    def _fit_specialist_sync(self, symbol, df):
        """Train away from the live path and atomically replace one specialist artifact."""
        with self._specialist_fit_lock:
            live_dir = self.specialist_dir
            with tempfile.TemporaryDirectory(dir=self.model_dir, prefix=".specialist-candidate-") as candidate_root:
                candidate_dir = Path(candidate_root)
                self.specialist_dir = candidate_dir
                try:
                    live_path = live_dir / f"{symbol}.pkl"
                    candidate_path = candidate_dir / f"{symbol}.pkl"
                    if live_path.exists():
                        candidate_path.write_bytes(live_path.read_bytes())
                    result = super()._fit_specialist_sync(symbol, df)
                    if result.get("decision") == "PROMOTED":
                        artifact = joblib.load(candidate_path)
                        self._specialist_store.publish(symbol, artifact)
                        self.model._specialists.pop(symbol.upper(), None)
                    return result
                finally:
                    self.specialist_dir = live_dir