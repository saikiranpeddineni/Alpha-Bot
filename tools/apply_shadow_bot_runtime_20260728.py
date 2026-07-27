from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend/app/core/engine.py"
TEST = ROOT / "backend/tests/test_shadow_bot_runtime_20260728.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    if old not in text:
        raise RuntimeError(f"anchor not found for {label}")
    return text.replace(old, new, 1)


src = ENGINE.read_text(encoding="utf-8")
src = replace_once(
    src,
    "from app.research.shadow_runtime import ShadowResearchRuntime\n",
    "from app.research.shadow_runtime import ShadowResearchRuntime\nfrom app.research.shadow_bot import ShadowAlphaBotRuntime\n",
    "shadow bot import",
)
src = replace_once(
    src,
    '''        self.shadow_research = ShadowResearchRuntime(\n            db=self.db,\n            bankroll_getter=lambda: self.bankroll,\n            outcome_bar_provider=self._shadow_outcome_bars,\n            target_d_observation_sink=self._record_target_d_observation,\n        )\n''',
    '''        self.shadow_research = ShadowResearchRuntime(\n            db=self.db,\n            bankroll_getter=lambda: self.bankroll,\n            outcome_bar_provider=self._shadow_outcome_bars,\n            target_d_observation_sink=self._record_target_d_observation,\n        )\n        # Single control-plane facade over the EXISTING passive research stack.\n        # It owns no broker, production risk, model training, or promotion authority.\n        self.shadow_bot = ShadowAlphaBotRuntime(self.shadow_research)\n''',
    "shadow bot construction",
)
src = replace_once(src, "            await self.shadow_research.start()\n", "            await self.shadow_bot.start()\n", "shadow bot start")
src = replace_once(src, "            await self.shadow_research.stop()\n", "            await self.shadow_bot.stop()\n", "shadow bot stop")
src = replace_once(src, "                    self.shadow_research.publish(bar_event)\n", "                    self.shadow_bot.publish(bar_event)\n", "bar publish")
src = replace_once(src, "                    self.shadow_research.publish(rejected_event)\n", "                    self.shadow_bot.publish(rejected_event)\n", "reject publish")
src = replace_once(src, "                        self.shadow_research.publish(event)\n", "                        self.shadow_bot.publish(event)\n", "candidate publish")
src = replace_once(
    src,
    '''                "shadow_research": self.shadow_research.telemetry(),\n''',
    '''                "shadow_research": self.shadow_research.telemetry(),\n                "shadow_bot": self.shadow_bot.telemetry(),\n''',
    "shadow bot telemetry",
)
ENGINE.write_text(src, encoding="utf-8")

TEST.write_text('''from __future__ import annotations\n\nimport asyncio\nfrom datetime import UTC, datetime\n\nfrom app.research.shadow_bot import ShadowAlphaBotRuntime\nfrom app.research.shadow_event import TradingObservationEvent\n\n\nclass _ResearchStub:\n    def __init__(self):\n        self.started = False; self.events = []\n    async def start(self): self.started = True\n    async def stop(self): self.started = False\n    def publish(self, event): self.events.append(event); return True\n    def telemetry(self):\n        return {\n            "started": self.started,\n            "publisher": {"queue_depth": 0, "queue_capacity": 4096, "events_published": len(self.events), "events_processed": len(self.events), "events_dropped": 0, "events_duplicate": 0, "consumer_errors": 0, "consumer_errors_by_name": {}},\n            "spawn": {"observations_processed": len(self.events), "candidate_observations_processed": len(self.events), "market_bars_processed": 0},\n            "target_d": {"artifact_status": "READY"}, "outcomes": {}, "exit_challengers": {}, "lifecycle_errors": 0,\n        }\n\n\ndef test_shadow_bot_has_no_production_authority():\n    bot = ShadowAlphaBotRuntime(_ResearchStub())\n    caps = bot.capabilities\n    assert caps.broker_authority is False\n    assert caps.production_mutation is False\n    assert caps.model_training is False\n    assert caps.automatic_promotion is False\n    assert not hasattr(bot, "broker")\n    assert not hasattr(bot, "submit_order")\n\n\ndef test_shadow_bot_is_one_way_facade_over_existing_runtime():\n    research = _ResearchStub(); bot = ShadowAlphaBotRuntime(research)\n    asyncio.run(bot.start())\n    event = TradingObservationEvent(event_id="e1", event_type="candidate", ts=datetime(2026,7,28,13,31,tzinfo=UTC), candidate_id="c1", symbol="NVDA", strategy="momentum", direction="long", price=100.0)\n    assert bot.publish(event) is True\n    assert research.events == [event]\n    telem = bot.telemetry()\n    assert telem["started"] is True and telem["health"]["events_published"] == 1\n    asyncio.run(bot.stop())\n\n\ndef test_shadow_bot_reuses_existing_components_only():\n    bot = ShadowAlphaBotRuntime(_ResearchStub())\n    caps = bot.telemetry()["capabilities"]\n    assert caps["target_d"] and caps["spawn"] and caps["outcome_joiner"] and caps["exit_challengers"]\n    assert caps["model_training"] is False\n''', encoding="utf-8")

print("Applied ShadowAlphaBot integration using existing shadow components only.")
print("No new model, strategy, seed family, broker path, production risk rule, or automatic promotion was added.")
