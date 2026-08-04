import inspect
import sys
import re

sys.path.append("backend")
from app.core.engine import TradingEngine

methods_to_extract = [
    '_drain_fills',
    'verify_broker_protective_stops',
    '_is_valid_protective_stop',
    '_resolve_recovery_stop',
    '_reconcile_pending_submissions'
]

source_code = ""
with open("backend/app/core/engine.py", "r", encoding="utf-8") as f:
    source_code = f.read()

# 1. Create engine_execution.py
mixin_code = """\"\"\"Execution and reconciliation mixin for TradingEngine.\"\"\"
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional, cast, Dict, Any, Tuple

from app.core.applog import applog
from app.core.domain import EngineState, OrderStatus, OrderType, OrderRequest, ProtectionState, PositionState
from app.core.time_utils import utcnow

if TYPE_CHECKING:
    from app.core.engine import TradingEngine

class TradingEngineExecutionMixin:
"""

for method_name in methods_to_extract:
    method = getattr(TradingEngine, method_name)
    src = inspect.getsource(method)
    mixin_code += "\n" + src

with open("backend/app/core/engine_execution.py", "w", encoding="utf-8") as f:
    f.write(mixin_code)

# 2. Remove methods from engine.py
for method_name in methods_to_extract:
    method = getattr(TradingEngine, method_name)
    src = inspect.getsource(method)
    source_code = source_code.replace(src, "")

# 3. Add Mixin to engine.py imports and class definition
source_code = source_code.replace(
    "from app.core.engine_evaluation import TradingEngineEvaluationMixin",
    "from app.core.engine_evaluation import TradingEngineEvaluationMixin\nfrom app.core.engine_execution import TradingEngineExecutionMixin"
)
source_code = source_code.replace(
    "class TradingEngine(TradingEngineEvaluationMixin, TradingEnginePortfolioMixin):",
    "class TradingEngine(TradingEngineExecutionMixin, TradingEngineEvaluationMixin, TradingEnginePortfolioMixin):"
)

with open("backend/app/core/engine.py", "w", encoding="utf-8") as f:
    f.write(source_code)

print("Refactoring complete.")
