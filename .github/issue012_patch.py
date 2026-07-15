from pathlib import Path

path = Path("backend/app/core/engine.py")
text = path.read_text()

if "from app.core.entry_reservations import SameCycleEntryReservations" not in text:
    text = text.replace(
        "from app.core.logging import applog\n",
        "from app.core.logging import applog\nfrom app.core.entry_reservations import SameCycleEntryReservations\n",
    )

old = '''                    effective_pending_entries = set(pending_entries)\n\n                    for sig, kelly_val in decision_outcome.approved_entries:\n                        df = df_by_symbol[sig.symbol]\n                        ctx = AccountContext(\n                            equity=account.equity,\n                            cash=account.cash,\n                            positions=positions,\n'''
new = '''                    effective_pending_entries = set(pending_entries)\n                    reservations = SameCycleEntryReservations.from_snapshot(account.cash, positions)\n\n                    for sig, kelly_val in decision_outcome.approved_entries:\n                        df = df_by_symbol[sig.symbol]\n                        ctx = AccountContext(\n                            equity=account.equity,\n                            cash=reservations.cash,\n                            positions=reservations.positions,\n'''
if old in text:
    text = text.replace(old, new)
elif new not in text:
    raise SystemExit("012 context construction anchor not found")

old = '''                        # Add to effective pending set if successfully submitted into an active state\n                        if order.status not in (OrderStatus.CANCELED, OrderStatus.REJECTED):\n                            effective_pending_entries.add(sig.symbol.upper())\n'''
new = '''                        # Reserve active same-cycle submissions before evaluating the next candidate.\n                        # This overlays the stale broker snapshot for cash, position-count, and sector checks.\n                        reference_price = sig.price or float(df["close"].iloc[-1])\n                        if reservations.reserve(order, reference_price):\n                            effective_pending_entries.add(sig.symbol.upper())\n'''
if old in text:
    text = text.replace(old, new)
elif new not in text:
    raise SystemExit("012 submission reservation anchor not found")

path.write_text(text)
