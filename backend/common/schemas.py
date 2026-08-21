"""Pydantic schemas — the interface contracts between all modules.

This is the most important file in the project. It defines the data shapes
that flow between agents. All cross-module communication uses these types.

RULES:
- Agents must use these types, never ad-hoc dicts or custom classes.
- If you need a new shared type, add it HERE.
- All monetary values use CENTS (int), matching the Kalshi API convention.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, PlainSerializer, field_validator


def _serialize_utc(v: datetime) -> str:
    """Serialize naive datetimes as UTC ISO 8601 strings with Z suffix.

    The database stores all timestamps as timezone-naive UTC. This serializer
    makes the API contract explicit by appending "Z" so the frontend knows
    the value is UTC without guessing.
    """
    if v.tzinfo is None:
        return v.isoformat() + "Z"
    return v.isoformat()


UTCDatetime = Annotated[datetime, PlainSerializer(_serialize_utc, return_type=str)]

# ─── City Type ───

CityCode = Literal["NYC", "CHI", "MIA", "AUS"]

# ─── Weather Source Type ───
# The five forecast feeds the weather pipeline can fetch. Which of them
# actually reach the ensemble is user-configurable — see
# UserSettings.enabled_weather_sources and DEFAULT_ENABLED_WEATHER_SOURCES.
WeatherSourceName = Literal[
    "NWS",
    "NWS:gridpoint",
    "Open-Meteo:ECMWF",
    "Open-Meteo:GFS",
    "Open-Meteo:ICON",
]

ALL_WEATHER_SOURCES: tuple[WeatherSourceName, ...] = (
    "NWS",
    "NWS:gridpoint",
    "Open-Meteo:ECMWF",
    "Open-Meteo:GFS",
    "Open-Meteo:ICON",
)

# Default working ensemble. Chosen from the 2026-08-21 source review
# (docs/ALGO_CHANGELOG.md): NWS and NWS:gridpoint are ~the same feed
# (error corr 0.983, identical on 80% of days), and ECMWF is the weakest
# member — dropping it slightly improved MAE. ICON is the only source whose
# removal significantly hurt the ensemble (p = 0.004), so it stays.
# The other two keep being fetched and scored; they are just not blended.
DEFAULT_ENABLED_WEATHER_SOURCES: tuple[WeatherSourceName, ...] = (
    "NWS:gridpoint",
    "Open-Meteo:GFS",
    "Open-Meteo:ICON",
)

# The ensemble needs at least two independent feeds to compute a spread.
MIN_ENABLED_WEATHER_SOURCES = 2


def parse_enabled_weather_sources(raw: str | None) -> list[WeatherSourceName]:
    """Parse the stored comma-separated source list into validated names.

    Unknown names (e.g. a feed retired from the pipeline) are dropped. If
    fewer than ``MIN_ENABLED_WEATHER_SOURCES`` survive, the default set is
    returned so the ensemble always has enough inputs to compute a spread.

    Args:
        raw: Comma-separated source names from ``User.enabled_weather_sources``.

    Returns:
        A list of valid source names, in canonical order.
    """
    names = {s.strip() for s in (raw or "").split(",") if s.strip()}
    enabled = [s for s in ALL_WEATHER_SOURCES if s in names]
    if len(enabled) < MIN_ENABLED_WEATHER_SOURCES:
        return list(DEFAULT_ENABLED_WEATHER_SOURCES)
    return enabled


TradeSide = Literal["yes", "no"]
ConfidenceLevel = Literal["high", "medium", "low"]
TradeStatusType = Literal["OPEN", "RESTING", "WON", "LOST", "CANCELED"]
PendingTradeStatusType = Literal["PENDING", "APPROVED", "REJECTED", "EXPIRED", "EXECUTED"]


# ─── Weather Schemas (Agent 1 → Agent 3) ───


class WeatherVariables(BaseModel):
    """Detailed weather variables from a forecast source."""

    temp_high_f: float
    temp_low_f: float | None = None
    humidity_pct: float | None = None
    wind_speed_mph: float | None = None
    wind_gust_mph: float | None = None
    cloud_cover_pct: float | None = None
    dew_point_f: float | None = None
    pressure_mb: float | None = None


class WeatherData(BaseModel):
    """A single weather forecast from one source for one city/date.

    This is the output of Agent 1 (Weather) and input to Agent 3 (Prediction).
    """

    city: CityCode
    date: date
    forecast_high_f: float
    source: str  # "NWS", "Open-Meteo:GFS", "Open-Meteo:ECMWF", etc.
    model_run_timestamp: UTCDatetime
    variables: WeatherVariables
    raw_data: dict  # Full raw API response for debugging
    fetched_at: UTCDatetime


# ─── Prediction Schemas (Agent 3 → Agent 4) ───


class BracketProbability(BaseModel):
    """Probability that the actual temperature lands in this bracket."""

    bracket_label: str  # e.g., "53-54°F", "≤52°F", "≥61°F"
    lower_bound_f: float | None = None  # None for bottom edge bracket
    upper_bound_f: float | None = None  # None for top edge bracket
    probability: float = Field(ge=0.0, le=1.0)


class BracketPrediction(BaseModel):
    """Full prediction output for one city/date: 6 brackets with probabilities.

    This is the output of Agent 3 (Prediction) and input to Agent 4 (Trading).
    """

    city: CityCode
    date: date
    brackets: list[BracketProbability]  # Always 6 items, sum to ~1.0
    ensemble_mean_f: float  # Weighted average forecast temperature
    ensemble_std_f: float  # Standard deviation of ensemble spread
    confidence: ConfidenceLevel
    model_sources: list[str]  # ["NWS", "GFS", "ECMWF", "ICON", ...]
    generated_at: UTCDatetime

    @field_validator("brackets")
    @classmethod
    def validate_bracket_probabilities(
        cls,
        v: list[BracketProbability],
    ) -> list[BracketProbability]:
        """Validate that bracket probabilities sum to approximately 1.0."""
        total = sum(b.probability for b in v)
        if not (0.95 <= total <= 1.05):
            msg = f"Bracket probabilities must sum to ~1.0, got {total:.4f}"
            raise ValueError(msg)
        return v


# ─── Trading Schemas (Agent 4) ───


class TradeSignal(BaseModel):
    """A potential trade identified by the EV calculator.

    Generated when the model finds a +EV opportunity.
    In auto mode, this triggers immediate execution.
    In manual mode, this becomes a PendingTrade for user approval.
    """

    city: CityCode
    bracket: str  # Bracket label, e.g., "55-56°F"
    side: TradeSide
    price_cents: int = Field(ge=1, le=99)  # Kalshi prices are always 1-99 cents
    quantity: int = Field(ge=1, default=1)
    model_probability: float = Field(ge=0.0, le=1.0)
    blended_probability: float | None = None  # After guardrails, None if disabled
    market_probability: float = Field(ge=0.0, le=1.0)
    ev: float  # Expected value in dollars (positive = profitable)
    confidence: ConfidenceLevel
    market_ticker: str  # e.g., "KXHIGHNY-25FEB15-B3"
    reasoning: str = ""  # Human-readable explanation


class TradeRecord(BaseModel):
    """A completed or open trade stored in the database.

    This is the full trade record including outcome and post-mortem.
    """

    id: str
    kalshi_order_id: str | None = None
    city: CityCode
    date: date
    market_ticker: str | None = None
    bracket_label: str
    side: TradeSide
    price_cents: int
    quantity: int
    model_probability: float
    market_probability: float
    ev_at_entry: float
    confidence: ConfidenceLevel
    status: TradeStatusType = "OPEN"
    settlement_temp_f: float | None = None
    settlement_source: str | None = None
    pnl_cents: int | None = None  # Profit/loss in cents after fees
    fees_cents: int | None = None  # Fees in cents
    postmortem_narrative: str | None = None
    created_at: UTCDatetime
    settled_at: UTCDatetime | None = None


class PostMortem(BaseModel):
    """Post-settlement analysis explaining why a trade won or lost."""

    trade_id: str
    actual_temp_f: float
    actual_bracket: str
    forecast_at_trade_time: float
    model_sources_accuracy: dict[str, float]  # source -> error in °F
    narrative: str  # Human-readable explanation
    pnl_after_fees_cents: int


class PendingTrade(BaseModel):
    """A trade awaiting user approval in manual mode."""

    id: str
    city: CityCode
    bracket: str
    market_ticker: str | None = None
    side: TradeSide
    price_cents: int
    quantity: int
    model_probability: float
    market_probability: float
    ev: float
    confidence: ConfidenceLevel
    reasoning: str
    status: PendingTradeStatusType = "PENDING"
    created_at: UTCDatetime
    expires_at: UTCDatetime
    acted_at: UTCDatetime | None = None


# ─── User Settings ───


class UserSettings(BaseModel):
    """User-configurable settings for trading behavior and risk limits."""

    trading_mode: Literal["auto", "manual"] = "manual"
    max_trade_size_cents: int = 100  # $1.00 default
    daily_loss_limit_cents: int = 1000  # $10.00 default
    max_daily_exposure_cents: int = 2500  # $25.00 default
    min_ev_threshold: float = Field(default=0.05, ge=0.0, le=1.0)  # 5% (legacy)
    min_ev_threshold_yes: float = Field(default=0.15, ge=0.0, le=1.0)  # 15%
    min_ev_threshold_no: float = Field(default=0.05, ge=0.0, le=1.0)  # 5%
    cooldown_per_loss_minutes: int = Field(default=60, ge=0, le=1440)
    consecutive_loss_limit: int = Field(default=3, ge=0, le=10)
    active_cities: list[CityCode] = ["NYC", "CHI", "MIA", "AUS"]
    demo_mode: bool = True  # Default to demo for safety
    notifications_enabled: bool = True

    # ─── Weather Sources ───
    # Which forecast feeds are blended into the ensemble. Disabled sources are
    # still fetched, stored, and scored in /api/accuracy/sources — they are
    # only excluded from prediction, so re-enabling one is backed by history.
    enabled_weather_sources: list[WeatherSourceName] = Field(
        default_factory=lambda: list(DEFAULT_ENABLED_WEATHER_SOURCES),
        min_length=MIN_ENABLED_WEATHER_SOURCES,
    )

    # ─── Kelly Criterion Position Sizing ───
    use_kelly_sizing: bool = False  # Disabled by default (always 1 contract)
    kelly_fraction: float = Field(default=0.25, ge=0.01, le=1.0)  # Quarter Kelly
    max_bankroll_pct_per_trade: float = Field(default=0.05, ge=0.01, le=0.25)  # 5%
    max_contracts_per_trade: int = Field(default=10, ge=1, le=100)

    # ─── Per-Bracket Position Cap ───
    max_contracts_per_bracket: int = Field(default=3, ge=1, le=20)

    # ─── Consecutive Loss Toggle ───
    enable_consecutive_loss_limit: bool = True

    # ─── Per-Loss Cooldown Toggle ───
    enable_per_loss_cooldown: bool = True

    # ─── Trading Engine Guardrails ───
    model_weight: float = Field(default=0.4, ge=0.0, le=1.0)  # Blend weight for model
    max_model_market_divergence: float = Field(default=0.25, ge=0.0, le=0.50)
    min_market_prob_for_yes: float = Field(default=0.15, ge=0.0, le=0.50)

    # ─── Fee Estimation ───
    fee_estimate_mode: str = "realistic"  # "realistic" or "conservative"

    # ─── Display Preferences ───
    timezone: str = ""  # IANA timezone (e.g. "America/Chicago"), empty = browser default


# ─── Portfolio Sync ───


class SyncResult(BaseModel):
    """Result of a Kalshi portfolio sync operation."""

    synced_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    errors: list[str] = Field(default_factory=list)
    synced_at: UTCDatetime
