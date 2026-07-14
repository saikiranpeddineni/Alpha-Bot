// Global app state (zustand) — fed by the WebSocket stream + REST bootstrap.

import { create } from "zustand";
import type {
  Account,
  Bar,
  Bootstrap,
  Insight,
  LogRow,
  Metrics,
  OrderRow,
  PositionRow,
  QuoteRow,
  Status,
  StrategyInfo,
  TradeRow,
  WsMessage,
} from "./types";

const MAX_LOGS = 400;
const MAX_INSIGHTS = 60;
const MAX_TRADES = 300;

interface AppState {
  connected: boolean;
  status: Status | null;
  account: Account | null;
  quotes: QuoteRow[];
  positions: PositionRow[];
  orders: OrderRow[];
  trades: TradeRow[];
  logs: LogRow[];
  insights: Insight[];
  metrics: Metrics | null;
  strategies: StrategyInfo[];
  selectedSymbol: string;
  liveBar: { symbol: string; bar: Bar } | null;
  backtestProgress: number | null;
  toast: { kind: "info" | "error"; text: string } | null;

  setConnected(v: boolean): void;
  applyMessage(msg: WsMessage): void;
  applyBootstrap(b: Bootstrap): void;
  selectSymbol(symbol: string): void;
  setStatus(s: Status): void;
  setStrategies(s: StrategyInfo[]): void;
  setTrades(t: TradeRow[]): void;
  setPositions(p: PositionRow[]): void;
  showToast(kind: "info" | "error", text: string): void;
  clearToast(): void;
}

export const useStore = create<AppState>((set, get) => ({
  connected: false,
  status: null,
  account: null,
  quotes: [],
  positions: [],
  orders: [],
  trades: [],
  logs: [],
  insights: [],
  metrics: null,
  strategies: [],
  selectedSymbol: "SPY",
  liveBar: null,
  backtestProgress: null,
  toast: null,

  setConnected: (v) => set({ connected: v }),

  applyBootstrap: (b) => {
    const sel = get().selectedSymbol;
    const watchlist = b.status?.watchlist ?? [];
    set({
      status: b.status,
      quotes: b.quotes ?? [],
      orders: b.orders ?? [],
      insights: (b.insights ?? []).slice(-MAX_INSIGHTS).reverse(),
      logs: (b.logs ?? []).slice(-MAX_LOGS).reverse(),
      metrics: b.metrics ?? null,
      positions: b.positions ?? [],
      account: b.account ?? null,
      selectedSymbol: watchlist.includes(sel) ? sel : (watchlist[0] ?? sel),
    });
  },

  applyMessage: (msg) => {
    switch (msg.type) {
      case "bootstrap":
        get().applyBootstrap(msg.data);
        break;
      case "status":
        set({ status: msg.data });
        break;
      case "account":
        set({ account: msg.data });
        break;
      case "quotes":
        set({ quotes: msg.data });
        break;
      case "positions":
        set({ positions: msg.data });
        break;
      case "orders":
        set({ orders: msg.data });
        break;
      case "trade":
        set({ trades: [msg.data, ...get().trades].slice(0, MAX_TRADES) });
        break;
      case "log":
        set({ logs: [msg.data, ...get().logs].slice(0, MAX_LOGS) });
        break;
      case "insight":
        set({ insights: [msg.data, ...get().insights].slice(0, MAX_INSIGHTS) });
        break;
      case "bar":
        set({ liveBar: msg.data });
        break;
      case "metrics":
        set({ metrics: msg.data });
        break;
      case "backtest_progress":
        set({ backtestProgress: msg.data.progress >= 1 ? null : msg.data.progress });
        break;
    }
  },

  selectSymbol: (symbol) => set({ selectedSymbol: symbol }),
  setStatus: (s) => set({ status: s }),
  setStrategies: (s) => set({ strategies: s }),
  setTrades: (t) => set({ trades: t }),
  setPositions: (p) => set({ positions: p }),
  showToast: (kind, text) => {
    set({ toast: { kind, text } });
    window.setTimeout(() => {
      if (get().toast?.text === text) set({ toast: null });
    }, 4000);
  },
  clearToast: () => set({ toast: null }),
}));
