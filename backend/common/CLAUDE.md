# Backend Common — Shared Foundation

## Overview

This module contains everything shared across all backend agents: Pydantic schemas (the interface contracts), database models, logging setup, configuration, encryption, and custom exceptions. This is built BEFORE the agents start, and agents import from here — never from each other.

## What Lives Here

```
backend/common/
├── __init__.py
├── schemas.py        -> Pydantic models for ALL cross-module data (THE interface contracts)
├── database.py       -> SQLAlchemy async engine, session factory, dependency injection, async_session() helper
├── models.py         -> SQLAlchemy ORM models (all database tables)
├── logging.py        -> Structured logging setup with module tags, secret redaction + Redis log persistence
├── config.py         -> App settings via pydantic-settings (reads .env)
├── encryption.py     -> AES-256 encryption helpers for API key storage
├── exceptions.py     -> Base exception classes shared across modules
├── middleware.py     -> Production middleware (request ID, logging, Prometheus metrics, security headers + smart Cache-Control)
└── metrics.py        -> Centralized Prometheus metric definitions (counters, histograms, gauges)
```

---

## schemas.py — Interface Contracts

This is the most important file in the project. It defines the data shapes that flow between modules. Agents must use these types — not ad-hoc dicts or custom classes.

### Key Schemas:

```python
# Weather Data (Agent 1 -> Agent 3)
class WeatherData(BaseModel):
    city: Literal["NYC", "CHI", "MIA", "AUS"]
    date: date
    forecast_high_f: float
    source: str  # "NWS", "Open-Meteo:GFS", "Open-Meteo:ECMWF", etc.
    model_run_timestamp: datetime
    variables: WeatherVariables  # temp, humidity, wind, cloud cover, etc.
    raw_data: dict  # full raw API response
    fetched_at: datetime


class WeatherVariables(BaseModel):
    temp_high_f: float
    temp_low_f: float | None
    humidity_pct: float | None
    wind_speed_mph: float | None
    wind_gust_mph: float | None
    cloud_cover_pct: float | None
    dew_point_f: float | None
    pressure_mb: float | None


# Bracket Prediction (Agent 3 -> Agent 4)
class BracketProbability(BaseModel):
    bracket_label: str  # e.g., "53-54F"
    lower_bound_f: float | None  # None for bottom edge bracket
    upper_bound_f: float | None  # None for top edge bracket
    probability: float  # 0.0 to 1.0


class BracketPrediction(BaseModel):
    city: str
    date: date
    brackets: list[BracketProbability]  # 6 items, probabilities sum to 1.0
    ensemble_forecast_f: float
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    model_sources: list[str]
    forecast_spread_f: float
    error_std_f: float
    generated_at: datetime


# Trade Signal (Agent 4 internal + queue)
class TradeSignal(BaseModel):
    city: str
    bracket: BracketProbability
    side: Literal["yes", "no"]
    market_price: float
    model_probability: float
    blended_probability: (
        float | None
    )  # guardrail-blended prob (model_weight * model + (1-model_weight) * market)
    ev: float
    confidence: str
    reasoning: str


# Trade Record (for post-mortem, Agent 4 -> storage)
class TradeRecord(BaseModel):
    id: str
    city: str
    date: date
    bracket_label: str
    side: Literal["yes", "no"]
    entry_price: float
    quantity: int
    model_probability: float
    market_probability: float
    ev_at_entry: float
    confidence: str
    weather_forecasts: list[WeatherData]  # snapshots at time of trade
    prediction: BracketPrediction  # model output at time of trade
    status: Literal["OPEN", "WON", "LOST", "CANCELLED"]
    settlement_temp_f: float | None
    settlement_source: str | None
    pnl: float | None
    postmortem: str | None  # generated narrative
    created_at: datetime
    settled_at: datetime | None


# User Settings
class UserSettings(BaseModel):
    trading_mode: Literal["auto", "manual"]
    max_trade_size: float  # dollars
    daily_loss_limit: float
    max_daily_exposure: float
    min_ev_threshold: float  # 0.0 to 1.0
    cooldown_per_loss_minutes: int
    consecutive_loss_limit: int
    cities: list[str]  # which cities to trade
    # Guardrail settings (Phase 41)
    model_weight: float  # 0.0-1.0, weight for model vs market in blended probability
    max_model_market_divergence: float  # max allowed abs diff between model and market prob
    min_market_prob_for_yes: float  # min market probability to allow YES trades
    # Split EV thresholds (Phase 48)
    min_ev_threshold_yes: float  # 0.0 to 1.0, minimum EV for YES trades
    min_ev_threshold_no: float  # 0.0 to 1.0, minimum EV for NO trades
    # Fee estimation mode (Phase 49)
    fee_estimate_mode: str  # "conservative" or "realistic"
```

---

## models.py — SQLAlchemy ORM Models

All database tables are defined here. These map directly to PostgreSQL tables managed by Alembic migrations. Agents should never define their own ORM models — everything goes through this file.

### Enums

```python
from sqlalchemy import (
    Column,
    String,
    Float,
    DateTime,
    Integer,
    Boolean,
    JSON,
    Text,
    ForeignKey,
    Enum as SQLEnum,
)
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.ext.asyncio import AsyncAttrs
from datetime import datetime, date
import enum


class Base(AsyncAttrs, DeclarativeBase):
    pass


class CityEnum(str, enum.Enum):
    NYC = "NYC"
    CHI = "CHI"
    MIA = "MIA"
    AUS = "AUS"


class TradeStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXECUTED = "EXECUTED"
    WON = "WON"
    LOST = "LOST"
    CANCELLED = "CANCELLED"
```

### User Model

```python
class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True)
    api_key_id = Column(String, nullable=False)
    encrypted_private_key = Column(Text, nullable=False)  # AES-256 encrypted
    settings = Column(JSON, nullable=False, default=dict)
    # Guardrail columns (Phase 41)
    model_weight = Column(Float, default=0.7)  # 0.0-1.0 weight for model vs market prob
    max_model_market_divergence = Column(Float, default=0.30)  # max abs diff before blocking
    min_market_prob_for_yes = Column(Float, default=0.05)  # min market prob for YES trades
    # Split EV thresholds (Phase 48)
    min_ev_threshold_yes = Column(Float, default=0.05)  # separate YES EV threshold
    min_ev_threshold_no = Column(Float, default=0.05)  # separate NO EV threshold
    # Fee estimation (Phase 49)
    fee_estimate_mode = Column(String, default="conservative")  # "conservative" or "realistic"
    created_at = Column(DateTime, default=datetime.utcnow)
    trades = relationship("Trade", back_populates="user")
```

### WeatherForecast Model

```python
class WeatherForecast(Base):
    __tablename__ = "weather_forecasts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    city = Column(SQLEnum(CityEnum), nullable=False, index=True)
    forecast_date = Column(DateTime, nullable=False, index=True)
    forecast_high_f = Column(Float, nullable=False)
    source = Column(String, nullable=False)  # "NWS", "Open-Meteo:GFS", etc.
    model_run_timestamp = Column(DateTime)
    raw_data = Column(JSON)
    fetched_at = Column(DateTime, default=datetime.utcnow)
```

### Prediction Model

```python
class Prediction(Base):
    __tablename__ = "predictions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    city = Column(SQLEnum(CityEnum), nullable=False, index=True)
    prediction_date = Column(DateTime, nullable=False)
    ensemble_forecast_f = Column(Float, nullable=False)
    bracket_probabilities = Column(
        JSON, nullable=False
    )  # list of {label, lower, upper, probability}
    confidence = Column(String, nullable=False)
    model_sources = Column(JSON)
    forecast_spread_f = Column(Float)
    error_std_f = Column(Float)
    generated_at = Column(DateTime, default=datetime.utcnow)
```

### Trade Model

```python
class Trade(Base):
    __tablename__ = "trades"
    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    city = Column(SQLEnum(CityEnum), nullable=False, index=True)
    trade_date = Column(DateTime, nullable=False)
    bracket_label = Column(String, nullable=False)
    side = Column(String, nullable=False)  # "yes" or "no"
    entry_price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    model_probability = Column(Float, nullable=False)
    market_probability = Column(Float, nullable=False)
    blended_probability = Column(Float)  # guardrail-blended probability (Phase 41)
    ev_at_entry = Column(Float, nullable=False)
    confidence = Column(String)
    kalshi_order_id = Column(String)
    status = Column(SQLEnum(TradeStatus), nullable=False, default=TradeStatus.PENDING)
    settlement_temp_f = Column(Float)
    pnl = Column(Float)
    postmortem_narrative = Column(Text)
    weather_snapshot = Column(JSON)  # forecasts at time of trade
    prediction_snapshot = Column(JSON)  # model output at time of trade
    created_at = Column(DateTime, default=datetime.utcnow)
    settled_at = Column(DateTime)
    user = relationship("User", back_populates="trades")
```

### Settlement Model

```python
class Settlement(Base):
    __tablename__ = "settlements"
    id = Column(Integer, primary_key=True, autoincrement=True)
    city = Column(SQLEnum(CityEnum), nullable=False)
    settlement_date = Column(DateTime, nullable=False, index=True)
    actual_high_f = Column(Float, nullable=False)
    source = Column(String, default="NWS_CLI")
    raw_report = Column(JSON)
    fetched_at = Column(DateTime, default=datetime.utcnow)
```

### LogEntry Model

```python
class LogEntry(Base):
    __tablename__ = "log_entries"
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    level = Column(String, nullable=False)
    module = Column(String, nullable=False, index=True)
    message = Column(String, nullable=False)
    data = Column(JSON)
```

### Full models.py Implementation (Copy-Paste Ready)

```python
from sqlalchemy import (
    Column,
    String,
    Float,
    DateTime,
    Integer,
    Boolean,
    JSON,
    Text,
    ForeignKey,
    Enum as SQLEnum,
)
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.ext.asyncio import AsyncAttrs
from datetime import datetime, date
import enum


class Base(AsyncAttrs, DeclarativeBase):
    pass


class CityEnum(str, enum.Enum):
    NYC = "NYC"
    CHI = "CHI"
    MIA = "MIA"
    AUS = "AUS"


class TradeStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXECUTED = "EXECUTED"
    WON = "WON"
    LOST = "LOST"
    CANCELLED = "CANCELLED"


class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True)
    api_key_id = Column(String, nullable=False)
    encrypted_private_key = Column(Text, nullable=False)  # AES-256 encrypted
    settings = Column(JSON, nullable=False, default=dict)
    # Guardrail columns (Phase 41)
    model_weight = Column(Float, default=0.7)  # 0.0-1.0 weight for model vs market prob
    max_model_market_divergence = Column(Float, default=0.30)  # max abs diff before blocking
    min_market_prob_for_yes = Column(Float, default=0.05)  # min market prob for YES trades
    # Split EV thresholds (Phase 48)
    min_ev_threshold_yes = Column(Float, default=0.05)  # separate YES EV threshold
    min_ev_threshold_no = Column(Float, default=0.05)  # separate NO EV threshold
    # Fee estimation (Phase 49)
    fee_estimate_mode = Column(String, default="conservative")  # "conservative" or "realistic"
    created_at = Column(DateTime, default=datetime.utcnow)
    trades = relationship("Trade", back_populates="user")


class WeatherForecast(Base):
    __tablename__ = "weather_forecasts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    city = Column(SQLEnum(CityEnum), nullable=False, index=True)
    forecast_date = Column(DateTime, nullable=False, index=True)
    forecast_high_f = Column(Float, nullable=False)
    source = Column(String, nullable=False)  # "NWS", "Open-Meteo:GFS", etc.
    model_run_timestamp = Column(DateTime)
    raw_data = Column(JSON)
    fetched_at = Column(DateTime, default=datetime.utcnow)


class Prediction(Base):
    __tablename__ = "predictions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    city = Column(SQLEnum(CityEnum), nullable=False, index=True)
    prediction_date = Column(DateTime, nullable=False)
    ensemble_forecast_f = Column(Float, nullable=False)
    bracket_probabilities = Column(
        JSON, nullable=False
    )  # list of {label, lower, upper, probability}
    confidence = Column(String, nullable=False)
    model_sources = Column(JSON)
    forecast_spread_f = Column(Float)
    error_std_f = Column(Float)
    generated_at = Column(DateTime, default=datetime.utcnow)


class Trade(Base):
    __tablename__ = "trades"
    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    city = Column(SQLEnum(CityEnum), nullable=False, index=True)
    trade_date = Column(DateTime, nullable=False)
    bracket_label = Column(String, nullable=False)
    side = Column(String, nullable=False)  # "yes" or "no"
    entry_price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    model_probability = Column(Float, nullable=False)
    market_probability = Column(Float, nullable=False)
    blended_probability = Column(Float)  # guardrail-blended probability (Phase 41)
    ev_at_entry = Column(Float, nullable=False)
    confidence = Column(String)
    kalshi_order_id = Column(String)
    status = Column(SQLEnum(TradeStatus), nullable=False, default=TradeStatus.PENDING)
    settlement_temp_f = Column(Float)
    pnl = Column(Float)
    postmortem_narrative = Column(Text)
    weather_snapshot = Column(JSON)  # forecasts at time of trade
    prediction_snapshot = Column(JSON)  # model output at time of trade
    created_at = Column(DateTime, default=datetime.utcnow)
    settled_at = Column(DateTime)
    user = relationship("User", back_populates="trades")


class Settlement(Base):
    __tablename__ = "settlements"
    id = Column(Integer, primary_key=True, autoincrement=True)
    city = Column(SQLEnum(CityEnum), nullable=False)
    settlement_date = Column(DateTime, nullable=False, index=True)
    actual_high_f = Column(Float, nullable=False)
    source = Column(String, default="NWS_CLI")
    raw_report = Column(JSON)
    fetched_at = Column(DateTime, default=datetime.utcnow)


class LogEntry(Base):
    __tablename__ = "log_entries"
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    level = Column(String, nullable=False)
    module = Column(String, nullable=False, index=True)
    message = Column(String, nullable=False)
    data = Column(JSON)
```

---

## database.py — Async Database Setup

Manages the SQLAlchemy async engine, session factory, and FastAPI dependency injection.

### Full Implementation

```python
# backend/common/database.py
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from backend.common.config import settings

engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncSession:
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# FastAPI dependency
from fastapi import Depends


async def get_db_session(session: AsyncSession = Depends(get_db)):
    return session
```

### Usage in FastAPI Routers

```python
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from backend.common.database import get_db_session

router = APIRouter()


@router.get("/trades")
async def list_trades(db: AsyncSession = Depends(get_db_session)):
    result = await db.execute(select(Trade).order_by(Trade.created_at.desc()))
    return result.scalars().all()
```

### async_session() Helper (Non-FastAPI Context)

The `async_session()` helper function provides database sessions outside of FastAPI's dependency injection (e.g., in Celery tasks, training pipelines, or background workers). It creates a fresh session from the async session factory.

```python
import asyncio
from backend.common.database import async_session


async def _do_db_work():
    async with async_session() as session:
        # ... your queries here
        await session.commit()


def celery_task():
    asyncio.run(_do_db_work())
```

---

## encryption.py — AES-256 Key Encryption

Handles encryption and decryption of Kalshi RSA private keys at rest. Uses Fernet symmetric encryption (AES-128-CBC under the hood via the `cryptography` library).

### Full Implementation

```python
# backend/common/encryption.py
from cryptography.fernet import Fernet
from backend.common.config import settings


def encrypt_api_key(plaintext: str) -> str:
    f = Fernet(settings.encryption_key.encode())
    return f.encrypt(plaintext.encode()).decode()


def decrypt_api_key(ciphertext: str) -> str:
    f = Fernet(settings.encryption_key.encode())
    return f.decrypt(ciphertext.encode()).decode()
```

### Key Generation

Generate a new Fernet key for your `.env` file:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Store the output as `ENCRYPTION_KEY` in your `.env` file. Never commit this value to version control.

### Usage

```python
from backend.common.encryption import encrypt_api_key, decrypt_api_key

# During user onboarding (store encrypted)
encrypted = encrypt_api_key(user_provided_private_key)
user.encrypted_private_key = encrypted

# When placing a Kalshi order (decrypt in memory only)
plaintext_key = decrypt_api_key(user.encrypted_private_key)
# ... use plaintext_key for Kalshi API auth ...
# plaintext_key goes out of scope and is garbage collected
```

### Security Notes

- The Fernet key in `settings.encryption_key` is the master secret. If it is compromised, all stored private keys are compromised.
- Never log the plaintext key, the Fernet key, or the ciphertext.
- In production, consider using a secrets manager (AWS Secrets Manager, HashiCorp Vault) instead of a `.env` file.

---

## logging.py — Structured Logging

Provides a `get_logger(module_tag)` function that returns a logger configured for structured JSON-enriched output with automatic secret redaction.

### Full Implementation

```python
# backend/common/logging.py
import logging
import json
from datetime import datetime


class StructuredFormatter(logging.Formatter):
    def format(self, record):
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        module = getattr(record, "module_tag", "SYSTEM")
        data = getattr(record, "data", {})
        # SECURITY: redact anything that looks like a key
        data_str = json.dumps(data) if data else "{}"
        return f"[{timestamp}] {record.levelname:<8} {module:<12} {record.getMessage()}  {data_str}"


def get_logger(module_tag: str) -> logging.Logger:
    logger = logging.getLogger(f"boz.{module_tag}")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(StructuredFormatter())
        logger.addHandler(handler)
    # Attach module_tag so formatter can use it
    old_makeRecord = logger.makeRecord

    def makeRecord(name, level, fn, lno, msg, args, exc_info, func=None, extra=None, sinfo=None):
        extra = extra or {}
        extra["module_tag"] = module_tag
        return old_makeRecord(name, level, fn, lno, msg, args, exc_info, func, extra, sinfo)

    logger.makeRecord = makeRecord
    return logger
```

### Output Format

```
[2026-02-17 10:00:01] INFO     WEATHER      Fetched NWS forecast  {"city":"NYC","high_f":55}
[2026-02-17 10:00:02] WARN     RISK         Approaching daily loss limit  {"current_loss":8.50,"limit":10.00}
[2026-02-17 10:00:03] ERROR    ORDER        Kalshi order rejected  {"order_id":"abc123","reason":"insufficient_balance"}
```

### Usage in Modules

```python
from backend.common.logging import get_logger

logger = get_logger("WEATHER")

# Basic logging
logger.info("Fetched NWS forecast", extra={"data": {"city": "NYC", "high_f": 55}})

# With structured data
logger.warning("Stale forecast detected", extra={"data": {"city": "CHI", "age_minutes": 95}})

# Error logging
logger.error(
    "API call failed", extra={"data": {"url": "https://api.weather.gov/...", "status": 503}}
)
```

### Module Tag Reference

| Tag        | Used By                                  |
|------------|------------------------------------------|
| WEATHER    | Weather data pipeline (Agent 1)          |
| MARKET     | Kalshi API client (Agent 2)              |
| MODEL      | Prediction engine (Agent 3)              |
| TRADING    | Trade decision engine (Agent 4)          |
| ORDER      | Order placement and management           |
| RISK       | Risk limit checks and cooldowns          |
| COOLDOWN   | Cooldown timer management                |
| AUTH       | Authentication and key management        |
| SETTLE     | Settlement data and resolution           |
| POSTMORTEM | Trade post-mortem narrative generation   |
| SYSTEM     | General system operations                |
| API        | API endpoint operations                  |

### Log Persistence (DatabaseLogHandler)

INFO+ log entries are automatically published to Redis (`boz:log_entries` channel) via a `DatabaseLogHandler` attached to every logger. The `log_subscriber` asyncio task in the FastAPI process picks these up and writes them to the `log_entries` database table, powering the `/logs` frontend viewer.

- **Sync Redis client** (`_get_sync_redis()`): Lazy-initialized with 200ms timeout, caches connection failure
- **Secret redaction**: Applied to both message text and structured data before publishing
- **Graceful failure**: Redis errors are silently caught — log persistence never crashes the app
- **`reset_loggers()`**: Resets cached loggers and Redis state — used in tests

---

## config.py — Application Settings

Uses pydantic-settings to read from environment variables and `.env` file. All configuration is centralized here so agents never read environment variables directly.

### Full Implementation

```python
# backend/common/config.py
from pydantic_settings import BaseSettings
from typing import Literal


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+asyncpg://boz:boz@localhost:5432/boz_weather_trader"
    redis_url: str = "redis://localhost:6379/0"

    # Encryption
    encryption_key: str  # Fernet key for AES-256 (generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

    # App
    environment: Literal["development", "production"] = "development"
    log_level: str = "INFO"

    # NWS
    nws_user_agent: str = "BozWeatherTrader/1.0 (boz@example.com)"
    nws_rate_limit_per_second: float = 1.0

    # Trading defaults
    default_max_trade_size: float = 1.00
    default_daily_loss_limit: float = 10.00
    default_max_daily_exposure: float = 25.00
    default_min_ev_threshold: float = 0.05
    default_cooldown_minutes: int = 60
    default_consecutive_loss_limit: int = 3

    # Celery
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
```

### Settings Reference

| Setting                        | Type   | Default                  | Description                                       |
|--------------------------------|--------|--------------------------|---------------------------------------------------|
| `database_url`                 | str    | postgresql+asyncpg://... | Async PostgreSQL connection string                 |
| `redis_url`                    | str    | redis://localhost:6379/0 | Redis for caching and pub/sub                      |
| `encryption_key`               | str    | (required)               | Fernet key for encrypting stored API keys          |
| `environment`                  | str    | development              | "development" or "production"                      |
| `log_level`                    | str    | INFO                     | Minimum log level                                  |
| `nws_user_agent`               | str    | BozWeatherTrader/1.0     | Required by NWS API (must include contact email)   |
| `nws_rate_limit_per_second`    | float  | 1.0                      | Max NWS API calls per second                       |
| `default_max_trade_size`       | float  | 1.00                     | Default max dollars per trade                      |
| `default_daily_loss_limit`     | float  | 10.00                    | Default daily loss limit in dollars                |
| `default_max_daily_exposure`   | float  | 25.00                    | Default max total exposure per day                 |
| `default_min_ev_threshold`     | float  | 0.05                     | Minimum expected value to trigger a trade           |
| `default_cooldown_minutes`     | int    | 60                       | Cooldown after a loss before trading again          |
| `default_consecutive_loss_limit`| int   | 3                        | Max consecutive losses before halting               |
| `celery_broker_url`            | str    | redis://localhost:6379/1 | Celery task broker                                  |
| `cors_origins`                 | str    | ""                       | Extra CORS origins (comma-separated) for reverse proxy/tunnel deployments |
| `retrain_settlement_threshold` | int    | 50                       | Min settlements since last training to trigger auto-retrain |
| `retrain_brier_threshold`      | float  | 0.35                     | Brier score above which retraining is triggered |
| `retrain_max_days`             | int    | 30                       | Max days since last training before forced retrain |
| `celery_result_backend`        | str    | redis://localhost:6379/2 | Celery result storage                               |

### Environment Variable Mapping

pydantic-settings automatically maps settings to environment variables. The mapping is case-insensitive:

```bash
# .env
DATABASE_URL=postgresql+asyncpg://boz:boz@localhost:5432/boz_weather_trader
REDIS_URL=redis://localhost:6379/0
ENCRYPTION_KEY=your-fernet-key-here
ENVIRONMENT=development
LOG_LEVEL=INFO
NWS_USER_AGENT=BozWeatherTrader/1.0 (you@example.com)
NWS_RATE_LIMIT_PER_SECOND=1.0
DEFAULT_MAX_TRADE_SIZE=1.00
DEFAULT_DAILY_LOSS_LIMIT=10.00
DEFAULT_MAX_DAILY_EXPOSURE=25.00
DEFAULT_MIN_EV_THRESHOLD=0.05
DEFAULT_COOLDOWN_MINUTES=60
DEFAULT_CONSECUTIVE_LOSS_LIMIT=3
CELERY_BROKER_URL=redis://localhost:6379/1
CELERY_RESULT_BACKEND=redis://localhost:6379/2
```

---

## exceptions.py — Custom Exception Classes

All custom exceptions inherit from `BozBaseException`, which carries an optional `context` dict for structured error data. Agents should raise these instead of generic `Exception` or `ValueError`.

### Full Implementation

```python
# backend/common/exceptions.py
class BozBaseException(Exception):
    """Base exception for all Boz Weather Trader errors."""

    def __init__(self, message: str, context: dict | None = None):
        super().__init__(message)
        self.context = context or {}


class StaleDataError(BozBaseException):
    """Weather data is too old to trade on."""

    pass


class RiskLimitError(BozBaseException):
    """A risk limit would be violated by this action."""

    pass


class CooldownActiveError(BozBaseException):
    """Trading is paused due to cooldown."""

    pass


class InsufficientBalanceError(BozBaseException):
    """Not enough balance to place this trade."""

    pass


class InvalidOrderError(BozBaseException):
    """Order parameters are invalid."""

    pass
```

### Usage

```python
from backend.common.exceptions import RiskLimitError, StaleDataError

# In trading engine
if daily_loss >= settings.default_daily_loss_limit:
    raise RiskLimitError(
        "Daily loss limit reached",
        context={"current_loss": daily_loss, "limit": settings.default_daily_loss_limit},
    )

# In weather pipeline
if forecast_age_minutes > 90:
    raise StaleDataError(
        "Forecast too old for trading", context={"city": "NYC", "age_minutes": forecast_age_minutes}
    )
```

### FastAPI Exception Handler

Register a global handler in `backend/main.py` so all exceptions return consistent JSON:

```python
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from backend.common.exceptions import BozBaseException

app = FastAPI()


@app.exception_handler(BozBaseException)
async def boz_exception_handler(request: Request, exc: BozBaseException):
    return JSONResponse(
        status_code=400,
        content={
            "error": type(exc).__name__,
            "message": str(exc),
            "context": exc.context,
        },
    )
```

---

## Alembic — Database Migrations

Alembic manages all schema changes to PostgreSQL. Never modify the database schema by hand.

### Initial Setup

```bash
cd backend
alembic init alembic
```

### Configure alembic/env.py

Edit `alembic/env.py` to import your models so autogenerate can detect changes:

```python
from backend.common.models import Base

target_metadata = Base.metadata
```

### Configure alembic.ini

Set the database URL (or override via environment variable):

```ini
sqlalchemy.url = postgresql+asyncpg://boz:boz@localhost:5432/boz_weather_trader
```

### Create and Run Migrations

```bash
# Generate a migration from model changes
alembic revision --autogenerate -m "initial tables"

# Apply migrations
alembic upgrade head

# Check current migration state
alembic current

# Rollback one migration
alembic downgrade -1
```

### Important Notes

- Always review autogenerated migrations before applying them. Alembic sometimes misses renames or generates incorrect diffs.
- Keep migrations in version control. They are part of the deployment process.
- In production, migrations run as part of the deployment pipeline, not manually.

---

## middleware.py — Production Middleware

Four middleware classes registered in `backend/main.py`, applied in order (outermost first):

1. **RequestIdMiddleware** — Injects a unique request ID (`X-Request-ID` header) into every request/response. Stores in `ContextVar` for structured logging.
2. **RequestLoggingMiddleware** — Logs every HTTP request with method, path, status, and duration. Skips `/health`, `/ready`, `/metrics`.
3. **PrometheusMiddleware** — Records Prometheus HTTP metrics (request count, duration histogram, in-progress gauge). Skips `/health`, `/ready`, `/metrics`. Normalizes dynamic path segments (UUIDs, hex, numeric IDs) to `{id}` to avoid high-cardinality labels.
4. **SecurityHeadersMiddleware** — Adds OWASP security headers (X-Content-Type-Options, X-Frame-Options, HSTS, etc.).

### Key Exports

```python
from backend.common.middleware import request_id_var  # ContextVar for current request ID
from backend.common.middleware import PrometheusMiddleware, _normalize_path
```

---

## metrics.py — Prometheus Metrics Definitions

All Prometheus metric objects are module-level singletons. Import them wherever you need to instrument code.

### Metric Objects

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `APP_INFO` | Info | version, environment | Application metadata |
| `HTTP_REQUESTS_TOTAL` | Counter | method, path_template, status_code | Total HTTP requests |
| `HTTP_REQUEST_DURATION_SECONDS` | Histogram | method, path_template | HTTP request duration (custom buckets: 5ms–10s) |
| `HTTP_REQUESTS_IN_PROGRESS` | Gauge | method | Concurrent HTTP requests |
| `CELERY_TASK_TOTAL` | Counter | task_name, status | Celery task completions (success/failure/retry) |
| `CELERY_TASK_DURATION_SECONDS` | Histogram | task_name | Celery task duration (custom buckets: 0.1s–300s) |
| `TRADING_CYCLES_TOTAL` | Counter | outcome | Trading cycle outcomes (completed/skipped/timeout/error) |
| `TRADES_EXECUTED_TOTAL` | Counter | mode, city | Trades executed (auto/queued) |
| `TRADES_RISK_BLOCKED_TOTAL` | Counter | reason | Trades blocked by risk manager |
| `WEATHER_FETCHES_TOTAL` | Counter | source, city, outcome | Weather data fetch results |
| `XGB_PREDICTIONS_TOTAL` | Counter | city, status | XGBoost temperature predictions (success/error) |
| `XGB_TRAINING_DURATION_SECONDS` | Histogram | — | XGBoost model training duration |
| `ML_PREDICTIONS_TOTAL` | Counter | city, model, status | Multi-model ML predictions (per-model tracking) |
| `ML_TRAINING_DURATION_SECONDS` | Histogram | — | Multi-model training duration |
| `ML_ENSEMBLE_AGREEMENT_F` | Histogram | — | Spread (max-min) across ML model predictions |
| `ML_MODELS_AVAILABLE` | Gauge | — | Number of ML models currently loaded |
| `PORTFOLIO_SYNC_TOTAL` | Counter | outcome | Portfolio sync results (success/error) |
| `PORTFOLIO_SYNC_TRADES_CREATED` | Counter | — | Number of trades created via portfolio sync |
| `ML_RETRAIN_TRIGGERS_TOTAL` | Counter | reason | ML retraining triggers (settlement_count/brier_degradation/time_elapsed) |
| `ML_SOURCE_WEIGHTS_UPDATED_TOTAL` | Counter | — | Source weight recalculations after retraining |
| `BRACKET_CAP_BLOCKED_TOTAL` | Counter | city | Trades blocked by per-bracket position cap |
| `GUARDRAIL_BLOCKED_TOTAL` | Counter | reason | Trades blocked by guardrails (divergence_cap, yes_floor) |
| `ORDERBOOK_FALLBACK_TOTAL` | Counter | city | Times scheduler fell back to orderbook for bracket pricing |

### Usage

```python
from backend.common.metrics import WEATHER_FETCHES_TOTAL, set_app_info

# Increment a counter with labels
WEATHER_FETCHES_TOTAL.labels(source="NWS", city="NYC", outcome="success").inc()

# Set app info at startup
set_app_info(version="0.1.0", environment="production")
```

### `/metrics` Endpoint

The Prometheus scrape endpoint is mounted at `/metrics` in `backend/main.py` via `prometheus_client.make_asgi_app()`. FastAPI's `mount()` redirects `/metrics` to `/metrics/` (307).

### Celery Signal Integration

Celery task metrics are collected automatically via signals in `backend/celery_app.py` — no changes to task bodies required. Signals: `task_prerun`, `task_postrun`, `task_failure`, `task_retry`.

### Grafana Dashboards

All metrics defined here are visualized in two auto-provisioned Grafana dashboards (see `monitoring/grafana/dashboards/`):
- **API Overview** — request rate, error rate, latency percentiles, status distribution
- **Trading & Weather** — trading cycles, trades by city, risk blocks, weather fetch rates, Celery task performance

Dashboard JSON is validated by `tests/test_grafana_dashboards.py` which checks that all PromQL expressions reference metric names from this file.

---

## Rules for Agents

1. **Import from here, not from each other.** If Agent 4 needs weather data types, import from `backend.common.schemas`, not from `backend.weather`.
2. **Don't modify schemas without coordination.** Schema changes affect multiple agents. If you need a new field, add it to schemas.py and note it as optional with a default so existing code doesn't break.
3. **Use the logger.** Every module should use `get_logger("MODULE_TAG")`. Don't use `print()`.
4. **Never log secrets.** The logger should have a filter that catches and redacts anything that looks like an API key or private key.
5. **Use the custom exceptions.** Raise `BozBaseException` subclasses, not generic Python exceptions. Always include a `context` dict with relevant debugging info.
6. **Use the encryption helpers.** Never store plaintext API keys or private keys in the database. Always use `encrypt_api_key()` and `decrypt_api_key()`.
7. **Use the config singleton.** Import `settings` from `backend.common.config`. Never read environment variables directly with `os.getenv()`.
8. **Use the database dependency.** In FastAPI routes, inject the session with `Depends(get_db_session)`. In Celery tasks, use `async_session()` context manager directly.
9. **Run migrations for schema changes.** After modifying `models.py`, generate an Alembic migration and apply it. Never ALTER TABLE by hand.
10. **Type everything.** All functions must have type hints. All Pydantic models must have field types. The codebase uses `from __future__ import annotations` everywhere.
11. **Instrument with metrics.** When adding new business-critical operations, increment the appropriate Prometheus counter from `backend.common.metrics`. Keep label cardinality bounded — normalize dynamic values before using as labels.
