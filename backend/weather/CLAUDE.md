# Agent 1: Weather Data Pipeline

## Your Mission

Build the weather data fetching, parsing, normalization, and storage layer. You are the foundation -- every other module depends on your data being correct, timely, and consistently formatted.

## What You Build

```
backend/weather/
├── __init__.py
├── nws.py            -> NWS API client (forecasts, CLI reports, text fetch)
├── cli_parser.py     -> NWS CLI text parser (pure, no I/O — regex extraction)
├── openmeteo.py      -> Open-Meteo API client (forecasts, historical, multi-model)
├── normalizer.py     -> Normalize data from all sources into WeatherData schema
├── scheduler.py      -> Celery tasks for scheduled data fetching + Settlement creation
├── stations.py       -> NWS station configs for each Kalshi city
├── rate_limiter.py   -> Async rate limiter for NWS API
└── exceptions.py     -> Weather-specific exceptions (StaleDataError, etc.)
```

---

## Data Sources

### NWS API (api.weather.gov)

- **No auth needed** -- just set a descriptive User-Agent header: `"BozWeatherTrader/1.0 (contact@email.com)"`
- **Rate limit:** Be respectful -- no more than 1 request per second
- **Gotcha:** NWS API occasionally returns 500 errors. Implement retry with backoff.
- **Gotcha:** Forecast grid coordinates need to be looked up once per station, then cached.

**Key endpoints:**

| Endpoint | Returns | Temperature Unit |
|----------|---------|-----------------|
| `/points/{lat},{lon}` | Grid coordinates + forecast office | N/A |
| `/gridpoints/{office}/{x},{y}/forecast` | 12-hour period forecasts (7 days) | **Fahrenheit** |
| `/gridpoints/{office}/{x},{y}` | Raw numerical forecast data (hourly) | **Celsius** |
| `/gridpoints/{office}/{x},{y}/forecast/hourly` | Hourly text forecasts | **Fahrenheit** |
| `/stations/{stationId}/observations/latest` | Current conditions | **Celsius** |

#### NWS API Response Examples

These are the actual JSON structures you will encounter. Use these to write your parsers and tests.

**Step 1 -- Grid Coordinate Lookup (call once per city, then cache forever):**

```
GET https://api.weather.gov/points/40.7828,-73.9653
```
```json
{
    "properties": {
        "gridId": "OKX",
        "gridX": 33,
        "gridY": 37,
        "forecast": "https://api.weather.gov/gridpoints/OKX/33,37/forecast",
        "forecastHourly": "https://api.weather.gov/gridpoints/OKX/33,37/forecast/hourly",
        "forecastGridData": "https://api.weather.gov/gridpoints/OKX/33,37"
    }
}
```

**Step 2a -- 12-Hour Period Forecast (human-readable, Fahrenheit):**

```
GET https://api.weather.gov/gridpoints/OKX/33,37/forecast
```
```json
{
    "properties": {
        "periods": [
            {
                "number": 1,
                "name": "Today",
                "startTime": "2026-02-17T06:00:00-05:00",
                "endTime": "2026-02-17T18:00:00-05:00",
                "isDaytime": true,
                "temperature": 55,
                "temperatureUnit": "F",
                "windSpeed": "10 to 15 mph",
                "shortForecast": "Partly Cloudy",
                "detailedForecast": "Partly cloudy, with a high near 55..."
            }
        ]
    }
}
```

To extract the high temperature for a given date, iterate `periods` and find the daytime period (`isDaytime == true`) whose `startTime` falls on the target date. The `temperature` field is already in Fahrenheit.

**Step 2b -- Raw Numerical Gridpoint Data (machine-readable, Celsius):**

```
GET https://api.weather.gov/gridpoints/OKX/33,37
```
```json
{
    "properties": {
        "maxTemperature": {
            "values": [
                {"validTime": "2026-02-17T11:00:00+00:00/PT1H", "value": 12.8}
            ]
        },
        "temperature": {
            "values": [
                {"validTime": "2026-02-17T06:00:00+00:00/PT1H", "value": 5.0},
                {"validTime": "2026-02-17T07:00:00+00:00/PT1H", "value": 6.2}
            ]
        },
        "relativeHumidity": {
            "values": [
                {"validTime": "2026-02-17T06:00:00+00:00/PT1H", "value": 75}
            ]
        }
    }
}
```

**CRITICAL: All temperature values in raw gridpoint data are in Celsius.** You MUST convert to Fahrenheit before storing as `forecast_high_f`. The `validTime` format is ISO 8601 with a duration suffix (e.g., `/PT1H` means this value is valid for 1 hour). Parse the datetime portion before the `/`.

### NWS Station Mapping (Critical for Settlement)

These are the EXACT stations Kalshi uses for settlement:

| City | Station ID | Lat/Lon | NWS Office |
|------|-----------|---------|------------|
| NYC | KNYC (Central Park) | 40.7828, -73.9653 | OKX |
| Chicago | KMDW (Midway) | 41.7868, -87.7522 | LOT |
| Miami | KMIA (Miami Intl) | 25.7959, -80.2870 | MFL |
| Austin | KAUS (Bergstrom Intl) | 30.1945, -97.6699 | EWX |

#### Station Configuration Implementation

Put this in `backend/weather/stations.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


@dataclass
class StationConfig:
    city: str
    lat: float
    lon: float
    station_id: str
    nws_office: str
    timezone: ZoneInfo
    standard_utc_offset: int  # UTC offset ignoring DST
    grid: dict | None = field(default=None)  # cached after first NWS lookup


STATION_CONFIGS: dict[str, StationConfig] = {
    "NYC": StationConfig(
        city="NYC",
        lat=40.7828,
        lon=-73.9653,
        station_id="KNYC",
        nws_office="OKX",
        timezone=ZoneInfo("America/New_York"),
        standard_utc_offset=-5,
    ),
    "CHI": StationConfig(
        city="CHI",
        lat=41.7868,
        lon=-87.7522,
        station_id="KMDW",
        nws_office="LOT",
        timezone=ZoneInfo("America/Chicago"),
        standard_utc_offset=-6,
    ),
    "MIA": StationConfig(
        city="MIA",
        lat=25.7959,
        lon=-80.2870,
        station_id="KMIA",
        nws_office="MFL",
        timezone=ZoneInfo("America/New_York"),
        standard_utc_offset=-5,
    ),
    "AUS": StationConfig(
        city="AUS",
        lat=30.1945,
        lon=-97.6699,
        station_id="KAUS",
        nws_office="EWX",
        timezone=ZoneInfo("America/Chicago"),
        standard_utc_offset=-6,
    ),
}

VALID_CITIES = list(STATION_CONFIGS.keys())
```

#### Grid Coordinate Caching

Grid coordinates are geographic and never change. Look them up once, cache forever:

```python
# In backend/weather/nws.py


async def get_grid_coordinates(city: str) -> dict:
    """Get NWS grid coordinates for a city. Cached after first call.

    Returns:
        dict with keys: office (str), x (int), y (int)
    """
    config = STATION_CONFIGS[city]
    if config.grid is not None:
        return config.grid

    data = await fetch_with_retry(
        f"https://api.weather.gov/points/{config.lat},{config.lon}",
        headers={"User-Agent": settings.nws_user_agent},
    )
    grid = {
        "office": data["properties"]["gridId"],
        "x": data["properties"]["gridX"],
        "y": data["properties"]["gridY"],
    }
    config.grid = grid  # cache in memory

    # Also persist to database so cache survives restarts
    await persist_grid_coordinates(city, grid)

    return grid


async def build_forecast_url(city: str) -> str:
    """Build the NWS forecast URL for a city."""
    grid = await get_grid_coordinates(city)
    return f"https://api.weather.gov/gridpoints/{grid['office']}/{grid['x']},{grid['y']}/forecast"


async def build_gridpoint_url(city: str) -> str:
    """Build the NWS raw gridpoint data URL for a city."""
    grid = await get_grid_coordinates(city)
    return f"https://api.weather.gov/gridpoints/{grid['office']}/{grid['x']},{grid['y']}"
```

### Open-Meteo API (api.open-meteo.com)

- **No auth needed**
- **Key endpoints:**
  - `/v1/forecast` -- up to 16-day hourly forecasts, multiple models
  - `/v1/archive` -- historical weather data (ERA5 reanalysis, back to 1940)
  - `/v1/ecmwf` -- ECMWF model specifically
- **Models to fetch:** GFS, ECMWF (IFS), ICON, GEM, JMA -- use `&models=` parameter
- **Key variables:** `temperature_2m_max`, `temperature_2m_min`, `cloudcover`, `windspeed_10m`, `relative_humidity_2m`, `dewpoint_2m`

#### Open-Meteo API Response Example

```
GET https://api.open-meteo.com/v1/forecast?latitude=40.7828&longitude=-73.9653&daily=temperature_2m_max,temperature_2m_min&models=gfs_seamless,ecmwf_ifs025,icon_seamless&temperature_unit=fahrenheit&timezone=America/New_York
```
```json
{
    "daily": {
        "time": ["2026-02-17", "2026-02-18"],
        "temperature_2m_max": [55.2, 52.1],
        "temperature_2m_min": [38.5, 35.2]
    },
    "daily_units": {
        "temperature_2m_max": "\u00b0F"
    }
}
```

**IMPORTANT:** When requesting multiple models via `&models=gfs_seamless,ecmwf_ifs025,icon_seamless`, each model returns its own separate arrays. The response structure nests model data differently. You must parse each model's forecast individually. Always request `&temperature_unit=fahrenheit` so you do not need to convert.

#### Open-Meteo Client Implementation Notes

```python
# In backend/weather/openmeteo.py

OPENMETEO_MODELS = ["gfs_seamless", "ecmwf_ifs025", "icon_seamless"]


async def fetch_openmeteo_forecast(city: str) -> list[WeatherData]:
    """Fetch forecasts from Open-Meteo for all configured models.

    Returns one WeatherData per model per forecast day.
    """
    config = STATION_CONFIGS[city]
    params = {
        "latitude": config.lat,
        "longitude": config.lon,
        "daily": "temperature_2m_max,temperature_2m_min",
        "models": ",".join(OPENMETEO_MODELS),
        "temperature_unit": "fahrenheit",
        "timezone": str(config.timezone),
        "forecast_days": 7,
    }
    data = await fetch_with_retry(
        "https://api.open-meteo.com/v1/forecast",
        params=params,
    )
    # Parse each model's results into WeatherData objects
    # ...
```

---

## Temperature Unit Conversion

This is a common source of bugs. Follow these rules strictly:

```python
def celsius_to_fahrenheit(c: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return (c * 9 / 5) + 32


def fahrenheit_to_celsius(f: float) -> float:
    """Convert Fahrenheit to Celsius."""
    return (f - 32) * 5 / 9
```

**Conversion Rules by Source:**

| Source | Endpoint | Unit Returned | Action |
|--------|----------|---------------|--------|
| NWS | `/gridpoints/{o}/{x},{y}/forecast` (periods) | **Fahrenheit** | Use directly |
| NWS | `/gridpoints/{o}/{x},{y}` (raw numerical) | **Celsius** | MUST convert to F |
| NWS | `/stations/{id}/observations/latest` | **Celsius** | MUST convert to F |
| Open-Meteo | `/v1/forecast` with `&temperature_unit=fahrenheit` | **Fahrenheit** | Use directly |
| Open-Meteo | `/v1/forecast` without unit param | **Celsius** | MUST convert to F |

**Always request `&temperature_unit=fahrenheit` from Open-Meteo to avoid needing conversion.**

---

## Timezone Handling (CRITICAL)

Kalshi weather markets settle using **LOCAL STANDARD TIME (LST)**, not daylight saving time. This is one of the trickiest parts of the system.

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


CITY_TIMEZONES: dict[str, ZoneInfo] = {
    "NYC": ZoneInfo("America/New_York"),
    "CHI": ZoneInfo("America/Chicago"),
    "MIA": ZoneInfo("America/New_York"),
    "AUS": ZoneInfo("America/Chicago"),
}

# UTC offsets for LOCAL STANDARD TIME (ignoring DST)
STANDARD_UTC_OFFSETS: dict[str, int] = {
    "NYC": -5,  # EST
    "CHI": -6,  # CST
    "MIA": -5,  # EST
    "AUS": -6,  # CST
}


def get_standard_time_now(city: str) -> datetime:
    """Get the current time in the city's LOCAL STANDARD TIME.

    This ignores daylight saving time, which is what Kalshi uses
    for settlement.
    """
    offset = STANDARD_UTC_OFFSETS[city]
    standard_tz = timezone(timedelta(hours=offset))
    return datetime.now(timezone.utc).astimezone(standard_tz)


def get_settlement_date(city: str) -> str:
    """Get today's settlement date in YYYY-MM-DD format.

    Uses local standard time to determine "today".
    """
    return get_standard_time_now(city).strftime("%Y-%m-%d")


def is_forecast_for_today(forecast_date: str, city: str) -> bool:
    """Check if a forecast date matches today's settlement date."""
    return forecast_date == get_settlement_date(city)
```

**Why this matters:**

- During DST (March-November), clocks shift forward by 1 hour.
- EST (UTC-5) becomes EDT (UTC-4), CST (UTC-6) becomes CDT (UTC-5).
- But Kalshi settlement still uses standard time (EST/CST).
- This means during DST, the settlement "day" runs from:
  - 1:00 AM EDT (day of) to 12:59 AM EDT (next day)
  - Which equals 12:00 AM EST to 11:59 PM EST
- The NWS CLI report handles this correctly because it reports based on the station's standard time.
- But when checking "is this forecast for today's market?", you MUST use standard time, not local wall-clock time.

---

## Rate Limiter Implementation

NWS asks for no more than 1 request per second. Put this in `backend/weather/rate_limiter.py`:

```python
from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """Async rate limiter using a simple token bucket approach."""

    def __init__(self, calls_per_second: float = 1.0) -> None:
        self.min_interval = 1.0 / calls_per_second
        self.last_call = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a request is allowed under the rate limit."""
        async with self._lock:
            now = time.monotonic()
            wait_time = self.min_interval - (now - self.last_call)
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            self.last_call = time.monotonic()


# Module-level instances
nws_limiter = RateLimiter(calls_per_second=1.0)
openmeteo_limiter = RateLimiter(calls_per_second=5.0)  # Open-Meteo is more lenient
```

---

## HTTP Client with Retry Logic

All external API calls must use retry with exponential backoff. This is the core fetch function used by both NWS and Open-Meteo clients:

```python
# In backend/weather/nws.py (or a shared http_client.py)

from __future__ import annotations

import asyncio

import httpx

from backend.common.logging import get_logger
from backend.config import settings
from backend.weather.rate_limiter import nws_limiter

logger = get_logger("WEATHER")


async def fetch_with_retry(
    url: str,
    max_retries: int = 3,
    headers: dict | None = None,
    params: dict | None = None,
) -> dict:
    """Fetch a URL with exponential backoff retry.

    Args:
        url: The URL to fetch.
        max_retries: Maximum number of retries after the initial attempt.
        headers: Optional HTTP headers.
        params: Optional query parameters.

    Returns:
        Parsed JSON response as a dict.

    Raises:
        httpx.HTTPStatusError: If all retries are exhausted on HTTP errors.
        httpx.RequestError: If all retries are exhausted on network errors.
    """
    default_headers = {"User-Agent": settings.nws_user_agent}
    if headers:
        default_headers.update(headers)

    for attempt in range(max_retries + 1):
        try:
            await nws_limiter.acquire()
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    url,
                    headers=default_headers,
                    params=params,
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 500 and attempt < max_retries:
                wait = 2**attempt  # 1s, 2s, 4s
                logger.warning(
                    "NWS returned 500, retrying",
                    extra={
                        "data": {
                            "url": url,
                            "attempt": attempt + 1,
                            "wait_seconds": wait,
                        }
                    },
                )
                await asyncio.sleep(wait)
            else:
                logger.error(
                    "HTTP error fetching URL",
                    extra={
                        "data": {
                            "url": url,
                            "status_code": e.response.status_code,
                            "attempts": attempt + 1,
                        }
                    },
                )
                raise
        except httpx.RequestError as e:
            if attempt < max_retries:
                wait = 2**attempt
                logger.warning(
                    "Network error, retrying",
                    extra={
                        "data": {
                            "url": url,
                            "error": str(e),
                            "attempt": attempt + 1,
                            "wait_seconds": wait,
                        }
                    },
                )
                await asyncio.sleep(wait)
            else:
                logger.error(
                    "Network error fetching URL, all retries exhausted",
                    extra={"data": {"url": url, "error": str(e)}},
                )
                raise
```

---

## Output Schema

All weather data you produce MUST conform to the `WeatherData` schema defined in `backend/common/schemas.py`. This is the contract between you and Agent 3 (Prediction Engine).

Key fields:

- `city`: str (one of: "NYC", "CHI", "MIA", "AUS")
- `date`: date (the day being forecast)
- `forecast_high_f`: float (predicted high in Fahrenheit)
- `source`: str (e.g., "NWS", "Open-Meteo:GFS", "Open-Meteo:ECMWF")
- `model_run_timestamp`: datetime (when this forecast was generated)
- `raw_data`: dict (full raw response for audit trail)
- `fetched_at`: datetime (when we fetched it)

---

## Scheduling

Use Celery tasks for all scheduled work. Put these in `backend/weather/scheduler.py`:

| Schedule | Task | Purpose |
|----------|------|---------|
| Every 30 minutes | `fetch_all_forecasts` | Fetch latest NWS + Open-Meteo forecasts for all 4 cities |
| 06:00 AM ET daily | `full_data_refresh` | Full data refresh for the day's markets |
| 08:00 AM ET daily (D+1) | `fetch_cli_reports` | Fetch NWS CLI report (settlement data) |

#### Celery Task Implementation

```python
# backend/weather/scheduler.py
from __future__ import annotations

from asgiref.sync import async_to_sync
from celery import shared_task

from backend.common.logging import get_logger
from backend.weather.nws import fetch_nws_forecast
from backend.weather.openmeteo import fetch_openmeteo_forecast
from backend.weather.stations import VALID_CITIES

logger = get_logger("WEATHER")


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def fetch_all_forecasts(self):
    """Fetch forecasts from all sources for all cities.

    Runs every 30 minutes via Celery beat. Errors for individual
    city/source combinations are logged but do not fail the entire task.
    """
    for city in VALID_CITIES:
        try:
            nws_data = async_to_sync(fetch_nws_forecast)(city)
            logger.info(
                "Fetched NWS forecast",
                extra={
                    "data": {
                        "city": city,
                        "high_f": nws_data.forecast_high_f,
                    }
                },
            )
        except Exception as exc:
            logger.error(
                "NWS fetch failed",
                extra={"data": {"city": city, "error": str(exc)}},
            )
            # Don't retry the whole task -- just skip this city/source

        try:
            om_data = async_to_sync(fetch_openmeteo_forecast)(city)
            logger.info(
                "Fetched Open-Meteo forecast",
                extra={
                    "data": {
                        "city": city,
                        "high_f": om_data[0].forecast_high_f if om_data else None,
                    }
                },
            )
        except Exception as exc:
            logger.error(
                "Open-Meteo fetch failed",
                extra={"data": {"city": city, "error": str(exc)}},
            )


@shared_task(bind=True, max_retries=3, default_retry_delay=120)
def fetch_cli_reports(self):
    """Fetch NWS Daily Climate Reports for settlement verification.

    Runs at 8 AM ET daily (D+1). The CLI report contains the official
    high temperature used by Kalshi for settlement.
    """
    for city in VALID_CITIES:
        try:
            # Implementation: fetch CLI report from NWS
            pass
        except Exception as exc:
            logger.error(
                "CLI report fetch failed",
                extra={"data": {"city": city, "error": str(exc)}},
            )
```

#### Celery Beat Schedule

Add this to your Celery configuration (e.g., `backend/celeryconfig.py`):

```python
from celery.schedules import crontab

beat_schedule = {
    "fetch-forecasts-every-30-min": {
        "task": "backend.weather.scheduler.fetch_all_forecasts",
        "schedule": crontab(minute="*/30"),
    },
    "full-data-refresh-morning": {
        "task": "backend.weather.scheduler.fetch_all_forecasts",
        "schedule": crontab(hour=6, minute=0),  # 6 AM ET
    },
    "fetch-cli-report-morning": {
        "task": "backend.weather.scheduler.fetch_cli_reports",
        "schedule": crontab(hour=8, minute=0),  # 8 AM ET
    },
}
```

---

## Staleness Detection

- Track `fetched_at` timestamp for all data
- If the newest forecast for a city is > 2 hours old, raise `StaleDataError`
- The trading engine will pause trading if data is stale

```python
# In backend/weather/exceptions.py
from __future__ import annotations


class WeatherError(Exception):
    """Base exception for all weather module errors."""


class StaleDataError(WeatherError):
    """Raised when the newest forecast for a city is too old."""

    def __init__(self, city: str, age_minutes: float) -> None:
        self.city = city
        self.age_minutes = age_minutes
        super().__init__(
            f"Weather data for {city} is {age_minutes:.0f} minutes old (threshold: 120 minutes)"
        )


class FetchError(WeatherError):
    """Raised when an API fetch fails after all retries."""


class ParseError(WeatherError):
    """Raised when API response has unexpected structure."""
```

---

## Normalizer

The normalizer converts raw API responses into the standard `WeatherData` schema. Put this in `backend/weather/normalizer.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

from backend.common.schemas import WeatherData
from backend.weather.stations import STATION_CONFIGS


def normalize_nws_forecast(city: str, raw_response: dict) -> WeatherData | None:
    """Normalize NWS period forecast response into WeatherData.

    Extracts the daytime high temperature from the forecast periods.
    Returns None if no daytime period is found for the target date.
    """
    periods = raw_response.get("properties", {}).get("periods", [])
    for period in periods:
        if period.get("isDaytime") and period.get("temperatureUnit") == "F":
            # Parse the date from startTime
            start = datetime.fromisoformat(period["startTime"])
            return WeatherData(
                city=city,
                date=start.date(),
                forecast_high_f=float(period["temperature"]),
                source="NWS",
                model_run_timestamp=datetime.now(timezone.utc),
                raw_data=raw_response,
                fetched_at=datetime.now(timezone.utc),
            )
    return None


def normalize_nws_gridpoint(city: str, raw_response: dict) -> WeatherData | None:
    """Normalize NWS raw gridpoint data into WeatherData.

    CRITICAL: Raw gridpoint temperatures are in Celsius. Must convert.
    """
    from backend.weather.normalizer import celsius_to_fahrenheit

    max_temps = raw_response.get("properties", {}).get("maxTemperature", {}).get("values", [])
    if not max_temps:
        return None

    first = max_temps[0]
    valid_time_str = first["validTime"].split("/")[0]  # strip duration
    valid_time = datetime.fromisoformat(valid_time_str)
    temp_c = first["value"]
    temp_f = celsius_to_fahrenheit(temp_c)

    return WeatherData(
        city=city,
        date=valid_time.date(),
        forecast_high_f=round(temp_f, 1),
        source="NWS:gridpoint",
        model_run_timestamp=datetime.now(timezone.utc),
        raw_data=raw_response,
        fetched_at=datetime.now(timezone.utc),
    )
```

---

## Testing Requirements

Your tests go in `tests/weather/`:

- `test_nws.py` -- mock NWS API responses, test parsing, error handling, retries
- `test_openmeteo.py` -- mock Open-Meteo responses, test multi-model parsing
- `test_normalizer.py` -- test that raw data correctly maps to WeatherData schema
- `test_stations.py` -- test station config correctness
- `test_scheduler.py` -- test Celery task scheduling logic
- `test_rate_limiter.py` -- test rate limiter timing behavior

**Critical test cases:**

- NWS returns 500 -> retry logic kicks in (verify 3 retries with exponential backoff)
- NWS returns unexpected JSON structure -> `ParseError`, not crash
- NWS returns 500 three times in a row -> `FetchError` raised after exhausting retries
- Open-Meteo model missing from response -> handle gracefully, return data for models that are present
- Temperature in Celsius from raw gridpoint -> correctly converted to Fahrenheit (12.8C = 55.04F)
- Temperature in Fahrenheit from forecast periods -> used directly without conversion
- Timezone handling for each city (especially LST vs DST) -- verify `get_settlement_date` returns correct date at DST boundary
- Grid coordinate caching -> second call for same city does NOT make an HTTP request
- Rate limiter -> two rapid calls have at least 1 second gap
- Staleness detection -> `StaleDataError` raised when data is older than 2 hours

#### Example Test Using the API Response Shapes Above

```python
# tests/weather/test_nws.py
import pytest
from unittest.mock import AsyncMock, patch

from backend.weather.normalizer import normalize_nws_forecast, celsius_to_fahrenheit


NWS_FORECAST_RESPONSE = {
    "properties": {
        "periods": [
            {
                "number": 1,
                "name": "Today",
                "startTime": "2026-02-17T06:00:00-05:00",
                "endTime": "2026-02-17T18:00:00-05:00",
                "isDaytime": True,
                "temperature": 55,
                "temperatureUnit": "F",
                "windSpeed": "10 to 15 mph",
                "shortForecast": "Partly Cloudy",
                "detailedForecast": "Partly cloudy, with a high near 55...",
            }
        ]
    }
}

NWS_GRIDPOINT_RESPONSE = {
    "properties": {
        "maxTemperature": {
            "values": [{"validTime": "2026-02-17T11:00:00+00:00/PT1H", "value": 12.8}]
        }
    }
}


def test_normalize_nws_forecast():
    result = normalize_nws_forecast("NYC", NWS_FORECAST_RESPONSE)
    assert result is not None
    assert result.city == "NYC"
    assert result.forecast_high_f == 55.0
    assert result.source == "NWS"


def test_celsius_to_fahrenheit():
    assert celsius_to_fahrenheit(0) == 32.0
    assert celsius_to_fahrenheit(100) == 212.0
    assert abs(celsius_to_fahrenheit(12.8) - 55.04) < 0.01


def test_normalize_nws_gridpoint_converts_celsius():
    from backend.weather.normalizer import normalize_nws_gridpoint

    result = normalize_nws_gridpoint("NYC", NWS_GRIDPOINT_RESPONSE)
    assert result is not None
    assert abs(result.forecast_high_f - 55.0) < 0.1  # 12.8C ~ 55.04F
    assert result.source == "NWS:gridpoint"
```

---

## Complete Build Checklist

When building this module, implement in this order:

1. **`exceptions.py`** -- Define `WeatherError`, `StaleDataError`, `FetchError`, `ParseError`
2. **`stations.py`** -- Define `StationConfig` dataclass and `STATION_CONFIGS` dict
3. **`rate_limiter.py`** -- Implement `RateLimiter` class with `nws_limiter` and `openmeteo_limiter` instances
4. **`nws.py`** -- Implement `fetch_with_retry`, `get_grid_coordinates`, `fetch_nws_forecast` (periods), `fetch_nws_gridpoint` (raw)
5. **`openmeteo.py`** -- Implement `fetch_openmeteo_forecast` for all configured models
6. **`normalizer.py`** -- Implement `celsius_to_fahrenheit`, `normalize_nws_forecast`, `normalize_nws_gridpoint`, `normalize_openmeteo`
7. **`scheduler.py`** -- Implement Celery tasks `fetch_all_forecasts` and `fetch_cli_reports`
8. **`__init__.py`** -- Export public API: `fetch_nws_forecast`, `fetch_openmeteo_forecast`, `STATION_CONFIGS`, `VALID_CITIES`
9. **Tests** -- Write all test files, using the JSON response examples in this document as mock data
10. **Run `pytest`** -- All tests must pass before this module is considered complete
