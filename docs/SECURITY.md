# Security & Safety

## Credentials

- API keys live in `.env` only. The file is gitignored; never commit it.
- Keys are read by the backend process and **never sent to the frontend** — the UI only
  sees boolean flags (`has_alpaca_keys`, `live_trading_enabled`).
- The optional `ANTHROPIC_API_KEY` is used server-side only, for insight narration.

## Network posture

- The server binds `127.0.0.1` by default — reachable only from your machine. If you
  change `ALPHADESK_HOST`, put a reverse proxy with TLS + authentication in front; the
  app itself is single-user and unauthenticated by design (local-first).
- CORS is restricted to the local UI origins.
- Outbound connections are limited to the data provider / broker you configure
  (demo mode makes none).

## Live trading gates

Live orders require **all** of:

1. `ENABLE_LIVE_TRADING=true` set manually in `.env`;
2. valid broker credentials;
3. mode switched to Live in the UI **and** the literal string `LIVE` typed into the
   confirmation dialog (the API enforces this server-side — it is not a UI nicety);
4. a real broker adapter — the engine refuses to run "live" on the paper simulator.

Even then, every order passes the risk manager: sizing caps, daily-loss halt, drawdown
kill switch, EOD flatten. Bracket protective orders are placed server-side at the broker
(Alpaca), so stops survive a crash of this machine.

## Operational safety

- **Kill switch**: max-drawdown breach flattens everything and halts the engine until a
  human resets it (`POST /api/engine/reset-halt` / the banner button).
- **Fail-safe loop**: a tick that throws is logged and retried; ~30 consecutive failures
  stop the engine instead of trading blind.
- **Crash recovery**: orders, trades and equity snapshots persist to SQLite; on Alpaca,
  positions and brackets live at the broker independent of this process.
- **PDT protection**: live accounts under $25k are blocked from a 4th same-day round trip.

## Data & privacy

Everything — bars cache, model artifacts, trade history, settings — stays in the local
`data/` directory. Delete it to reset the platform completely.

## Responsible use

This software places real orders when you configure it to. Review
[docs/STRATEGIES.md](STRATEGIES.md), run demo and paper modes extensively, start with
small risk settings, and never trade money you cannot afford to lose.
