# Agent 2: Kalshi API Client

## Your Mission

Build a robust, well-tested Kalshi API client that handles authentication (RSA signing), market data fetching, order placement, position management, and WebSocket connections. This is the bridge between our bot and real money — correctness is critical.

## What You Build

```
backend/kalshi/
├── __init__.py
├── auth.py           -> RSA key management, request signing
├── client.py         -> KalshiClient class (REST API wrapper)
├── websocket.py      -> WebSocket client for real-time data (supports demo URL)
├── markets.py        -> Market discovery, bracket parsing, ticker mapping
├── orders.py         -> Order construction, validation, placement
├── models.py         -> Kalshi-specific Pydantic models (responses, requests)
├── rate_limiter.py   -> Token bucket rate limiter
├── cache.py          -> Redis cache helpers for real-time market prices
├── market_feed.py    -> MarketFeedConsumer — persistent WS feed + Redis cache
└── exceptions.py     -> Kalshi-specific exceptions (AuthError, OrderRejected, etc.)
```

---

## Kalshi API Details

### Authentication (RSA-PSS Signing)

- User provides: **API Key ID** (string) + **RSA Private Key** (PEM format)
- Each API request must be signed with the private key
- Signature process:
  1. Build signing string: `timestamp_ms + HTTP_METHOD + path` (strip query params from path!)
  2. Sign with **RSA-PSS + MGF1(SHA-256), salt_length=DIGEST_LENGTH** (per Kalshi official SDK)
  3. Base64-encode the signature
  4. Include in headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP`
- **EC key fallback:** The auth module also supports EC keys (ECDSA + SHA-256) but Kalshi docs only reference RSA. EC support is kept as a defensive fallback.
- **CRITICAL SECURITY:** Private keys are stored AES-256 encrypted. Your auth module receives the decrypted key in-memory only. NEVER log, print, or include keys in error messages.

#### Reference Implementation: `backend/kalshi/auth.py`

```python
# backend/kalshi/auth.py
from __future__ import annotations

import base64
import time
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from backend.common.logging import get_logger

logger = get_logger("AUTH")


class KalshiAuth:
    def __init__(self, api_key_id: str, private_key_pem: str):
        self.api_key_id = api_key_id
        self.private_key = serialization.load_pem_private_key(
            private_key_pem.encode(), password=None
        )

    def sign_request(self, method: str, path: str, timestamp_ms: int | None = None) -> dict:
        """Generate authentication headers for a Kalshi API request.

        Args:
            method: HTTP method (GET, POST, DELETE)
            path: Request path (e.g., /trade-api/v2/markets)
            timestamp_ms: Unix timestamp in milliseconds (auto-generated if None)

        Returns:
            Dict of headers to include in the request.
        """
        ts = timestamp_ms or int(time.time() * 1000)
        ts_str = str(ts)

        # Signing string: timestamp + method + path
        message = ts_str + method.upper() + path

        signature = self.private_key.sign(
            message.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

        sig_b64 = base64.b64encode(signature).decode()

        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
            "KALSHI-ACCESS-TIMESTAMP": ts_str,
            "Content-Type": "application/json",
        }
```

**Key details:**
- The signing algorithm is **RSA-PSS** with MGF1(SHA-256) and salt_length=DIGEST_LENGTH. This matches the Kalshi official SDK.
- Query parameters MUST be stripped from the path before signing (e.g., `/trade-api/v2/events?limit=5` → sign as `/trade-api/v2/events`).
- The `path` passed to `sign_request` must be the full path starting from `/trade-api/...`, not just the endpoint suffix.
- Timestamps are in **milliseconds** (multiply `time.time()` by 1000).

---

### CRITICAL: Prices Are in CENTS

**All prices in the Kalshi API are integers representing cents, NOT dollars.**

| What you mean | API value | Field name   |
|---------------|-----------|--------------|
| $0.22         | `22`      | `yes_price`  |
| $0.99         | `99`      | `yes_price`  |
| $0.01         | `1`       | `yes_price`  |

Conversion helpers:
```python
def dollars_to_cents(price: float) -> int:
    """Convert dollar price to Kalshi API cents. Rounds to nearest cent."""
    return int(round(price * 100))


def cents_to_dollars(cents: int) -> float:
    """Convert Kalshi API cents to dollar price."""
    return cents / 100.0
```

If you send `"yes_price": 0.22` instead of `"yes_price": 22`, the API will reject the order or (worse) place it at a wildly wrong price. **Always validate that prices going to the API are integers in range [1, 99].**

---

### REST API

- **Base URL (production):** `https://api.elections.kalshi.com/trade-api/v2`
- **Base URL (demo):** `https://demo-api.kalshi.co/trade-api/v2`
- **Key endpoints:**
  - `GET /events` — List events (filter with `?series_ticker=KXHIGHNY`)
  - `GET /events/{event_ticker}` — Get event details + child markets
  - `GET /markets` — List markets with filters
  - `GET /markets/{ticker}` — Get specific market (bracket) details
  - `GET /markets/{ticker}/orderbook` — Get current orderbook
  - `POST /portfolio/orders` — Place an order
  - `GET /portfolio/orders` — List user's orders
  - `DELETE /portfolio/orders/{order_id}` — Cancel an order
  - `GET /portfolio/positions` — Get current positions
  - `GET /portfolio/balance` — Get account balance (in cents!)
  - `GET /portfolio/settlements` — Get settlement history
  - `GET /exchange/status` — Exchange status

#### API Response Examples

**GET /trade-api/v2/events?series_ticker=KXHIGHNY** — List weather events for NYC:
```json
{
    "events": [
        {
            "event_ticker": "KXHIGHNY-26FEB18",
            "series_ticker": "KXHIGHNY",
            "title": "Highest temperature in NYC on Feb 18?",
            "category": "Climate",
            "status": "active",
            "markets": [
                "KXHIGHNY-26FEB18-T48",
                "KXHIGHNY-26FEB18-T50",
                "KXHIGHNY-26FEB18-T52",
                "KXHIGHNY-26FEB18-T54",
                "KXHIGHNY-26FEB18-T56",
                "KXHIGHNY-26FEB18-T58"
            ]
        }
    ],
    "cursor": null
}
```

**GET /trade-api/v2/markets/KXHIGHNY-26FEB18-T52** — Middle bracket:
```json
{
    "market": {
        "ticker": "KXHIGHNY-26FEB18-T52",
        "event_ticker": "KXHIGHNY-26FEB18",
        "title": "NYC high temp: 52\u00b0F to 53\u00b0F?",
        "subtitle": "Will the highest temperature in NYC on Feb 18 be between 52\u00b0F and 53\u00b0F?",
        "status": "active",
        "yes_bid": 22,
        "yes_ask": 25,
        "no_bid": 74,
        "no_ask": 78,
        "last_price": 23,
        "volume": 1542,
        "open_interest": 823,
        "floor_strike": 52.0,
        "cap_strike": 53.99,
        "result": null,
        "close_time": "2026-02-18T23:00:00Z",
        "expiration_time": "2026-02-19T14:00:00Z"
    }
}
```

**GET /trade-api/v2/markets/KXHIGHNY-26FEB18-T48** — Bottom edge bracket (catch-all below):
```json
{
    "market": {
        "ticker": "KXHIGHNY-26FEB18-T48",
        "title": "NYC high temp: Below 48\u00b0F?",
        "floor_strike": null,
        "cap_strike": 47.99,
        "yes_bid": 5,
        "yes_ask": 8,
        "status": "active"
    }
}
```

**GET /trade-api/v2/markets/KXHIGHNY-26FEB18-T58** — Top edge bracket (catch-all above):
```json
{
    "market": {
        "ticker": "KXHIGHNY-26FEB18-T58",
        "title": "NYC high temp: 58\u00b0F or above?",
        "floor_strike": 58.0,
        "cap_strike": null,
        "yes_bid": 10,
        "yes_ask": 14,
        "status": "active"
    }
}
```

**POST /trade-api/v2/portfolio/orders** — Place an order:
```json
// Request body:
{
    "ticker": "KXHIGHNY-26FEB18-T52",
    "action": "buy",
    "side": "yes",
    "type": "limit",
    "count": 1,
    "yes_price": 22
}
// NOTE: yes_price is in CENTS (22 = $0.22). Not dollars!

// Response:
{
    "order": {
        "order_id": "abc-123-def",
        "ticker": "KXHIGHNY-26FEB18-T52",
        "action": "buy",
        "side": "yes",
        "type": "limit",
        "count": 1,
        "yes_price": 22,
        "status": "resting",
        "created_time": "2026-02-17T10:05:00Z"
    }
}
```

**GET /trade-api/v2/portfolio/balance**:
```json
{
    "balance": 50000
}
// NOTE: 50000 cents = $500.00. Convert with balance / 100.
```

---

### Weather Event Ticker Patterns

```python
# Series tickers for weather markets (city -> series)
WEATHER_SERIES_TICKERS = {
    "NYC": "KXHIGHNY",
    "CHI": "KXHIGHCHI",
    "MIA": "KXHIGHMIA",
    "AUS": "KXHIGHAUS",
}

# Reverse lookup (series -> city)
SERIES_TO_CITY = {v: k for k, v in WEATHER_SERIES_TICKERS.items()}

# Event ticker format: {series}-{YY}{MON}{DD}
# Example: KXHIGHNY-26FEB18 = NYC high temp for Feb 18, 2026
#          KXHIGHCHI-26MAR05 = Chicago high temp for Mar 5, 2026

# Market (bracket) ticker format: {event}-T{temp}
# Example: KXHIGHNY-26FEB18-T52 = bracket with floor_strike 52
# The T{temp} suffix represents the floor_strike of the bracket.
# Edge brackets: T48 might be "below 48" (floor_strike=null, cap_strike=47.99)
#                T58 might be "58 or above" (floor_strike=58.0, cap_strike=null)
```

---

### Bracket Parsing from Market Data

Each event has 6 brackets. The middle 4 are 2 degrees F wide. The bottom and top brackets are catch-all edge brackets. Parse them from `floor_strike` and `cap_strike`:

#### Reference Implementation: `backend/kalshi/markets.py` (parsing function)

```python
# Part of backend/kalshi/markets.py
from __future__ import annotations
import math


def parse_bracket_from_market(market: dict) -> dict:
    """Parse bracket range from Kalshi market data.

    Uses floor_strike and cap_strike from the market object.
    Edge brackets have one null bound.

    Kalshi's cap_strike is an exclusive upper bound — typically X.99
    (e.g., 72.99) but sometimes an integer (e.g., 73.0). We use
    math.ceil(cap) - 1 for bottom brackets to handle both formats.

    Args:
        market: Dict from Kalshi market API response.

    Returns:
        Dict with bracket metadata:
            label: Human-readable label (e.g., "52° to 53°F", "47°F or below")
            lower_bound_f: Floor temp in Fahrenheit, or None for bottom edge
            upper_bound_f: Cap temp in Fahrenheit, or None for top edge
            is_edge_lower: True if this is the bottom catch-all bracket
            is_edge_upper: True if this is the top catch-all bracket
    """
    floor = market.get("floor_strike")
    cap = market.get("cap_strike")

    if floor is None:
        # Bottom edge bracket: "X°F or below"
        # ceil(72.99)-1=72, ceil(73.0)-1=72 — handles both .99 and integer cap
        return {
            "label": f"{math.ceil(cap) - 1}°F or below",
            "lower_bound_f": None,
            "upper_bound_f": cap,
            "is_edge_lower": True,
            "is_edge_upper": False,
        }
    elif cap is None:
        # Top edge bracket: "X°F or above"
        return {
            "label": f"{int(floor)}°F or above",
            "lower_bound_f": floor,
            "upper_bound_f": None,
            "is_edge_lower": False,
            "is_edge_upper": True,
        }
    else:
        # Middle bracket: "X° to Y°F"
        # Kalshi sends cap as the inclusive display value (85.0 for "84° to 85°F"
        # or 85.99), so int(cap) gives the correct display in both cases.
        return {
            "label": f"{int(floor)}° to {int(cap)}°F",
            "lower_bound_f": floor,
            "upper_bound_f": cap,
            "is_edge_lower": False,
            "is_edge_upper": False,
        }
```

**Example bracket layout for an event with 6 markets:**

| Ticker suffix | floor_strike | cap_strike | Label             | Type          |
|---------------|-------------|------------|-------------------|---------------|
| T48           | null        | 47.99      | 47°F or below     | Bottom edge   |
| T50           | 50.0        | 51.99      | 50° to 51°F       | Middle        |
| T52           | 52.0        | 53.99      | 52° to 53°F       | Middle        |
| T54           | 54.0        | 55.99      | 54° to 55°F       | Middle        |
| T56           | 56.0        | 57.99      | 56° to 57°F       | Middle        |
| T58           | 58.0        | null       | 58°F or above     | Top edge      |

Note: The gap between T48 (cap 47.99) and T50 (floor 50.0) means temperatures 48.00-49.99 would fall in neither bracket. Verify against actual Kalshi market data whether these gaps exist or whether the brackets are contiguous. If the actual API returns contiguous brackets, adjust parsing accordingly.

---

### Rate Limiter

Use a token bucket rate limiter to stay under Kalshi's rate limits. Default conservative limit: 10 requests/second.

#### Reference Implementation: `backend/kalshi/rate_limiter.py`

```python
# backend/kalshi/rate_limiter.py
from __future__ import annotations

import asyncio
import time


class TokenBucketRateLimiter:
    """Token bucket rate limiter for Kalshi API.

    Args:
        rate: Tokens added per second (sustained request rate).
        burst: Maximum token count (allows short bursts above sustained rate).
    """

    def __init__(self, rate: float = 10.0, burst: int = 10):
        self.rate = rate  # tokens per second
        self.burst = burst
        self.tokens = float(burst)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Acquire a token, waiting if necessary.

        Blocks until a token is available. Call this before every API request.
        """
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.last_refill = now

            if self.tokens < 1:
                wait = (1 - self.tokens) / self.rate
                await asyncio.sleep(wait)
                self.tokens = 0
            else:
                self.tokens -= 1
```

---

### WebSocket

- **URL:** `wss://api.elections.kalshi.com/trade-api/ws/v2`
- **Auth:** Same RSA signing mechanism (sign `GET /trade-api/ws/v2`)
- **Subscriptions:** orderbook_delta, trade fills, ticker updates
- **Heartbeat:** Ping every 10 seconds to keep connection alive
- **Reconnection:** Exponential backoff on disconnect (2^attempt seconds, max 5 retries)

#### Reference Implementation: `backend/kalshi/websocket.py`

```python
# backend/kalshi/websocket.py
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import websockets

from backend.kalshi.auth import KalshiAuth
from backend.common.logging import get_logger

logger = get_logger("MARKET")


class KalshiWebSocket:
    """WebSocket client for real-time Kalshi market data.

    Handles connection, authentication, subscriptions, heartbeat,
    and automatic reconnection with exponential backoff.

    Supports demo mode via optional url parameter.
    """

    WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    DEMO_WS_URL = "wss://demo-api.kalshi.co/trade-api/ws/v2"

    def __init__(self, auth: KalshiAuth, url: str | None = None):
        self.auth = auth
        self._url = url or self.WS_URL
        self.ws = None
        self._running = False
        self._subscriptions: list[dict] = []  # track for re-subscribe on reconnect
        self._msg_id = 0

    async def connect(self) -> None:
        """Establish WebSocket connection with signed auth headers."""
        headers = self.auth.sign_request("GET", "/trade-api/ws/v2")
        self.ws = await websockets.connect(self._url, extra_headers=headers)
        self._running = True
        logger.info("WebSocket connected")
        # Start heartbeat task
        asyncio.create_task(self._heartbeat())

    async def subscribe_orderbook(self, ticker: str) -> None:
        """Subscribe to orderbook delta updates for a market ticker."""
        self._msg_id += 1
        msg = {
            "id": self._msg_id,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_ticker": ticker,
            },
        }
        await self.ws.send(json.dumps(msg))
        self._subscriptions.append(msg)
        logger.info("Subscribed to orderbook", extra={"data": {"ticker": ticker}})

    async def subscribe_ticker(self, ticker: str) -> None:
        """Subscribe to ticker-level updates (price, volume changes)."""
        self._msg_id += 1
        msg = {
            "id": self._msg_id,
            "cmd": "subscribe",
            "params": {
                "channels": ["ticker"],
                "market_ticker": ticker,
            },
        }
        await self.ws.send(json.dumps(msg))
        self._subscriptions.append(msg)

    async def listen(self) -> AsyncIterator[dict]:
        """Yield parsed messages from the WebSocket.

        Automatically handles reconnection on disconnect.
        Yields dict objects (parsed JSON messages).
        """
        while self._running:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=30)
                data = json.loads(raw)
                yield data
            except asyncio.TimeoutError:
                continue  # heartbeat handles keepalive
            except websockets.ConnectionClosed:
                logger.warning("WebSocket disconnected, reconnecting...")
                await self._reconnect()

    async def close(self) -> None:
        """Gracefully close the WebSocket connection."""
        self._running = False
        if self.ws:
            await self.ws.close()

    async def _heartbeat(self) -> None:
        """Send periodic pings to keep the connection alive."""
        while self._running:
            try:
                await self.ws.ping()
                await asyncio.sleep(10)
            except Exception:
                break

    async def _reconnect(self, max_retries: int = 5) -> None:
        """Reconnect with exponential backoff, re-subscribe to previous channels."""
        for attempt in range(max_retries):
            try:
                wait = 2**attempt
                logger.info(f"Reconnect attempt {attempt + 1}/{max_retries}, waiting {wait}s")
                await asyncio.sleep(wait)
                await self.connect()
                # Re-subscribe to all previous subscriptions
                for sub in self._subscriptions:
                    await self.ws.send(json.dumps(sub))
                logger.info("Reconnected and re-subscribed")
                return
            except Exception:
                logger.error(f"Reconnect attempt {attempt + 1} failed")
        raise ConnectionError("WebSocket reconnection failed after max retries")
```

---

## Full KalshiClient Implementation

This is the main class that Agent 4 (trading engine) calls. It wraps REST endpoints, handles auth, rate limiting, and error mapping.

#### Reference Implementation: `backend/kalshi/client.py`

```python
# backend/kalshi/client.py
from __future__ import annotations

import httpx

from backend.kalshi.auth import KalshiAuth
from backend.kalshi.rate_limiter import TokenBucketRateLimiter
from backend.kalshi.exceptions import (
    KalshiApiError,
    KalshiAuthError,
    KalshiRateLimitError,
    KalshiOrderRejectedError,
    KalshiConnectionError,
)
from backend.kalshi.models import (
    KalshiEvent,
    KalshiMarket,
    KalshiOrderbook,
    OrderRequest,
    OrderResponse,
    KalshiPosition,
    KalshiSettlement,
)
from backend.kalshi.markets import WEATHER_SERIES_TICKERS
from backend.common.logging import get_logger

logger = get_logger("KALSHI")


class KalshiClient:
    """Async Kalshi API client with auth, rate limiting, and error handling.

    This is the primary interface for all Kalshi operations. Agent 4 (trading
    engine) calls this class to fetch markets, place orders, and manage positions.
    """

    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(self, api_key_id: str, private_key_pem: str, demo: bool = False):
        """Initialize the client.

        Args:
            api_key_id: Kalshi API key identifier.
            private_key_pem: RSA private key in PEM format (decrypted).
            demo: If True, use the demo API endpoint.
        """
        if demo:
            self.BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
        self.auth = KalshiAuth(api_key_id, private_key_pem)
        self.rate_limiter = TokenBucketRateLimiter(rate=10.0)
        self.client = httpx.AsyncClient(timeout=30.0)

    async def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_data: dict | None = None,
    ) -> dict:
        """Make an authenticated, rate-limited request to the Kalshi API.

        Args:
            method: HTTP method (GET, POST, DELETE).
            path: Endpoint path starting with / (e.g., /events).
            params: Optional query parameters.
            json_data: Optional JSON body (for POST).

        Returns:
            Parsed JSON response as a dict.

        Raises:
            KalshiAuthError: 401 response.
            KalshiRateLimitError: 429 response.
            KalshiOrderRejectedError: 400 response on order endpoints.
            KalshiApiError: Any other non-2xx response.
            KalshiConnectionError: Network failure.
        """
        await self.rate_limiter.acquire()
        url = f"{self.BASE_URL}{path}"
        full_path = f"/trade-api/v2{path}"
        headers = self.auth.sign_request(method, full_path)

        try:
            response = await self.client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_data,
            )
        except httpx.RequestError as exc:
            raise KalshiConnectionError(f"Network error: {exc}", context={"path": path}) from exc

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "unknown")
            raise KalshiRateLimitError(
                "Rate limit exceeded",
                context={"path": path, "retry_after": retry_after},
            )
        if response.status_code == 401:
            raise KalshiAuthError(
                "Authentication failed",
                context={"path": path},
            )
        if response.status_code == 400 and "/portfolio/orders" in path:
            body = response.json()
            raise KalshiOrderRejectedError(
                body.get("message", "Order rejected"),
                context={"path": path, "detail": body},
            )
        if response.status_code >= 400:
            raise KalshiApiError(
                f"API error {response.status_code}",
                context={"path": path, "status": response.status_code},
            )

        return response.json()

    # --- Account ---

    async def get_balance(self) -> float:
        """Get account balance in dollars.

        Returns:
            Balance in dollars (converted from API cents).
        """
        data = await self._request("GET", "/portfolio/balance")
        return data["balance"] / 100  # cents -> dollars

    # --- Events & Markets ---

    async def get_weather_events(self, city: str | None = None) -> list[KalshiEvent]:
        """Fetch active weather events, optionally filtered by city.

        Args:
            city: City code (NYC, CHI, MIA, AUS) or None for all.

        Returns:
            List of KalshiEvent models.
        """
        params = {}
        if city:
            series = WEATHER_SERIES_TICKERS.get(city.upper())
            if series:
                params["series_ticker"] = series
        data = await self._request("GET", "/events", params=params)
        return [KalshiEvent(**e) for e in data.get("events", [])]

    async def get_event_markets(self, event_ticker: str) -> list[KalshiMarket]:
        """Get all bracket markets for a specific event.

        Args:
            event_ticker: e.g., "KXHIGHNY-26FEB18"

        Returns:
            List of KalshiMarket models (one per bracket).
        """
        data = await self._request("GET", f"/events/{event_ticker}")
        market_tickers = data.get("event", {}).get("markets", [])
        # Fetch full market details for each bracket
        markets = []
        for ticker in market_tickers:
            market_data = await self._request("GET", f"/markets/{ticker}")
            markets.append(KalshiMarket(**market_data["market"]))
        return markets

    async def get_market(self, ticker: str) -> KalshiMarket:
        """Get details for a single market (bracket).

        Args:
            ticker: Market ticker, e.g., "KXHIGHNY-26FEB18-T52"
        """
        data = await self._request("GET", f"/markets/{ticker}")
        return KalshiMarket(**data["market"])

    async def get_orderbook(self, ticker: str) -> KalshiOrderbook:
        """Get the current orderbook for a market.

        Args:
            ticker: Market ticker.
        """
        data = await self._request("GET", f"/markets/{ticker}/orderbook")
        return KalshiOrderbook(**data["orderbook"])

    # --- Orders ---

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        """Place an order on Kalshi.

        Validates the order locally before sending. Logs the order details
        (but never API keys).

        Args:
            order: Validated OrderRequest model.

        Returns:
            OrderResponse with order_id and status.

        Raises:
            ValueError: If order fails local validation.
            KalshiOrderRejectedError: If Kalshi rejects the order.
        """
        # CRITICAL: validate before sending to API
        order.validate_for_submission()
        data = await self._request(
            "POST",
            "/portfolio/orders",
            json_data=order.to_api_dict(),
        )
        logger.info(
            "Order placed",
            extra={
                "data": {
                    "order_id": data["order"]["order_id"],
                    "ticker": order.ticker,
                    "side": order.side,
                    "price_cents": order.yes_price,
                    "qty": order.count,
                }
            },
        )
        return OrderResponse(**data["order"])

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a resting order.

        Args:
            order_id: The order ID to cancel.

        Returns:
            True if cancelled successfully.
        """
        await self._request("DELETE", f"/portfolio/orders/{order_id}")
        logger.info("Order cancelled", extra={"data": {"order_id": order_id}})
        return True

    # --- Positions ---

    async def get_positions(self) -> list[KalshiPosition]:
        """Get all current open positions."""
        data = await self._request("GET", "/portfolio/positions")
        return [KalshiPosition(**p) for p in data.get("market_positions", [])]

    async def get_settlements(self, limit: int = 100) -> list[KalshiSettlement]:
        """Get settlement history.

        Args:
            limit: Max number of settlements to return.
        """
        data = await self._request(
            "GET",
            "/portfolio/settlements",
            params={"limit": limit},
        )
        return [KalshiSettlement(**s) for s in data.get("settlements", [])]

    # --- Lifecycle ---

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self.client.aclose()
```

---

## Output Interface

The `KalshiClient` class exposes these methods (this is the contract with Agent 4):

```python
class KalshiClient:
    async def get_balance(self) -> float
    async def get_weather_events(self, city: str | None = None) -> list[KalshiEvent]
    async def get_event_markets(self, event_ticker: str) -> list[KalshiMarket]
    async def get_market(self, ticker: str) -> KalshiMarket
    async def get_orderbook(self, ticker: str) -> KalshiOrderbook
    async def place_order(self, order: OrderRequest) -> OrderResponse
    async def get_orders(self, status: str | None = None, limit: int = 200) -> list[OrderResponse]  # cursor-paginated
    async def cancel_order(self, order_id: str) -> bool
    async def get_positions(self) -> list[KalshiPosition]
    async def get_settlements(self, limit: int = 100) -> list[KalshiSettlement]
```

All return types must be Pydantic models defined in your `models.py` and also referenced in `backend/common/schemas.py`.

---

## Pydantic Models (models.py)

Define at minimum these models in `backend/kalshi/models.py`:

```python
# Suggested structure — expand fields as needed from API responses
from pydantic import BaseModel, field_validator
from datetime import datetime


class KalshiEvent(BaseModel):
    event_ticker: str
    series_ticker: str
    title: str
    category: str
    status: str
    markets: list[str]  # list of market ticker strings


class KalshiMarket(BaseModel):
    ticker: str
    event_ticker: str
    title: str
    subtitle: str | None = None
    status: str
    yes_bid: int  # cents
    yes_ask: int  # cents
    no_bid: int  # cents
    no_ask: int  # cents
    last_price: int  # cents
    volume: int
    open_interest: int
    floor_strike: float | None  # null for bottom edge
    cap_strike: float | None  # null for top edge
    result: str | None
    close_time: datetime
    expiration_time: datetime


class KalshiOrderbook(BaseModel):
    yes: list[list[int]]  # [[price_cents, quantity], ...]
    no: list[list[int]]


class OrderRequest(BaseModel):
    ticker: str
    action: str  # "buy" or "sell"
    side: str  # "yes" or "no"
    type: str  # "limit" or "market"
    count: int  # number of contracts
    yes_price: int  # cents [1-99]

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        if v not in ("buy", "sell"):
            raise ValueError(f"action must be 'buy' or 'sell', got '{v}'")
        return v

    @field_validator("side")
    @classmethod
    def validate_side(cls, v: str) -> str:
        if v not in ("yes", "no"):
            raise ValueError(f"side must be 'yes' or 'no', got '{v}'")
        return v

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in ("limit", "market"):
            raise ValueError(f"type must be 'limit' or 'market', got '{v}'")
        return v

    @field_validator("count")
    @classmethod
    def validate_count(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"count must be >= 1, got {v}")
        return v

    @field_validator("yes_price")
    @classmethod
    def validate_price(cls, v: int) -> int:
        if not (1 <= v <= 99):
            raise ValueError(f"yes_price must be 1-99 cents, got {v}")
        return v

    def validate_for_submission(self) -> None:
        """Run all validators. Raises ValueError if invalid."""
        # Pydantic validators already ran at construction, but this
        # is an explicit call site for the client to use before sending.
        pass

    def to_api_dict(self) -> dict:
        """Convert to the dict format expected by the Kalshi API."""
        return {
            "ticker": self.ticker,
            "action": self.action,
            "side": self.side,
            "type": self.type,
            "count": self.count,
            "yes_price": self.yes_price,
        }


class OrderResponse(BaseModel):
    order_id: str
    ticker: str
    action: str
    side: str
    type: str
    count: int
    yes_price: int  # cents
    status: str
    created_time: datetime


class KalshiPosition(BaseModel):
    ticker: str
    market_exposure: int  # cents
    resting_orders_count: int
    total_traded: int
    realized_pnl: int  # cents


class KalshiSettlement(BaseModel):
    ticker: str
    market_result: str
    revenue: int  # cents
    settled_time: datetime
```

---

## Error Handling

### Exception Classes: `backend/kalshi/exceptions.py`

```python
# backend/kalshi/exceptions.py
from __future__ import annotations


class KalshiError(Exception):
    """Base exception for all Kalshi errors."""

    def __init__(self, message: str, context: dict | None = None):
        self.context = context or {}
        super().__init__(message)

    def __str__(self) -> str:
        base = super().__str__()
        if self.context:
            # NEVER include API keys in context!
            safe_ctx = {
                k: v
                for k, v in self.context.items()
                if "key" not in k.lower() and "secret" not in k.lower()
            }
            return f"{base} | context={safe_ctx}"
        return base


class KalshiAuthError(KalshiError):
    """Invalid keys, expired signature, or 401 response."""

    pass


class KalshiRateLimitError(KalshiError):
    """Rate limit exceeded (429). Check context for retry_after."""

    pass


class KalshiOrderRejectedError(KalshiError):
    """Order rejected by Kalshi (insufficient balance, market closed, etc.)."""

    pass


class KalshiApiError(KalshiError):
    """Generic API error with status code and message."""

    pass


class KalshiConnectionError(KalshiError):
    """Network issues, timeout, or WebSocket disconnect."""

    pass
```

All errors must include context (what we were trying to do, relevant IDs) but **NEVER** include API keys, private keys, or secrets. The base `KalshiError.__str__` filters out keys automatically as a safety net.

---

## Market Discovery (`markets.py`)

Your `markets.py` must:
- Discover active weather events by filtering events API with `series_ticker`
- Parse bracket ranges from `floor_strike` / `cap_strike` (see bracket parsing section above)
- Map tickers to cities using `WEATHER_SERIES_TICKERS` and `SERIES_TO_CITY`
- Handle the case where markets haven't launched yet (before 10 AM ET) — return empty list, don't crash
- Extract market event dates from tickers via `parse_market_date_from_ticker()` (parses YYMONDD segment)
- Provide a function to build event tickers from city + date:

```python
from datetime import date


def build_event_ticker(city: str, target_date: date) -> str:
    """Build a Kalshi event ticker for a city and date.

    Args:
        city: City code (NYC, CHI, MIA, AUS).
        target_date: The date of the weather event.

    Returns:
        Event ticker string, e.g., "KXHIGHNY-26FEB18"

    Raises:
        ValueError: If city code is not recognized.
    """
    series = WEATHER_SERIES_TICKERS.get(city.upper())
    if not series:
        raise ValueError(f"Unknown city code: {city}")
    date_str = target_date.strftime("%y%b%d").upper()  # e.g., "26FEB18"
    return f"{series}-{date_str}"
```

---

## Order Construction & Validation

Before any order reaches Kalshi, validate locally:

| Check                  | Rule                                        | Error if violated                     |
|------------------------|---------------------------------------------|---------------------------------------|
| `action`               | Must be "buy" or "sell"                     | ValueError                            |
| `side`                 | Must be "yes" or "no"                       | ValueError                            |
| `yes_price`            | Integer in [1, 99] (cents)                  | ValueError                            |
| `count`                | Positive integer >= 1                       | ValueError                            |
| `type`                 | Must be "limit" or "market"                 | ValueError                            |
| `ticker`               | Non-empty string                            | ValueError                            |
| Market status          | Must be "active" (check before placing)     | KalshiOrderRejectedError              |

**Always validate locally first.** Catching bad orders before they hit the network saves time and avoids confusing API error messages.

---

## Testing Requirements

Your tests go in `tests/kalshi/`:
- `test_auth.py` — RSA signing produces correct signatures, handles invalid keys
- `test_client.py` — mock HTTP responses for all endpoints, test happy path + error cases
- `test_markets.py` — bracket parsing, ticker mapping, event discovery, `build_event_ticker`
- `test_orders.py` — order validation (reject invalid orders before they hit the API)
- `test_websocket.py` — WebSocket connection, subscription, reconnection logic
- `test_rate_limiter.py` — rate limiting correctly throttles requests
- `test_models.py` — Pydantic model validation (especially OrderRequest validators)
- `test_cache.py` — Redis cache helpers (17 tests, mock Redis with `unittest.mock.AsyncMock`)
- `test_market_feed.py` — MarketFeedConsumer lifecycle, auth, message processing (25 tests)

### Critical test cases:

- **Auth:** Invalid API key -> `KalshiAuthError` raised, key not in error message
- **Auth:** Signing string is correctly formed (`timestamp + METHOD + path`)
- **Client:** Insufficient balance -> `KalshiOrderRejectedError` with clear reason
- **Client:** Rate limit 429 response -> `KalshiRateLimitError` raised
- **Client:** Network timeout -> `KalshiConnectionError` raised
- **WebSocket:** Disconnect -> automatic reconnection with re-subscription
- **Markets:** Edge brackets parsed correctly (null floor_strike, null cap_strike)
- **Markets:** Middle brackets parsed correctly
- **Markets:** `build_event_ticker("NYC", date(2026, 2, 18))` -> `"KXHIGHNY-26FEB18"`
- **Orders:** Market closed -> order rejected gracefully
- **Orders:** Malformed order (negative qty, price > 99, price < 1) -> rejected before API call
- **Orders:** Price in dollars instead of cents (e.g., 0.22 instead of 22) -> caught by validator
- **Rate limiter:** Burst of requests correctly queued
- **Rate limiter:** Sustained rate stays under limit

### Test fixture example:

```python
# tests/kalshi/conftest.py
import pytest
from unittest.mock import AsyncMock
from backend.kalshi.client import KalshiClient
from backend.kalshi.auth import KalshiAuth

# Generate a test RSA key (DO NOT use real keys in tests)
TEST_PRIVATE_KEY = """-----BEGIN RSA PRIVATE KEY-----
... (generate with: openssl genrsa -out test_key.pem 2048) ...
-----END RSA PRIVATE KEY-----"""

TEST_API_KEY_ID = "test-api-key-id"


@pytest.fixture
def mock_kalshi_market_response():
    """Sample market response for testing bracket parsing."""
    return {
        "market": {
            "ticker": "KXHIGHNY-26FEB18-T52",
            "event_ticker": "KXHIGHNY-26FEB18",
            "title": "NYC high temp: 52F to 53F?",
            "subtitle": "Will the highest temperature be between 52F and 53F?",
            "status": "active",
            "yes_bid": 22,
            "yes_ask": 25,
            "no_bid": 74,
            "no_ask": 78,
            "last_price": 23,
            "volume": 1542,
            "open_interest": 823,
            "floor_strike": 52.0,
            "cap_strike": 53.99,
            "result": None,
            "close_time": "2026-02-18T23:00:00Z",
            "expiration_time": "2026-02-19T14:00:00Z",
        }
    }


@pytest.fixture
def mock_edge_bracket_bottom():
    """Bottom edge bracket (below X)."""
    return {
        "market": {
            "ticker": "KXHIGHNY-26FEB18-T48",
            "event_ticker": "KXHIGHNY-26FEB18",
            "title": "NYC high temp: Below 48F?",
            "status": "active",
            "yes_bid": 5,
            "yes_ask": 8,
            "floor_strike": None,
            "cap_strike": 47.99,
        }
    }


@pytest.fixture
def mock_edge_bracket_top():
    """Top edge bracket (X or above)."""
    return {
        "market": {
            "ticker": "KXHIGHNY-26FEB18-T58",
            "event_ticker": "KXHIGHNY-26FEB18",
            "title": "NYC high temp: 58F or above?",
            "status": "active",
            "yes_bid": 10,
            "yes_ask": 14,
            "floor_strike": 58.0,
            "cap_strike": None,
        }
    }
```

---

## Dependencies

Add these to `requirements.txt`:

```
cryptography>=41.0.0    # RSA-PSS signing (SHA256)
httpx>=0.25.0           # Async HTTP client
websockets>=12.0        # WebSocket client
pydantic>=2.0           # Data validation and models
```

---

## Common Pitfalls

1. **Prices in cents, not dollars.** The single most common bug. `yes_price: 22` means $0.22. If you see a float going to the API, something is wrong.
2. **Signing path must include `/trade-api/v2`.** Sign `/trade-api/v2/markets`, not `/markets`.
3. **Signing uses RSA-PSS, not PKCS1v15.** Per official Kalshi SDK. PSS is non-deterministic (random salt).
4. **Timestamps are milliseconds.** `int(time.time() * 1000)`, not `int(time.time())`.
5. **Edge brackets have null strikes.** Bottom bracket: `floor_strike=null`. Top bracket: `cap_strike=null`. Your parsing must handle both.
6. **Markets may not exist yet.** Before 10 AM ET the day before, weather markets for the next day might not be listed. Handle gracefully.
7. **Balance is in cents.** `GET /portfolio/balance` returns cents. Divide by 100 for dollars.
8. **Rate limit yourself.** Don't rely on Kalshi's 429 responses; proactively limit to 10 req/s.
9. **Never log API keys or private keys.** The exception base class filters "key"/"secret" from context, but don't rely on that alone. Never put keys in context dicts.
10. **WebSocket re-subscribe on reconnect.** If the connection drops, you must re-subscribe to all channels after reconnecting.

---

## Real-Time Market Data Feed (Phase 20)

### Architecture

```
Kalshi WS API (wss://api.elections.kalshi.com/trade-api/ws/v2)
       │
       ▼
MarketFeedConsumer (asyncio background task in FastAPI lifespan)
       │
       ├──▶ Redis Cache: kalshi:prices:{city}:{YYMMDD} (TTL 120s)
       │    └── Trading cycle reads cache first, REST fallback
       │
       └──▶ Redis pub/sub: boz:events → browser (via existing WS infra)
            └── "market.price_update" events for frontend
```

### Redis Cache (`cache.py`)

Pure async utility functions for storing/retrieving market prices in Redis.

**Redis keys:**
- `kalshi:prices:{city}:{YYMMDD}` — JSON `{"bracket_label": price_cents}`, TTL 120s
- `kalshi:tickers:{city}:{YYMMDD}` — JSON `{"bracket_label": "ticker"}`, TTL 300s
- `kalshi:feed:status` — `"1"` or `"0"`, no TTL

**Key functions:**
- `get_redis_client()` — creates async Redis client from settings
- `set_city_prices(redis, city, date, prices, tickers, ttl)` — atomic pipeline write
- `get_city_prices(redis, city, date)` — returns `(prices, tickers)` or `None` on miss
- `set_feed_status(redis, connected)` / `get_feed_status(redis)` — feed health tracking

**Important:** `redis.pipeline()` is synchronous (returns pipeline directly), but `pipeline.execute()` is async. In mocks, use `MagicMock` for the pipeline object with `AsyncMock` for `execute()`.

### Market Feed Consumer (`market_feed.py`)

Background asyncio task that maintains a persistent WebSocket connection to Kalshi, receives real-time ticker updates, and caches prices in Redis.

**`MarketFeedConsumer` class:**
- `start()` — outer loop: load creds → connect → subscribe → process messages → reconnect
- `stop()` — graceful shutdown (close WS + Redis)
- `_get_auth()` — reads User from DB via `get_task_session()` (same pattern as trading scheduler)
- `_get_active_tickers()` — builds tickers for today + tomorrow across active cities, REST bootstrap
- `_handle_ticker_update(data)` — updates Redis cache + publishes `market.price_update` event

**`market_feed_consumer()`** — top-level entry point (wraps MarketFeedConsumer with retry loop).

**Key patterns:**
- Uses `session = await get_task_session()` with `try/finally: await session.close()` (NOT `async with`)
- Demo mode: reads `KALSHI_DEMO_MODE` setting, passes `DEMO_WS_URL` to `KalshiWebSocket`
- Cache date format: `YYMMDD` (e.g., "260219"), distinct from event ticker format `YYMMMDD` (e.g., "26FEB19")
- Started as asyncio task in FastAPI lifespan alongside `redis_subscriber()`

### Trading Cycle Integration

The trading scheduler (`backend/trading/scheduler.py`) was modified to try Redis cache first:
1. `_fetch_market_prices()` — tries `get_city_prices()` from Redis, falls back to REST
2. `_fetch_market_tickers()` — tries tickers from Redis cache, falls back to REST
3. Cache hits/misses tracked via `KALSHI_WS_CACHE_HITS_TOTAL` counter (labels: `source="cache"` or `source="rest_fallback"`)

### Prometheus Metrics (4 new)

- `kalshi_ws_connected` (Gauge) — feed connected 1/0
- `kalshi_ws_messages_total` (Counter, label: channel) — messages received
- `kalshi_ws_reconnects_total` (Counter) — reconnection attempts
- `kalshi_ws_cache_hits_total` (Counter, label: source) — cache vs REST fallback

### Alert Rules (`monitoring/prometheus/rules/kalshi_ws.yml`)

- **KalshiWSFeedDisconnected** (warning, 5m) — feed down
- **KalshiWSFeedStalled** (warning, 2m) — connected but no messages
- **KalshiWSReconnectsHigh** (warning, 5m) — >3 reconnects in 15m

### Grafana Dashboard (`monitoring/grafana/dashboards/kalshi-ws-feed.json`)

6-panel dashboard visualizing all 4 Kalshi WS metrics:
1. **Feed Connection Status** (stat) — connected/disconnected with color
2. **Reconnections (Last Hour)** (stat) — reconnect count with thresholds
3. **Cache Hit Rate** (gauge) — % of lookups from cache vs REST fallback
4. **Message Throughput by Channel** (timeseries) — stacked area by channel
5. **Reconnection Events** (timeseries) — reconnect spikes over time
6. **Cache Source Distribution** (piechart) — donut chart of cache vs REST

### WebSocket Demo Mode Support

`KalshiWebSocket` now accepts an optional `url` parameter:
```python
DEMO_WS_URL = "wss://demo-api.kalshi.co/trade-api/ws/v2"


def __init__(self, auth: KalshiAuth, url: str | None = None) -> None:
    self._url = url or self.WS_URL
```

### Config Settings

Two new optional settings in `backend/common/config.py`:
- `kalshi_ws_cache_ttl_seconds: int = 120` — Redis cache TTL for prices
- `kalshi_ws_refresh_minutes: int = 5` — how often to refresh ticker subscriptions
