"""Unauthenticated client for Kalshi's PUBLIC market-data endpoints.

Kalshi serves market listings, candlesticks and the historical archive without
authentication. This client is read-only and holds no credentials. Order
placement and portfolio access stay in ``KalshiClient`` (backend/kalshi/client.py).

Settled markets move from the live endpoints to ``/historical/...`` once they
are older than ``GET /historical/cutoff`` (``market_settled_ts``). Live and
historical candle payloads use different field names (``close_dollars`` vs
``close``, ``volume_fp`` vs ``volume``). The archive layer
(backend/kalshi/archive.py) normalizes both.

Unauthenticated requests are rate-limited by Kalshi (HTTP 429 after a short
burst), so every request goes through a token bucket and 429/5xx/timeouts are
retried with exponential backoff, honoring ``Retry-After``.

Usage:
    from backend.kalshi.public_client import KalshiPublicClient

    async with KalshiPublicClient() as client:
        markets = await client.get_event_markets("KXHIGHNY-26OCT06")
        candles = await client.get_candlesticks(
            "KXHIGHNY", "KXHIGHNY-26OCT06-T70", start_ts, end_ts, historical=False
        )
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime
from typing import Any

import httpx

from backend.common.logging import get_logger
from backend.common.metrics import KALSHI_PUBLIC_REQUESTS_TOTAL
from backend.kalshi.rate_limiter import TokenBucketRateLimiter

logger = get_logger("MARKET")

PUBLIC_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# Conservative defaults: unauthenticated bursts above ~10 requests trip 429s.
DEFAULT_RATE_PER_SECOND = 2.0
DEFAULT_BURST = 2
DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF_BASE_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0
PAGE_LIMIT = 200
MAX_PAGES = 50  # Safety valve against a cursor that never terminates

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class KalshiPublicAPIError(Exception):
    """A public Kalshi request failed after all retries (or with a non-retryable status)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _endpoint_label(path: str) -> str:
    """Normalize a request path to a bounded-cardinality metric label.

    Args:
        path: Request path, e.g. "/historical/markets/KXHIGHNY-26OCT06-T70/candlesticks".

    Returns:
        A label such as "historical_candlesticks" or "markets".
    """
    hist = path.startswith("/historical")
    if path.endswith("/candlesticks"):
        return "historical_candlesticks" if hist else "candlesticks"
    if path.endswith("/cutoff"):
        return "historical_cutoff"
    return "historical_markets" if hist else "markets"


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a numeric ``Retry-After`` header (seconds), if present."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


class KalshiPublicClient:
    """Async, unauthenticated, rate-limited client for Kalshi public market data.

    Args:
        base_url: API base URL (production public data by default).
        rate_per_second: Sustained request rate for the token bucket.
        burst: Token bucket burst size.
        max_retries: Retries for 429/5xx/transport errors before giving up.
        backoff_base_seconds: Base for exponential backoff (doubles per attempt).
        http_client: Optional pre-built httpx client (used by tests).
    """

    def __init__(
        self,
        base_url: str = PUBLIC_BASE_URL,
        rate_per_second: float = DEFAULT_RATE_PER_SECOND,
        burst: int = DEFAULT_BURST,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self._limiter = TokenBucketRateLimiter(rate=rate_per_second, burst=burst)
        self._client = http_client or httpx.AsyncClient(timeout=30.0)

    async def __aenter__(self) -> KalshiPublicClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def _sleep(self, seconds: float) -> None:
        """Sleep helper (patched in tests)."""
        await asyncio.sleep(seconds)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """GET a public endpoint with rate limiting and retry/backoff.

        Args:
            path: Path relative to the base URL, starting with "/".
            params: Query parameters.

        Returns:
            The decoded JSON body.

        Raises:
            KalshiPublicAPIError: On a non-retryable status, or after retries
                are exhausted.
        """
        label = _endpoint_label(path)
        url = f"{self.base_url}{path}"
        last_error = ""
        last_status: int | None = None

        for attempt in range(self.max_retries + 1):
            await self._limiter.acquire()
            try:
                response = await self._client.get(url, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                last_status = None
                delay = None
            else:
                if response.status_code == 200:
                    KALSHI_PUBLIC_REQUESTS_TOTAL.labels(endpoint=label, outcome="success").inc()
                    return response.json()
                last_status = response.status_code
                last_error = f"HTTP {response.status_code}"
                if response.status_code not in _RETRYABLE_STATUS:
                    KALSHI_PUBLIC_REQUESTS_TOTAL.labels(endpoint=label, outcome="error").inc()
                    raise KalshiPublicAPIError(
                        f"GET {path} failed: {last_error}", status_code=response.status_code
                    )
                delay = _retry_after_seconds(response)

            if attempt >= self.max_retries:
                break

            backoff = min(
                MAX_BACKOFF_SECONDS,
                self.backoff_base_seconds * (2**attempt) * (1 + random.random() * 0.25),
            )
            # Honor Retry-After as a floor; never wait less than it asks.
            wait = max(delay, backoff) if delay is not None else backoff
            KALSHI_PUBLIC_REQUESTS_TOTAL.labels(endpoint=label, outcome="retry").inc()
            logger.warning(
                "Kalshi public request retrying",
                extra={
                    "data": {
                        "endpoint": label,
                        "attempt": attempt + 1,
                        "error": last_error,
                        "wait_seconds": round(wait, 2),
                    }
                },
            )
            await self._sleep(wait)

        KALSHI_PUBLIC_REQUESTS_TOTAL.labels(endpoint=label, outcome="error").inc()
        raise KalshiPublicAPIError(
            f"GET {path} failed after {self.max_retries + 1} attempts: {last_error}",
            status_code=last_status,
        )

    async def get_historical_cutoff(self) -> datetime:
        """Return the settled-market cutoff between live and historical endpoints.

        Markets that settled before this instant are only served by the
        ``/historical/...`` endpoints.

        Returns:
            Timezone-aware UTC datetime of ``market_settled_ts``.
        """
        body = await self._get("/historical/cutoff")
        return datetime.fromisoformat(body["market_settled_ts"].replace("Z", "+00:00")).astimezone(
            UTC
        )

    async def get_event_markets(self, event_ticker: str, historical: bool = False) -> list[dict]:
        """List every market (bracket) of one event, following pagination.

        Args:
            event_ticker: e.g. "KXHIGHNY-26OCT06" or legacy "HIGHNY-24JUL01".
            historical: Query ``/historical/markets`` instead of ``/markets``.

        Returns:
            Raw market dicts as returned by Kalshi (possibly empty).
        """
        path = "/historical/markets" if historical else "/markets"
        markets: list[dict] = []
        cursor = ""
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {"event_ticker": event_ticker, "limit": PAGE_LIMIT}
            if cursor:
                params["cursor"] = cursor
            body = await self._get(path, params)
            markets.extend(body.get("markets") or [])
            cursor = body.get("cursor") or ""
            if not cursor:
                break
        return markets

    async def get_candlesticks(
        self,
        series_ticker: str,
        market_ticker: str,
        start_ts: int,
        end_ts: int,
        period_minutes: int = 60,
        historical: bool = False,
    ) -> list[dict]:
        """Fetch OHLC candlesticks (yes_bid / yes_ask / trade price) for one market.

        Args:
            series_ticker: Series of the market (live endpoint only), e.g. "KXHIGHNY".
            market_ticker: Market ticker.
            start_ts: Window start, unix seconds.
            end_ts: Window end, unix seconds.
            period_minutes: Candle period: 1, 60 or 1440.
            historical: Use the ``/historical/markets/{ticker}/candlesticks`` endpoint.

        Returns:
            Raw candle dicts (hours with no activity may be absent).
        """
        if historical:
            path = f"/historical/markets/{market_ticker}/candlesticks"
        else:
            path = f"/series/{series_ticker}/markets/{market_ticker}/candlesticks"
        params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_minutes}
        body = await self._get(path, params)
        return body.get("candlesticks") or []
