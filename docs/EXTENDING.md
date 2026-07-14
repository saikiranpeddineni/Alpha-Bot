# Extending AlphaDesk

Every extension point is a small class + one registry line. The engine, backtester,
API and UI pick new components up automatically.

## Add a strategy

Create `backend/app/strategies/my_strategy.py`:

```python
import pandas as pd
from app.core.domain import Signal, SignalAction
from app.strategies.base import Strategy, StrategyContext, register

@register
class GapFade(Strategy):
    name = "gap_fade"
    description = "Fade large opening gaps back toward yesterday's close"
    default_params = {"min_gap_pct": 1.5, "atr_stop_mult": 1.5, "reward_risk": 2.0}

    def on_bar(self, ctx: StrategyContext, symbol: str, df: pd.DataFrame) -> Signal | None:
        if ctx.position is not None or len(df) < 30:
            return None
        last = df.iloc[-1]
        if pd.isna(last.get("atr14")):
            return None
        # ... your logic over the enriched frame (see indicators/ta.py for columns)
        return Signal(
            symbol=symbol,
            action=SignalAction.ENTER_LONG,
            confidence=0.6,
            strategy=self.name,
            reason="gap fade setup",
            price=float(last["close"]),
            stop_loss=float(last["close"]) - self.params["atr_stop_mult"] * float(last["atr14"]),
            take_profit=None,  # risk manager defaults to 2R when omitted
        )
```

Then import it in `backend/app/strategies/__init__.py`. Done: it appears in
`GET /api/strategies`, the UI toggle list, and backtests. Rules of the road:

- **Be causal.** Only read rows ≤ the last one; indicators in the frame already are.
- **Return `None` freely.** Warmup guards (`pd.isna`) keep the engine quiet and safe.
- **Exits**: return `SignalAction.EXIT` when `ctx.position` belongs to you
  (`ctx.position.strategy == self.name`); brackets and EOD flatten are automatic.
- Give it a regime weight row in `strategies/pipeline.py::REGIME_WEIGHTS`
  (unlisted strategies default to 1.0 in every regime).
- Add a crafted-scenario test like those in `backend/tests/test_strategies.py`.

## Add a broker

Subclass `Broker` (`backend/app/brokers/base.py`): implement `connect`, `account`,
`positions`, `open_orders`, `submit`, `cancel`, `close_position`, `flatten_all`,
`drain_fills`. Fills you report through `drain_fills()` drive the entire portfolio /
trade-record / risk pipeline — nothing else to integrate. Register the class in
`make_broker()` and add any credentials to `config.py` + `.env.example`.
`brokers/alpaca.py` is the reference (~200 lines): server-side bracket orders,
poll-based fill detection.

## Add a data provider

Subclass `DataProvider` (`backend/app/data/base.py`): `get_bars()` returning the
canonical frame (use `normalize_bars()`), and `get_quotes()`. Register in
`data/manager.py::make_provider()`. You inherit the SQLite bar cache, closed-bar
detection and resampling for free. `data/yahoo.py` is the minimal reference.

## Add an indicator / ML feature

1. Add the function to `indicators/ta.py` and a column in `enrich()`.
2. To feed it to the model, append the column name to
   `ai/features.py::FEATURE_COLUMNS` (and compute any derivation in
   `compute_features()`).
3. Retrain the model; live inference, backtests and training all pick it up because
   they share the same pipeline.
4. Add a known-value test in `tests/test_indicators.py`.

## Swap the AI model

`ai/model.py::SignalModel.train()` is the only place that touches the estimator. Any
classifier with `predict_proba` drops in (sklearn pipelines included). For deep models,
keep the contract: `p_up(features) -> float in [0,1]`, persisted under `data/models/`.
The `score_action()` interface is what the pipeline consumes — leave it stable.

## Add API surface / UI panels

Backend: add routes in `api/routes.py` (state lives on `request.app.state`), broadcast
via `request.app.state.hub.publish(type, data)`. Frontend: add the message type in
`src/types.ts`, handle it in `src/store.ts::applyMessage`, render from the store.
