"""Tests for the unauthenticated Kalshi public market-data client (algo v2, S1).

All HTTP is mocked with httpx.MockTransport — no real Kalshi calls.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from backend.kalshi.public_client import KalshiPublicAPIError, KalshiPublicClient


def _client(handler, max_retries: int = 3) -> tuple[KalshiPublicClient, list[float]]:
    """Build a fast client around a mock transport; returns (client, recorded sleeps)."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = KalshiPublicClient(
        base_url="https://kalshi.test/trade-api/v2",
        rate_per_second=1000.0,
        burst=1000,
        max_retries=max_retries,
        backoff_base_seconds=0.01,
        http_client=http,
    )
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client._sleep = fake_sleep  # type: ignore[method-assign]
    return client, sleeps


class TestRetries:
    async def test_429_with_retry_after_is_retried_without_raising(self) -> None:
        """AC1: Given a 429 with Retry-After, the client waits >= Retry-After and succeeds."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "2"})
            return httpx.Response(200, json={"markets": [], "cursor": ""})

        client, sleeps = _client(handler)
        markets = await client.get_event_markets("KXHIGHNY-26OCT06")
        assert markets == []
        assert calls["n"] == 2
        assert sleeps and sleeps[0] >= 2.0
        await client.close()

    async def test_5xx_is_retried(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(503)
            return httpx.Response(200, json={"markets": [{"ticker": "X"}], "cursor": ""})

        client, sleeps = _client(handler)
        assert await client.get_event_markets("E") == [{"ticker": "X"}]
        assert calls["n"] == 3
        assert len(sleeps) == 2
        await client.close()

    async def test_transport_error_is_retried(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("boom", request=request)
            return httpx.Response(200, json={"candlesticks": [{"end_period_ts": 1}]})

        client, _ = _client(handler)
        assert await client.get_candlesticks("S", "T", 0, 10) == [{"end_period_ts": 1}]
        await client.close()

    async def test_non_retryable_status_raises_immediately(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(404, text="not found")

        client, sleeps = _client(handler)
        with pytest.raises(KalshiPublicAPIError) as exc:
            await client.get_event_markets("E")
        assert exc.value.status_code == 404
        assert calls["n"] == 1
        assert sleeps == []
        await client.close()

    async def test_exhausted_retries_raise(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(429)

        client, _ = _client(handler, max_retries=2)
        with pytest.raises(KalshiPublicAPIError) as exc:
            await client.get_event_markets("E")
        assert exc.value.status_code == 429
        assert calls["n"] == 3
        await client.close()


class TestEndpoints:
    async def test_event_markets_follows_cursor(self) -> None:
        pages = {
            "": {"markets": [{"ticker": "A"}], "cursor": "c1"},
            "c1": {"markets": [{"ticker": "B"}], "cursor": ""},
        }
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            seen.append(params)
            return httpx.Response(200, json=pages[params.get("cursor", "")])

        client, _ = _client(handler)
        markets = await client.get_event_markets("KXHIGHNY-26OCT06")
        assert [m["ticker"] for m in markets] == ["A", "B"]
        assert seen[0]["event_ticker"] == "KXHIGHNY-26OCT06"
        await client.close()

    async def test_historical_and_live_paths(self) -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path.endswith("/candlesticks"):
                return httpx.Response(200, json={"candlesticks": []})
            return httpx.Response(200, json={"markets": [], "cursor": ""})

        client, _ = _client(handler)
        await client.get_event_markets("E", historical=True)
        await client.get_event_markets("E", historical=False)
        await client.get_candlesticks("KXHIGHNY", "KXHIGHNY-26OCT06-T70", 0, 1, historical=False)
        await client.get_candlesticks("HIGHNY", "HIGHNY-24JUL01-B80.5", 0, 1, historical=True)
        assert paths == [
            "/trade-api/v2/historical/markets",
            "/trade-api/v2/markets",
            "/trade-api/v2/series/KXHIGHNY/markets/KXHIGHNY-26OCT06-T70/candlesticks",
            "/trade-api/v2/historical/markets/HIGHNY-24JUL01-B80.5/candlesticks",
        ]
        await client.close()

    async def test_historical_cutoff_parsed_as_aware_utc(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"market_settled_ts": "2026-08-08T00:00:00Z"})

        client, _ = _client(handler)
        assert await client.get_historical_cutoff() == datetime(2026, 8, 8, tzinfo=UTC)
        await client.close()

    async def test_no_credentials_are_sent(self) -> None:
        """The public client must never send auth headers."""
        headers: list[httpx.Headers] = []

        def handler(request: httpx.Request) -> httpx.Response:
            headers.append(request.headers)
            return httpx.Response(200, json={"markets": [], "cursor": ""})

        client, _ = _client(handler)
        await client.get_event_markets("E")
        assert not any(h.lower().startswith("kalshi-access") for h in headers[0])
        assert "authorization" not in {h.lower() for h in headers[0]}
        await client.close()
