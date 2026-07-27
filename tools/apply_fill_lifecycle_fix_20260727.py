"""Surgical follow-up for Alpaca cumulative-fill lifecycle.

Run from repository root. The script fails closed if the checkpointed source no
longer matches the reviewed production-stabilization revision.
"""
from pathlib import Path

ALPACA = Path("backend/app/brokers/alpaca.py")
ENGINE = Path("backend/app/core/engine.py")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


alpaca = ALPACA.read_text(encoding="utf-8")
alpaca = replace_once(
    alpaca,
    "        self._processed_fill_qty: dict[str, float] = {}\n        self._processed_fill_notional: dict[str, float] = {}\n",
    "        # Acknowledged watermarks advance only after PortfolioTracker commits.\n"
    "        self._processed_fill_qty: dict[str, float] = {}\n"
    "        self._processed_fill_notional: dict[str, float] = {}\n"
    "        # In-flight watermarks prevent a repeated Alpaca cumulative snapshot from\n"
    "        # being emitted twice while the previous Fill awaits durable processing.\n"
    "        self._emitted_fill_qty: dict[str, float] = {}\n"
    "        self._emitted_fill_notional: dict[str, float] = {}\n",
    "alpaca watermark initialization",
)

alpaca = replace_once(
    alpaca,
    "            processed = self._processed_fill_qty.get(oid, 0.0)\n            if db:\n                processed = max(processed, db.processed_fill_qty(mode=self._broker_mode, account_id=self.account_id, fill_key=oid))\n            if cumulative <= processed + 1e-9:\n",
    "            processed = max(\n"
    "                self._processed_fill_qty.get(oid, 0.0),\n"
    "                self._emitted_fill_qty.get(oid, 0.0),\n"
    "            )\n"
    "            if db:\n"
    "                processed = max(processed, db.processed_fill_qty(mode=self._broker_mode, account_id=self.account_id, fill_key=oid))\n"
    "            if cumulative <= processed + 1e-9:\n",
    "alpaca candidate watermark",
)

alpaca = replace_once(
    alpaca,
    "                processed = self._processed_fill_qty.get(oid, 0.0)\n                if db:\n                    processed = max(processed, db.processed_fill_qty(mode=self._broker_mode, account_id=self.account_id, fill_key=oid))\n            delta = cumulative - processed\n",
    "                processed = max(\n"
    "                    self._processed_fill_qty.get(oid, 0.0),\n"
    "                    self._emitted_fill_qty.get(oid, 0.0),\n"
    "                )\n"
    "                if db:\n"
    "                    processed = max(processed, db.processed_fill_qty(mode=self._broker_mode, account_id=self.account_id, fill_key=oid))\n"
    "            delta = cumulative - processed\n",
    "alpaca emission watermark",
)

alpaca = replace_once(
    alpaca,
    "            prev_notional = self._processed_fill_notional.get(oid, 0.0)\n\n            if processed > 0 and delta > 0 and cum_notional > prev_notional:\n",
    "            ack_qty = self._processed_fill_qty.get(oid, 0.0)\n"
    "            emitted_qty = self._emitted_fill_qty.get(oid, 0.0)\n"
    "            if emitted_qty >= ack_qty and emitted_qty >= processed - 1e-9:\n"
    "                prev_notional = self._emitted_fill_notional.get(oid, 0.0)\n"
    "            elif ack_qty >= processed - 1e-9:\n"
    "                prev_notional = self._processed_fill_notional.get(oid, 0.0)\n"
    "            else:\n"
    "                # A durable DB watermark can survive a process restart while the\n"
    "                # volatile notional cannot. Use the broker cumulative average as\n"
    "                # the conservative price basis; quantity de-dup remains exact.\n"
    "                prev_notional = processed * cum_avg_price\n\n"
    "            if processed > 0 and delta > 0 and cum_notional > prev_notional:\n",
    "alpaca incremental notional basis",
)

alpaca = replace_once(
    alpaca,
    "            # local_watermarks prevents duplicates within this poll only. Durable/in-memory\n            # acknowledgement happens after PortfolioTracker atomically commits the fill.\n            local_watermarks[oid] = cumulative\n",
    "            # Reserve the cumulative snapshot as in-flight before returning it to\n"
    "            # the engine. ACK promotes this reservation; NACK releases it.\n"
    "            local_watermarks[oid] = cumulative\n"
    "            self._emitted_fill_qty[oid] = cumulative\n"
    "            self._emitted_fill_notional[oid] = cum_notional\n",
    "alpaca reserve emitted fill",
)

alpaca = replace_once(
    alpaca,
    "        if cumulative > 0:\n            self._processed_fill_qty[oid] = max(self._processed_fill_qty.get(oid, 0.0), cumulative)\n        self._processed_fill_notional[oid] = self._processed_fill_notional.get(oid, 0.0) + float(fill.qty) * float(fill.price)\n\n\n    def drain_fills",
    "        if cumulative > 0:\n"
    "            previous_qty = self._processed_fill_qty.get(oid, 0.0)\n"
    "            if cumulative >= previous_qty:\n"
    "                self._processed_fill_qty[oid] = cumulative\n"
    "                self._processed_fill_notional[oid] = max(\n"
    "                    self._processed_fill_notional.get(oid, 0.0),\n"
    "                    self._emitted_fill_notional.get(oid, 0.0),\n"
    "                    self._processed_fill_notional.get(oid, 0.0) + float(fill.qty) * float(fill.price),\n"
    "                )\n"
    "        if self._emitted_fill_qty.get(oid, 0.0) <= self._processed_fill_qty.get(oid, 0.0) + 1e-9:\n"
    "            self._emitted_fill_qty.pop(oid, None)\n"
    "            self._emitted_fill_notional.pop(oid, None)\n\n"
    "    def nack_fill_processed(self, fill: Fill) -> None:\n"
    "        \"\"\"Release an uncommitted reservation so a later poll can retry it.\"\"\"\n"
    "        oid = str(fill.broker_fill_key or fill.order_id or \"\")\n"
    "        if not oid:\n"
    "            return\n"
    "        cumulative = float(fill.cumulative_qty or 0.0)\n"
    "        if self._emitted_fill_qty.get(oid, 0.0) <= cumulative + 1e-9:\n"
    "            self._emitted_fill_qty.pop(oid, None)\n"
    "            self._emitted_fill_notional.pop(oid, None)\n\n\n"
    "    def drain_fills",
    "alpaca ack/nack lifecycle",
)
ALPACA.write_text(alpaca, encoding="utf-8")

engine = ENGINE.read_text(encoding="utf-8")
engine = replace_once(
    engine,
    "            trade = self.portfolio.on_fill(\n                fill, peak_price=peak, trough_price=trough, initial_stop=init_stop,\n                risk_manager=self.risk, now=now\n            )\n            ack_fill = getattr(self.broker, \"ack_fill_processed\", None)\n            if ack_fill is not None:\n                ack_fill(fill)\n",
    "            try:\n"
    "                trade = self.portfolio.on_fill(\n"
    "                    fill, peak_price=peak, trough_price=trough, initial_stop=init_stop,\n"
    "                    risk_manager=self.risk, now=now\n"
    "                )\n"
    "            except Exception:\n"
    "                # The broker reservation is volatile only. If durable portfolio\n"
    "                # processing fails, release it so a later tick can safely retry.\n"
    "                nack_fill = getattr(self.broker, \"nack_fill_processed\", None)\n"
    "                if nack_fill is not None:\n"
    "                    nack_fill(fill)\n"
    "                raise\n"
    "            ack_fill = getattr(self.broker, \"ack_fill_processed\", None)\n"
    "            if ack_fill is not None:\n"
    "                ack_fill(fill)\n",
    "engine fill ack/nack boundary",
)
ENGINE.write_text(engine, encoding="utf-8")

print("Applied safe Alpaca in-flight fill lifecycle fix.")
