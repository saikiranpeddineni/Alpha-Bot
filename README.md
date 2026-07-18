# ⚡ AlphaDesk — Adaptive AI Day Trading & Research Platform

AlphaDesk is a local-first automated day-trading and quantitative research platform for U.S. stocks and ETFs. It combines an asynchronous trading engine, multiple intraday strategies, machine-learning signal filters, portfolio/risk controls, broker reconciliation, event-driven backtesting, shadow evaluation, Champion/Challenger experimentation, contextual performance memory, and a real-time React trading terminal.

> **Start safely in Demo mode with no broker account or API keys.** AlphaDesk includes a deterministic synthetic market so the full engine can run offline before connecting real market data or brokerage services.

> **Important:** This project is under active development and audit. Research and adaptive-intelligence features must remain isolated from production execution unless explicitly promoted through controlled governance. Review open GitHub issues before enabling live trading.

---

## Key Features

### Three operating modes

- **Demo** — deterministic synthetic market and simulated execution.
- **Paper** — real market data with simulated or paper-broker execution.
- **Live** — real brokerage execution behind explicit live-trading safety gates.

The architecture is designed to reuse the same core strategy, pipeline, portfolio and risk components across operating modes while keeping broker-specific execution isolated behind broker interfaces.

### Multi-strategy intraday engine

The strategy registry supports multiple independent signal generators, including:

- Opening Range Breakout (ORB)
- VWAP mean reversion
- EMA momentum
- RSI(2) dip-buy

Strategies emit candidate signals into a sequential production decision path rather than submitting orders directly.

### Production signal path

A typical entry flows through:

```text
Strategy.on_bar()
    ↓
TradingEngine._evaluate_symbol()
    ↓
Reconciliation safety guard
    ↓
SignalPipeline.process()
    ├─ ML probability gate
    ├─ Meta-label / model gates
    └─ Composite trade-score gate
    ↓
Portfolio gate + allocator
    ↓
RiskManager.evaluate_entry()
    ↓
Broker.submit()
```

This layered design intentionally separates signal generation, statistical filtering, portfolio constraints, risk approval and broker execution.

### Machine-learning signal intelligence

AlphaDesk includes a locally trained ML stack for scoring trading opportunities.

Capabilities include:

- Global LONG and SHORT models
- Strategy/symbol specialist models where sufficient data exists
- Validation-AUC-based model promotion
- Fallback to global models when specialist quality is insufficient
- Configurable ML confidence thresholds
- Meta-labeling support
- Feature generation from market and strategy context
- Model persistence and reload
- Training from accumulated historical market data

Model promotion should be treated independently from trading-policy promotion: a statistically improved model must still operate within production risk and governance controls.

### Market regime awareness

The engine classifies market conditions and can adapt scoring behavior to environments such as:

- Trend
- Range
- Volatile / high-volatility conditions

Regime context is used by strategy scoring and research analysis without allowing research-only calculations to silently bypass production gates.

### Dynamic asset discovery

The live scanning architecture can discover active market opportunities rather than relying exclusively on a fixed watchlist.

The intended discovery path includes active stocks and market movers from supported market-data/broker APIs, with fallback behavior when dynamic discovery is unavailable.

Liquidity, price and eligibility filters remain part of the candidate-selection process.

### Systemic market-risk monitoring

The platform supports systemic stress monitoring, including VIX-oriented market-risk gating, to reduce new exposure during abnormal volatility conditions.

Market-risk data should be treated as safety-critical input: stale or unavailable risk data must be handled explicitly rather than silently interpreted as a normal market.

---

## Risk Management

Capital preservation is a first-class part of the engine.

Controls include:

- ATR/stop-distance-aware position sizing
- Configurable risk per trade
- Maximum position sizing
- Maximum concurrent positions
- Daily loss circuit breaker
- Maximum drawdown kill switch
- Consecutive-loss cooldown
- Pre-close no-entry window
- End-of-day liquidation
- Stop-loss and take-profit handling
- PDT-related controls where applicable
- Liquidity and minimum-price filters
- Portfolio allocation and concentration controls
- Broker/local position reconciliation

### Fail-closed reconciliation

The engine tracks whether broker and local portfolio state have been authoritatively reconciled.

If reconciliation fails or becomes uncertain:

- New entries are blocked.
- Existing-position exits remain available.
- The engine attempts reconciliation again.
- Entry eligibility returns only after authoritative state is restored.

This prevents uncertain local state from creating additional exposure.

### End-of-day safety

The engine includes an EOD flattening window and reconciliation logic intended to verify that positions and relevant open orders are actually cleared rather than assuming a liquidation request succeeded.

---

## ShadowLedger & Missed-Opportunity Analysis

AlphaDesk records rejected and accepted candidate decisions for research without allowing those measurements to directly alter production execution.

Shadow research can capture:

- Candidate identity and lineage
- Strategy and direction
- Entry/reference price
- Stop loss and take profit
- ML confidence
- Final decision
- Rejection stage and reason
- Gate evidence
- Market context
- Forward outcome observations
- MFE / MAE
- Simulated R-multiple / opportunity cost

### Rejected-signal replay

Rejected signals can be evaluated against subsequent historical 1-minute OHLC data to answer questions such as:

- Would the rejected trade have reached TP?
- Would SL have triggered first?
- What were maximum favorable and adverse excursions?
- Did a particular ML, portfolio or risk gate prevent profit or avoid loss?

Replay and labeling must remain idempotent and strictly causal. Incomplete observation horizons should not be silently treated as losses or zero-return outcomes.

---

## CR-146 — Alpha Intelligence Loop

CR-146 introduces an autonomous **research and self-improvement architecture** around the production Champion policy.

The central safety principle is:

> **Research observes production. Research does not silently mutate production.**

### Parallel evidence evaluation

Decision gates can be evaluated in parallel for measurement and veto attribution while the existing sequential production path remains authoritative.

The evidence evaluator is designed to be fail-open with respect to measurement: research failures should not block or delay Champion execution.

### Veto attribution

The research layer measures which gates rejected a candidate and attempts to attribute opportunity cost across:

- Unique vetoes
- Shared vetoes
- Redundant vetoes

This is intended to identify filters that consistently prevent profitable trades or protect the system from losing trades.

### Contextual performance memory

Historical episodes can be aggregated by contextual features such as strategy, direction and market environment.

The architecture includes:

- Bayesian shrinkage / contextual expectancy
- Episodic memory
- Nearest-neighbor comparisons
- Feature standardization
- Strategy/direction partitioning
- Strategy-aware outcome horizons

All contextual research must remain temporally causal during backtests and historical analysis.

### Champion / Challenger experimentation

The production configuration is the **Champion**.

Alternative policies run as **Challengers** in shadow mode.

Challengers can vary policy parameters such as ML thresholds or scoring behavior while observing the same decision environment. Challenger results are recorded separately and must not place production orders.

A Challenger should only become eligible for promotion after sufficient statistically meaningful evidence.

### Nomination and manual promotion

The governance flow is intended to be:

```text
Champion production decisions
        ↓
Shadow measurement
        ↓
Challenger experiments
        ↓
Statistical qualification
        ↓
Policy nomination
        ↓
Authorized human review
        ↓
Atomic promotion
        ↓
New Champion
```

Promotion must deploy exactly the configuration that was tested and nominated, persist a complete immutable policy snapshot, survive restart equivalently, and provide rollback/auditability.

### Resurrection queue

CR-146 also includes experimental signal resurrection for rejected candidates.

Resurrection is intended for paper/shadow research only. A resurrected opportunity must be re-evaluated using fresh market state and must pass the normal production-style gates again before simulated execution.

Live trading must not execute stale rejected-signal prices, stops or targets.

---

## Gemini / AI Research Integration

The platform includes AI-assisted analysis capabilities for research and insight generation.

AI components are intentionally downstream of deterministic trading controls. Gemini/LLM analysis should never be required for the Champion order path to continue operating.

Structured response schemas are used where applicable to make AI outputs predictable and machine-readable.

---

## Backtesting

AlphaDesk includes an event-driven backtesting engine designed to replay historical data through production-like components.

Backtest considerations include:

- Next-bar-open execution assumptions
- Stop/target handling
- Adverse stop slippage
- Stop-before-target tie-breaking when ordering is unknowable
- No-look-ahead indicator calculations
- Strategy-aware outcome horizons
- Portfolio and risk constraints
- End-of-day behavior

Performance metrics can include:

- Total return / P&L
- Win rate
- Sharpe ratio
- Sortino ratio
- Maximum drawdown
- Profit factor
- Expectancy
- R-multiples

Backtest and shadow results are research evidence, not guarantees of future performance.

---

## Trading Terminal

The React/TypeScript frontend provides a real-time interface for operating and observing the engine.

Typical surfaces include:

- Candlestick charts
- EMA/VWAP overlays
- Trade markers
- Watchlist
- Market regime indicators
- Positions
- Orders
- Trades
- Equity curve
- Strategy controls
- Risk settings
- Engine logs
- AI insights
- Backtest results
- Research/shadow information

Updates are streamed through WebSocket with REST endpoints used for commands and queries.

---

## Architecture

```text
┌──────────────────────── React Trading Terminal ────────────────────────┐
│ Charts · Watchlist · Positions · Orders · Logs · Research · Backtests │
└───────────────────────▲──────────────────────▲─────────────────────────┘
                        │ REST /api/*           │ WebSocket /ws
┌───────────────────────┴──────────────────────┴─────────────────────────┐
│                         FastAPI + Event Hub                            │
│                                                                       │
│  ┌──────────────────── TradingEngine ──────────────────────────────┐   │
│  │                                                                 │   │
│  │ DataManager → Indicators → Regime → Strategies                 │   │
│  │                                      ↓                          │   │
│  │                              Signal Pipeline                    │   │
│  │                                      ↓                          │   │
│  │                           Portfolio Gate/Allocator              │   │
│  │                                      ↓                          │   │
│  │                               Risk Manager                      │   │
│  │                                      ↓                          │   │
│  │                                   Broker                        │   │
│  │                                                                 │   │
│  │ Production Champion ────────► Orders/Fills/Portfolio            │   │
│  │          │                                                      │   │
│  │          └────► ShadowLedger / Evidence / Challengers           │   │
│  │                         │                                       │   │
│  │                         └────► Nomination / Human Promotion     │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                       │
│ Data providers: Synthetic · Yahoo · Alpaca                            │
│ Brokers: Paper simulator · Alpaca                                     │
│ Persistence: SQLite                                                    │
└───────────────────────────────────────────────────────────────────────┘
```

---

## Quickstart

### Prerequisites

- Python 3.10+
- Node.js 18+

### Windows

```powershell
.\install.ps1
.\start.ps1
```

You can also use `install.cmd` and `start.cmd`.

### macOS / Linux

```bash
chmod +x install.sh start.sh dev.sh
./install.sh
./start.sh
```

Open the local AlphaDesk UI at `http://127.0.0.1:8742` and start in **Demo** mode.

---

## Alpaca Paper Trading

Copy `.env.example` to `.env` and configure your credentials:

```ini
ALPACA_API_KEY=...
ALPACA_API_SECRET=...
ALPACA_PAPER=true
BROKER=alpaca
```

Restart AlphaDesk, select **Paper**, and start the engine.

Never commit API keys or secrets to Git.

---

## Live Trading

Live trading is intentionally safety-gated.

At minimum, the existing configuration requires explicit live enablement:

```ini
ENABLE_LIVE_TRADING=true
```

The UI also requires live confirmation before starting live execution.

Before enabling live trading:

1. Review `docs/SECURITY.md`.
2. Review all open safety-critical GitHub issues.
3. Verify broker account mode and credentials.
4. Verify reconciliation succeeds.
5. Confirm risk limits.
6. Run the same configuration in paper mode first.
7. Confirm EOD flattening and kill-switch behavior.
8. Verify the currently promoted policy snapshot.

---

## Configuration

Configuration is primarily supplied through `.env` and persisted runtime settings.

Common variables include:

| Variable | Example / Default | Purpose |
| --- | --- | --- |
| `ALPHADESK_MODE` | `demo` | Startup mode: demo / paper / live |
| `DATA_PROVIDER` | `auto` | Synthetic, Yahoo, Alpaca or automatic provider selection |
| `BROKER` | `paper_sim` | Broker implementation |
| `ALPACA_API_KEY` | — | Alpaca API key |
| `ALPACA_API_SECRET` | — | Alpaca API secret |
| `ALPACA_PAPER` | `true` | Select Alpaca paper endpoint where supported |
| `ENABLE_LIVE_TRADING` | `false` | Explicit live-order safety gate |
| `RISK_PER_TRADE_PCT` | configurable | Equity risk per trade |
| `MAX_DAILY_LOSS_PCT` | configurable | Daily loss circuit-breaker threshold |
| `MAX_DRAWDOWN_PCT` | configurable | Maximum drawdown kill-switch threshold |

See `.env.example` for the authoritative annotated configuration list.

Risk settings can also be changed through the application where supported and persisted to SQLite.

---

## Project Layout

```text
backend/app/
  api/          REST routes and WebSocket integration
  ai/           ML features, models and AI insights
  backtest/     event-driven historical replay and metrics
  brokers/      broker registry and implementations
  core/         trading engine, domain models, market clock and logging
  data/         market-data providers, discovery and caching
  db/           SQLite persistence layer
  indicators/   technical indicators
  portfolio/    allocation, portfolio state, fills and trade accounting
  risk/         position sizing, entry controls, halts and safety rules
  strategies/   strategies, signal pipeline and CR-146 research policies

backend/tests/   backend regression and behavioral tests
frontend/src/    React + TypeScript trading terminal
docs/            architecture, strategy, extension and security documentation
```

---

## Testing

Run the backend test suite before merging trading-engine changes.

### Windows

```powershell
.venv\Scripts\python -m pytest
```

### macOS / Linux

```bash
.venv/bin/python -m pytest
```

Important regression areas include:

- Indicator correctness
- Strategy behavior
- ML pipeline gates
- Position sizing
- Daily-loss and drawdown halts
- Broker reconciliation
- Entry fail-closed behavior
- Exit availability during safety halts
- Order idempotency
- Paper fill behavior
- Stop/target ordering
- Portfolio accounting
- EOD liquidation
- Backtest determinism
- Look-ahead protection
- Candidate lineage
- Champion/Challenger isolation
- Shadow outcome idempotency
- Restart equivalence
- Promotion governance

---

## Development

```bash
./dev.sh
```

Windows:

```powershell
.\dev.ps1
```

Linting:

```bash
ruff check backend
```

When modifying the trading engine, trace the complete signal-to-order path before changing code. A strategy signal passes through multiple independent safety gates; fixing one layer without understanding downstream behavior can create unintended execution changes.

For production-critical changes, add regression tests that demonstrate both the unsafe state and the expected recovery path.

---

## Extending AlphaDesk

The platform is registry-oriented and intended to support additional:

- Strategies
- Brokers
- Market-data providers
- Indicators
- ML models
- Risk controls
- Research evaluators

See `docs/EXTENDING.md` and `docs/ARCHITECTURE.md` for deeper implementation guidance.

---

## Security & Operational Safety

Treat any endpoint or configuration capable of changing production trading behavior as privileged.

Operational recommendations:

- Never expose the API publicly without authentication and authorization.
- Never commit broker credentials.
- Keep live trading disabled by default.
- Validate whether the broker is paper or live before enabling experimental execution features.
- Persist and audit production policy changes.
- Require explicit human approval for research-policy promotion.
- Keep AI/LLM systems outside the deterministic order-submission dependency chain.
- Reconcile broker state after startup and connectivity failures.
- Fail closed for new exposure when authoritative state is unavailable.

See `docs/SECURITY.md`.

---

## Known Development Status

AlphaDesk is actively evolving. Recent development has focused heavily on autonomous research, shadow measurement and Champion/Challenger governance.

Open issues are the authoritative source for currently known defects and safety gaps. In particular, promotion governance, research/production isolation and resurrection behavior should be reviewed before relying on adaptive features in a live environment.

---

## Disclaimer

AlphaDesk is provided for **education, experimentation and research**.

Day trading involves substantial risk of financial loss. Automated systems can amplify software defects, bad assumptions, stale data and configuration mistakes at machine speed. Backtested, paper, shadow or synthetic performance does not guarantee future results.

Nothing in this repository is financial advice. Test extensively in Demo and Paper modes before considering live execution. If you enable live trading, you are solely responsible for the orders generated by the system and their financial consequences.

---

## License

MIT — see `LICENSE`.