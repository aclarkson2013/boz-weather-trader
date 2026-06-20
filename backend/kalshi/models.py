"""Pydantic models for Kalshi API requests and responses.

All prices are in CENTS (integers 1-99), NOT dollars. This matches the
Kalshi API convention and prevents float rounding errors in trading.

Usage:
    from backend.kalshi.models import (
        KalshiMarket, OrderRequest, dollars_to_cents, cents_to_dollars,
    )

    order = OrderRequest(
        ticker="KXHIGHNY-26FEB18-T52",
        action="buy",
        side="yes",
        type="limit",
        count=1,
        yes_price=22,  # $0.22 in cents
    )
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ─── Helper Functions ───


def dollars_to_cents(price: float) -> int:
    """Convert a dollar price to Kalshi API cents.

    Rounds to the nearest cent to handle floating point imprecision.

    Args:
        price: Price in dollars (e.g., 0.22).

    Returns:
        Price in cents as an integer (e.g., 22).
    """
    return int(round(price * 100))


def cents_to_dollars(cents: int) -> float:
    """Convert Kalshi API cents to a dollar price.

    Args:
        cents: Price in cents (e.g., 22).

    Returns:
        Price in dollars as a float (e.g., 0.22).
    """
    return cents / 100.0


# ─── Event & Market Models ───


class KalshiEvent(BaseModel):
    """A Kalshi event containing multiple bracket markets.

    Example: "Highest temperature in NYC on Feb 18?" with 6 bracket markets.
    """

    event_ticker: str
    series_ticker: str
    title: str
    category: str
    status: str
    markets: list[str] = Field(default_factory=list)


class KalshiMarket(BaseModel):
    """A single Kalshi market (bracket) with current pricing.

    Prices are in cents (integers). Edge brackets have one null strike:
    - Bottom edge: floor_strike=None, cap_strike=49
    - Top edge: floor_strike=58, cap_strike=None
    - Middle: floor_strike=49, cap_strike=50

    After the March 2026 fixed-point migration, Kalshi returns dollar-string
    fields (``yes_ask_dollars``, ``volume_fp``, etc.) instead of integer cent
    fields. The ``convert_dollars_to_cents`` model validator transparently
    converts these to integer cents so downstream code is unaffected.
    """

    model_config = ConfigDict(extra="allow")

    ticker: str
    event_ticker: str
    title: str
    subtitle: str | None = None
    status: str
    yes_bid: int = 0
    yes_ask: int = 0
    no_bid: int = 0
    no_ask: int = 0
    last_price: int = 0
    volume: int = 0
    open_interest: int = 0
    floor_strike: float | None = None
    cap_strike: float | None = None
    result: str | None = None
    close_time: datetime | None = None
    expiration_time: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def convert_dollars_to_cents(cls, data: dict) -> dict:
        """Convert Kalshi fixed-point ``_dollars`` fields to integer cents.

        Handles the March 2026 API migration where ``yes_ask``, ``yes_bid``,
        ``no_ask``, ``no_bid``, ``last_price`` (integer cents) were replaced
        by ``yes_ask_dollars``, ``yes_bid_dollars``, etc. (string dollars).
        Also converts ``volume_fp`` and ``open_interest_fp`` string fields.

        If the legacy cent fields are already populated with nonzero values
        (e.g. in tests or cached data), they are left untouched.
        """
        if not isinstance(data, dict):
            return data

        # Price fields: _dollars (string "0.3000") -> cents (int 30)
        dollar_to_cent_pairs = [
            ("yes_ask_dollars", "yes_ask"),
            ("yes_bid_dollars", "yes_bid"),
            ("no_ask_dollars", "no_ask"),
            ("no_bid_dollars", "no_bid"),
            ("last_price_dollars", "last_price"),
        ]
        for dollar_field, cent_field in dollar_to_cent_pairs:
            if dollar_field in data and not data.get(cent_field):
                with contextlib.suppress(ValueError, TypeError):
                    data[cent_field] = int(round(float(data[dollar_field]) * 100))

        # Volume: volume_fp (string "2887.00") -> volume (int 2887)
        if "volume_fp" in data and not data.get("volume"):
            with contextlib.suppress(ValueError, TypeError):
                data["volume"] = int(float(data["volume_fp"]))

        # Open interest: open_interest_fp -> open_interest
        if "open_interest_fp" in data and not data.get("open_interest"):
            with contextlib.suppress(ValueError, TypeError):
                data["open_interest"] = int(float(data["open_interest_fp"]))

        # result: Kalshi now returns "" instead of null for unsettled markets
        if data.get("result") == "":
            data["result"] = None

        return data


class KalshiOrderbook(BaseModel):
    """Current orderbook for a Kalshi market.

    Each entry in yes/no lists is [price_cents, quantity].

    Example:
        yes: [[22, 10], [21, 5]]  # 10 contracts at 22c, 5 at 21c
        no: [[78, 8], [79, 3]]
    """

    yes: list[list[int]] = Field(default_factory=list)
    no: list[list[int]] = Field(default_factory=list)


# ─── Order Models ───


class OrderRequest(BaseModel):
    """A validated order request ready to send to the Kalshi API.

    All validation happens at construction time via field_validators.
    Call validate_for_submission() as an explicit pre-flight check
    before sending to the API.

    Attributes:
        ticker: Market ticker (e.g., "KXHIGHNY-26FEB18-T52").
        action: "buy" or "sell".
        side: "yes" or "no".
        type: "limit" or "market".
        count: Number of contracts (>= 1).
        yes_price: Price in cents (1-99).
    """

    ticker: str
    action: str
    side: str
    type: str
    count: int = Field(ge=1)
    yes_price: int = Field(ge=1, le=99)
    expiration_ts: int | None = None  # Unix timestamp in seconds for auto-expiry

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        """Ensure action is 'buy' or 'sell'."""
        if v not in ("buy", "sell"):
            msg = f"action must be 'buy' or 'sell', got '{v}'"
            raise ValueError(msg)
        return v

    @field_validator("side")
    @classmethod
    def validate_side(cls, v: str) -> str:
        """Ensure side is 'yes' or 'no'."""
        if v not in ("yes", "no"):
            msg = f"side must be 'yes' or 'no', got '{v}'"
            raise ValueError(msg)
        return v

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        """Ensure order type is 'limit' or 'market'."""
        if v not in ("limit", "market"):
            msg = f"type must be 'limit' or 'market', got '{v}'"
            raise ValueError(msg)
        return v

    @field_validator("count")
    @classmethod
    def validate_count(cls, v: int) -> int:
        """Ensure count is at least 1."""
        if v < 1:
            msg = f"count must be >= 1, got {v}"
            raise ValueError(msg)
        return v

    @field_validator("yes_price")
    @classmethod
    def validate_price(cls, v: int) -> int:
        """Ensure yes_price is in valid cent range [1, 99]."""
        if not (1 <= v <= 99):
            msg = f"yes_price must be 1-99 cents, got {v}"
            raise ValueError(msg)
        return v

    def validate_for_submission(self) -> None:
        """Run all validators as an explicit pre-flight check.

        Pydantic validators already ran at construction time, but this
        provides a clear call site for the client to use before sending
        the order to the API. Also checks that the ticker is non-empty.

        Raises:
            ValueError: If the ticker is empty.
        """
        if not self.ticker or not self.ticker.strip():
            msg = "ticker must be a non-empty string"
            raise ValueError(msg)

    def to_api_dict(self) -> dict:
        """Convert to the dict format for the Kalshi v2 POST /portfolio/events/orders API.

        The v2 endpoint uses a single-book bid/ask model with fixed-point dollar
        strings, not the legacy yes/no + cent-integer model. We translate:

        - Buy YES at X cents  → side="bid",  price="{X/100:.4f}"
        - Buy NO  at X cents  → side="ask",  price="{(100-X)/100:.4f}"
            (selling YES at the complementary price is economically identical to
            buying NO at X — Kalshi's matching engine handles the position.)

        time_in_force is hardcoded to "good_till_canceled" because the v2 API
        no longer supports a per-order expiration_ts; the trading scheduler's
        _sync_resting_orders() cancels stale resting orders every 15 minutes
        which preserves the previous 14-min auto-expiry behavior.

        Returns:
            Dict with the v2 payload.
        """
        if self.side == "yes":
            v2_side = "bid"
            yes_side_cents = self.yes_price
        else:
            v2_side = "ask"
            yes_side_cents = 100 - self.yes_price

        return {
            "ticker": self.ticker,
            "side": v2_side,
            "count": f"{self.count}.00",
            "price": f"{yes_side_cents / 100:.4f}",
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
        }


class OrderResponse(BaseModel):
    """Response from a successful order placement on Kalshi.

    The Kalshi v2 API returns many fields; we capture the ones we need
    and allow extras so the model doesn't break on new API fields.

    After the March 2026 fixed-point migration, some fields gained
    ``_dollars`` / ``_fp`` variants. The ``convert_dollars_to_cents``
    model validator converts these transparently.

    Attributes:
        order_id: Unique identifier assigned by Kalshi.
        ticker: Market ticker the order was placed on.
        action: "buy" or "sell".
        side: "yes" or "no".
        type: "limit" or "market".
        fill_count: Number of contracts filled (Kalshi v2 field).
        initial_count: Number of contracts requested (Kalshi v2 field).
        yes_price: Price in cents.
        status: Order status (e.g., "resting", "executed", "canceled").
        created_time: When the order was created.
        taker_fees: Taker fees in cents.
        taker_fill_cost: Total fill cost in cents.
    """

    model_config = ConfigDict(extra="allow")

    order_id: str
    ticker: str
    action: str
    side: str
    type: str
    fill_count: int = 0
    initial_count: int = 0
    yes_price: int = 0
    status: str
    created_time: datetime
    taker_fees: int = 0
    taker_fill_cost: int = 0

    @model_validator(mode="before")
    @classmethod
    def convert_dollars_to_cents(cls, data: dict) -> dict:
        """Convert ``_dollars`` fields from the fixed-point API migration."""
        if not isinstance(data, dict):
            return data

        dollar_to_cent_pairs = [
            ("yes_price_dollars", "yes_price"),
            ("taker_fees_dollars", "taker_fees"),
            ("taker_fill_cost_dollars", "taker_fill_cost"),
        ]
        for dollar_field, cent_field in dollar_to_cent_pairs:
            if dollar_field in data and not data.get(cent_field):
                with contextlib.suppress(ValueError, TypeError):
                    data[cent_field] = int(round(float(data[dollar_field]) * 100))

        # fill_count_fp / initial_count_fp (string int)
        for fp_field, int_field in [
            ("fill_count_fp", "fill_count"),
            ("initial_count_fp", "initial_count"),
        ]:
            if fp_field in data and not data.get(int_field):
                with contextlib.suppress(ValueError, TypeError):
                    data[int_field] = int(float(data[fp_field]))

        return data

    @property
    def count(self) -> int:
        """Backward-compatible count property (returns fill_count)."""
        return self.fill_count

    @classmethod
    def from_v2_place_response(cls, body: dict, request: OrderRequest) -> OrderResponse:
        """Construct an OrderResponse from the v2 POST /portfolio/events/orders body.

        The v2 response is much sparser than the legacy one — it returns only
        ``order_id``, ``client_order_id``, ``fill_count``, ``remaining_count``,
        ``ts_ms``, and (only when filled) ``average_fill_price`` /
        ``average_fee_paid``. The other fields downstream code reads (ticker,
        side, yes_price, action, type, status, created_time, taker_fill_cost,
        taker_fees) we synthesize from the request we just sent and the
        partial response.

        Args:
            body: Parsed JSON body from the v2 endpoint.
            request: The OrderRequest we sent — used to fill missing fields
                with their original (legacy-semantic) values.

        Returns:
            An OrderResponse with the same shape downstream code expects.
        """

        # Counts come back as fixed-point strings ("10.00") on the new API.
        def _to_int(value: object, default: int = 0) -> int:
            if value is None:
                return default
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return default

        fill_count = _to_int(body.get("fill_count"), 0)
        remaining_count = _to_int(body.get("remaining_count"), 0)
        initial_count = fill_count + remaining_count

        # Derive a legacy-style status from the count split.
        if fill_count == 0 and remaining_count > 0:
            status = "resting"
        elif remaining_count == 0 and fill_count > 0:
            status = "executed"
        elif fill_count > 0 and remaining_count > 0:
            # Treat partial fills as executed; the resting remainder will be
            # cancelled by _sync_resting_orders on the next cycle.
            status = "executed"
        else:
            # 0/0 — rare; treat as resting so callers handle it as unfilled.
            status = "resting"

        # average_fill_price and average_fee_paid are dollar strings, present
        # only when at least one contract has filled.
        avg_fill_dollars = body.get("average_fill_price")
        avg_fee_dollars = body.get("average_fee_paid")
        taker_fill_cost_cents = 0
        taker_fees_cents = 0
        if fill_count > 0 and avg_fill_dollars is not None:
            with contextlib.suppress(TypeError, ValueError):
                taker_fill_cost_cents = int(round(float(avg_fill_dollars) * 100 * fill_count))
        if fill_count > 0 and avg_fee_dollars is not None:
            with contextlib.suppress(TypeError, ValueError):
                taker_fees_cents = int(round(float(avg_fee_dollars) * 100 * fill_count))

        # Build created_time from ts_ms if present, otherwise "now".
        ts_ms = body.get("ts_ms")
        if isinstance(ts_ms, (int, float)) and ts_ms > 0:
            created = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
        else:
            created = datetime.now(UTC)

        return cls(
            order_id=str(body.get("order_id", "")),
            ticker=request.ticker,
            action=request.action,
            side=request.side,
            type=request.type,
            fill_count=fill_count,
            initial_count=initial_count if initial_count > 0 else request.count,
            yes_price=request.yes_price,
            status=status,
            created_time=created,
            taker_fees=taker_fees_cents,
            taker_fill_cost=taker_fill_cost_cents,
        )


# ─── Position & Settlement Models ───


class KalshiPosition(BaseModel):
    """A current open position on Kalshi.

    All monetary values are in cents.

    Attributes:
        ticker: Market ticker.
        market_exposure: Current exposure in cents.
        resting_orders_count: Number of unfilled resting orders.
        total_traded: Total number of contracts traded.
        realized_pnl: Realized profit/loss in cents.
    """

    ticker: str
    market_exposure: int = 0
    resting_orders_count: int = 0
    total_traded: int = 0
    realized_pnl: int = 0


class KalshiSettlement(BaseModel):
    """A settled market position with outcome.

    Attributes:
        ticker: Market ticker.
        market_result: Settlement result (e.g., "yes", "no").
        revenue: Revenue in cents.
        settled_time: When the market was settled.
    """

    model_config = ConfigDict(extra="allow")

    ticker: str
    market_result: str
    revenue: int = 0
    settled_time: datetime

    @model_validator(mode="before")
    @classmethod
    def convert_dollars_to_cents(cls, data: dict) -> dict:
        """Convert ``revenue_dollars`` from the fixed-point API migration."""
        if not isinstance(data, dict):
            return data

        if "revenue_dollars" in data and not data.get("revenue"):
            with contextlib.suppress(ValueError, TypeError):
                data["revenue"] = int(round(float(data["revenue_dollars"]) * 100))

        return data
