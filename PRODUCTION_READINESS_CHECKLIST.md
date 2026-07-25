# Alpha-Bot Production Readiness Checklist & Engineering Policy

**Version Tag**: `v1.0.0-execution-freeze`  
**Date**: July 26, 2026  
**Status**: ARCHITECTURE FROZEN — OPERATIONAL TRANSITION  

---

## 1. Subsystem Stabilization Status

- [x] **Spawn Commander V4**: Portfolio-aware capital allocator verified fail-closed on missing marks, deterministic, and broker-isolated (22 unit tests passing).
- [x] **Target D Observer**: Passive, read-only observer verified fail-open (`UNAVAILABLE`) with directional short fallback targets corrected (3 unit tests passing).
- [x] **V2 Exit Engine**: Monotonic trailing stops, intra-bar stop-loss precedence over take-profit, strict capital release ordering, and double-exit prevention verified (5 unit tests passing).
- [x] **Core Test Suite**: 30/30 unit & integration tests passing cleanly.

---

## 2. Operational Readiness Checklist

### Infrastructure Resilience
- [ ] Clean startup from an empty state (initial DB migration & directory bootstrap).
- [ ] Clean startup from an interrupted checkpoint (serialized virtual portfolio & engine state restore).
- [ ] Graceful shutdown (handling SIGINT/SIGTERM, flushing logs, closing websockets).
- [ ] Recovery after forced process kill (`kill -9` / ungraceful termination state reconciliation).
- [ ] Recovery after broker disconnect (Alpaca WebSocket & REST API reconnect loop).
- [ ] Recovery after internet outage (re-establishing data streams & position reconciliation).

### Broker & Order Execution
- [ ] Duplicate order prevention (in-flight symbol locks & `client_order_id` idempotency).
- [ ] Partial fills (handling non-atomic fill execution & cumulative quantity tracking).
- [ ] Cancel/replace logic (safely adjusting limit orders without double-submitting).
- [ ] Rejected orders (fail-closed response handling for margin or sizing rejections).
- [ ] Market halt & volatility pause handling.
- [ ] Early market close (FOMC, holiday hours schedule adjustments).
- [ ] Extended-hours configuration & session boundary enforcement.

### Risk Controls
- [ ] Daily max loss circuit breaker (hard stop at daily loss limit).
- [ ] Max drawdown circuit breaker (portfolio watermark protection).
- [ ] Single-position & total portfolio capital limits.
- [ ] Buying power & leverage limits.
- [ ] Pattern Day Trader (PDT) balance & transaction restrictions.
- [ ] System clock synchronization (NTP drift monitoring < 100ms).

### Data Stream Integrity
- [ ] Missing bar detection & forward-fill handling.
- [ ] Delayed bar detection (timestamp latency monitoring).
- [ ] Duplicate bar filtering.
- [ ] Out-of-order bar rejection.
- [ ] Stock split & corporate action adjustments.
- [ ] Trading halt detection.

### Monitoring & Telemetry
- [ ] Engine health heartbeat (periodic liveness metric).
- [ ] Exception alerts (Slack/Discord/PagerDuty webhook on critical unhandled errors).
- [ ] Broker connectivity heartbeat.
- [ ] Position reconciliation (reconciling local portfolio tracker with broker positions).
- [ ] Cash & equity reconciliation (reconciling local ledger with broker account balance).
- [ ] Telemetry dashboard (real-time stream of signals, gates, and fills).

---

## 3. Future Development Policy

### Allowed Without Architecture Review
- ML model retraining & continuous learning updates.
- Feature engineering & feature selection additions.
- Calibration table updates (pre-cutoff historical training data).
- New strategy signal generators (submitting signals through existing pipeline).
- Scanner filters & universe selection adjustments.
- Telemetry logging, dashboard, and UI enhancements.
- Non-behavioral performance & speed optimizations.

### STRICTLY PROHIBITED Without Formal Architecture Review
- Execution ordering or pipeline stage sequence changes.
- Risk manager gate pipeline modifications.
- Spawn Commander or capital allocator redesign.
- Target D observer logic redesign or authority expansion.
- V2 Exit sequencing or trailing stop algorithm modifications.
- Broker authority expansion (granting research modules execution powers).
- Modification of frozen benchmark replay datasets.

---

## 4. Next Investment Priorities

1. **Extended Replay Validation**: Run multi-week chronological replays across historical sessions.
2. **Paper Trading Phase**: Execute 2–3 weeks of live paper trading with full order lifecycle logging.
3. **Operational Tooling**: Operationalize heartbeat monitors, Slack alerts, and automated position reconcilers.
4. **Model Retraining**: Periodically retrain ML models and meta-label classifiers using pre-cutoff data.
