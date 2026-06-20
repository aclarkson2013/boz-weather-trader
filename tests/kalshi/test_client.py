"""Tests for the main KalshiClient — REST API wrapper with auth and error handling.

Uses unittest.mock to mock httpx.AsyncClient.request and verify that
the client correctly handles various HTTP status codes, converts
responses to models, and maps errors to specific exception types.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from backend.kalshi.client import (
    DEMO_BASE_URL,
    PROD_BASE_URL,
    KalshiClient,
    _parse_orderbook_response,
)
from backend.kalshi.exceptions import (
    KalshiApiError,
    KalshiAuthError,
    KalshiConnectionError,
    KalshiOrderRejectedError,
    KalshiRateLimitError,
)
from backend.kalshi.models import OrderRequest


def _make_response(
    status_code: int,
    json_data: dict | None = None,
    headers: dict | None = None,
) -> MagicMock:
    """Create a mock httpx.Response with the given status code and JSON body."""
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = json_data or {}
    response.text = str(json_data or {})
    response.headers = headers or {}
    return response


class TestKalshiClientInit:
    """Tests for KalshiClient initialization."""

    def test_initializes_with_demo_true_uses_demo_url(self, rsa_key_pair) -> None:
        """KalshiClient with demo=True uses DEMO_BASE_URL."""
        client = KalshiClient(
            api_key_id=rsa_key_pair["api_key_id"],
            private_key_pem=rsa_key_pair["private_key_pem"],
            demo=True,
        )
        assert client.base_url == DEMO_BASE_URL

    def test_initializes_with_demo_false_uses_prod_url(self, rsa_key_pair) -> None:
        """KalshiClient with demo=False uses PROD_BASE_URL."""
        client = KalshiClient(
            api_key_id=rsa_key_pair["api_key_id"],
            private_key_pem=rsa_key_pair["private_key_pem"],
            demo=False,
        )
        assert client.base_url == PROD_BASE_URL


class TestKalshiClientRequest:
    """Tests for KalshiClient._request error mapping."""

    @pytest.fixture
    def client(self, rsa_key_pair):
        """Create a KalshiClient with mocked rate limiter for testing."""
        c = KalshiClient(
            api_key_id=rsa_key_pair["api_key_id"],
            private_key_pem=rsa_key_pair["private_key_pem"],
            demo=True,
        )
        # Mock rate limiter to avoid async timing issues in tests
        c.rate_limiter.acquire = AsyncMock()
        return c

    @pytest.mark.asyncio
    async def test_raises_kalshi_auth_error_on_401(self, client) -> None:
        """_request raises KalshiAuthError on 401 response."""
        mock_response = _make_response(401)
        client.client.request = AsyncMock(return_value=mock_response)

        with pytest.raises(KalshiAuthError, match="Authentication failed"):
            await client._request("GET", "/portfolio/balance")

    @pytest.mark.asyncio
    async def test_raises_kalshi_rate_limit_error_on_429(self, client) -> None:
        """_request raises KalshiRateLimitError on 429 response."""
        mock_response = _make_response(
            429,
            headers={"Retry-After": "5"},
        )
        client.client.request = AsyncMock(return_value=mock_response)

        with pytest.raises(KalshiRateLimitError, match="Rate limit exceeded"):
            await client._request("GET", "/markets")

    @pytest.mark.asyncio
    async def test_raises_kalshi_order_rejected_on_400_for_orders(self, client) -> None:
        """_request raises KalshiOrderRejectedError on 400 for order endpoints."""
        mock_response = _make_response(
            400,
            json_data={"message": "Insufficient balance"},
        )
        client.client.request = AsyncMock(return_value=mock_response)

        with pytest.raises(KalshiOrderRejectedError, match="Insufficient balance"):
            await client._request("POST", "/portfolio/orders")

    @pytest.mark.asyncio
    async def test_raises_kalshi_api_error_on_other_4xx_5xx(self, client) -> None:
        """_request raises KalshiApiError on non-401/429/400 error codes."""
        mock_response = _make_response(
            500,
            json_data={"message": "Internal Server Error"},
        )
        client.client.request = AsyncMock(return_value=mock_response)

        with pytest.raises(KalshiApiError, match="Internal Server Error"):
            await client._request("GET", "/events")

    @pytest.mark.asyncio
    async def test_raises_kalshi_connection_error_on_network_error(self, client) -> None:
        """_request raises KalshiConnectionError on httpx.RequestError."""
        client.client.request = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused"),
        )

        with pytest.raises(KalshiConnectionError, match="Network error"):
            await client._request("GET", "/events")


class TestKalshiClientMethods:
    """Tests for specific KalshiClient business methods."""

    @pytest.fixture
    def client(self, rsa_key_pair):
        """Create a KalshiClient with mocked internals."""
        c = KalshiClient(
            api_key_id=rsa_key_pair["api_key_id"],
            private_key_pem=rsa_key_pair["private_key_pem"],
            demo=True,
        )
        c.rate_limiter.acquire = AsyncMock()
        return c

    @pytest.mark.asyncio
    async def test_get_balance_converts_cents_to_dollars(self, client) -> None:
        """get_balance converts API balance (cents) to dollars."""
        mock_response = _make_response(200, json_data={"balance": 50000})
        client.client.request = AsyncMock(return_value=mock_response)

        balance = await client.get_balance()
        assert balance == 500.0

    @pytest.mark.asyncio
    async def test_close_calls_aclose(self, client) -> None:
        """close() calls the underlying httpx client's aclose()."""
        client.client.aclose = AsyncMock()

        await client.close()
        client.client.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_place_order_calls_validate_for_submission(self, client) -> None:
        """place_order calls validate_for_submission before sending the request."""
        order = OrderRequest(
            ticker="KXHIGHNY-26FEB18-T52",
            action="buy",
            side="yes",
            type="limit",
            count=1,
            yes_price=22,
        )

        # v2 endpoint returns a flat sparse body, not the legacy {"order": {...}}.
        mock_response = _make_response(
            201,
            json_data={
                "order_id": "abc-123",
                "client_order_id": "test-client-id",
                "fill_count": "0.00",
                "remaining_count": "1.00",
                "ts_ms": 1781889243000,
            },
        )
        client.client.request = AsyncMock(return_value=mock_response)

        response = await client.place_order(order)
        assert response.order_id == "abc-123"
        # No fills + remaining count > 0 → resting
        assert response.status == "resting"
        # The legacy fields downstream code reads are synthesized from the request
        assert response.ticker == "KXHIGHNY-26FEB18-T52"
        assert response.side == "yes"
        assert response.yes_price == 22

    @pytest.mark.asyncio
    async def test_place_order_rejects_empty_ticker(self, client) -> None:
        """place_order raises ValueError for empty ticker via validate_for_submission."""
        order = OrderRequest(
            ticker="   ",
            action="buy",
            side="yes",
            type="limit",
            count=1,
            yes_price=22,
        )

        with pytest.raises(ValueError, match="ticker"):
            await client.place_order(order)


class TestCancelOrderV2:
    """Tests for cancel_order using the Kalshi v2 path."""

    @pytest.fixture
    def client(self, rsa_key_pair):
        c = KalshiClient(
            api_key_id=rsa_key_pair["api_key_id"],
            private_key_pem=rsa_key_pair["private_key_pem"],
            demo=True,
        )
        c.rate_limiter.acquire = AsyncMock()
        return c

    @pytest.mark.asyncio
    async def test_cancel_hits_v2_path(self, client) -> None:
        """cancel_order must POST DELETE to /portfolio/events/orders/{id}.

        Kalshi deprecated /portfolio/orders/{id} alongside the v1 create
        endpoint (changelog window June 18–25, 2026).
        """
        mock_response = _make_response(
            200,
            json_data={
                "order_id": "abc-123",
                "client_order_id": "cli-id",
                "reduced_by": "1.00",
                "ts_ms": 1781889243000,
            },
        )
        client.client.request = AsyncMock(return_value=mock_response)

        result = await client.cancel_order("abc-123")

        assert result is True
        # The URL the underlying httpx request was called with must end
        # with the v2 path. We assert on the call args rather than mocking
        # _request so the path translation is actually exercised.
        called_url = client.client.request.call_args.args[1]
        assert called_url.endswith("/portfolio/events/orders/abc-123")
        assert "/portfolio/orders/abc-123" not in called_url.replace(
            "/portfolio/events/orders/", ""
        )
        # And the HTTP method must still be DELETE
        assert client.client.request.call_args.args[0] == "DELETE"


class TestGetOrdersPagination:
    """Tests for get_orders cursor-based pagination."""

    @pytest.fixture
    def client(self, rsa_key_pair):
        """Create a KalshiClient with mocked internals."""
        c = KalshiClient(
            api_key_id=rsa_key_pair["api_key_id"],
            private_key_pem=rsa_key_pair["private_key_pem"],
            demo=True,
        )
        c.rate_limiter.acquire = AsyncMock()
        return c

    def _make_orders_page(self, count: int, cursor: str | None = None) -> dict:
        """Create a mock Kalshi orders API response page."""
        orders = []
        for i in range(count):
            orders.append(
                {
                    "order_id": f"order-{i}",
                    "ticker": f"KXHIGHNY-26FEB21-T{50 + i}",
                    "action": "buy",
                    "side": "yes",
                    "type": "limit",
                    "fill_count": 1,
                    "initial_count": 1,
                    "yes_price": 22,
                    "status": "executed",
                    "created_time": "2026-02-21T15:00:00Z",
                    "taker_fees": 0,
                    "taker_fill_cost": 22,
                }
            )
        result: dict = {"orders": orders}
        if cursor:
            result["cursor"] = cursor
        return result

    @pytest.mark.asyncio
    async def test_single_page_no_cursor(self, client) -> None:
        """Single page with no cursor returns all orders."""
        page_data = self._make_orders_page(3)
        mock_resp = _make_response(200, json_data=page_data)
        client.client.request = AsyncMock(return_value=mock_resp)

        orders = await client.get_orders(status="executed")

        assert len(orders) == 3
        assert client.client.request.call_count == 1

    @pytest.mark.asyncio
    async def test_two_pages_with_cursor(self, client) -> None:
        """Two pages fetched when first page returns a cursor."""
        page1 = self._make_orders_page(200, cursor="next-page-token")
        page2 = self._make_orders_page(50)

        mock_resp1 = _make_response(200, json_data=page1)
        mock_resp2 = _make_response(200, json_data=page2)
        client.client.request = AsyncMock(side_effect=[mock_resp1, mock_resp2])

        orders = await client.get_orders(status="executed")

        assert len(orders) == 250
        assert client.client.request.call_count == 2

    @pytest.mark.asyncio
    async def test_empty_first_page(self, client) -> None:
        """Empty first page returns empty list."""
        page_data = self._make_orders_page(0)
        mock_resp = _make_response(200, json_data=page_data)
        client.client.request = AsyncMock(return_value=mock_resp)

        orders = await client.get_orders(status="executed")

        assert len(orders) == 0
        assert client.client.request.call_count == 1

    @pytest.mark.asyncio
    async def test_short_last_page_stops(self, client) -> None:
        """Pagination stops when page has fewer items than limit."""
        # First page full (200 items), second page short (10 items)
        page1 = self._make_orders_page(200, cursor="page2")
        page2 = self._make_orders_page(10)

        mock_resp1 = _make_response(200, json_data=page1)
        mock_resp2 = _make_response(200, json_data=page2)
        client.client.request = AsyncMock(side_effect=[mock_resp1, mock_resp2])

        orders = await client.get_orders(status="executed", limit=200)

        assert len(orders) == 210
        # Should stop after 2 pages (second is short)
        assert client.client.request.call_count == 2

    @pytest.mark.asyncio
    async def test_status_filter_passed_on_every_page(self, client) -> None:
        """Status filter is included in params for every page request."""
        page1 = self._make_orders_page(200, cursor="page2")
        page2 = self._make_orders_page(50)

        mock_resp1 = _make_response(200, json_data=page1)
        mock_resp2 = _make_response(200, json_data=page2)
        client.client.request = AsyncMock(side_effect=[mock_resp1, mock_resp2])

        await client.get_orders(status="executed")

        # Both calls should include status=executed in params
        for call in client.client.request.call_args_list:
            params = call.kwargs.get("params", {})
            assert params.get("status") == "executed"


# ─── Orderbook Response Parsing ───


class TestParseOrderbookResponse:
    """Tests for _parse_orderbook_response handling legacy and v2 fp formats."""

    def test_legacy_format_with_orderbook_key(self):
        """Legacy format: data['orderbook'] with cents integer arrays."""
        data = {
            "orderbook": {
                "yes": [[22, 10], [21, 5]],
                "no": [[78, 8], [79, 3]],
            }
        }
        ob = _parse_orderbook_response(data)
        assert ob.yes == [[22, 10], [21, 5]]
        assert ob.no == [[78, 8], [79, 3]]

    def test_fp_format_with_orderbook_fp_key(self):
        """Current v2 format: data['orderbook_fp'] with dollar strings."""
        data = {
            "orderbook_fp": {
                "yes_dollars": [["0.2200", "10.00"], ["0.2100", "5.00"]],
                "no_dollars": [["0.7800", "8.00"], ["0.7900", "3.00"]],
            }
        }
        ob = _parse_orderbook_response(data)
        assert ob.yes == [[22, 10], [21, 5]]
        assert ob.no == [[78, 8], [79, 3]]

    def test_fp_format_empty_sides(self):
        """FP format with empty yes_dollars and no_dollars."""
        data = {
            "orderbook_fp": {
                "yes_dollars": [],
                "no_dollars": [],
            }
        }
        ob = _parse_orderbook_response(data)
        assert ob.yes == []
        assert ob.no == []

    def test_fp_format_missing_sides_defaults_empty(self):
        """FP format missing yes_dollars/no_dollars keys defaults to empty."""
        data = {"orderbook_fp": {}}
        ob = _parse_orderbook_response(data)
        assert ob.yes == []
        assert ob.no == []

    def test_fp_format_price_rounding(self):
        """Dollar strings that need rounding convert correctly to cents."""
        data = {
            "orderbook_fp": {
                "yes_dollars": [["0.1500", "100.00"], ["0.0100", "1.00"]],
                "no_dollars": [["0.9900", "50.00"]],
            }
        }
        ob = _parse_orderbook_response(data)
        assert ob.yes == [[15, 100], [1, 1]]
        assert ob.no == [[99, 50]]

    def test_unknown_format_raises_error(self):
        """Neither 'orderbook' nor 'orderbook_fp' key raises KalshiApiError."""
        data = {"something_else": {}}
        with pytest.raises(KalshiApiError, match="Unexpected orderbook response"):
            _parse_orderbook_response(data)

    def test_legacy_format_preferred_when_both_present(self):
        """If both keys present, legacy 'orderbook' is used."""
        data = {
            "orderbook": {"yes": [[30, 5]], "no": []},
            "orderbook_fp": {
                "yes_dollars": [["0.9900", "99.00"]],
                "no_dollars": [],
            },
        }
        ob = _parse_orderbook_response(data)
        # Should use legacy format
        assert ob.yes == [[30, 5]]
