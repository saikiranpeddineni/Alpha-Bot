"""Structural safety guard for issue #276's one-way research boundary."""
from pathlib import Path


RESEARCH_ROOT = Path(__file__).resolve().parents[1] / "app" / "research"
FORBIDDEN_IMPORTS = (
    "from app.brokers",
    "import app.brokers",
)
FORBIDDEN_ORDER_CALLS = (
    ".submit_order(",
    ".submit(",
    ".cancel_order(",
    ".close_position(",
    ".close_all_positions(",
)


def test_research_tree_has_no_broker_import_or_order_authority():
    violations: list[str] = []
    for path in RESEARCH_ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_IMPORTS + FORBIDDEN_ORDER_CALLS:
            if token in source:
                violations.append(f"{path.relative_to(RESEARCH_ROOT)}: {token}")
    assert violations == [], "Research code crossed broker boundary: " + "; ".join(violations)
