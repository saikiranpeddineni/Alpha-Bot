import { useState } from "react";
import { api } from "../api";
import { fmtDateTime, fmtMoney, fmtPct, fmtQty, fmtSigned, fmtTime, pnlClass } from "../format";
import { useStore } from "../store";

type Tab = "positions" | "orders" | "trades" | "logs";

export function BottomTabs() {
  const [tab, setTab] = useState<Tab>("positions");
  const positions = useStore((s) => s.positions);
  const trades = useStore((s) => s.trades);
  const logs = useStore((s) => s.logs);

  return (
    <div className="panel">
      <div className="tabs">
        <button className={tab === "positions" ? "active" : ""} onClick={() => setTab("positions")}>
          Positions {positions.length > 0 && `(${positions.length})`}
        </button>
        <button className={tab === "orders" ? "active" : ""} onClick={() => setTab("orders")}>
          Orders
        </button>
        <button className={tab === "trades" ? "active" : ""} onClick={() => setTab("trades")}>
          Trades {trades.length > 0 && `(${trades.length})`}
        </button>
        <button className={tab === "logs" ? "active" : ""} onClick={() => setTab("logs")}>
          Logs
        </button>
        <span className="spacer" />
        {tab === "positions" && positions.length > 0 && <FlattenButton />}
      </div>
      <div className="panel-body">
        {tab === "positions" && <PositionsTable />}
        {tab === "orders" && <OrdersTable />}
        {tab === "trades" && <TradesTable />}
        {tab === "logs" && (
          <div>
            {logs.length === 0 && <div className="empty-state">No log entries yet</div>}
            {logs.map((l, i) => (
              <div className="log-line" key={`${l.ts}-${i}`}>
                <span className="faint num">{fmtTime(l.ts)}</span>
                <span className="src">{l.source}</span>
                <span className={`msg lvl-${l.level}`}>{l.message}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function FlattenButton() {
  const showToast = useStore((s) => s.showToast);
  return (
    <div className="tab-actions">
      <button
        className="btn sm danger"
        onClick={() =>
          api.flatten().then(
            () => showToast("info", "All positions closed"),
            (e) => showToast("error", (e as Error).message),
          )
        }
      >
        Flatten all
      </button>
    </div>
  );
}

function PositionsTable() {
  const positions = useStore((s) => s.positions);
  const selectSymbol = useStore((s) => s.selectSymbol);

  if (positions.length === 0) {
    return <div className="empty-state">No open positions</div>;
  }
  return (
    <table className="grid">
      <thead>
        <tr>
          <th>Symbol</th><th>Side</th><th className="r">Qty</th><th className="r">Entry</th>
          <th className="r">Last</th><th className="r">Stop</th><th className="r">Target</th>
          <th className="r">Value</th><th className="r">Unrlzd P&L</th><th>Strategy</th><th></th>
        </tr>
      </thead>
      <tbody>
        {positions.map((p) => (
          <tr key={p.symbol} onClick={() => selectSymbol(p.symbol)} style={{ cursor: "pointer" }}>
            <td><b>{p.symbol}</b></td>
            <td><span className={`side-tag ${p.side}`}>{p.side}</span></td>
            <td className="r num">{fmtQty(Math.abs(p.qty))}</td>
            <td className="r num">{p.avg_entry_price.toFixed(2)}</td>
            <td className="r num">{p.market_price.toFixed(2)}</td>
            <td className="r num faint">{p.stop_loss?.toFixed(2) ?? "—"}</td>
            <td className="r num faint">{p.take_profit?.toFixed(2) ?? "—"}</td>
            <td className="r num">{fmtMoney(p.market_value)}</td>
            <td className={`r num ${pnlClass(p.unrealized_pnl)}`}>
              {fmtSigned(p.unrealized_pnl)} ({fmtPct(p.unrealized_pnl_pct)})
            </td>
            <td className="muted">{p.strategy}</td>
            <td className="r">
              <button
                className="btn sm"
                onClick={(e) => {
                  e.stopPropagation();
                  api.closePosition(p.symbol).catch((err) =>
                    useStore.getState().showToast("error", (err as Error).message),
                  );
                }}
              >
                Close
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function OrdersTable() {
  const orders = useStore((s) => s.orders);
  const status = useStore((s) => s.status);
  const showToast = useStore((s) => s.showToast);
  const [symbol, setSymbol] = useState("");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [qty, setQty] = useState("10");
  const running = status?.state === "running";

  async function place() {
    const sym = (symbol || useStore.getState().selectedSymbol).trim().toUpperCase();
    const q = Number(qty);
    if (!sym || !Number.isFinite(q) || q <= 0) {
      showToast("error", "Enter a symbol and a positive quantity");
      return;
    }
    try {
      const order = await api.submitOrder({ symbol: sym, side, qty: q });
      showToast(
        order.status === "rejected" ? "error" : "info",
        `Order ${order.status}: ${side.toUpperCase()} ${q} ${sym}`,
      );
    } catch (e) {
      showToast("error", (e as Error).message);
    }
  }

  return (
    <div>
      <div className="order-form">
        <div className="field">
          <label>Symbol</label>
          <input
            type="text"
            placeholder={useStore.getState().selectedSymbol}
            value={symbol}
            onChange={(e) => setSymbol(e.target.value.toUpperCase())}
          />
        </div>
        <div className="field">
          <label>Side</label>
          <select value={side} onChange={(e) => setSide(e.target.value as "buy" | "sell")}>
            <option value="buy">Buy</option>
            <option value="sell">Sell</option>
          </select>
        </div>
        <div className="field">
          <label>Qty</label>
          <input type="number" className="num" min={1} value={qty} onChange={(e) => setQty(e.target.value)} />
        </div>
        <button className={`btn ${side === "buy" ? "success" : "danger"}`} onClick={place} disabled={!running}>
          {running ? `${side === "buy" ? "Buy" : "Sell"} market` : "Engine stopped"}
        </button>
      </div>
      {orders.length === 0 ? (
        <div className="empty-state">No orders this session</div>
      ) : (
        <table className="grid">
          <thead>
            <tr>
              <th>Time</th><th>Symbol</th><th>Side</th><th className="r">Qty</th><th>Type</th>
              <th className="r">Fill</th><th>Status</th><th>Strategy</th><th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {orders.map((o) => (
              <tr key={o.id}>
                <td className="num faint">{fmtTime(o.submitted_at)}</td>
                <td><b>{o.symbol}</b></td>
                <td><span className={`side-tag ${o.side}`}>{o.side}</span></td>
                <td className="r num">{fmtQty(o.qty)}</td>
                <td className="muted">{o.order_type}</td>
                <td className="r num">{o.filled_avg_price?.toFixed(2) ?? "—"}</td>
                <td><span className={`status-pill ${o.status}`}>{o.status}</span></td>
                <td className="muted">{o.strategy}</td>
                <td className="muted" style={{ maxWidth: 340, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {o.reason}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function TradesTable() {
  const trades = useStore((s) => s.trades);
  if (trades.length === 0) {
    return <div className="empty-state">No closed trades yet</div>;
  }
  return (
    <table className="grid">
      <thead>
        <tr>
          <th>Closed</th><th>Symbol</th><th>Side</th><th className="r">Qty</th>
          <th className="r">Entry</th><th className="r">Exit</th><th className="r">P&L</th>
          <th className="r">P&L %</th><th className="r">R</th><th>Strategy</th><th>Exit reason</th>
        </tr>
      </thead>
      <tbody>
        {trades.map((t) => (
          <tr key={t.id}>
            <td className="num faint">{fmtDateTime(t.exit_at)}</td>
            <td><b>{t.symbol}</b></td>
            <td><span className={`side-tag ${t.side}`}>{t.side}</span></td>
            <td className="r num">{fmtQty(t.qty)}</td>
            <td className="r num">{t.entry_price.toFixed(2)}</td>
            <td className="r num">{t.exit_price.toFixed(2)}</td>
            <td className={`r num ${pnlClass(t.pnl)}`}>{fmtSigned(t.pnl)}</td>
            <td className={`r num ${pnlClass(t.pnl_pct)}`}>{fmtPct(t.pnl_pct)}</td>
            <td className={`r num ${pnlClass(t.r_multiple)}`}>{t.r_multiple?.toFixed(1) ?? "—"}</td>
            <td className="muted">{t.strategy}</td>
            <td className="muted">{t.exit_reason}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
