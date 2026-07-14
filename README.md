# ⚡ AlphaDesk — AI-Powered Day Trading Platform

A production-grade, local-first automated day trading platform for U.S. stocks and ETFs.
One machine, one process, zero cloud dependencies: an async trading engine, an adaptive
AI strategy stack, institutional-style risk management, an event-driven backtester and a
real-time trading terminal UI.

> **Runs out of the box with no accounts and no API keys** — Demo mode ships with a
> deterministic synthetic market so you can watch the full system trade offline, then
> connect real data/brokerage when ready.

---

## Highlights

- **Three modes, one engine** — `demo` (offline synthetic market), `paper` (real data,
  simulated fills), `live` (real orders via broker) share the *identical* strategy, risk
  and fill-model code path. What you backtest is what trades.
- **AI strategy stack** — four classic intraday strategies (opening-range breakout, VWAP
  reversion, EMA momentum, RSI(2) dip-buy) feed an AI signal pipeline: a locally trained
  gradient-boosting model scores every signal, a market-regime detector (trend / range /
  volatile) reweights strategies as conditions change, and conflicts are resolved by
  composite score. Train the model on your own data with one click.
- **Capital preservation first** — ATR-based position sizing at a fixed % of equity risk,
  bracket exits on every entry, max daily loss halt, max drawdown kill switch, loss-streak
  cooldowns, end-of-day auto-flatten, PDT protection, liquidity/price filters.
- **Honest backtesting** — event-driven replay through the live engine stack with
  next-bar-open fills, adverse slippage on stops, stop-before-target tie-breaking and
  strictly causal indicators. Full metrics: Sharpe, Sortino, max DD, profit factor,
  expectancy, R-multiples.
- **Trading terminal UI** — real-time candlestick charts with EMA/VWAP overlays and trade
  markers, watchlist with regime badges, positions/orders/trades/logs, AI insight feed,
  equity curve, strategy toggles, risk settings — streamed over WebSocket.
- **Extensible by design** — brokers, data providers, strategies, indicators and AI models
  are all pluggable registries. See [docs/EXTENDING.md](docs/EXTENDING.md).

## Quickstart

**Prerequisites:** Python 3.10+ and Node.js 18+.

### Windows
```powershell
.\install.ps1     # or double-click install.cmd
.\start.ps1       # or double-click start.cmd
```

### macOS / Linux
```bash
chmod +x install.sh start.sh dev.sh
./install.sh
./start.sh
```

Open **http://127.0.0.1:8742**, press **▶ Start** — the engine begins trading the
synthetic demo market immediately. No configuration required.

### Going beyond demo

1. Copy `.env.example` → `.env` (the installer does this) and add
   [Alpaca](https://alpaca.markets) keys (free paper-trading account):
   ```ini
   ALPACA_API_KEY=...
   ALPACA_API_SECRET=...
   ALPACA_PAPER=true
   BROKER=alpaca
   ```
2. Restart, switch the mode selector to **Paper**, press Start. Real market data,
   simulated (or Alpaca paper) fills.
3. **Live trading** is intentionally double-gated: set `ENABLE_LIVE_TRADING=true` in
   `.env` *and* type `LIVE` in the confirmation dialog. Please read
   [docs/SECURITY.md](docs/SECURITY.md) first.

## Architecture

```
┌─────────────────────────────  React terminal UI  ─────────────────────────────┐
│  charts · watchlist · positions · orders · logs · AI insights · backtests     │
└───────────────▲───────────────────────────────────────────────▲───────────────┘
                │ REST /api/*                                    │ WebSocket /ws
┌───────────────┴────────────────────────────────────────────────┴──────────────┐
│                            FastAPI + Event Hub                                 │
│  ┌──────────────────────────  TradingEngine (async tick loop)  ─────────────┐  │
│  │                                                                          │  │
│  │  DataManager ─► Indicators ─► Regime detector ─► Strategies (registry)   │  │
│  │      │                                              │                    │  │
│  │      │                             AI Signal Pipeline (ML gate + regime  │  │
│  │      │                                weights + conflict resolution)     │  │
│  │      │                                              │                    │  │
│  │      │                                        Risk Manager (sizing,      │  │
│  │      │                                        halts, PDT, EOD windows)   │  │
│  │      │                                              │                    │  │
│  │      ▼                                              ▼                    │  │
│  │  SQLite bar cache                            Broker (registry)           │  │
│  │                                              ├─ PaperBroker (sim fills)  │  │
│  │  Providers (registry)                        └─ AlpacaBroker (REST)      │  │
│  │  ├─ Synthetic (demo)                                │                    │  │
│  │  ├─ Yahoo Finance                             PortfolioTracker           │  │
│  │  └─ Alpaca Data                               (fills → trades → equity)  │  │
│  └──────────────────────────────────────────────────────────────────────────┘  │
│  Backtester = the same stack replayed over history (next-open fills)           │
│  SQLite: orders · trades · equity · backtests · settings                       │
└────────────────────────────────────────────────────────────────────────────────┘
```

Deep dive: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) ·
strategy details: [docs/STRATEGIES.md](docs/STRATEGIES.md)

## Configuration

Everything lives in `.env` (see `.env.example` for the full annotated list). Key values:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ALPHADESK_MODE` | `demo` | boot mode: `demo` / `paper` / `live` |
| `DATA_PROVIDER` | `auto` | `synthetic`, `yahoo`, `alpaca`, or auto-pick |
| `BROKER` | `paper_sim` | `paper_sim` (built-in) or `alpaca` |
| `RISK_PER_TRADE_PCT` | `0.5` | % of equity risked entry→stop per trade |
| `MAX_DAILY_LOSS_PCT` | `2.0` | day loss that halts trading until next session |
| `MAX_DRAWDOWN_PCT` | `10.0` | equity drawdown that trips the kill switch |
| `ENABLE_LIVE_TRADING` | `false` | hard gate for live orders |

Risk parameters are also editable at runtime in **Settings** (persisted to SQLite).

## Testing

75 backend tests cover indicators (known-value), the paper fill engine (slippage,
brackets, gaps, tie-breaks), risk rules (sizing, halts, PDT, cooldowns), portfolio
accounting (round trips, flips, R-multiples), strategies on crafted scenarios, the
backtester (determinism, no-overnight-holds, look-ahead protection) and the API surface.

```bash
.venv/Scripts/python -m pytest        # Windows
.venv/bin/python -m pytest            # macOS/Linux
```

## Project layout

```
backend/app/
  core/        engine, market clock, domain models, logging
  data/        provider registry: synthetic · yahoo · alpaca + SQLite cache
  brokers/     broker registry: paper simulator · alpaca
  indicators/  vectorized TA library (EMA, RSI, MACD, ATR, VWAP, BB, ER)
  strategies/  strategy registry + AI signal pipeline
  ai/          features · regime detection · ML model · insights
  risk/        risk manager (sizing + halts)
  portfolio/   fill ledger, trade records, equity curve
  backtest/    event-driven backtester + performance metrics
  api/         REST routes + WebSocket hub
  db/          SQLAlchemy persistence
backend/tests/ pytest suite
frontend/src/  React + TypeScript terminal UI (lightweight-charts, zustand)
docs/          architecture, strategies, extending, security
```

## Development

```bash
./dev.sh          # or .\dev.ps1 — backend --reload + Vite HMR on :5173
ruff check backend
```

## ⚠️ Disclaimer

AlphaDesk is provided for **education and research**. Day trading involves substantial
risk of loss; algorithmic trading can amplify mistakes at machine speed. Past (and
especially backtested or synthetic) performance does not indicate future results. Nothing
here is financial advice. Test extensively in demo and paper modes; if you enable live
trading, you alone are responsible for every order it places.

## License

MIT — see [LICENSE](LICENSE).
