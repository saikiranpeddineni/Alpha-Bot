"""Harden pending-submission, flatten propagation, and reconciliation ownership.

Run after the two earlier 20260729 runtime applicators.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend/app/core/engine.py"
text = ENGINE.read_text(encoding="utf-8")


def replace_once(old: str, new: str, label: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    text = text.replace(old, new, 1)


# EOD flatten submission is not proof of a fill. Keep the close guard until the
# durable fill consumer proves the local lot flat.
replace_once(
'''                await self.broker.flatten_all(reason="end of day")
                if hasattr(self, "_pending_close_symbols"):
                    self._pending_close_symbols.clear()
                await self._drain_fills(now)
''',
'''                for position in positions:
                    self._pending_close_symbols.add(position.symbol.upper())
                await self.broker.flatten_all(reason="end of day")
                await self._drain_fills(now)
''',
"EOD close guard ownership",
)

# A successful submit call proves broker acknowledgement, not immediate broker
# visibility. Preserve the durable client-id guard until reconciliation observes
# the order or its resulting position/fill. Definitive rejection can release it.
replace_once(
'''                                order = await self.broker.submit(decision.order)
                                self.pending_reconciliation_orders.pop(client_order_id, None)
                                self.db.set_setting("pending_reconciliation_orders", self.pending_reconciliation_orders)
                                self.recent_orders.appendleft(order)
''',
'''                                order = await self.broker.submit(decision.order)
                                if order.status == OrderStatus.REJECTED:
                                    self.pending_reconciliation_orders.pop(client_order_id, None)
                                else:
                                    self.pending_reconciliation_orders[client_order_id]["submission_state"] = "broker_acknowledged"
                                    self.pending_reconciliation_orders[client_order_id]["broker_order_id"] = getattr(order, "id", None)
                                self.db.set_setting("pending_reconciliation_orders", self.pending_reconciliation_orders)
                                self.recent_orders.appendleft(order)
''',
"retain acknowledged submission guard",
)

# Explicitly label the pre-submit record. If submit throws, the same record is
# retained as ambiguous and blocks duplicate entry attempts.
replace_once(
'''                                "submitted_at": utcnow().isoformat(),
                            }
''',
'''                                "submitted_at": utcnow().isoformat(),
                                "submission_state": "submitting",
                            }
''',
"submission state initialization",
)
replace_once(
'''                            except Exception as exc:
                                applog.error("engine", f"Ambiguous submission exception for {decision.order.symbol} ({client_order_id}): {exc}")
                                order = Order(
''',
'''                            except Exception as exc:
                                self.pending_reconciliation_orders[client_order_id]["submission_state"] = "ambiguous"
                                self.pending_reconciliation_orders[client_order_id]["last_error"] = str(exc)
                                self.db.set_setting("pending_reconciliation_orders", self.pending_reconciliation_orders)
                                applog.error("engine", f"Ambiguous submission exception for {decision.order.symbol} ({client_order_id}): {exc}")
                                order = Order(
''',
"persist ambiguous submission state",
)

# 404/not-found is not proof that a just-submitted order never reached the
# broker. Keep the guard fail-closed. Broker observation resolves the guard;
# terminal broker state is persisted before release.
old = '''    async def _reconcile_pending_submissions(self) -> None:
        """Query the broker for ambiguous order submissions and resolve their status."""
        if not self.pending_reconciliation_orders:
            return

        client_ids = list(self.pending_reconciliation_orders.keys())
        applog.info("engine", f"Checking status of {len(client_ids)} ambiguous order submissions: {client_ids}")

        for cid in client_ids:
            order_info = self.pending_reconciliation_orders[cid]
            symbol = order_info["symbol"]
            try:
                order = await self.broker.get_order_by_client_id(cid)
                if order is not None:
                    applog.info("engine", f"Ambiguous order resolved: {symbol} ({cid}) is active/filled at broker.")
                    self.pending_reconciliation_orders.pop(cid, None)
                    self.db.set_setting("pending_reconciliation_orders", self.pending_reconciliation_orders)
                    self.db.save_order(order, self.mode)
                else:
                    applog.info("engine", f"Ambiguous order resolved: {symbol} ({cid}) was NEVER received by broker. Treating as REJECTED.")
                    self.pending_reconciliation_orders.pop(cid, None)
                    self.db.set_setting("pending_reconciliation_orders", self.pending_reconciliation_orders)
                    rejected_order = Order(
                        id=cid,
                        client_order_id=cid,
                        symbol=symbol,
                        side=OrderSide.BUY if order_info["side"].lower() == "buy" else OrderSide.SELL,
                        qty=order_info["qty"],
                        order_type=OrderType.LIMIT if order_info["order_type"].lower() == "limit" else OrderType.MARKET,
                        limit_price=order_info["limit_price"],
                        status=OrderStatus.REJECTED,
                        reason="Ambiguous submit resolved to not found at broker."
                    )
                    self.db.save_order(rejected_order, self.mode)
            except Exception as exc:
                applog.warning("engine", f"Reconciliation query failed for client ID {cid}: {exc}. Retaining guard.")
'''
new = '''    async def _reconcile_pending_submissions(self) -> None:
        """Resolve durable submission guards only from affirmative broker truth.

        A transient 404 immediately after submit is propagation uncertainty, not a
        rejection. Unknown submissions therefore remain fail-closed until the broker
        exposes the client id, a resulting position/fill is observed, or an operator
        explicitly reconciles the account.
        """
        if not self.pending_reconciliation_orders:
            return

        client_ids = list(self.pending_reconciliation_orders.keys())
        applog.info("engine", f"Checking status of {len(client_ids)} guarded order submissions: {client_ids}")

        broker_positions = await self.broker.positions()
        broker_symbols = {p.symbol.upper() for p in broker_positions if abs(float(p.qty)) > 1e-9}
        changed = False
        for cid in client_ids:
            order_info = self.pending_reconciliation_orders.get(cid)
            if not order_info:
                continue
            symbol = order_info["symbol"].upper()
            try:
                order = await self.broker.get_order_by_client_id(cid)
                if order is None:
                    # A resulting position is affirmative evidence that the entry made
                    # it through even if the order endpoint is temporarily lagging.
                    if symbol in broker_symbols:
                        applog.info("engine", f"Submission guard resolved by broker position: {symbol} ({cid})")
                        self.pending_reconciliation_orders.pop(cid, None)
                        changed = True
                    else:
                        order_info["submission_state"] = "awaiting_broker_visibility"
                        applog.warning(
                            "engine",
                            f"Submission {symbol} ({cid}) not yet visible at broker; retaining fail-closed guard.",
                        )
                    continue

                self.db.save_order(order, self.mode)
                terminal = order.status in (OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.FILLED)
                active = order.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED)
                if terminal or active:
                    applog.info("engine", f"Submission guard resolved from broker order truth: {symbol} ({cid}) status={order.status}")
                    self.pending_reconciliation_orders.pop(cid, None)
                    changed = True
            except Exception as exc:
                applog.warning("engine", f"Reconciliation query failed for client ID {cid}: {exc}. Retaining guard.")

        if changed:
            self.db.set_setting("pending_reconciliation_orders", self.pending_reconciliation_orders)
        else:
            # Persist state transitions such as awaiting_broker_visibility too.
            self.db.set_setting("pending_reconciliation_orders", self.pending_reconciliation_orders)
'''
replace_once(old, new, "pending submission reconciliation semantics")

# Heartbeat must not manufacture a REJECTED order from a temporary 404. Pending
# submission reconciliation has sole ownership of this durable guard.
old_fragment = '''                    if order is None:
                        symbol = str((info or {}).get("symbol", "")).upper()
                        if symbol and symbol not in broker_syms:
                            stale.append(cid)
                        else:
                            order_bad = True
                for cid in stale:
                    info = self.pending_reconciliation_orders.pop(cid, None)
                    if info and isinstance(info, dict):
                        try:
                            sym = str(info.get("symbol", "")).upper()
                            side = OrderSide.BUY if str(info.get("side", "")).lower() in ("buy", "long") else OrderSide.SELL
                            qty = float(info.get("qty", 1.0))
                            rej_order = Order(
                                id=cid,
                                client_order_id=cid,
                                symbol=sym,
                                side=side,
                                qty=qty,
                                order_type=OrderType.LIMIT if info.get("limit_price") else OrderType.MARKET,
                                limit_price=info.get("limit_price"),
                                status=OrderStatus.REJECTED,
                                reason="Terminal reconciliation order cleared for flat position",
                            )
                            self.db.save_order(rej_order, self.mode)
                        except Exception:
                            pass
                if stale:
                    self.db.set_setting("pending_reconciliation_orders", self.pending_reconciliation_orders)
                    applog.info("engine.heartbeat", f"Cleared {len(stale)} terminal reconciliation order(s) for flat positions")
'''
new_fragment = '''                    if order is None:
                        # Not-found is propagation uncertainty. Never infer rejection
                        # from flat position + 404; retain the durable submission guard.
                        order_bad = True
'''
replace_once(old_fragment, new_fragment, "heartbeat pending-order authority")

ENGINE.write_text(text, encoding="utf-8")
compile(text, str(ENGINE), "exec")
print("Applied submission/reconciliation fixes; engine.py syntax compile passed.")
