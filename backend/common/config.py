"""Application configuration via environment variables.

Uses pydantic-settings to load from .env file and environment variables.
All config is centralized here — agents should import `get_settings()`.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ─── Database ───
    database_url: str = "postgresql+asyncpg://boz:boz@localhost:5432/boz_weather_trader"
    redis_url: str = "redis://localhost:6379/0"

    # ─── Encryption ───
    encryption_key: str  # Required — no default (fail fast if missing)

    # ─── App ───
    environment: str = "development"
    log_level: str = "INFO"
    cors_origins: str = ""  # Extra CORS origins (comma-separated), e.g. "https://boznet.org"

    # ─── NWS API ───
    nws_user_agent: str = "BozWeatherTrader/1.0 (contact@example.com)"
    nws_rate_limit_per_second: float = 1.0

    # ─── Open-Meteo API ───
    openmeteo_rate_limit_per_second: float = 5.0

    # ─── Trading Defaults ───
    default_max_trade_size: float = 1.00  # dollars
    default_daily_loss_limit: float = 10.00  # dollars
    default_max_daily_exposure: float = 25.00  # dollars
    default_min_ev_threshold: float = 0.05  # 5%
    default_cooldown_minutes: int = 60
    default_consecutive_loss_limit: int = 3

    # ─── Database Pool ───
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # ─── Kalshi WebSocket Feed ───
    kalshi_ws_cache_ttl_seconds: int = 120
    kalshi_ws_refresh_minutes: int = 5

    # ─── ML Models ───
    xgb_model_dir: str = "models"
    xgb_ensemble_weight: float = 0.30  # Deprecated: use ml_ensemble_weight
    xgb_min_training_samples: int = 20  # Min forecast+settlement pairs to train
    xgb_retrain_interval_days: int = 7  # Retrain frequency
    ml_ensemble_weight: float = 0.30  # ML ensemble weight in final blend (0.0 = disabled)

    # ─── Retraining Triggers ───
    retrain_settlement_threshold: int = 25  # Retrain every N settlements since last train
    retrain_brier_threshold: float = 0.25  # Retrain if avg Brier score exceeds this
    retrain_max_days: int = 7  # Retrain if more than N days since last training

    # ─── Celery ───
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # ─── Push Notifications (VAPID) ───
    vapid_private_key: str | None = None
    vapid_email: str | None = None

    # ─── Algo v2 ───
    # Kalshi market archive + quote recorder (read-only public data; kill switch)
    v2_archive_enabled: bool = True

    # ─── Self-Update Sidecar ───
    updater_url: str = "http://updater:9999"
    updater_secret: str = "changeme"


@lru_cache
def get_settings() -> Settings:
    """Get cached application settings singleton.

    Uses lru_cache so Settings is only instantiated once.
    In tests, call `get_settings.cache_clear()` to reset.
    """
    return Settings()
