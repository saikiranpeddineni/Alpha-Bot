// REST client — same-origin in production, proxied in dev.

import type {
  BacktestResult,
  BacktestSummary,
  Bar,
  Insight,
  LogRow,
  Metrics,
  ModelInfo,
  OrderRow,
  PositionRow,
  RiskConfig,
  Status,
  StrategyInfo,
  TradeRow,
  TrainerStatus,
} from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      /* keep statusText */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  status: () => request<Status>("/api/status"),
  startEngine: (mode: string, confirm = "") =>
    request<Status>("/api/engine/start", { method: "POST", body: JSON.stringify({ mode, confirm }) }),
  stopEngine: (flatten: boolean | null = null) =>
    request<Status>("/api/engine/stop", { method: "POST", body: JSON.stringify({ flatten }) }),
  resetHalt: () => request<Status>("/api/engine/reset-halt", { method: "POST" }),

  watchlist: () => request<{ symbols: string[] }>("/api/watchlist"),
  setWatchlist: (symbols: string[]) =>
    request<{ symbols: string[] }>("/api/watchlist", { method: "PUT", body: JSON.stringify({ symbols }) }),

  bars: (symbol: string, timeframe = "1Min", limit = 500) =>
    request<{ symbol: string; timeframe: string; bars: Bar[] }>(
      `/api/bars?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}&limit=${limit}`,
    ),

  positions: () => request<PositionRow[]>("/api/positions"),
  closePosition: (symbol: string) =>
    request<OrderRow>(`/api/positions/${encodeURIComponent(symbol)}/close`, { method: "POST" }),
  flatten: () => request<{ ok: boolean }>("/api/flatten", { method: "POST" }),

  orders: () => request<OrderRow[]>("/api/orders"),
  submitOrder: (payload: {
    symbol: string;
    side: "buy" | "sell";
    qty: number;
    order_type?: "market" | "limit";
    limit_price?: number | null;
  }) => request<OrderRow>("/api/orders", { method: "POST", body: JSON.stringify(payload) }),

  trades: (limit = 200) => request<TradeRow[]>(`/api/trades?limit=${limit}`),
  logs: (limit = 200) => request<LogRow[]>(`/api/logs?limit=${limit}`),
  insights: () => request<Insight[]>("/api/insights"),
  metrics: () => request<Metrics>("/api/metrics"),

  updateRisk: (partial: Partial<RiskConfig>) =>
    request<RiskConfig>("/api/settings/risk", { method: "PUT", body: JSON.stringify(partial) }),

  strategies: () => request<StrategyInfo[]>("/api/strategies"),
  updateStrategy: (name: string, body: { enabled?: boolean; params?: Record<string, unknown> }) =>
    request<StrategyInfo>(`/api/strategies/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  modelInfo: () => request<ModelInfo>("/api/ai/model"),
  trainModel: (symbols: string[] | null, days: number) =>
    request<ModelInfo>("/api/ai/train", { method: "POST", body: JSON.stringify({ symbols, days }) }),
  trainerStatus: () => request<TrainerStatus>("/api/ai/trainer/status"),
  runTrainer: () => request<{ status: string; symbols: string[] }>("/api/ai/trainer/run", { method: "POST" }),

  runBacktest: (body: {
    symbols: string[];
    days: number;
    starting_cash?: number;
    use_ml_gate?: boolean;
    use_dynamic_scanner?: boolean;
    ignore_temporal_leakage?: boolean;
    start_date?: string | null;
    end_date?: string | null;
  }) => request<BacktestResult>("/api/backtest", { method: "POST", body: JSON.stringify(body) }),
  backtests: () => request<BacktestSummary[]>("/api/backtests"),
  backtest: (id: string) => request<BacktestResult>(`/api/backtests/${id}`),
  runScanner: () => request<{ status: string; watchlist: string[] }>("/api/scanner/run", { method: "POST" }),
  runProfitValidation: (body?: { symbols?: string[]; start_date?: string | null; end_date?: string | null }) =>
    request<{ status: string; report: string }>("/api/validation/profit-optimization", {
      method: "POST",
      body: body ? JSON.stringify(body) : undefined,
    }),
};
