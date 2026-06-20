"""Tests for backend.trading.executor -- execute_trade places orders and records them.

All prices are in CENTS (integers). Trade records are stored in the database.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.common.exceptions import InvalidOrderError
from backend.common.schemas import TradeRecord, TradeSignal
from backend.trading.executor import execute_trade


def _make_mock_response(
    order_id: str = "order-123",
    count: int = 1,
    status: str = "filled",
    taker_fill_cost: int | None = None,
    taker_fees: int = 0,
) -> MagicMock:
    """Create a mock order response from Kalshi.

    If taker_fill_cost is not provided, defaults to 22 * count (assuming 22¢ fill).
    """
    mock = MagicMock()
    mock.order_id = order_id
    mock.count = count
    mock.status = status
    mock.taker_fill_cost = taker_fill_cost if taker_fill_cost is not None else 22 * count
    mock.taker_fees = taker_fees
    return mock


class TestExecuteTrade:
    """Tests for execute_trade -- the full order placement flow."""

    @pytest.mark.asyncio
    async def test_successful_execution(
        self, sample_signal: TradeSignal, mock_db: AsyncMock, mock_kalshi_client: AsyncMock
    ) -> None:
        """A successful execution returns a TradeRecord with correct fields."""
        result = await execute_trade(
            signal=sample_signal,
            kalshi_client=mock_kalshi_client,
            db=mock_db,
            user_id="test-user",
        )
        assert isinstance(result, TradeRecord)
        assert result.city == "NYC"
        assert result.side == "yes"
        assert result.status == "OPEN"

    @pytest.mark.asyncio
    async def test_records_trade_in_db(
        self, sample_signal: TradeSignal, mock_db: AsyncMock, mock_kalshi_client: AsyncMock
    ) -> None:
        """db.add is called with a Trade instance."""
        await execute_trade(
            signal=sample_signal,
            kalshi_client=mock_kalshi_client,
            db=mock_db,
            user_id="test-user",
        )
        mock_db.add.assert_called_once()
        mock_db.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_actual_fill_price(
        self, sample_signal: TradeSignal, mock_db: AsyncMock, mock_kalshi_client: AsyncMock
    ) -> None:
        """TradeRecord.price_cents reflects the actual fill price from Kalshi."""
        # mock_kalshi_client has taker_fill_cost=22 for 1 contract → 22¢ per contract
        result = await execute_trade(
            signal=sample_signal,
            kalshi_client=mock_kalshi_client,
            db=mock_db,
            user_id="test-user",
        )
        assert result.price_cents == 22

    @pytest.mark.asyncio
    async def test_fill_price_differs_from_limit_price(
        self, sample_signal: TradeSignal, mock_db: AsyncMock
    ) -> None:
        """When actual fill price differs from limit, the fill price is recorded."""
        mock_client = AsyncMock()
        # Limit was 22¢ but filled at 20¢ (price improvement)
        mock_client.place_order.return_value = _make_mock_response(taker_fill_cost=20, count=1)

        result = await execute_trade(
            signal=sample_signal,
            kalshi_client=mock_client,
            db=mock_db,
            user_id="test-user",
        )
        # Should use 20¢ fill price, not 22¢ limit price
        assert result.price_cents == 20

    @pytest.mark.asyncio
    async def test_fill_price_multi_contract(self, mock_db: AsyncMock) -> None:
        """Fill price is correctly computed for multi-contract orders."""
        signal = TradeSignal(
            city="NYC",
            bracket="55-56°F",
            side="yes",
            price_cents=22,
            quantity=3,
            model_probability=0.30,
            market_probability=0.22,
            ev=0.05,
            confidence="medium",
            market_ticker="KXHIGHNY-26FEB18-B3",
        )
        mock_client = AsyncMock()
        # 3 contracts × 20¢ each = 60¢ total fill cost
        mock_client.place_order.return_value = _make_mock_response(taker_fill_cost=60, count=3)

        result = await execute_trade(
            signal=signal,
            kalshi_client=mock_client,
            db=mock_db,
            user_id="test-user",
        )
        assert result.price_cents == 20  # 60 // 3 = 20
        assert result.quantity == 3

    @pytest.mark.asyncio
    async def test_fallback_to_limit_price_when_no_fill_cost(
        self, sample_signal: TradeSignal, mock_db: AsyncMock
    ) -> None:
        """Falls back to signal limit price when taker_fill_cost is 0."""
        mock_client = AsyncMock()
        mock_client.place_order.return_value = _make_mock_response(taker_fill_cost=0, count=1)

        result = await execute_trade(
            signal=sample_signal,
            kalshi_client=mock_client,
            db=mock_db,
            user_id="test-user",
        )
        # Falls back to signal's limit price of 22¢
        assert result.price_cents == sample_signal.price_cents

    @pytest.mark.asyncio
    async def test_db_trade_uses_fill_price(
        self, sample_signal: TradeSignal, mock_db: AsyncMock
    ) -> None:
        """The Trade ORM record stored in DB uses the fill price, not limit price."""
        mock_client = AsyncMock()
        # Fill at 18¢ instead of 22¢ limit
        mock_client.place_order.return_value = _make_mock_response(taker_fill_cost=18, count=1)

        await execute_trade(
            signal=sample_signal,
            kalshi_client=mock_client,
            db=mock_db,
            user_id="test-user",
        )

        trade_obj = mock_db.add.call_args[0][0]
        assert trade_obj.price_cents == 18  # Fill price, not 22¢ limit

    @pytest.mark.asyncio
    async def test_api_failure_propagates(
        self, sample_signal: TradeSignal, mock_db: AsyncMock
    ) -> None:
        """When kalshi_client.place_order raises, the exception propagates."""
        mock_client = AsyncMock()
        mock_client.place_order.side_effect = ConnectionError("API unreachable")

        with pytest.raises(ConnectionError, match="API unreachable"):
            await execute_trade(
                signal=sample_signal,
                kalshi_client=mock_client,
                db=mock_db,
                user_id="test-user",
            )

    @pytest.mark.asyncio
    async def test_canceled_order_raises(
        self, sample_signal: TradeSignal, mock_db: AsyncMock
    ) -> None:
        """When response.status == 'canceled', InvalidOrderError is raised."""
        mock_client = AsyncMock()
        mock_client.place_order.return_value = _make_mock_response(status="canceled")

        with pytest.raises(InvalidOrderError):
            await execute_trade(
                signal=sample_signal,
                kalshi_client=mock_client,
                db=mock_db,
                user_id="test-user",
            )

    @pytest.mark.asyncio
    async def test_partial_fill_logged(
        self, sample_signal: TradeSignal, mock_db: AsyncMock
    ) -> None:
        """A response with 'resting' status (partial fill) still records the trade."""
        mock_client = AsyncMock()
        mock_client.place_order.return_value = _make_mock_response(status="resting", count=1)
        # resting is NOT canceled, so should proceed
        result = await execute_trade(
            signal=sample_signal,
            kalshi_client=mock_client,
            db=mock_db,
            user_id="test-user",
        )
        assert isinstance(result, TradeRecord)

    @pytest.mark.asyncio
    async def test_trade_id_is_uuid(
        self, sample_signal: TradeSignal, mock_db: AsyncMock, mock_kalshi_client: AsyncMock
    ) -> None:
        """The returned trade id is a valid UUID string."""
        result = await execute_trade(
            signal=sample_signal,
            kalshi_client=mock_kalshi_client,
            db=mock_db,
            user_id="test-user",
        )
        # Should not raise
        parsed = uuid.UUID(result.id)
        assert str(parsed) == result.id

    @pytest.mark.asyncio
    async def test_status_is_open(
        self, sample_signal: TradeSignal, mock_db: AsyncMock, mock_kalshi_client: AsyncMock
    ) -> None:
        """The returned status is 'OPEN'."""
        result = await execute_trade(
            signal=sample_signal,
            kalshi_client=mock_kalshi_client,
            db=mock_db,
            user_id="test-user",
        )
        assert result.status == "OPEN"

    @pytest.mark.asyncio
    async def test_trade_gets_market_date_from_ticker(
        self, mock_db: AsyncMock, mock_kalshi_client: AsyncMock
    ) -> None:
        """Trade ORM record has market_date parsed from the ticker."""
        from datetime import date

        from backend.common.schemas import TradeSignal

        signal = TradeSignal(
            city="AUS",
            bracket="63-65°F",
            side="yes",
            price_cents=35,
            quantity=1,
            model_probability=0.45,
            market_probability=0.35,
            ev=0.08,
            confidence="medium",
            market_ticker="KXHIGHAUS-26FEB23-T63",
            reasoning="test market_date",
        )

        await execute_trade(
            signal=signal,
            kalshi_client=mock_kalshi_client,
            db=mock_db,
            user_id="test-user",
        )

        # The Trade ORM added to DB should have market_date set
        trade_obj = mock_db.add.call_args[0][0]
        assert trade_obj.market_date == date(2026, 2, 23)

    @pytest.mark.asyncio
    async def test_no_side_fill_price_is_yes_equivalent(self, mock_db: AsyncMock) -> None:
        """For NO side trades, price_cents is stored as the YES-equivalent.

        When Kalshi returns taker_fill_cost=59 for a NO fill, the raw NO
        cost per contract is 59¢. The YES-equivalent is 100-59=41¢.
        Settlement code expects price_cents to be the YES price so that
        NO cost = (100 - price_cents) * qty works correctly.
        """
        signal = TradeSignal(
            city="AUS",
            bracket="65-66°F",
            side="no",
            price_cents=40,
            quantity=1,
            model_probability=0.60,
            market_probability=0.40,
            ev=0.12,
            confidence="high",
            market_ticker="KXHIGHAUS-26FEB23-B65.5",
        )
        mock_client = AsyncMock()
        # NO fill: taker_fill_cost=59 means you paid 59¢ for the NO contract
        mock_client.place_order.return_value = _make_mock_response(taker_fill_cost=59, count=1)

        result = await execute_trade(
            signal=signal,
            kalshi_client=mock_client,
            db=mock_db,
            user_id="test-user",
        )

        # Should store YES-equivalent: 100 - 59 = 41
        assert result.price_cents == 41
        trade_obj = mock_db.add.call_args[0][0]
        assert trade_obj.price_cents == 41

    @pytest.mark.asyncio
    async def test_no_side_fallback_converts_to_actual_cost(self, mock_db: AsyncMock) -> None:
        """When taker_fill_cost is 0 for NO side, converts YES price to actual NO cost."""
        signal = TradeSignal(
            city="AUS",
            bracket="65-66°F",
            side="no",
            price_cents=40,  # YES market price
            quantity=1,
            model_probability=0.60,
            market_probability=0.40,
            ev=0.12,
            confidence="high",
            market_ticker="KXHIGHAUS-26FEB23-B65.5",
        )
        mock_client = AsyncMock()
        mock_client.place_order.return_value = _make_mock_response(taker_fill_cost=0, count=1)

        result = await execute_trade(
            signal=signal,
            kalshi_client=mock_client,
            db=mock_db,
            user_id="test-user",
        )

        # price_cents = 100 - 40 = 60 (actual NO cost, not YES market price)
        assert result.price_cents == 60

    @pytest.mark.asyncio
    async def test_yes_side_fill_price_not_converted(self, mock_db: AsyncMock) -> None:
        """For YES side trades, fill price is stored directly (no conversion)."""
        signal = TradeSignal(
            city="NYC",
            bracket="55-56°F",
            side="yes",
            price_cents=22,
            quantity=1,
            model_probability=0.30,
            market_probability=0.22,
            ev=0.05,
            confidence="medium",
            market_ticker="KXHIGHNY-26FEB18-B3",
        )
        mock_client = AsyncMock()
        mock_client.place_order.return_value = _make_mock_response(taker_fill_cost=20, count=1)

        result = await execute_trade(
            signal=signal,
            kalshi_client=mock_client,
            db=mock_db,
            user_id="test-user",
        )

        # YES side: store the fill price directly (20¢), NOT 100-20=80
        assert result.price_cents == 20

    @pytest.mark.asyncio
    async def test_resting_unfilled_returns_resting_status(
        self, sample_signal: TradeSignal, mock_db: AsyncMock
    ) -> None:
        """When order is resting with 0 fills, trade is recorded as RESTING."""
        mock_client = AsyncMock()
        mock_client.place_order.return_value = _make_mock_response(
            status="resting", count=0, taker_fill_cost=0
        )

        result = await execute_trade(
            signal=sample_signal,
            kalshi_client=mock_client,
            db=mock_db,
            user_id="test-user",
        )

        assert result.status == "RESTING"
        assert result.quantity == sample_signal.quantity
        assert result.price_cents == sample_signal.price_cents

    @pytest.mark.asyncio
    async def test_resting_unfilled_records_in_db(
        self, sample_signal: TradeSignal, mock_db: AsyncMock
    ) -> None:
        """RESTING trade is persisted in the database with correct status."""
        mock_client = AsyncMock()
        mock_client.place_order.return_value = _make_mock_response(
            status="resting", count=0, taker_fill_cost=0
        )

        await execute_trade(
            signal=sample_signal,
            kalshi_client=mock_client,
            db=mock_db,
            user_id="test-user",
        )

        mock_db.add.assert_called_once()
        trade_obj = mock_db.add.call_args[0][0]
        assert trade_obj.status.value == "RESTING"
        assert trade_obj.quantity == sample_signal.quantity
        assert trade_obj.price_cents == sample_signal.price_cents

    @pytest.mark.asyncio
    async def test_resting_unfilled_does_not_cancel(
        self, sample_signal: TradeSignal, mock_db: AsyncMock
    ) -> None:
        """Unfilled resting orders are NOT canceled — they auto-expire via expiration_ts."""
        mock_client = AsyncMock()
        mock_client.place_order.return_value = _make_mock_response(
            status="resting", count=0, taker_fill_cost=0
        )

        await execute_trade(
            signal=sample_signal,
            kalshi_client=mock_client,
            db=mock_db,
            user_id="test-user",
        )

        # cancel_order should NOT have been called
        mock_client.cancel_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_order_includes_expiration_ts(
        self, sample_signal: TradeSignal, mock_db: AsyncMock, mock_kalshi_client: AsyncMock
    ) -> None:
        """OrderRequest sent to Kalshi includes expiration_ts for auto-expiry."""
        await execute_trade(
            signal=sample_signal,
            kalshi_client=mock_kalshi_client,
            db=mock_db,
            user_id="test-user",
        )

        # Extract the OrderRequest passed to place_order
        order_arg = mock_kalshi_client.place_order.call_args[0][0]
        assert order_arg.expiration_ts is not None
        assert order_arg.expiration_ts > 0

    @pytest.mark.asyncio
    async def test_api_dict_uses_good_till_canceled_tif(
        self, sample_signal: TradeSignal, mock_db: AsyncMock, mock_kalshi_client: AsyncMock
    ) -> None:
        """The v2 payload uses time_in_force=good_till_canceled (no expiration_ts).

        The v2 API dropped per-order expiry; we rely on _sync_resting_orders
        to cancel stale resting orders each 15-minute trading cycle to
        preserve the previous auto-expiry behavior.
        """
        await execute_trade(
            signal=sample_signal,
            kalshi_client=mock_kalshi_client,
            db=mock_db,
            user_id="test-user",
        )

        order_arg = mock_kalshi_client.place_order.call_args[0][0]
        api_dict = order_arg.to_api_dict()
        assert "expiration_ts" not in api_dict
        assert api_dict["time_in_force"] == "good_till_canceled"


class TestNoSidePriceCents:
    """Verify price_cents stores actual NO cost (not YES market price)."""

    @pytest.mark.asyncio
    async def test_no_side_price_stores_actual_cost_when_taker_fill_cost_zero(
        self, mock_db: AsyncMock
    ) -> None:
        """When taker_fill_cost is 0 (fallback), NO price_cents = 100 - signal.price_cents."""
        signal = TradeSignal(
            city="NYC",
            bracket="55-56°F",
            side="no",
            price_cents=22,  # YES market price
            quantity=1,
            model_probability=0.30,
            market_probability=0.22,
            ev=0.05,
            confidence="medium",
            market_ticker="KXHIGHNY-26FEB18-B3",
            reasoning="test",
        )

        mock_response = MagicMock()
        mock_response.order_id = "order-no-1"
        mock_response.count = 1
        mock_response.status = "filled"
        mock_response.taker_fill_cost = 0  # Fallback path!
        mock_response.taker_fees = 0

        client = AsyncMock()
        client.place_order.return_value = mock_response

        result = await execute_trade(
            signal=signal,
            kalshi_client=client,
            db=mock_db,
            user_id="test-user",
        )

        # price_cents should be 100 - 22 = 78 (actual NO cost), not 22 (YES price)
        assert result.price_cents == 78

    @pytest.mark.asyncio
    async def test_no_side_price_stores_actual_cost_when_taker_fill_cost_positive(
        self, mock_db: AsyncMock
    ) -> None:
        """When taker_fill_cost is positive, NO price_cents = 100 - (cost // count)."""
        signal = TradeSignal(
            city="NYC",
            bracket="55-56°F",
            side="no",
            price_cents=22,
            quantity=1,
            model_probability=0.30,
            market_probability=0.22,
            ev=0.05,
            confidence="medium",
            market_ticker="KXHIGHNY-26FEB18-B3",
            reasoning="test",
        )

        mock_response = MagicMock()
        mock_response.order_id = "order-no-2"
        mock_response.count = 1
        mock_response.status = "filled"
        mock_response.taker_fill_cost = 20  # YES-equivalent = 20c per contract
        mock_response.taker_fees = 0

        client = AsyncMock()
        client.place_order.return_value = mock_response

        result = await execute_trade(
            signal=signal,
            kalshi_client=client,
            db=mock_db,
            user_id="test-user",
        )

        # price_cents should be 100 - 20 = 80 (actual NO cost)
        assert result.price_cents == 80

    @pytest.mark.asyncio
    async def test_resting_no_order_stores_actual_cost(self, mock_db: AsyncMock) -> None:
        """Resting NO orders store 100 - signal.price_cents as price_cents."""
        signal = TradeSignal(
            city="AUS",
            bracket="90° to 91°F",
            side="no",
            price_cents=37,  # YES market price
            quantity=1,
            model_probability=0.06,
            market_probability=0.37,
            ev=0.05,
            confidence="low",
            market_ticker="KXHIGHAUS-26MAR24-B87.5",
            reasoning="test",
        )

        mock_response = MagicMock()
        mock_response.order_id = "order-rest-1"
        mock_response.count = 0  # No fills — fully resting
        mock_response.status = "resting"
        mock_response.taker_fill_cost = 0
        mock_response.taker_fees = 0

        client = AsyncMock()
        client.place_order.return_value = mock_response

        result = await execute_trade(
            signal=signal,
            kalshi_client=client,
            db=mock_db,
            user_id="test-user",
        )

        # Resting NO: price_cents should be 100 - 37 = 63 (actual NO cost)
        assert result.price_cents == 63
        assert result.status == "RESTING"

    @pytest.mark.asyncio
    async def test_yes_side_price_unchanged_in_fallback(self, mock_db: AsyncMock) -> None:
        """YES-side trades are unaffected — price_cents = signal.price_cents."""
        signal = TradeSignal(
            city="NYC",
            bracket="55-56°F",
            side="yes",
            price_cents=22,
            quantity=1,
            model_probability=0.30,
            market_probability=0.22,
            ev=0.05,
            confidence="medium",
            market_ticker="KXHIGHNY-26FEB18-B3",
            reasoning="test",
        )

        mock_response = MagicMock()
        mock_response.order_id = "order-yes-1"
        mock_response.count = 1
        mock_response.status = "filled"
        mock_response.taker_fill_cost = 0  # Fallback path
        mock_response.taker_fees = 0

        client = AsyncMock()
        client.place_order.return_value = mock_response

        result = await execute_trade(
            signal=signal,
            kalshi_client=client,
            db=mock_db,
            user_id="test-user",
        )

        # YES side: price_cents stays as-is
        assert result.price_cents == 22
