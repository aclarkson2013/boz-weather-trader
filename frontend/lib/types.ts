/**
 * TypeScript interfaces matching the backend Pydantic response schemas.
 *
 * IMPORTANT: Field names match the actual JSON returned by the API,
 * NOT the original CLAUDE.md spec names.
 */

// ─── Literal Types ───

export type CityCode = "NYC" | "CHI" | "MIA" | "AUS";

/** Forecast feeds the weather pipeline can fetch. Which ones are blended into
 *  the ensemble is user-configurable — see Settings.enabled_weather_sources. */
export type WeatherSourceName =
  | "NWS"
  | "NWS:gridpoint"
  | "Open-Meteo:ECMWF"
  | "Open-Meteo:GFS"
  | "Open-Meteo:ICON";
export type TradeSide = "yes" | "no";
export type ConfidenceLevel = "high" | "medium" | "low";
export type TradeStatus = "OPEN" | "RESTING" | "WON" | "LOST" | "CANCELED";
export type PendingTradeStatus =
  | "PENDING"
  | "APPROVED"
  | "REJECTED"
  | "EXPIRED"
  | "EXECUTED";
export type TradingMode = "auto" | "manual";

// ─── Auth ───

export interface AuthValidateRequest {
  key_id: string;
  private_key: string;
  demo_mode?: boolean;
}

export interface AuthValidateResponse {
  valid: boolean;
  balance_cents: number;
  demo_mode: boolean;
}

export interface AuthStatusResponse {
  authenticated: boolean;
  user_id: string;
  demo_mode: boolean;
  key_id_prefix: string;
}

// ─── Dashboard ───

export interface DashboardData {
  balance_cents: number;
  today_pnl_cents: number;
  active_positions: TradeRecord[];
  recent_trades: TradeRecord[];
  next_market_launch: string | null;
  predictions: BracketPrediction[];
}

// ─── Trades ───

export interface TradeRecord {
  id: string;
  kalshi_order_id: string | null;
  city: CityCode;
  date: string; // ISO date string YYYY-MM-DD
  market_ticker: string | null;
  bracket_label: string;
  side: TradeSide;
  price_cents: number;
  quantity: number;
  model_probability: number;
  market_probability: number;
  ev_at_entry: number;
  confidence: ConfidenceLevel;
  status: TradeStatus;
  settlement_temp_f: number | null;
  settlement_source: string | null;
  pnl_cents: number | null;
  fees_cents: number | null;
  postmortem_narrative: string | null;
  created_at: string; // ISO datetime string
  settled_at: string | null;
}

export interface TradesPage {
  trades: TradeRecord[];
  total: number;
  page: number;
}

// ─── Pending Trades (Queue) ───

export interface PendingTrade {
  id: string;
  city: CityCode;
  bracket: string;
  market_ticker: string | null;
  side: TradeSide;
  price_cents: number;
  quantity: number;
  model_probability: number;
  market_probability: number;
  ev: number;
  confidence: ConfidenceLevel;
  reasoning: string;
  status: PendingTradeStatus;
  created_at: string;
  expires_at: string;
  acted_at: string | null;
}

// ─── Predictions / Markets ───

export interface BracketProbability {
  bracket_label: string;
  lower_bound_f: number | null;
  upper_bound_f: number | null;
  probability: number;
}

export interface BracketPrediction {
  city: CityCode;
  date: string;
  brackets: BracketProbability[];
  ensemble_mean_f: number;
  ensemble_std_f: number;
  confidence: ConfidenceLevel;
  model_sources: string[];
  generated_at: string;
}

// ─── Settings ───

export interface UserSettings {
  trading_mode: TradingMode;
  max_trade_size_cents: number;
  daily_loss_limit_cents: number;
  max_daily_exposure_cents: number;
  min_ev_threshold: number;
  min_ev_threshold_yes: number;
  min_ev_threshold_no: number;
  cooldown_per_loss_minutes: number;
  consecutive_loss_limit: number;
  active_cities: CityCode[];
  enabled_weather_sources: WeatherSourceName[];
  notifications_enabled: boolean;
  max_contracts_per_bracket: number;
  enable_consecutive_loss_limit: boolean;
  enable_per_loss_cooldown: boolean;
  // Trading engine guardrails
  model_weight: number;
  max_model_market_divergence: number;
  min_market_prob_for_yes: number;
  // Fee estimation
  fee_estimate_mode: string;
  // Display preferences
  timezone: string; // IANA timezone or "" for browser default
}

export interface SettingsUpdate {
  trading_mode?: TradingMode;
  max_trade_size_cents?: number;
  daily_loss_limit_cents?: number;
  max_daily_exposure_cents?: number;
  min_ev_threshold?: number;
  min_ev_threshold_yes?: number;
  min_ev_threshold_no?: number;
  cooldown_per_loss_minutes?: number;
  consecutive_loss_limit?: number;
  active_cities?: CityCode[];
  enabled_weather_sources?: WeatherSourceName[];
  notifications_enabled?: boolean;
  demo_mode?: boolean;
  max_contracts_per_bracket?: number;
  enable_consecutive_loss_limit?: boolean;
  enable_per_loss_cooldown?: boolean;
  // Trading engine guardrails
  model_weight?: number;
  max_model_market_divergence?: number;
  min_market_prob_for_yes?: number;
  // Fee estimation
  fee_estimate_mode?: string;
  // Display preferences
  timezone?: string;
}

// ─── Logs ───

export interface LogEntry {
  id: number;
  timestamp: string;
  level: string;
  module: string;
  message: string;
  data: Record<string, unknown> | null;
}

// ─── Performance ───

export interface CumulativePnlPoint {
  date: string;
  cumulative_pnl: number;
}

export interface AccuracyPoint {
  date: string;
  accuracy: number;
}

export interface PerformanceData {
  total_trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  total_pnl_cents: number;
  best_trade_pnl_cents: number;
  worst_trade_pnl_cents: number;
  cumulative_pnl: CumulativePnlPoint[];
  pnl_by_city: Record<string, number>;
  cost_by_city: Record<string, number>;
  accuracy_over_time: AccuracyPoint[];
}

// ─── Accuracy / Calibration ───

export interface CalibrationBucket {
  bin_start: number;
  bin_end: number;
  predicted_avg: number;
  actual_rate: number;
  sample_count: number;
}

export interface CalibrationReport {
  city: string;
  lookback_days: number;
  sample_count: number;
  brier_score: number | null;
  calibration_buckets: CalibrationBucket[];
  status: string;
}

export interface SourceAccuracy {
  source: string;
  sample_count: number;
  mae_f: number;
  rmse_f: number;
  bias_f: number;
}

// ─── Calendar ───

export interface CalendarDay {
  date: string; // YYYY-MM-DD
  trade_count: number;
  wins: number;
  losses: number;
  pnl_cents: number;
  win_rate: number; // 0.0 to 1.0
}

export interface CalendarWeek {
  week_number: number;
  pnl_cents: number;
  trade_count: number;
  trading_days: number;
}

export interface CalendarMonth {
  year: number;
  month: number;
  days: CalendarDay[];
  weeks: CalendarWeek[];
  total_pnl_cents: number;
  total_trades: number;
  total_wins: number;
  total_losses: number;
  trading_days: number;
}

// ─── Current Weather (Ticker) ───

export interface CityCurrentWeather {
  city: CityCode;
  city_name: string;
  current_temp_f: number;
  today_high_f: number;
  today_low_f: number;
}

export interface CurrentWeatherResponse {
  cities: CityCurrentWeather[];
  fetched_at: string; // ISO datetime with Z suffix
}

// ─── Version Info ───

export interface VersionInfo {
  current_version: string;
  latest_version: string | null;
  update_available: boolean;
  release_url: string | null;
}

export interface UpdateTriggerResponse {
  status: string;
  message: string;
}

export interface UpdateStatus {
  status: "idle" | "pulling" | "building" | "restarting" | "done" | "error";
  step: string | null;
  error: string | null;
  started_at: string | null;
}

// ─── Dashboard Stats (time-period P&L and W/L) ───

export interface PeriodStats {
  pnl_cents: number;
  wins: number;
  losses: number;
}

export interface DashboardStats {
  yesterday: PeriodStats;
  week: PeriodStats;
  month: PeriodStats;
  year: PeriodStats;
  all_time: PeriodStats;
}

export type StatsPeriod = "yesterday" | "week" | "month" | "year" | "all_time";

// ─── Cooldown Status ───

export interface CooldownStatus {
  is_active: boolean;
  cooldown_type: "per_loss" | "consecutive_loss" | null;
  cooldown_until: string | null; // ISO datetime
  remaining_minutes: number | null;
  consecutive_losses: number;
}

// ─── Grouped Trades (frontend-only aggregation) ───

/** Trades sharing the same market_ticker+bracket+side+status, aggregated into one card. */
export interface GroupedTrade {
  groupKey: string;

  // Representative fields (same across all trades in group)
  city: CityCode;
  date: string;
  market_ticker: string | null;
  bracket_label: string;
  side: TradeSide;
  status: TradeStatus;
  confidence: ConfidenceLevel;

  // Aggregated fields
  totalQuantity: number;
  totalCostCents: number;
  vwapCents: number; // volume-weighted average price
  totalPnlCents: number | null;
  avgModelProbability: number;
  avgMarketProbability: number;
  avgEvAtEntry: number;

  // Metadata
  tradeIds: string[];
  trades: TradeRecord[];
  earliestCreatedAt: string;
  latestCreatedAt: string;
  settlement_temp_f: number | null;
  settlement_source: string | null;
  postmortem_narrative: string | null;
}

/** Market section header grouping for the trades page. */
export interface MarketGroup {
  label: string;
  marketKey: string;
  city: CityCode;
  date: string;
  groups: GroupedTrade[];
}

// ─── Portfolio Sync ───

export interface SyncResult {
  synced_count: number;
  skipped_count: number;
  failed_count: number;
  errors: string[];
  synced_at: string;
}

// ─── Training Reports ───

export interface ModelMetrics {
  model_name: string;
  rmse: number | null;
  mae: number | null;
  accepted: boolean;
  error: string | null;
}

export interface TrainingReport {
  id: number;
  triggered_by: string;
  trigger_reason: string | null;
  status: string;
  training_samples: number;
  test_samples: number;
  date_range_start: string | null;
  date_range_end: string | null;
  model_metrics: ModelMetrics[];
  weights_before: Record<string, number> | null;
  weights_after: Record<string, number> | null;
  source_weights_before: Record<string, number> | null;
  source_weights_after: Record<string, number> | null;
  brier_score_before: number | null;
  brier_score_after: number | null;
  duration_seconds: number;
  completed_at: string;
}

export interface TrainingReportList {
  reports: TrainingReport[];
  total: number;
}

// ─── Notifications ───

export interface PushSubscriptionPayload {
  endpoint: string;
  expirationTime: number | null;
  keys: Record<string, string>;
}

// ─── Model Edge ───

export interface ModelEdgeBucket {
  model_brier: number;
  market_brier: number;
  edge: number;
  sample_count: number;
  verdict: string;
}

export interface ModelEdgeReport {
  model_brier: number;
  market_brier: number;
  edge: number;
  edge_pct: string;
  verdict: string;
  sample_count: number;
  by_side: Record<string, ModelEdgeBucket>;
  by_city: Record<string, ModelEdgeBucket>;
}
