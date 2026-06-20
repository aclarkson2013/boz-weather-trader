"""Tests for Kalshi Pydantic models and helper functions.

Validates price conversion helpers, model construction, field validators,
and serialization methods on OrderRequest and related models.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from backend.kalshi.models import (
    KalshiEvent,
    KalshiMarket,
    KalshiOrderbook,
    KalshiPosition,
    KalshiSettlement,
    OrderRequest,
    OrderResponse,
    cents_to_dollars,
    dollars_to_cents,
)

# ─── Price Conversion Helpers ───


class TestDollarsToCents:
    """Tests for the dollars_to_cents helper."""

    def test_022_to_22(self) -> None:
        """0.22 dollars converts to 22 cents."""
        assert dollars_to_cents(0.22) == 22

    def test_099_to_99(self) -> None:
        """0.99 dollars converts to 99 cents."""
        assert dollars_to_cents(0.99) == 99

    def test_1_dollar_to_100(self) -> None:
        """1.0 dollars converts to 100 cents."""
        assert dollars_to_cents(1.0) == 100


class TestCentsToDollars:
    """Tests for the cents_to_dollars helper."""

    def test_22_to_022(self) -> None:
        """22 cents converts to 0.22 dollars."""
        assert cents_to_dollars(22) == 0.22

    def test_99_to_099(self) -> None:
        """99 cents converts to 0.99 dollars."""
        assert cents_to_dollars(99) == 0.99

    def test_100_to_1(self) -> None:
        """100 cents converts to 1.0 dollars."""
        assert cents_to_dollars(100) == 1.0


# ─── Event & Market Models ───


class TestKalshiEvent:
    """Tests for KalshiEvent model construction."""

    def test_creates_with_required_fields(self) -> None:
        """KalshiEvent accepts all required fields and defaults markets to empty."""
        event = KalshiEvent(
            event_ticker="KXHIGHNY-26FEB18",
            series_ticker="KXHIGHNY",
            title="Highest temperature in NYC on Feb 18?",
            category="Climate",
            status="active",
        )
        assert event.event_ticker == "KXHIGHNY-26FEB18"
        assert event.series_ticker == "KXHIGHNY"
        assert event.title == "Highest temperature in NYC on Feb 18?"
        assert event.category == "Climate"
        assert event.status == "active"
        assert event.markets == []


class TestKalshiMarket:
    """Tests for KalshiMarket model construction."""

    def test_creates_with_all_fields(self) -> None:
        """KalshiMarket accepts all fields including pricing data."""
        market = KalshiMarket(
            ticker="KXHIGHNY-26FEB18-T52",
            event_ticker="KXHIGHNY-26FEB18",
            title="NYC high temp: 52F to 53F?",
            subtitle="Will the highest temperature be between 52F and 53F?",
            status="active",
            yes_bid=22,
            yes_ask=25,
            no_bid=74,
            no_ask=78,
            last_price=23,
            volume=1542,
            open_interest=823,
            floor_strike=52.0,
            cap_strike=53.99,
        )
        assert market.ticker == "KXHIGHNY-26FEB18-T52"
        assert market.yes_bid == 22
        assert market.yes_ask == 25
        assert market.floor_strike == 52.0
        assert market.cap_strike == 53.99

    def test_defaults_for_optional_fields(self) -> None:
        """KalshiMarket defaults pricing fields to 0 and optional fields to None."""
        market = KalshiMarket(
            ticker="KXHIGHNY-26FEB18-T52",
            event_ticker="KXHIGHNY-26FEB18",
            title="NYC high temp: 52F to 53F?",
            status="active",
        )
        assert market.yes_bid == 0
        assert market.yes_ask == 0
        assert market.no_bid == 0
        assert market.no_ask == 0
        assert market.last_price == 0
        assert market.volume == 0
        assert market.open_interest == 0
        assert market.subtitle is None
        assert market.result is None
        assert market.close_time is None
        assert market.expiration_time is None

    def test_handles_none_floor_and_cap_strike(self) -> None:
        """KalshiMarket handles None for both floor_strike and cap_strike (edge brackets)."""
        # Bottom edge: floor_strike=None
        bottom = KalshiMarket(
            ticker="KXHIGHNY-26FEB18-T48",
            event_ticker="KXHIGHNY-26FEB18",
            title="Below 48F?",
            status="active",
            floor_strike=None,
            cap_strike=47.99,
        )
        assert bottom.floor_strike is None
        assert bottom.cap_strike == 47.99

        # Top edge: cap_strike=None
        top = KalshiMarket(
            ticker="KXHIGHNY-26FEB18-T58",
            event_ticker="KXHIGHNY-26FEB18",
            title="58F or above?",
            status="active",
            floor_strike=58.0,
            cap_strike=None,
        )
        assert top.floor_strike == 58.0
        assert top.cap_strike is None


class TestKalshiMarketDollarsConversion:
    """Tests for KalshiMarket model_validator converting _dollars fields to cents."""

    def test_converts_dollars_strings_to_cents(self) -> None:
        """Dollar-string fields are converted to integer cent fields."""
        market = KalshiMarket(
            ticker="KXHIGHNY-26MAR14-B49.5",
            event_ticker="KXHIGHNY-26MAR14",
            title="49-50 bracket",
            status="active",
            yes_ask_dollars="0.3000",
            yes_bid_dollars="0.2900",
            no_ask_dollars="0.7100",
            no_bid_dollars="0.7000",
            last_price_dollars="0.3000",
            volume_fp="2887.00",
            open_interest_fp="2679.00",
            floor_strike=49,
            cap_strike=50,
        )
        assert market.yes_ask == 30
        assert market.yes_bid == 29
        assert market.no_ask == 71
        assert market.no_bid == 70
        assert market.last_price == 30
        assert market.volume == 2887
        assert market.open_interest == 2679

    def test_legacy_cent_fields_preserved_when_nonzero(self) -> None:
        """If legacy cent fields are already nonzero, they are NOT overwritten."""
        market = KalshiMarket(
            ticker="KXHIGHNY-26FEB18-T52",
            event_ticker="KXHIGHNY-26FEB18",
            title="test",
            status="active",
            yes_ask=25,
            yes_bid=22,
            yes_ask_dollars="0.9900",
            yes_bid_dollars="0.9900",
        )
        assert market.yes_ask == 25
        assert market.yes_bid == 22

    def test_empty_result_string_becomes_none(self) -> None:
        """Kalshi returns result='' for unsettled markets; converted to None."""
        market = KalshiMarket(
            ticker="KXHIGHNY-26MAR14-B49.5",
            event_ticker="KXHIGHNY-26MAR14",
            title="test",
            status="active",
            result="",
        )
        assert market.result is None

    def test_integer_floor_and_cap_strike(self) -> None:
        """Post-migration integer strikes are handled correctly."""
        market = KalshiMarket(
            ticker="KXHIGHNY-26MAR14-B49.5",
            event_ticker="KXHIGHNY-26MAR14",
            title="49 to 50",
            status="active",
            floor_strike=49,
            cap_strike=50,
        )
        assert market.floor_strike == 49.0
        assert market.cap_strike == 50.0

    def test_full_api_response_shape(self) -> None:
        """A complete post-migration API response is parsed correctly."""
        api_data = {
            "ticker": "KXHIGHNY-26MAR14-B49.5",
            "event_ticker": "KXHIGHNY-26MAR14",
            "title": "Will the **high temp in NYC** be 49-50 on Mar 14?",
            "subtitle": "49 to 50",
            "status": "active",
            "yes_ask_dollars": "0.3000",
            "yes_bid_dollars": "0.2900",
            "no_ask_dollars": "0.7100",
            "no_bid_dollars": "0.7000",
            "last_price_dollars": "0.3000",
            "volume_fp": "2887.00",
            "open_interest_fp": "2679.00",
            "floor_strike": 49,
            "cap_strike": 50,
            "result": "",
            "close_time": "2026-03-15T03:59:00Z",
            "expiration_time": "2026-03-21T14:00:00Z",
        }
        market = KalshiMarket(**api_data)
        assert market.yes_ask == 30
        assert market.yes_bid == 29
        assert market.no_ask == 71
        assert market.no_bid == 70
        assert market.last_price == 30
        assert market.volume == 2887
        assert market.open_interest == 2679
        assert market.result is None
        assert market.floor_strike == 49.0
        assert market.cap_strike == 50.0

    def test_invalid_dollars_string_ignored(self) -> None:
        """Invalid dollar strings (non-numeric) are silently ignored."""
        market = KalshiMarket(
            ticker="KXHIGHNY-26MAR14-B49.5",
            event_ticker="KXHIGHNY-26MAR14",
            title="test",
            status="active",
            yes_ask_dollars="not_a_number",
        )
        assert market.yes_ask == 0  # default

    def test_extra_fields_allowed(self) -> None:
        """Extra fields from the API (e.g. yes_ask_size_fp) don't break parsing."""
        market = KalshiMarket(
            ticker="KXHIGHNY-26MAR14-B49.5",
            event_ticker="KXHIGHNY-26MAR14",
            title="test",
            status="active",
            yes_ask_dollars="0.3000",
            yes_ask_size_fp="158.00",
            yes_bid_size_fp="278.00",
            fractional_trading_enabled=False,
            price_level_structure="linear_cent",
        )
        assert market.yes_ask == 30


class TestKalshiOrderbook:
    """Tests for KalshiOrderbook model."""

    def test_creates_with_default_empty_lists(self) -> None:
        """KalshiOrderbook defaults to empty yes/no lists."""
        orderbook = KalshiOrderbook()
        assert orderbook.yes == []
        assert orderbook.no == []


# ─── OrderRequest Model ───


class TestOrderRequest:
    """Tests for OrderRequest Pydantic model and validators."""

    def test_valid_creation_with_all_fields(self) -> None:
        """A valid OrderRequest constructs without error with all fields."""
        order = OrderRequest(
            ticker="KXHIGHNY-26FEB18-T52",
            action="buy",
            side="yes",
            type="limit",
            count=5,
            yes_price=22,
        )
        assert order.ticker == "KXHIGHNY-26FEB18-T52"
        assert order.action == "buy"
        assert order.side == "yes"
        assert order.type == "limit"
        assert order.count == 5
        assert order.yes_price == 22

    def test_validates_action_must_be_buy_or_sell(self) -> None:
        """An action other than 'buy' or 'sell' raises ValidationError."""
        with pytest.raises(ValidationError, match="action"):
            OrderRequest(
                ticker="KXHIGHNY-26FEB18-T52",
                action="hold",
                side="yes",
                type="limit",
                count=1,
                yes_price=22,
            )

    def test_validates_side_must_be_yes_or_no(self) -> None:
        """A side other than 'yes' or 'no' raises ValidationError."""
        with pytest.raises(ValidationError, match="side"):
            OrderRequest(
                ticker="KXHIGHNY-26FEB18-T52",
                action="buy",
                side="maybe",
                type="limit",
                count=1,
                yes_price=22,
            )

    def test_validates_type_must_be_limit_or_market(self) -> None:
        """A type other than 'limit' or 'market' raises ValidationError."""
        with pytest.raises(ValidationError, match="type"):
            OrderRequest(
                ticker="KXHIGHNY-26FEB18-T52",
                action="buy",
                side="yes",
                type="stop_loss",
                count=1,
                yes_price=22,
            )

    def test_validates_count_ge_1(self) -> None:
        """count=0 is below minimum (1) and raises ValidationError."""
        with pytest.raises(ValidationError):
            OrderRequest(
                ticker="KXHIGHNY-26FEB18-T52",
                action="buy",
                side="yes",
                type="limit",
                count=0,
                yes_price=22,
            )

    def test_validates_yes_price_minimum(self) -> None:
        """yes_price=0 is below minimum (1) and raises ValidationError."""
        with pytest.raises(ValidationError):
            OrderRequest(
                ticker="KXHIGHNY-26FEB18-T52",
                action="buy",
                side="yes",
                type="limit",
                count=1,
                yes_price=0,
            )

    def test_validates_yes_price_maximum(self) -> None:
        """yes_price=100 exceeds maximum (99) and raises ValidationError."""
        with pytest.raises(ValidationError):
            OrderRequest(
                ticker="KXHIGHNY-26FEB18-T52",
                action="buy",
                side="yes",
                type="limit",
                count=1,
                yes_price=100,
            )

    def test_to_api_dict_yes_side_maps_to_bid(self) -> None:
        """Buying YES becomes side=bid with the YES price in fixed-point dollars."""
        order = OrderRequest(
            ticker="KXHIGHNY-26FEB18-T52",
            action="buy",
            side="yes",
            type="limit",
            count=3,
            yes_price=45,
        )
        api_dict = order.to_api_dict()
        assert api_dict == {
            "ticker": "KXHIGHNY-26FEB18-T52",
            "side": "bid",
            "count": "3.00",
            "price": "0.4500",
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
        }

    def test_to_api_dict_no_side_maps_to_ask_with_complementary_price(self) -> None:
        """Buying NO at X cents becomes side=ask at (100-X)/100 dollars."""
        order = OrderRequest(
            ticker="KXHIGHMIA-26JUN20-B92.5",
            action="buy",
            side="no",
            type="limit",
            count=1,
            yes_price=42,  # this means "pay 42c for NO"
        )
        api_dict = order.to_api_dict()
        # 100 - 42 = 58 → "0.5800"
        assert api_dict == {
            "ticker": "KXHIGHMIA-26JUN20-B92.5",
            "side": "ask",
            "count": "1.00",
            "price": "0.5800",
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
        }

    def test_validate_for_submission_raises_on_empty_ticker(self) -> None:
        """validate_for_submission raises ValueError for whitespace-only ticker."""
        order = OrderRequest(
            ticker="   ",
            action="buy",
            side="yes",
            type="limit",
            count=1,
            yes_price=22,
        )
        with pytest.raises(ValueError, match="ticker"):
            order.validate_for_submission()


# ─── OrderResponse Model ───


class TestOrderResponse:
    """Tests for OrderResponse model."""

    def test_creates_from_valid_data(self) -> None:
        """OrderResponse creates successfully from valid API response data."""
        response = OrderResponse(
            order_id="abc-123-def",
            ticker="KXHIGHNY-26FEB18-T52",
            action="buy",
            side="yes",
            type="limit",
            fill_count=1,
            initial_count=1,
            yes_price=22,
            status="resting",
            created_time=datetime(2026, 2, 17, 10, 5, 0),
        )
        assert response.order_id == "abc-123-def"
        assert response.ticker == "KXHIGHNY-26FEB18-T52"
        assert response.status == "resting"
        assert response.yes_price == 22
        assert response.count == 1  # backward-compat property


class TestOrderResponseDollarsConversion:
    """Tests for OrderResponse model_validator converting _dollars fields."""

    def test_converts_dollars_fields_to_cents(self) -> None:
        """Dollar-string fields are converted to integer cent fields."""
        response = OrderResponse(
            order_id="abc-123",
            ticker="KXHIGHNY-26MAR14-B49.5",
            action="buy",
            side="yes",
            type="limit",
            status="executed",
            created_time=datetime(2026, 3, 14, 10, 0, 0),
            yes_price_dollars="0.3000",
            taker_fees_dollars="0.1200",
            taker_fill_cost_dollars="0.3000",
            fill_count_fp="1.00",
            initial_count_fp="1.00",
        )
        assert response.yes_price == 30
        assert response.taker_fees == 12
        assert response.taker_fill_cost == 30
        assert response.fill_count == 1
        assert response.initial_count == 1

    def test_legacy_cent_fields_preserved(self) -> None:
        """Legacy integer cent fields are NOT overwritten when present."""
        response = OrderResponse(
            order_id="abc-123",
            ticker="KXHIGHNY-26FEB18-T52",
            action="buy",
            side="yes",
            type="limit",
            fill_count=1,
            initial_count=1,
            yes_price=22,
            status="resting",
            created_time=datetime(2026, 2, 17, 10, 5, 0),
            taker_fees=11,
            taker_fill_cost=22,
        )
        assert response.yes_price == 22
        assert response.taker_fees == 11
        assert response.taker_fill_cost == 22


class TestOrderResponseFromV2PlaceResponse:
    """Tests for the OrderResponse.from_v2_place_response constructor."""

    def _yes_request(self, **overrides):
        from backend.kalshi.models import OrderRequest

        defaults = {
            "ticker": "KXHIGHNY-26FEB18-T52",
            "action": "buy",
            "side": "yes",
            "type": "limit",
            "count": 2,
            "yes_price": 22,
        }
        defaults.update(overrides)
        return OrderRequest(**defaults)

    def test_resting_when_no_fills(self) -> None:
        body = {
            "order_id": "abc-123",
            "fill_count": "0.00",
            "remaining_count": "2.00",
            "ts_ms": 1781889243000,
        }
        resp = OrderResponse.from_v2_place_response(body, self._yes_request())
        assert resp.order_id == "abc-123"
        assert resp.status == "resting"
        assert resp.fill_count == 0
        assert resp.initial_count == 2

    def test_executed_when_fully_filled(self) -> None:
        body = {
            "order_id": "xyz-789",
            "fill_count": "2.00",
            "remaining_count": "0.00",
            "ts_ms": 1781889243000,
            "average_fill_price": "0.2200",
            "average_fee_paid": "0.0100",
        }
        resp = OrderResponse.from_v2_place_response(body, self._yes_request())
        assert resp.status == "executed"
        assert resp.fill_count == 2
        # avg fill price 0.22 × 2 contracts × 100 cents/dollar = 44 cents total
        assert resp.taker_fill_cost == 44
        # avg fee 0.01 × 2 contracts × 100 = 2 cents total
        assert resp.taker_fees == 2

    def test_partial_fill_treated_as_executed(self) -> None:
        body = {
            "order_id": "abc-456",
            "fill_count": "1.00",
            "remaining_count": "1.00",
            "ts_ms": 1781889243000,
            "average_fill_price": "0.2200",
        }
        resp = OrderResponse.from_v2_place_response(body, self._yes_request())
        # remainder will be cancelled by _sync_resting_orders next cycle
        assert resp.status == "executed"
        assert resp.fill_count == 1
        assert resp.initial_count == 2

    def test_synthesizes_missing_fields_from_request(self) -> None:
        body = {
            "order_id": "abc-123",
            "fill_count": "0.00",
            "remaining_count": "2.00",
        }
        req = self._yes_request(ticker="KXHIGHMIA-26JUN20-B92.5", side="no", yes_price=42)
        resp = OrderResponse.from_v2_place_response(body, req)
        # Legacy-semantic fields populated from the request, not the response
        assert resp.ticker == "KXHIGHMIA-26JUN20-B92.5"
        assert resp.side == "no"
        assert resp.action == "buy"
        assert resp.yes_price == 42


# ─── Position & Settlement Models ───


class TestKalshiPosition:
    """Tests for KalshiPosition model."""

    def test_creates_with_defaults(self) -> None:
        """KalshiPosition defaults numeric fields to 0."""
        position = KalshiPosition(ticker="KXHIGHNY-26FEB18-T52")
        assert position.ticker == "KXHIGHNY-26FEB18-T52"
        assert position.market_exposure == 0
        assert position.resting_orders_count == 0
        assert position.total_traded == 0
        assert position.realized_pnl == 0


class TestKalshiSettlement:
    """Tests for KalshiSettlement model."""

    def test_creates_with_required_fields(self) -> None:
        """KalshiSettlement accepts all required fields."""
        settlement = KalshiSettlement(
            ticker="KXHIGHNY-26FEB18-T52",
            market_result="yes",
            revenue=100,
            settled_time=datetime(2026, 2, 19, 14, 0, 0),
        )
        assert settlement.ticker == "KXHIGHNY-26FEB18-T52"
        assert settlement.market_result == "yes"
        assert settlement.revenue == 100
        assert settlement.settled_time == datetime(2026, 2, 19, 14, 0, 0)

    def test_converts_revenue_dollars_to_cents(self) -> None:
        """revenue_dollars string is converted to integer revenue cents."""
        settlement = KalshiSettlement(
            ticker="KXHIGHNY-26MAR14-B49.5",
            market_result="yes",
            revenue_dollars="1.0000",
            settled_time=datetime(2026, 3, 21, 14, 0, 0),
        )
        assert settlement.revenue == 100

    def test_legacy_revenue_preserved(self) -> None:
        """Legacy integer revenue is NOT overwritten when present."""
        settlement = KalshiSettlement(
            ticker="KXHIGHNY-26FEB18-T52",
            market_result="yes",
            revenue=100,
            revenue_dollars="0.5000",
            settled_time=datetime(2026, 2, 19, 14, 0, 0),
        )
        assert settlement.revenue == 100
