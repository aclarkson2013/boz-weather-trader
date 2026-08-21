"""API response and request schemas -- types used only by the REST layer."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from backend.common.schemas import (
    BracketPrediction,
    CityCode,
    TradeRecord,
    UTCDatetime,
    WeatherSourceName,
)


class AuthValidateRequest(BaseModel):
    """Request body for validating Kalshi API credentials."""

    key_id: str
    private_key: str
    demo_mode: bool = True  # Default to demo for safety


class AuthValidateResponse(BaseModel):
    """Response after successfully validating Kalshi API credentials."""

    valid: bool
    balance_cents: int
    demo_mode: bool


class AuthStatusResponse(BaseModel):
    """Response for checking current authentication status."""

    authenticated: bool
    user_id: str
    demo_mode: bool
    key_id_prefix: str


class DashboardData(BaseModel):
    """Aggregated dashboard data for the frontend."""

    balance_cents: int
    today_pnl_cents: int
    active_positions: list[TradeRecord]
    recent_trades: list[TradeRecord]
    next_market_launch: str | None
    predictions: list[BracketPrediction]


class TradesPage(BaseModel):
    """Paginated trade history response."""

    trades: list[TradeRecord]
    total: int
    page: int


class LogEntryResponse(BaseModel):
    """A single structured log entry for the log viewer."""

    id: int
    timestamp: UTCDatetime
    level: str
    module: str
    message: str
    data: dict | None = None


class CumulativePnlPoint(BaseModel):
    """A single point on the cumulative P&L chart."""

    date: str
    cumulative_pnl: int


class AccuracyPoint(BaseModel):
    """A single point on the accuracy-over-time chart."""

    date: str
    accuracy: float


class PerformanceData(BaseModel):
    """Aggregated performance metrics for the analytics dashboard."""

    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl_cents: int
    best_trade_pnl_cents: int
    worst_trade_pnl_cents: int
    cumulative_pnl: list[CumulativePnlPoint]
    pnl_by_city: dict[str, int]
    cost_by_city: dict[str, int] = {}  # city -> total cost in cents (for ROI)
    accuracy_over_time: list[AccuracyPoint]


class PeriodStats(BaseModel):
    """P&L and win/loss stats for a single time period."""

    pnl_cents: int
    wins: int
    losses: int


class DashboardStats(BaseModel):
    """P&L and W/L stats across multiple time periods for the dashboard."""

    yesterday: PeriodStats
    week: PeriodStats
    month: PeriodStats
    year: PeriodStats
    all_time: PeriodStats


class CalibrationBucket(BaseModel):
    """A single calibration bucket — predicted probability bin vs actual outcome rate."""

    bin_start: float
    bin_end: float
    predicted_avg: float
    actual_rate: float
    sample_count: int


class CalibrationReport(BaseModel):
    """Calibration report for a city's bracket predictions."""

    city: str
    lookback_days: int
    sample_count: int
    brier_score: float | None
    calibration_buckets: list[CalibrationBucket]
    status: str  # "ok" or "insufficient_data"


class SourceAccuracy(BaseModel):
    """Per-source forecast accuracy metrics (forecast vs settlement)."""

    source: str
    sample_count: int
    mae_f: float
    rmse_f: float
    bias_f: float


class ForecastErrorPoint(BaseModel):
    """A single point on the forecast error trend line."""

    date: str
    error_f: float


class ForecastErrorTrend(BaseModel):
    """Time series of forecast errors for a source/city combination."""

    city: str
    source: str
    points: list[ForecastErrorPoint]
    rolling_mae: float | None


class AccuracyOverview(BaseModel):
    """Combined accuracy overview — sources + calibration."""

    sources: list[SourceAccuracy]
    calibration: CalibrationReport


class CalendarDay(BaseModel):
    """Daily trading stats for the calendar view."""

    date: str  # YYYY-MM-DD
    trade_count: int
    wins: int
    losses: int
    pnl_cents: int
    win_rate: float  # 0.0 to 1.0


class CalendarWeek(BaseModel):
    """Weekly summary for the calendar sidebar."""

    week_number: int
    pnl_cents: int
    trade_count: int
    trading_days: int


class CalendarMonth(BaseModel):
    """Full month of calendar data with daily stats, weekly summaries, and totals."""

    year: int
    month: int
    days: list[CalendarDay]
    weeks: list[CalendarWeek]
    total_pnl_cents: int
    total_trades: int
    total_wins: int
    total_losses: int
    trading_days: int


class SettingsUpdate(BaseModel):
    """Partial update for user settings -- all fields optional."""

    trading_mode: Literal["auto", "manual"] | None = None
    max_trade_size_cents: int | None = None
    daily_loss_limit_cents: int | None = None
    max_daily_exposure_cents: int | None = None
    min_ev_threshold: float | None = None
    min_ev_threshold_yes: float | None = None
    min_ev_threshold_no: float | None = None
    cooldown_per_loss_minutes: int | None = None
    consecutive_loss_limit: int | None = None
    active_cities: list[CityCode] | None = None
    enabled_weather_sources: list[WeatherSourceName] | None = None
    notifications_enabled: bool | None = None
    demo_mode: bool | None = None
    max_contracts_per_bracket: int | None = None
    enable_consecutive_loss_limit: bool | None = None
    enable_per_loss_cooldown: bool | None = None

    # Trading engine guardrails
    model_weight: float | None = None
    max_model_market_divergence: float | None = None
    min_market_prob_for_yes: float | None = None

    # Fee estimation
    fee_estimate_mode: str | None = None

    # Display preferences
    timezone: str | None = None


# ─── Cooldown Status ───


class CooldownStatus(BaseModel):
    """Current cooldown state for the dashboard indicator."""

    is_active: bool
    cooldown_type: str | None = None  # "per_loss" | "consecutive_loss"
    cooldown_until: UTCDatetime | None = None
    remaining_minutes: int | None = None
    consecutive_losses: int = 0


# ─── Current Weather (Ticker) ───


class CityCurrentWeather(BaseModel):
    """Current weather observation for a single city."""

    city: CityCode
    city_name: str
    current_temp_f: float
    today_high_f: float
    today_low_f: float


class CurrentWeatherResponse(BaseModel):
    """Current weather for all active cities."""

    cities: list[CityCurrentWeather]
    fetched_at: UTCDatetime


# ─── Version Info ───


class VersionInfo(BaseModel):
    """Application version with optional update availability check."""

    current_version: str
    latest_version: str | None = None
    update_available: bool = False
    release_url: str | None = None


class UpdateTriggerResponse(BaseModel):
    """Response when triggering a self-update."""

    status: str
    message: str


class UpdateStatus(BaseModel):
    """Current status of an in-progress self-update."""

    status: str  # idle / pulling / building / restarting / done / error
    step: str | None = None
    error: str | None = None
    started_at: str | None = None


# ─── Training Reports ───


class ModelMetricsResponse(BaseModel):
    """Per-model training metrics in a training report."""

    model_name: str
    rmse: float | None = None
    mae: float | None = None
    accepted: bool = False
    error: str | None = None


class TrainingReportResponse(BaseModel):
    """A single training report for the frontend Training Log."""

    id: int
    triggered_by: str
    trigger_reason: str | None = None
    status: str
    training_samples: int
    test_samples: int
    date_range_start: str | None = None
    date_range_end: str | None = None
    model_metrics: list[ModelMetricsResponse]
    weights_before: dict[str, float] | None = None
    weights_after: dict[str, float] | None = None
    source_weights_before: dict[str, float] | None = None
    source_weights_after: dict[str, float] | None = None
    brier_score_before: float | None = None
    brier_score_after: float | None = None
    duration_seconds: float
    completed_at: str


class TrainingReportListResponse(BaseModel):
    """Paginated list of training reports."""

    reports: list[TrainingReportResponse]
    total: int


# ─── Model Edge Report ───


class ModelEdgeBucket(BaseModel):
    """Brier score comparison for a subset of trades (by side or city)."""

    model_brier: float
    market_brier: float
    edge: float  # market_brier - model_brier (positive = model better)
    sample_count: int
    verdict: str  # "Model outperforming", "Market outperforming", "Insufficient data"


class ModelEdgeReport(BaseModel):
    """Overall model edge report comparing model vs market Brier scores."""

    model_brier: float
    market_brier: float
    edge: float
    edge_pct: str  # e.g., "18%" — (edge / market_brier * 100)
    verdict: str
    sample_count: int
    by_side: dict[str, ModelEdgeBucket]  # "yes" and "no"
    by_city: dict[str, ModelEdgeBucket]  # "NYC", "CHI", etc.
