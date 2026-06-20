"""Main Kalshi API client — the primary interface for all Kalshi operations.

Wraps REST endpoints with authentication, rate limiting, and error mapping.
Agent 4 (trading engine) calls this class to fetch markets, place orders,
and manage positions.

SECURITY:
- API keys and private keys are NEVER logged or included in error messages.
- The client holds a KalshiAuth instance, not raw key material.

Usage:
    from backend.kalshi.client import KalshiClient

    client = KalshiClient(
        api_key_id="abc123",
        private_key_pem=decrypted_pem,
        demo=True,
    )
    balance = await client.get_balance()
    events = await client.get_weather_events(city="NYC")
    await client.close()
"""

from __future__ import annotations

import httpx

from backend.common.logging import get_logger
from backend.kalshi.auth import KalshiAuth
from backend.kalshi.exceptions import (
    KalshiApiError,
    KalshiAuthError,
    KalshiConnectionError,
    KalshiOrderRejectedError,
    KalshiRateLimitError,
)
from backend.kalshi.markets import WEATHER_SERIES_TICKERS
from backend.kalshi.models import (
    KalshiEvent,
    KalshiMarket,
    KalshiOrderbook,
    KalshiPosition,
    KalshiSettlement,
    OrderRequest,
    OrderResponse,
)
from backend.kalshi.rate_limiter import TokenBucketRateLimiter

logger = get_logger("MARKET")

# ─── Base URLs ───

PROD_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"


# ─── Orderbook Parsing ───


def _parse_orderbook_response(data: dict) -> KalshiOrderbook:
    """Parse a Kalshi orderbook API response into a KalshiOrderbook model.

    Handles two formats:
    - **Legacy**: ``{"orderbook": {"yes": [[cents, qty], ...], "no": [...]}}``
    - **Current (v2 fp)**: ``{"orderbook_fp": {"yes_dollars": [["0.22", "10.00"], ...],
      "no_dollars": [...]}}`` where each level is ``[price_dollars_str, qty_fp_str]``.

    The current format uses fixed-point strings for prices (dollars) and
    quantities.  We convert to ``[[price_cents, qty_int], ...]`` to match
    the ``KalshiOrderbook`` model used throughout the codebase.

    Args:
        data: Raw JSON dict from ``GET /markets/{ticker}/orderbook``.

    Returns:
        KalshiOrderbook with yes/no levels in ``[cents, qty]`` format.
    """
    # Legacy format (cents integer arrays)
    if "orderbook" in data:
        return KalshiOrderbook(**data["orderbook"])

    # Current v2 fixed-point dollar format
    if "orderbook_fp" in data:
        ob_fp = data["orderbook_fp"]
        yes_levels: list[list[int]] = []
        no_levels: list[list[int]] = []

        for price_str, qty_str in ob_fp.get("yes_dollars", []):
            price_cents = int(round(float(price_str) * 100))
            qty = int(round(float(qty_str)))
            yes_levels.append([price_cents, qty])

        for price_str, qty_str in ob_fp.get("no_dollars", []):
            price_cents = int(round(float(price_str) * 100))
            qty = int(round(float(qty_str)))
            no_levels.append([price_cents, qty])

        return KalshiOrderbook(yes=yes_levels, no=no_levels)

    # Neither key present — raise a clear error
    available_keys = list(data.keys())
    msg = (
        f"Unexpected orderbook response structure. "
        f"Expected 'orderbook' or 'orderbook_fp' key, got: {available_keys}"
    )
    raise KalshiApiError(msg, context={"keys": available_keys})


class KalshiClient:
    """Async Kalshi API client with auth, rate limiting, and error handling.

    This is the primary interface for all Kalshi operations. Agent 4 (trading
    engine) calls this class to fetch markets, place orders, and manage
    positions.

    Args:
        api_key_id: Kalshi API key identifier.
        private_key_pem: RSA private key in PEM format (decrypted).
        demo: If True, use the demo API endpoint instead of production.
    """

    def __init__(
        self,
        api_key_id: str,
        private_key_pem: str,
        demo: bool = False,
    ) -> None:
        self.base_url = DEMO_BASE_URL if demo else PROD_BASE_URL
        self.auth = KalshiAuth(api_key_id, private_key_pem)
        self.rate_limiter = TokenBucketRateLimiter(rate=10.0, burst=10)
        self.client = httpx.AsyncClient(timeout=30.0)
        self._demo = demo

        logger.info(
            "KalshiClient initialized",
            extra={"data": {"demo": demo, "base_url": self.base_url}},
        )

    # ─── Core Request Method ───

    async def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_data: dict | None = None,
    ) -> dict:
        """Make an authenticated, rate-limited request to the Kalshi API.

        Handles request signing, rate limiting, and maps HTTP error codes
        to specific exception types.

        Args:
            method: HTTP method (GET, POST, DELETE).
            path: Endpoint path starting with / (e.g., /events).
                  The /trade-api/v2 prefix is added automatically for signing.
            params: Optional query parameters.
            json_data: Optional JSON body (for POST).

        Returns:
            Parsed JSON response as a dict.

        Raises:
            KalshiAuthError: 401 response (invalid credentials).
            KalshiRateLimitError: 429 response (rate limit exceeded).
            KalshiOrderRejectedError: 400 response on order endpoints.
            KalshiApiError: Any other non-2xx response.
            KalshiConnectionError: Network failure or timeout.
        """
        await self.rate_limiter.acquire()

        url = f"{self.base_url}{path}"
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
            raise KalshiConnectionError(
                f"Network error: {exc}",
                context={"path": path, "method": method},
            ) from exc

        if response.status_code == 401:
            raise KalshiAuthError(
                "Authentication failed",
                context={"path": path, "status": 401},
            )

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "unknown")
            raise KalshiRateLimitError(
                "Rate limit exceeded",
                context={"path": path, "retry_after": retry_after},
            )

        if response.status_code == 400 and (
            "/portfolio/orders" in path or "/portfolio/events/orders" in path
        ):
            try:
                body = response.json()
            except Exception:
                body = {"message": response.text}
            raise KalshiOrderRejectedError(
                body.get("message", "Order rejected"),
                context={"path": path, "detail": body},
            )

        if response.status_code >= 400:
            try:
                body = response.json()
                error_msg = body.get("message", f"API error {response.status_code}")
            except Exception:
                error_msg = f"API error {response.status_code}"
            raise KalshiApiError(
                error_msg,
                context={"path": path, "status": response.status_code},
            )

        return response.json()

    # ─── Account ───

    async def get_balance(self) -> float:
        """Get account balance in dollars.

        The API returns balance in cents. This method converts to dollars.

        Returns:
            Balance in dollars (e.g., 500.0 for a $500 balance).
        """
        data = await self._request("GET", "/portfolio/balance")
        balance_cents = data["balance"]
        balance_dollars = balance_cents / 100.0
        logger.info(
            "Balance fetched",
            extra={"data": {"balance_dollars": balance_dollars}},
        )
        return balance_dollars

    # ─── Events & Markets ───

    async def get_weather_events(
        self,
        city: str | None = None,
    ) -> list[KalshiEvent]:
        """Fetch active weather events, optionally filtered by city.

        Args:
            city: City code (NYC, CHI, MIA, AUS) or None for all weather events.

        Returns:
            List of KalshiEvent models.
        """
        params: dict = {}
        if city:
            series = WEATHER_SERIES_TICKERS.get(city.upper())
            if series:
                params["series_ticker"] = series

        data = await self._request("GET", "/events", params=params or None)
        events = [KalshiEvent(**e) for e in data.get("events", [])]

        logger.info(
            "Weather events fetched",
            extra={
                "data": {
                    "city": city,
                    "count": len(events),
                }
            },
        )
        return events

    async def get_event_markets(
        self,
        event_ticker: str,
    ) -> list[KalshiMarket]:
        """Get all bracket markets for a specific event.

        Uses the /markets endpoint filtered by event_ticker to fetch all
        bracket markets in a single request.

        Args:
            event_ticker: Event ticker (e.g., "KXHIGHNY-26FEB21").

        Returns:
            List of KalshiMarket models (one per bracket, typically 6).
        """
        data = await self._request("GET", f"/markets?event_ticker={event_ticker}&limit=100")
        raw_markets = data.get("markets", [])

        markets = [KalshiMarket(**m) for m in raw_markets]

        logger.info(
            "Event markets fetched",
            extra={
                "data": {
                    "event_ticker": event_ticker,
                    "market_count": len(markets),
                }
            },
        )
        return markets

    async def get_market(self, ticker: str) -> KalshiMarket:
        """Get details for a single market (bracket).

        Args:
            ticker: Market ticker (e.g., "KXHIGHNY-26FEB18-T52").

        Returns:
            KalshiMarket model with current pricing and status.
        """
        data = await self._request("GET", f"/markets/{ticker}")
        return KalshiMarket(**data["market"])

    async def get_orderbook(self, ticker: str) -> KalshiOrderbook:
        """Get the current orderbook for a market.

        Handles both legacy format (``data["orderbook"]`` with cents) and
        the current Kalshi v2 format (``data["orderbook_fp"]`` with
        ``yes_dollars`` / ``no_dollars`` as string pairs).

        Args:
            ticker: Market ticker (e.g., "KXHIGHNY-26FEB18-T52").

        Returns:
            KalshiOrderbook with yes and no price/quantity levels in cents.
        """
        data = await self._request("GET", f"/markets/{ticker}/orderbook")
        return _parse_orderbook_response(data)

    # ─── Orders ───

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        """Place an order on Kalshi.

        Validates the order locally before sending to the API.
        Logs order details (but NEVER API keys or secrets).

        Args:
            order: Validated OrderRequest model.

        Returns:
            OrderResponse with order_id and status from Kalshi.

        Raises:
            ValueError: If order fails local validation.
            KalshiOrderRejectedError: If Kalshi rejects the order.
        """
        # Run explicit pre-flight validation
        order.validate_for_submission()

        # v2 endpoint, single-book payload. The legacy /portfolio/orders path
        # returns HTTP 410 (deprecated_v1_order_endpoint) as of June 2026.
        data = await self._request(
            "POST",
            "/portfolio/events/orders",
            json_data=order.to_api_dict(),
        )

        # v2 returns a flat body, not {"order": {...}}.
        response = OrderResponse.from_v2_place_response(data, order)

        logger.info(
            "Order placed",
            extra={
                "data": {
                    "order_id": response.order_id,
                    "ticker": order.ticker,
                    "action": order.action,
                    "side": order.side,
                    "price_cents": order.yes_price,
                    "count": order.count,
                    "status": response.status,
                }
            },
        )

        return response

    async def get_orders(
        self,
        status: str | None = None,
        limit: int = 200,
    ) -> list[OrderResponse]:
        """Get orders from the portfolio, optionally filtered by status.

        Paginates through all pages using Kalshi's cursor-based pagination.

        Args:
            status: Filter by order status (e.g., "resting", "executed", "canceled").
                    None returns all orders.
            limit: Maximum orders per page (1-200, default 200).

        Returns:
            List of OrderResponse models across all pages.
        """
        all_orders: list[OrderResponse] = []
        cursor: str | None = None

        while True:
            params: dict[str, str] = {"limit": str(limit)}
            if status is not None:
                params["status"] = status
            if cursor is not None:
                params["cursor"] = cursor

            data = await self._request("GET", "/portfolio/orders", params=params)
            page_orders = [OrderResponse(**o) for o in data.get("orders", [])]
            all_orders.extend(page_orders)

            # Check for next page: Kalshi returns a cursor for pagination
            cursor = data.get("cursor")
            if not cursor or len(page_orders) < limit:
                break

        logger.info(
            "Orders fetched",
            extra={"data": {"count": len(all_orders), "status_filter": status}},
        )
        return all_orders

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a resting order via the Kalshi v2 cancel endpoint.

        Kalshi deprecated DELETE /portfolio/orders/{id} alongside the v1
        order create endpoint (changelog window June 18–25, 2026). The
        v2 path is otherwise identical — same DELETE method, same
        order_id in the path, no request body. The response shape did
        change ({order_id, client_order_id, reduced_by, ts_ms} instead
        of a full order object), but we don't read it.

        Args:
            order_id: The Kalshi order ID to cancel.

        Returns:
            True if cancelled successfully.
        """
        await self._request("DELETE", f"/portfolio/events/orders/{order_id}")
        logger.info(
            "Order cancelled",
            extra={"data": {"order_id": order_id}},
        )
        return True

    # ─── Positions & Settlements ───

    async def get_positions(self) -> list[KalshiPosition]:
        """Get all current open positions.

        Returns:
            List of KalshiPosition models with exposure and P&L data.
        """
        data = await self._request("GET", "/portfolio/positions")
        positions = [KalshiPosition(**p) for p in data.get("market_positions", [])]
        logger.info(
            "Positions fetched",
            extra={"data": {"count": len(positions)}},
        )
        return positions

    async def get_settlements(self, limit: int = 100) -> list[KalshiSettlement]:
        """Get settlement history.

        Paginates through all pages using Kalshi's cursor-based pagination.

        Args:
            limit: Maximum settlements per page (1-100, default 100).

        Returns:
            List of KalshiSettlement models with outcomes and revenue.
        """
        all_settlements: list[KalshiSettlement] = []
        cursor: str | None = None

        while True:
            params: dict[str, str] = {"limit": str(limit)}
            if cursor is not None:
                params["cursor"] = cursor

            data = await self._request(
                "GET",
                "/portfolio/settlements",
                params=params,
            )
            page = [KalshiSettlement(**s) for s in data.get("settlements", [])]
            all_settlements.extend(page)

            cursor = data.get("cursor")
            if not cursor or len(page) < limit:
                break

        logger.info(
            "Settlements fetched",
            extra={"data": {"count": len(all_settlements)}},
        )
        return all_settlements

    # ─── Lifecycle ───

    async def close(self) -> None:
        """Close the underlying HTTP client.

        Should be called when the client is no longer needed to release
        network resources.
        """
        await self.client.aclose()
        logger.info("KalshiClient closed")
