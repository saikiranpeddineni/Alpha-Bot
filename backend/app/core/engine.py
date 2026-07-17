            self.state == EngineState.RUNNING
            and not self.risk.daily_halted
            and not in_flatten_window
            and (self.clock.is_open(now) or self.mode == "demo")
        )
        if can_trade and new_closed:
            pos_by_symbol = {p.symbol: p for p in positions}
            candidates = []
            for symbol in new_closed:
                entries = await self._evaluate_symbol(symbol, pos_by_symbol.get(symbol), account.equity, now)
                candidates.extend(entries)

            if candidates:
                from app.portfolio.allocator import process_portfolio_allocations
                from app.risk.manager import is_entry_order

                # Gather candidate signals
                alloc_signals = [sig for sig, _ in candidates]
                df_by_symbol = {sig.symbol: df for sig, df in candidates}

                # Shared portfolio gate and allocation
                decision_outcome = process_portfolio_allocations(
                    signals=alloc_signals,
                    positions=positions,
                    available_capital=account.cash,
                    equity=account.equity,
                    portfolio_gate=self.portfolio_gate,
                    portfolio_allocator=self.portfolio_allocator,
                )

                # Shadow research observes only deterministic post-allocation candidates.
                # The bridge has no return channel into risk, order construction, or submission.
                self.gemini_shadow.schedule(
                    [sig for sig, _ in decision_outcome.approved_entries],
                    session=session,
                )

                # Process gated/rejected entries
                for sig, reason in decision_outcome.gated_entries:
                    applog.info("engine", f"{sig.symbol}: Portfolio Gate/Allocator rejected -- {reason}")
                    self.publish("insight", self.insights.on_signal(sig, gated=True, gate_reason=reason).model_dump(mode="json"))
                    if getattr(sig, "candidate_id", None) is not None:
                        self._update_shadow_decision(sig.candidate_id, "rejected", reason, "portfolio_gate")

                # Size and submit approved entries
                if decision_outcome.approved_entries:
                    open_orders = await self.broker.open_orders()
                    pending_entries = [
                        o.symbol.upper() for o in open_orders
                        if is_entry_order(o, positions)
                    ]
                    effective_pending_entries = set(pending_entries)
                    for info in self.pending_reconciliation_orders.values():
                        effective_pending_entries.add(info["symbol"].upper())
                    reservations = SameCycleEntryReservations.from_snapshot(account.cash, positions)

                    # Fail-closed: if reconciliation is uncertain, do not dereference
                    # portfolio fields that may not exist yet. Preserve exits, but skip
                    # entry sizing/submission until reconciliation is healthy again.
                    if self.reconciliation_failed:
                        applog.warning("engine", "Skipping entry sizing while reconciliation is uncertain.")
                    else:
                        for sig, kelly_val in decision_outcome.approved_entries:
                            df = df_by_symbol[sig.symbol]
                            ctx = AccountContext(
                                equity=account.equity,
                                cash=reservations.cash,
                                positions=reservations.positions,
                                day_pnl=self.portfolio.day_pnl(account.equity),
                                session_start_equity=self.portfolio.session_start_equity or account.equity,
                                peak_equity=self.portfolio.peak_equity,
                                day_trades_today=self.portfolio.day_trades_today,
                                rolling_day_trades=self.portfolio.day_trades_in_window(now),
                                is_paper=self.broker.is_paper,
                                pending_entry_symbols=list(effective_pending_entries),
                            )
                            decision = self.risk.evaluate_entry(sig, ctx, df, now=now, kelly_override=kelly_val)
                            if not decision.approved:
                                applog.info("engine", f"{sig.symbol.upper()}: entry rejected by RiskManager — {decision.reason}")
                                self.publish("insight", self.insights.on_signal(sig, gated=True, gate_reason=decision.reason).model_dump(mode="json"))
                                if getattr(sig, "candidate_id", None) is not None:
                                    self._update_shadow_decision(sig.candidate_id, "rejected", decision.reason, "risk_gate")
                                continue

                            client_order_id = decision.order.client_order_id
                            self.pending_reconciliation_orders[client_order_id] = {
                                "symbol": decision.order.symbol.upper(),
                                "client_order_id": client_order_id,
                                "side": str(decision.order.side),
                                "qty": decision.order.qty,
                                "order_type": str(decision.order.order_type),
                                "limit_price": decision.order.limit_price,
                                "submitted_at": utcnow().isoformat(),
                            }
                            self.db.set_setting("pending_reconciliation_orders", self.pending_reconciliation_orders)

                            try:
                                order = await self.broker.submit(decision.order)
                                self.pending_reconciliation_orders.pop(client_order_id, None)
                                self.db.set_setting("pending_reconciliation_orders", self.pending_reconciliation_orders)
                                self.recent_orders.appendleft(order)
                                self.db.save_order(order, self.mode)
                                self.publish("insight", self.insights.on_signal(sig, gated=False).model_dump(mode="json"))
                                self.publish("orders", self.orders_payload())
                                if getattr(sig, "candidate_id", None) is not None:
                                    self._update_shadow_decision(sig.candidate_id, "accepted", "", "none")
                            except Exception as exc:
                                applog.error("engine", f"Ambiguous submission exception for {decision.order.symbol} ({client_order_id}): {exc}")
                                order = Order(
                                    id=client_order_id,
                                    client_order_id=client_order_id,
                                    symbol=decision.order.symbol,
                                    side=decision.order.side,
                                    qty=decision.order.qty,
                                    order_type=decision.order.order_type,
                                    limit_price=decision.order.limit_price,
                                    status=OrderStatus.PENDING,
                                    reason=f"Ambiguous submit: {exc}"
                                )
                                self.db.save_order(order, self.mode)

                            # Reserve active same-cycle submissions before evaluating the next candidate.
                            # This overlays the stale broker snapshot for cash, position-count, and sector checks.
                            reference_price = sig.price or float(df["close"].iloc[-1])
                            if reservations.reserve(order, reference_price):
                                effective_pending_entries.add(sig.symbol.upper())

            await self._drain_fills(now)

        # 5.5 Periodic background replay of pending shadow signals
        if self._tick_count % 10 == 0:
            try:
                await self._replay_pending_signals(now)
            except Exception as e:
                applog.error("engine", f"Background replay error: {e}")

        # 6. mark equity + persist
        account = await self.broker.account() if new_closed else account
        self.portfolio.mark_equity(account.equity, now)
        if self._tick_count % EQUITY_PERSIST_EVERY_TICKS == 0:
            self.db.save_equity(account.equity, account.cash, self.mode, now)

        # 7. broadcast
        self._broadcast_tick(account, now, new_closed, positions)

    async def _evaluate_symbol(self, symbol: str, position, equity: float, now: datetime) -> list[tuple[Signal, pd.DataFrame]]:  # noqa: ANN001
        assert self.data and self.broker and self.risk and self.pipeline