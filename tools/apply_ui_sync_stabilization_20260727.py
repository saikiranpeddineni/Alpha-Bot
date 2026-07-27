"""Surgical UI state-sync stabilization for the 2026-07-27 production findings.

Run from repository root. This changes presentation synchronization only; it does
not alter scanner, VIX, ML, strategy, sizing, or execution decision logic.
"""
from pathlib import Path

APP = Path("frontend/src/App.tsx")
STORE = Path("frontend/src/store.ts")
BOTTOM = Path("frontend/src/components/BottomTabs.tsx")


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


# REST bootstrap previously fetched positions/trades but never orders. A UI opened
# between WS bootstrap frames could therefore show a stale/empty Orders tab while
# Positions/Trades were current. Refresh all three execution views as one snapshot.
replace_once(
    APP,
    '    api.trades().then((t) => useStore.getState().setTrades(t)).catch(() => undefined);\n'
    '    api.positions().then((p) => useStore.getState().setPositions(p)).catch(() => undefined);\n',
    '    const refreshExecutionViews = () => {\n'
    '      void Promise.all([api.positions(), api.orders(), api.trades()])\n'
    '        .then(([positions, orders, trades]) => useStore.getState().setExecutionSnapshot(positions, orders, trades))\n'
    '        .catch(() => undefined);\n'
    '    };\n'
    '    refreshExecutionViews();\n'
    '    const executionRefresh = window.setInterval(refreshExecutionViews, 15000);\n',
    "App execution snapshot bootstrap",
)
replace_once(
    APP,
    '    return () => window.removeEventListener("resize", handleResize);\n',
    '    return () => { window.removeEventListener("resize", handleResize); window.clearInterval(executionRefresh); };\n',
    "App execution refresh cleanup",
)

# Keep the three execution tables capable of being atomically replaced from one
# REST refresh. This avoids rendering a new position snapshot beside stale orders.
replace_once(
    STORE,
    '  setTrades(t: TradeRow[]): void;\n  setPositions(p: PositionRow[]): void;\n',
    '  setTrades(t: TradeRow[]): void;\n'
    '  setPositions(p: PositionRow[]): void;\n'
    '  setOrders(o: OrderRow[]): void;\n'
    '  setExecutionSnapshot(p: PositionRow[], o: OrderRow[], t: TradeRow[]): void;\n',
    "store execution setters interface",
)
replace_once(
    STORE,
    '  setTrades: (t) => set({ trades: t }),\n  setPositions: (p) => set({ positions: p }),\n',
    '  setTrades: (t) => set({ trades: t }),\n'
    '  setPositions: (p) => set({ positions: p }),\n'
    '  setOrders: (o) => set({ orders: o }),\n'
    '  setExecutionSnapshot: (positions, orders, trades) => set({ positions, orders, trades }),\n',
    "store execution setters implementation",
)

# Orders had no count while Positions/Trades did, which made the three activity
# views look inconsistent in today's screenshots even when data existed.
replace_once(
    BOTTOM,
    '<button className={tab==="orders"?"active":""} onClick={()=>setTab("orders")}>Orders</button>',
    '<button className={tab==="orders"?"active":""} onClick={()=>setTab("orders")}>Orders {useStore(s=>s.orders).length>0&&`(${useStore(s=>s.orders).length})`}</button>',
    "Orders tab count",
)

print("Applied UI execution-state synchronization stabilization.")
