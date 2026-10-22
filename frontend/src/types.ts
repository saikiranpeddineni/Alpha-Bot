// API/WebSocket wire types (mirrors backend pydantic models)

export type EngineState = "stopped" | "running" | "halted";
export type Mode = "demo" | "paper" | "live";

export interface MarketStatus {
  is_open: boolean;
  session_date: string;
  minutes_to_close: number | null;
  next_open: string | null;
  always_open: boolean;
}

export interface TrainerStatus {
  training_in_progress: boolean;
  last_trained: string | null;
  last_result: {
    timestamp?: string;
    elapsed_sec?: number;
    symbols_trained?: number;
    global_auc?: number;
    specialists?: Record<string, SpecialistTrainingResult | number>;
  };
}

export interface SpecialistTrainingResult {
  candidate_auc?: number;
  incumbent_auc?: number;
  auc?: number;
  decision?: string;
}

export interface RiskStatus {
  hard_halted?: boolean;
  hard_halt_reason?: string;
  daily_halted?: boolean;
  profit_locked?: boolean;
  consecutive_losses?: number;
  cooldown_until?: string | null;
  config: RiskConfig;
}

export interface RiskConfig {
  risk_per_trade_pct: number;
  max_position_pct: number;
  max_open_positions: number;
  max_daily_loss_pct: number;
  daily_profit_lock_pct?: number;
  daily_profit_defend_pct?: number;
  max_drawdown_pct: number;
  symbol_cooldown_minutes: number;
  strategy_consecutive_loss_limit: number;
  strategy_cooldown_minutes: number;
  no_entry_minutes_before_close: number;
  flatten_minutes_before_close: number;
  min_price: number;
  min_avg_dollar_volume: number;
  data_gap_threshold_secs: number;
  ml_confidence_threshold: number;
  min_specialist_auc: number;
  min_stop_pct: number;
}

export interface ModelInfo {
  trained: boolean;
  engine: string;
  trained_at?: string;
  samples?: number;
  validation_auc?: number | null;
  validation_accuracy?: number;
  symbols?: string[];
}

export interface ScannerContextEntry {
  score: number;
  auc: number;
  scanned_at: string;
  expires_at: string;
}

export interface ScannerStatus {
  status: "never_run" | "running" | "success" | "failed" | string;
  outcome?: "SUCCESS_NONEMPTY" | "SUCCESS_EMPTY" | "FAILED" | string | null;
  last_started_at?: string | null;
  last_completed_at?: string | null;
  last_error?: string | null;
  last_watchlist_count?: number;
  is_intraday?: boolean | null;
  as_of?: string | null;
  discovery?: Record<string, unknown> | null;
}

export interface Status {
  state: EngineState;
  mode: Mode;
  provider: string | null;
  broker: string | null;
  market: MarketStatus;
  watchlist: string[];
  core_watchlist?: string[];
  scanner_context?: Record<string, ScannerContextEntry>;
  scanner?: ScannerStatus;
  risk: RiskStatus;
  model: ModelInfo;
  trainer?: TrainerStatus;
  live_trading_enabled: boolean;
  has_alpaca_keys: boolean;
  tick: number;
  last_error: string;
  reconciliation_failed?: boolean;
  system_health?: "NORMAL" | "DEGRADED" | "SAFE_MODE" | "HALTED" | string;
  allow_new_entries?: boolean;
  heartbeat_diagnostics?: Record<string, unknown>;
  data_gap_symbols?: string[];
}

export interface Account {
  equity: number;
  cash: number;
  buying_power: number;
  day_pnl: number;
  day_pnl_pct: number;
  drawdown_pct: number;
}

export interface QuoteRow {
  symbol: string;
  price: number;
  change_pct: number;
  volume: number;
  regime?: string | null;
}

export interface PositionRow {
  symbol: string;
  qty: number;
  side: string;
  avg_entry_price: number;
  market_price: number;
  market_value: number;
  unrealized_pnl: number;
  unrealized_pnl_pct: number;
  stop_loss: number | null;
  take_profit: number | null;
  strategy: string;
  opened_at: string;
}

export interface OrderRow {
  id: string;
  symbol: string;
  side: "buy" | "sell";
  qty: number;
  order_type: "market" | "limit";
  limit_price: number | null;
  status: string;
  filled_qty: number;
  filled_avg_price: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  strategy: string;
  reason: string;
  submitted_at: string;
  filled_at: string | null;
}

export interface TradeRow {
  id: string;
  symbol: string;
  side: string;
  qty: number;
  entry_price: number;
  exit_price: number;
  entry_at: string;
  exit_at: string;
  pnl: number;
  pnl_pct: number;
  r_multiple: number | null;
  strategy: string;
  exit_reason: string;
}

export interface LogRow {
  ts: string;
  level: string;
  source: string;
  message: string;
}

export interface Insight {
  id: string;
  ts: string;
  kind: string;
  title: string;
  detail: string;
  symbol?: string | null;
  severity: "info" | "success" | "warning" | "danger";
}

export interface Bar {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

export interface SessionStats {
  total_trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  gross_profit: number;
  gross_loss: number;
  profit_factor: number | null;
  realized_pnl: number;
  avg_win: number;
  avg_loss: number;
  day_trades_today: number;
}

export interface Metrics {
  stats: SessionStats | Record<string, never>;
  metrics: Record<string, number | null>;
  equity_curve: [string, number][];
}

export interface StrategyInfo {
  name: string;
  description: string;
  enabled: boolean;
  params: Record<string, number | string | boolean>;
}

export interface BacktestSummary {
  id: string;
  created_at: string;
  symbols: string[];
  days: number;
  total_return_pct: number | null;
  max_drawdown_pct: number | null;
  total_trades: number | null;
  win_rate: number | null;
}

export interface BacktestResult {
  id: string;
  created_at: string;
  params: Record<string, unknown>;
  metrics: Record<string, number | null>;
  equity_curve: [string, number][];
  trades: TradeRow[];
  bars_processed: number;
  warnings: string[];
}

export type HealthState = "NORMAL" | "WARNING" | "DEGRADED" | "SAFE_MODE" | "HALTED";
export type ActivityState = "IDLE" | "SCANNING" | "EVALUATING" | "TRAINING" | "REPLAYING" | "TRADING";
export type PulseIntensity = "small" | "medium" | "large" | "flash";
export type HeartbeatStyle = "minimal" | "ekg" | "heart" | "off";

export interface HeartbeatEvent {
  type: string;
  title: string;
  detail: string;
  symbol?: string | null;
  strategy?: string | null;
  pulse: PulseIntensity;
}

export interface SubsystemTimestamps {
  last_scan?: string | null;
  last_bar?: string | null;
  last_order?: string | null;
  last_strategy?: string | null;
  last_replay?: string | null;
  last_training?: string | null;
  last_broker_ping?: string | null;
}

export interface HeartbeatStats {
  broker_connected: boolean;
  market_data_fresh: boolean;
  scanner_ok: boolean;
  market_regime: string;
  watchlist_count: number;
  universe_count: number;
  signals_evaluated_today: number;
  evaluations_last_sec: number;
  orders_today: number;
  replay_queue_depth: number;
  latency_ms: number;
  degraded_reasons?: string[];
}

export interface HeartbeatTelemetry {
  seq: number;
  ts: string;
  health: HealthState;
  activity: ActivityState;
  event: HeartbeatEvent;
  timestamps: SubsystemTimestamps;
  stats: HeartbeatStats;
}

export type WsMessage =
  | { type: "bootstrap"; data: Bootstrap }
  | { type: "status"; data: Status }
  | { type: "account"; data: Account }
  | { type: "quotes"; data: QuoteRow[] }
  | { type: "positions"; data: PositionRow[] }
  | { type: "orders"; data: OrderRow[] }
  | { type: "trade"; data: TradeRow }
  | { type: "log"; data: LogRow }
  | { type: "insight"; data: Insight }
  | { type: "bar"; data: { symbol: string; bar: Bar } }
  | { type: "metrics"; data: Metrics }
  | { type: "backtest_progress"; data: { progress: number } }
  | { type: "heartbeat"; data: HeartbeatTelemetry };

export interface Bootstrap {
  status: Status;
  quotes: QuoteRow[];
  orders: OrderRow[];
  insights: Insight[];
  logs: LogRow[];
  metrics: Metrics;
  positions?: PositionRow[];
  account?: Account;
}



export interface ShadowEntry {
  id: number;
  created_at: string;
  signal_timestamp_utc: string | null;
  decision_bar_timestamp_utc: string | null;
  session_date: string;
  symbol: string;
  strategy: string | null;
  action: string | null;
  signal_price: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  atr14: number | null;
  confidence: number | null;
  ml_probability: number | null;
  meta_prob: number | null;
  trade_score: number | null;
  candidate_id: string | null;
  gate: string;
  reason: string | null;
  resolved: number;
  px_5m: number | null;
  px_15m: number | null;
  px_30m: number | null;
  px_60m: number | null;
  fav_pct_60m: number | null;
  mfe_pct_60m: number | null;
  mae_pct_60m: number | null;
  r_fav_60m: number | null;
  verdict: string | null;
}

export interface ShadowGateSummary {
  gate: string;
  total: number;
  missed: number;
  avoided: number;
  avg_forward_move: number | null;
}

export interface ShadowLedgerData {
  entries: ShadowEntry[];
  summary: Record<string, number>;
  gate_summary: ShadowGateSummary[];
  missed_winners: ShadowEntry[];
}
