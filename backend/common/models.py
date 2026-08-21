"""SQLAlchemy ORM models — all database tables for Boz Weather Trader.

All tables are defined here. Agents should never define their own models.
These map to PostgreSQL tables managed by Alembic migrations.

Usage:
    from backend.common.models import User, Trade, WeatherForecast
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.types import TypeDecorator


class TZNaiveDateTime(TypeDecorator):
    """DateTime that strips timezone info before INSERT/UPDATE.

    PostgreSQL TIMESTAMP WITHOUT TIME ZONE + asyncpg rejects timezone-aware
    Python datetimes. This TypeDecorator transparently strips tzinfo so all
    existing code using datetime.now(UTC) works without modification.

    On read, datetimes come back as naive (no tzinfo), which matches the
    column type.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001, ANN201
        if value is not None and hasattr(value, "tzinfo") and value.tzinfo is not None:
            return value.replace(tzinfo=None)
        return value


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    pass


# ─── Enums ───


class CityEnum(enum.StrEnum):
    """Supported cities for weather markets."""

    NYC = "NYC"
    CHI = "CHI"
    MIA = "MIA"
    AUS = "AUS"


class TradeStatus(enum.StrEnum):
    """Trade lifecycle status."""

    OPEN = "OPEN"
    RESTING = "RESTING"  # Order on book, awaiting fill
    WON = "WON"
    LOST = "LOST"
    CANCELED = "CANCELED"


class PendingTradeStatus(enum.StrEnum):
    """Pending trade (manual mode) lifecycle status."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXECUTED = "EXECUTED"


# ─── Helper ───


def _utcnow() -> datetime:
    """Return timezone-naive UTC now (for TIMESTAMP WITHOUT TIME ZONE columns).

    PostgreSQL TIMESTAMP WITHOUT TIME ZONE + asyncpg rejects timezone-aware
    datetimes. We strip tzinfo so the value is compatible with the column type.
    """
    return datetime.now(UTC).replace(tzinfo=None)


# ─── Models ───


class User(Base):
    """A user with encrypted Kalshi API credentials."""

    __tablename__ = "users"

    id = Column(String, primary_key=True)
    kalshi_key_id = Column(String, nullable=False)
    encrypted_private_key = Column(Text, nullable=False)  # AES-256 encrypted PEM
    trading_mode = Column(String, default="manual")
    max_trade_size_cents = Column(Integer, default=100)
    daily_loss_limit_cents = Column(Integer, default=1000)
    max_daily_exposure_cents = Column(Integer, default=2500)
    min_ev_threshold = Column(Float, default=0.05)
    min_ev_threshold_yes = Column(Float, default=0.15)
    min_ev_threshold_no = Column(Float, default=0.05)
    cooldown_per_loss_minutes = Column(Integer, default=60)
    consecutive_loss_limit = Column(Integer, default=3)
    active_cities = Column(String, default="NYC,CHI,MIA,AUS")  # Comma-separated
    # Comma-separated weather sources blended into the ensemble (see
    # schemas.DEFAULT_ENABLED_WEATHER_SOURCES). Disabled sources are still
    # fetched and scored, just not used for prediction.
    enabled_weather_sources = Column(String, default="NWS:gridpoint,Open-Meteo:GFS,Open-Meteo:ICON")
    demo_mode = Column(Boolean, default=True)  # Default to demo for safety
    notifications_enabled = Column(Boolean, default=True)
    push_subscription = Column(Text, nullable=True)  # JSON web push subscription

    # Kelly Criterion position sizing
    use_kelly_sizing = Column(Boolean, default=False)
    kelly_fraction = Column(Float, default=0.25)  # Quarter Kelly
    max_bankroll_pct_per_trade = Column(Float, default=0.05)  # 5%
    max_contracts_per_trade = Column(Integer, default=10)

    # Per-bracket position cap
    max_contracts_per_bracket = Column(Integer, default=3)

    # Consecutive loss limit toggle
    enable_consecutive_loss_limit = Column(Boolean, default=True)

    # Per-loss cooldown toggle
    enable_per_loss_cooldown = Column(Boolean, default=True)

    # Trading engine guardrails
    model_weight = Column(Float, default=0.4)
    max_model_market_divergence = Column(Float, default=0.25)
    min_market_prob_for_yes = Column(Float, default=0.15)

    # Fee estimation
    fee_estimate_mode = Column(String, default="realistic")

    # Display preferences
    timezone = Column(String, default="")

    created_at = Column(TZNaiveDateTime, default=_utcnow)
    updated_at = Column(TZNaiveDateTime, default=_utcnow, onupdate=_utcnow)

    # Relationships
    trades = relationship("Trade", back_populates="user", lazy="select")
    forecasts = relationship("WeatherForecast", back_populates="user", lazy="select")


class WeatherForecast(Base):
    """A stored weather forecast from NWS or Open-Meteo."""

    __tablename__ = "weather_forecasts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=True)
    city = Column(Enum(CityEnum, native_enum=False), nullable=False)
    forecast_date = Column(TZNaiveDateTime, nullable=False)
    source = Column(String, nullable=False)  # "NWS", "Open-Meteo:GFS", etc.
    forecast_high_f = Column(Float, nullable=False)
    forecast_low_f = Column(Float, nullable=True)
    humidity_pct = Column(Float, nullable=True)
    wind_speed_mph = Column(Float, nullable=True)
    cloud_cover_pct = Column(Float, nullable=True)
    raw_data = Column(JSON, nullable=True)
    fetched_at = Column(TZNaiveDateTime, default=_utcnow)

    # Relationships
    user = relationship("User", back_populates="forecasts")

    __table_args__ = (
        Index("ix_forecast_city_date", "city", "forecast_date"),
        Index("ix_forecast_source", "source"),
    )


class Prediction(Base):
    """A bracket probability prediction generated by the prediction engine."""

    __tablename__ = "predictions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    city = Column(Enum(CityEnum, native_enum=False), nullable=False)
    prediction_date = Column(TZNaiveDateTime, nullable=False)
    ensemble_mean_f = Column(Float, nullable=False)
    ensemble_std_f = Column(Float, nullable=False)
    confidence = Column(String, nullable=False)  # "high", "medium", "low"
    model_sources = Column(String, nullable=False)  # Comma-separated
    brackets_json = Column(JSON, nullable=False)  # Serialized BracketProbability list
    generated_at = Column(TZNaiveDateTime, default=_utcnow)

    __table_args__ = (Index("ix_prediction_city_date", "city", "prediction_date"),)


class Trade(Base):
    """A trade placed on Kalshi — the core business record."""

    __tablename__ = "trades"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    kalshi_order_id = Column(String, nullable=True)
    city = Column(Enum(CityEnum, native_enum=False), nullable=False)
    trade_date = Column(TZNaiveDateTime, nullable=False)
    market_date = Column(TZNaiveDateTime, nullable=True)  # Market event date (from ticker)
    market_ticker = Column(String, nullable=False)
    bracket_label = Column(String, nullable=False)
    side = Column(String, nullable=False)  # "yes" or "no"
    price_cents = Column(Integer, nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    model_probability = Column(Float, nullable=False)
    blended_probability = Column(Float, nullable=True)  # Post-guardrail probability
    market_probability = Column(Float, nullable=False)
    ev_at_entry = Column(Float, nullable=False)
    confidence = Column(String, nullable=False)
    status = Column(Enum(TradeStatus, native_enum=False), default=TradeStatus.OPEN)
    settlement_temp_f = Column(Float, nullable=True)
    settlement_source = Column(String, nullable=True)
    pnl_cents = Column(Integer, nullable=True)
    fees_cents = Column(Integer, nullable=True)
    postmortem_narrative = Column(Text, nullable=True)
    created_at = Column(TZNaiveDateTime, default=_utcnow)
    settled_at = Column(TZNaiveDateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="trades")

    __table_args__ = (
        Index("ix_trade_city_date", "city", "trade_date"),
        Index("ix_trade_status", "status"),
        Index("ix_trade_user", "user_id"),
    )


class PendingTradeModel(Base):
    """A trade awaiting user approval in manual mode."""

    __tablename__ = "pending_trades"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    city = Column(Enum(CityEnum, native_enum=False), nullable=False)
    bracket_label = Column(String, nullable=False)
    market_ticker = Column(String, nullable=False)
    side = Column(String, nullable=False)
    price_cents = Column(Integer, nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    model_probability = Column(Float, nullable=False)
    market_probability = Column(Float, nullable=False)
    ev = Column(Float, nullable=False)
    confidence = Column(String, nullable=False)
    reasoning = Column(Text, nullable=True)
    status = Column(Enum(PendingTradeStatus, native_enum=False), default=PendingTradeStatus.PENDING)
    created_at = Column(TZNaiveDateTime, default=_utcnow)
    expires_at = Column(TZNaiveDateTime, nullable=False)
    acted_at = Column(TZNaiveDateTime, nullable=True)


class Settlement(Base):
    """Actual NWS CLI (Daily Climate Report) data used for market settlement."""

    __tablename__ = "settlements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    city = Column(Enum(CityEnum, native_enum=False), nullable=False)
    settlement_date = Column(TZNaiveDateTime, nullable=False)
    actual_high_f = Column(Float, nullable=False)
    actual_low_f = Column(Float, nullable=True)
    source = Column(String, default="NWS_CLI")
    raw_data = Column(JSON, nullable=True)
    fetched_at = Column(TZNaiveDateTime, default=_utcnow)

    __table_args__ = (Index("ix_settlement_city_date", "city", "settlement_date"),)


class DailyRiskState(Base):
    """Per-user daily risk tracking: P&L, exposure, cooldowns.

    Reset at midnight ET each trading day.
    Uses SELECT FOR UPDATE for concurrency safety.
    """

    __tablename__ = "daily_risk_states"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    trading_day = Column(TZNaiveDateTime, nullable=False)
    total_loss_cents = Column(Integer, default=0)
    total_exposure_cents = Column(Integer, default=0)
    consecutive_losses = Column(Integer, default=0)
    cooldown_until = Column(TZNaiveDateTime, nullable=True)
    trades_count = Column(Integer, default=0)

    __table_args__ = (Index("ix_risk_user_day", "user_id", "trading_day", unique=True),)


class LogEntry(Base):
    """Structured log entry stored in the database for the log viewer."""

    __tablename__ = "log_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(TZNaiveDateTime, default=_utcnow, index=True)
    level = Column(String, nullable=False)
    module_tag = Column(String, nullable=False, index=True)
    message = Column(Text, nullable=False)
    data = Column(JSON, nullable=True)


class TrainingReport(Base):
    """Persisted record of an ML model training run.

    Created each time train_all_models runs (scheduled, post-settlement, or manual).
    Captures per-model metrics, weight changes, source weight updates, and Brier
    score snapshots so the user can see what the model learned over time.
    """

    __tablename__ = "training_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    triggered_by = Column(String, nullable=False)  # "schedule" | "settlement" | "manual"
    trigger_reason = Column(String, nullable=True)  # e.g. "weekly", "settlement_count_25"
    status = Column(String, nullable=False)  # "completed" | "skipped" | "error"

    # Data stats
    training_samples = Column(Integer, default=0)
    test_samples = Column(Integer, default=0)
    date_range_start = Column(TZNaiveDateTime, nullable=True)
    date_range_end = Column(TZNaiveDateTime, nullable=True)

    # Per-model metrics (JSON): {"XGBoost": {"rmse": 2.1, "mae": 1.7, "accepted": true}}
    model_metrics = Column(JSON, nullable=False)

    # ML model weights before and after training
    weights_before = Column(JSON, nullable=True)
    weights_after = Column(JSON, nullable=True)

    # Ensemble source weights before and after
    source_weights_before = Column(JSON, nullable=True)
    source_weights_after = Column(JSON, nullable=True)

    # Calibration snapshot
    brier_score_before = Column(Float, nullable=True)
    brier_score_after = Column(Float, nullable=True)

    # Timing
    duration_seconds = Column(Float, default=0.0)
    started_at = Column(TZNaiveDateTime, nullable=False)
    completed_at = Column(TZNaiveDateTime, default=_utcnow)

    # Error info (populated when status="error")
    error_message = Column(Text, nullable=True)

    __table_args__ = (Index("ix_training_report_completed", "completed_at"),)
