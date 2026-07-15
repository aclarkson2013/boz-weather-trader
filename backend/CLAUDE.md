# Backend — Python Conventions

## Overview

The backend is a Python FastAPI application. All backend modules live under `backend/`. Shared code (schemas, config, database, logging) lives in `backend/common/`.

> **Before changing prediction (`prediction/`) or trading (`trading/`) logic:** read `docs/ALGO_CHANGELOG.md` (repo root `docs/`) — it records every past change to how the bot predicts/decides trades and its measured performance impact. Add a row there when your change alters prediction/probability/EV/sizing/risk/execution behavior.

## Python Standards

- **Version:** Python 3.11+
- **Package Manager:** pip with `requirements.txt` (or Poetry if preferred)
- **Linter/Formatter:** ruff (replaces black, isort, flake8)
- **Type Checking:** All functions must have type hints. Use `from __future__ import annotations` at top of every file.
- **Docstrings:** Google style docstrings on all public functions and classes.

## Project Structure

```
backend/
├── main.py              → FastAPI app entry point, router registration, /metrics mount
├── celery_app.py        → Celery configuration, beat schedule, task signal instrumentation
├── common/
│   ├── schemas.py       → Pydantic models for ALL cross-module data
│   ├── database.py      → SQLAlchemy setup, session management
│   ├── models.py        → SQLAlchemy ORM models (database tables)
│   ├── logging.py       → Structured logging setup
│   ├── config.py        → App settings via pydantic-settings (reads .env)
│   ├── encryption.py    → AES-256 encryption helpers for API key storage
│   ├── exceptions.py    → Custom exception classes
│   ├── middleware.py    → Production middleware (request ID, logging, Prometheus, security headers, smart Cache-Control)
│   └── metrics.py       → Centralized Prometheus metric definitions (counters, histograms, gauges)
├── weather/             → Agent 1: Weather data pipeline
├── kalshi/              → Agent 2: Kalshi API client (auth, orders, markets, WS feed, cache)
├── prediction/          → Agent 3: Prediction engine (ensemble + multi-model ML + brackets + accuracy tracking + auto-retrain + source weights)
├── trading/             → Agent 4: Trading engine (incl. portfolio sync, bracket cap, consecutive loss toggle)
├── backtesting/         → Backtesting engine (sync simulation, reuses trading pure functions)
│   ├── schemas.py       → BacktestConfig, BacktestResult, BacktestDay, SimulatedTrade, CityStats, KellyStats
│   ├── risk_sim.py      → In-memory BacktestRiskManager (bankroll, daily limits, consecutive losses)
│   ├── data_loader.py   → Synthetic price/ticker generation, prediction grouping/filtering
│   ├── engine.py        → Day-by-day simulation loop (reuses scan_all_brackets, _did_bracket_win, estimate_fees)
│   ├── metrics.py       → Win rate, ROI, Sharpe ratio, max drawdown, per-city stats, Kelly effectiveness
│   └── exceptions.py    → BacktestError, InsufficientDataError
├── websocket/           → Real-time event streaming (Redis pub/sub → WebSocket → browser)
│   ├── events.py        → WebSocketEvent model, publish_event() async + publish_event_safe() (async, never raises) + publish_event_sync() (sync wrapper)
│   ├── manager.py       → ConnectionManager singleton (tracks WS connections, broadcasts)
│   ├── subscriber.py    → redis_subscriber() + log_subscriber() — asyncio tasks bridging Redis pub/sub → WS + DB
│   └── router.py        → FastAPI WebSocket endpoint at /ws
└── api/                 → FastAPI route handlers (auth, dashboard, dashboard_stats, settings, trades, training, version + self-update, etc.)
```

### Monitoring (sibling directory)
```
monitoring/
├── prometheus/
│   ├── prometheus.yml            → Scrape config, rule_files, alertmanager target
│   └── rules/                    → Alert rule YAML files (18 rules, 6 groups)
│       ├── http.yml              → HighErrorRate, SlowResponses, HighConcurrency
│       ├── celery.yml            → TaskFailureRateHigh, TaskDurationHigh, TradingCycleMissing
│       ├── trading.yml           → RiskBlocksHigh, NoTradesExecuted, TradingCycleErrors
│       ├── weather.yml           → FetchFailureRateHigh, NoWeatherFetches, AllSourcesFailing
│       ├── targets.yml           → BackendDown, WebSocketNoConnections
│       └── kalshi_ws.yml         → KalshiWSFeedDisconnected, KalshiWSFeedStalled, KalshiWSReconnectsHigh
├── alertmanager/
│   └── alertmanager.yml          → Webhook routing, severity-based repeat, inhibit rules
└── grafana/
    ├── provisioning/             → Auto-provisioned datasources + dashboard provider
    └── dashboards/               → API Overview + Trading & Weather dashboard JSON
```

## Key Conventions

### Pydantic Schemas (backend/common/schemas.py)
- ALL data that crosses module boundaries must be a Pydantic model
- Agents should NOT import classes from other agent modules directly
- Use schema validation to catch data issues at module boundaries

### Database
- SQLAlchemy 2.0+ with async support
- Alembic for migrations
- All database models in `backend/common/models.py`
- Use async sessions for all database operations

### API Endpoints
- FastAPI routers, one per domain (weather, trading, settings, etc.)
- All endpoints return Pydantic models (auto-generates OpenAPI spec)
- Use dependency injection for database sessions, auth, etc.
- Meaningful HTTP status codes (don't just return 200 for everything)
- Version endpoints in `api/version.py`: `GET /api/version` (current + update check), `POST /api/version/update` (triggers updater sidecar), `GET /api/version/update/status` (polls update progress)
- Update schemas in `api/response_schemas.py`: `UpdateTriggerResponse`, `UpdateStatus`
- Updater config in `common/config.py`: `updater_url` (sidecar HTTP address), `updater_secret` (shared secret for auth)

### Async
- Use `async/await` for all I/O operations (HTTP calls, database, WebSocket)
- Use `httpx` for async HTTP client (not `requests`)
- Celery tasks for scheduled work (data fetching, model runs, trade execution)

### Error Handling
- Custom exceptions in `backend/common/exceptions.py`
- FastAPI exception handlers for consistent error responses
- All external API calls wrapped in try/except with retry logic
- Log errors with full context (but NEVER log secrets)

### Testing
- Framework: pytest + pytest-asyncio + pytest-httpx
- Fixtures in `conftest.py` at each test directory level
- Mock all external APIs using `pytest-httpx` or `unittest.mock`
- Database tests use a test database (separate from production)
- Every public function needs at least one test
- Test file naming: `test_{module_name}.py`

### Logging
- Import logger from `backend.common.logging`
- Every module gets its own logger with a module tag
- Example: `logger = get_logger("WEATHER")`
- Log structured data as a dict in the extra parameter
