# Architecture

AlphaDesk is a single-machine, single-process system: a FastAPI app hosting an async
trading engine, a WebSocket event hub and the built React UI. Everything persists to
SQLite under `data/`. No cloud services are required for any mode.

## Design principles

1. **One code path.** Demo, paper, live and backtest all execute the same strategy →
   AI pipeline → risk → fill-model chain. Backtests differ only in the market-data
   iterator and the fill timing rule (next-bar open), so validated behavior transfers.
2. **Capital preservation outranks alpha.** Every entry is bracketed at the fill model
   or broker level. Halts (daily loss, drawdown) flatten first and ask questions later.
3. **Local-first.** Synthetic demo market, local ML training, SQLite storage. Network
   access is only used by the data provider / broker you explicitly configure.
4. **Registries over frameworks.** Strategies, brokers, data providers are plain classes
   registered in one place each; adding one never touches engine code.

## Component map

| Component | Module | Responsibility |
| --- | --- | --- |
| TradingEngine | `core/engine.py` | async tick loop; orchestrates everything below |
| MarketClock | `core/clock.py` | ET sessions, entry/flatten windows, demo always-open |
| DataManager | `data/manager.py` | provider facade, rolling frames, closed-bar detection, SQLite bar cache |
| Providers | `data/synthetic.py`, `data/yahoo.py`, `data/alpaca_data.py` | canonical OHLCV frames |
| Indicators | `indicators/ta.py` | vectorized, causal TA columns (`enrich()`) |
| Regime detector | `ai/regime.py` | trend/range/volatile classification per symbol |
| Strategies | `strategies/*.py` | pure signal functions over enriched frames |
| SignalPipeline | `strategies/pipeline.py` | ML gate, regime weights, conflict resolution |
| SignalModel | `ai/model.py` | gradient boosting p(favorable move); heuristic fallback |
| RiskManager | `risk/manager.py` | sizing, halts, cooldowns, session windows, PDT |
| Brokers | `brokers/paper.py`, `brokers/alpaca.py` | order execution + fills |
| PortfolioTracker | `portfolio/tracker.py` | fill ledger → TradeRecords, equity curve, day stats |
| Backtester | `backtest/engine.py` | historical replay of the identical stack |
| API / Hub | `api/routes.py`, `api/ws.py` | REST + WebSocket fan-out |
| Persistence | `db/database.py` | orders, trades, equity, backtests, settings |

## The tick

Every `POLL_INTERVAL_SEC` (demo: `DEMO_BAR_SECONDS`) the engine runs one tick:

```
1. poll data          new closed 1-min bars per watchlist symbol
2. fills              paper: replay bars through the fill sim (brackets, limits)
                      alpaca: poll order state; both -> drain_fills()
3. ledger             fills -> PortfolioTracker -> TradeRecords -> DB + WS
                      closed trades update the RiskManager loss streak
4. session roll       new ET session resets day P&L, day-trade count, daily halt
5. kill switches      max drawdown -> flatten + HALT (manual reset)
                      max daily loss -> flatten + halt until next session
6. EOD window         flatten all positions N minutes before the close
7. strategy pass      per symbol with a new bar:
                        enrich(frame) -> detect regime (insight on change)
                        each enabled strategy.on_bar() -> signals
                        SignalPipeline: ML prob + regime weight + conflicts
                        RiskManager.evaluate_entry -> sized bracket order
                        broker.submit -> DB + WS
8. mark equity        equity curve point; persisted every ~minute
9. broadcast          status, account, quotes, bars, positions, metrics
```

Ticks are exception-isolated: a failing tick logs and retries; ~30 consecutive failures
stop the engine rather than trade blind.

## Data layer

Canonical bar frame everywhere: tz-aware UTC `DatetimeIndex` + `open/high/low/close/volume`.
`DataManager` keeps a rolling in-memory frame per symbol (capped), detects *closed* bars
(bar window fully elapsed + grace), resamples 1-min → 5/15-min, and serves deep history
through a SQLite cache so backtests and ML training don't refetch.

The synthetic provider deserves a note: each (symbol, day) is generated deterministically
from a seeded RNG — regime-switching drift/vol blocks, U-shaped volume, occasional
news-style jumps, overnight gaps. Demo backtests are exactly reproducible.

## Fill simulation (paper + backtest)

`PaperBroker` models: immediate market fills with configurable slippage (live paper) or
**next-bar-open fills** (backtests — you cannot trade a close you just observed); limit
fills honoring gaps (fill at open when price gaps through); per-position brackets where
a bar touching both stop and target resolves to the **stop first** (conservative); stops
pay adverse slippage, targets fill at the limit; margin-style checks for shorts.

## AI layer

- **Features** (`ai/features.py`): returns (1/5/15m), RSI(14/2), normalized MACD
  histogram, ATR%, volume z-score, VWAP distance, Bollinger position/width, efficiency
  ratio, time-of-session encodings. Identical computation at train and serve time.
- **Model** (`ai/model.py`): `HistGradientBoostingClassifier` predicting
  p(forward return over N bars > cost threshold), chronological 70/30 holdout with AUC
  reported honestly next to the base rate. Persisted via joblib. Until trained, a
  transparent momentum heuristic serves the same interface (the UI labels it).
- **Regime** (`ai/regime.py`): efficiency ratio + EMA slope + ATR percentile →
  trend_up / trend_down / range / volatile. The pipeline reweights each strategy per
  regime (e.g. VWAP reversion 1.2× in range, 0.7× in trend).
- **Insights** (`ai/insights.py`): regime shifts, accepted/rejected signals with ML
  confidence, risk events, session performance — as structured cards. Optional Claude
  narrative if `ANTHROPIC_API_KEY` is set (never required).

## Scaling notes

The async engine comfortably handles a 30-symbol watchlist at 1-minute bars on a laptop.
Hot paths are vectorized; per-tick strategy work is O(symbols with new bars). To push
further: swap SQLite for Postgres/TimescaleDB (one class), move providers to websocket
streams (the DataManager contract already isolates this), and shard symbols across
engine instances. The UI already consumes everything over a single multiplexed WS.
