"""FastAPI dependencies and ORM-to-schema converters.

Provides dependency injection for authenticated users, Kalshi clients,
and helper functions to convert ORM models to Pydantic response schemas.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import datetime

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.database import get_db
from backend.common.encryption import decrypt_api_key
from backend.common.logging import get_logger
from backend.common.models import PendingTradeModel, Prediction, Trade, User
from backend.common.schemas import (
    BracketPrediction,
    BracketProbability,
    PendingTrade,
    TradeRecord,
    UserSettings,
    parse_enabled_weather_sources,
)
from backend.kalshi.client import KalshiClient

logger = get_logger("API")


async def get_current_user(db: AsyncSession = Depends(get_db)) -> User:
    """Return the first user in the database, or raise 401 if none exists.

    This is a single-user system (v1). The first User record represents
    the authenticated account.

    Args:
        db: Async database session from FastAPI dependency injection.

    Returns:
        The User ORM model.

    Raises:
        HTTPException: 401 if no user record exists (not onboarded).
    """
    result = await db.execute(select(User).limit(1))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated — complete onboarding first")
    return user


async def get_kalshi_client(
    user: User = Depends(get_current_user),
) -> AsyncGenerator[KalshiClient, None]:
    """Provide an authenticated KalshiClient for the request lifecycle.

    Decrypts the user's stored private key, creates a KalshiClient,
    yields it for the endpoint to use, and closes it after the request.

    Args:
        user: The authenticated User from dependency injection.

    Yields:
        An authenticated KalshiClient instance.
    """
    private_key_pem = decrypt_api_key(user.encrypted_private_key)
    demo = user.demo_mode if user.demo_mode is not None else True
    client = KalshiClient(
        api_key_id=user.kalshi_key_id,
        private_key_pem=private_key_pem,
        demo=demo,
    )
    try:
        yield client
    finally:
        await client.close()


def user_to_settings(user: User) -> UserSettings:
    """Convert a User ORM model to a UserSettings Pydantic schema.

    Handles the conversion of the comma-separated active_cities string
    to a list of CityCode literals.

    Args:
        user: The User ORM model.

    Returns:
        A UserSettings schema populated from the user's columns.
    """
    # Parse active_cities from comma-separated string to list
    cities_str = user.active_cities or "NYC,CHI,MIA,AUS"
    active_cities = [c.strip() for c in cities_str.split(",") if c.strip()]

    # Parse enabled_weather_sources (drops unknown names, falls back to the
    # default set if too few survive).
    enabled_sources = parse_enabled_weather_sources(user.enabled_weather_sources)

    ev_thresh = user.min_ev_threshold if user.min_ev_threshold is not None else 0.05
    ev_thresh_yes = user.min_ev_threshold_yes if user.min_ev_threshold_yes is not None else 0.15
    ev_thresh_no = user.min_ev_threshold_no if user.min_ev_threshold_no is not None else 0.05
    cooldown = user.cooldown_per_loss_minutes if user.cooldown_per_loss_minutes is not None else 60
    consec = user.consecutive_loss_limit if user.consecutive_loss_limit is not None else 3
    notifs = user.notifications_enabled if user.notifications_enabled is not None else True

    demo = user.demo_mode if user.demo_mode is not None else True

    return UserSettings(
        trading_mode=user.trading_mode or "manual",
        max_trade_size_cents=user.max_trade_size_cents or 100,
        daily_loss_limit_cents=user.daily_loss_limit_cents or 1000,
        max_daily_exposure_cents=user.max_daily_exposure_cents or 2500,
        min_ev_threshold=ev_thresh,
        min_ev_threshold_yes=ev_thresh_yes,
        min_ev_threshold_no=ev_thresh_no,
        cooldown_per_loss_minutes=cooldown,
        consecutive_loss_limit=consec,
        active_cities=active_cities,
        enabled_weather_sources=enabled_sources,
        demo_mode=demo,
        notifications_enabled=notifs,
        use_kelly_sizing=user.use_kelly_sizing if user.use_kelly_sizing is not None else False,
        kelly_fraction=user.kelly_fraction if user.kelly_fraction is not None else 0.25,
        max_bankroll_pct_per_trade=user.max_bankroll_pct_per_trade
        if user.max_bankroll_pct_per_trade is not None
        else 0.05,
        max_contracts_per_trade=user.max_contracts_per_trade
        if user.max_contracts_per_trade is not None
        else 10,
        max_contracts_per_bracket=user.max_contracts_per_bracket
        if user.max_contracts_per_bracket is not None
        else 3,
        enable_consecutive_loss_limit=user.enable_consecutive_loss_limit
        if user.enable_consecutive_loss_limit is not None
        else True,
        enable_per_loss_cooldown=user.enable_per_loss_cooldown
        if user.enable_per_loss_cooldown is not None
        else True,
        model_weight=user.model_weight if user.model_weight is not None else 0.4,
        max_model_market_divergence=user.max_model_market_divergence
        if user.max_model_market_divergence is not None
        else 0.25,
        min_market_prob_for_yes=user.min_market_prob_for_yes
        if user.min_market_prob_for_yes is not None
        else 0.15,
        fee_estimate_mode=user.fee_estimate_mode or "realistic",
        timezone=user.timezone if user.timezone is not None else "",
    )


def trade_to_record(trade: Trade) -> TradeRecord:
    """Convert a Trade ORM model to a TradeRecord Pydantic schema.

    Handles enum-to-string conversion for city and status fields,
    and datetime-to-date conversion for trade_date.

    Args:
        trade: The Trade ORM model.

    Returns:
        A TradeRecord schema populated from the trade's columns.
    """
    # Handle city: may be a CityEnum or a plain string
    city = trade.city.value if hasattr(trade.city, "value") else trade.city

    # Handle trade_date: may be a datetime, convert to date
    trade_date = trade.trade_date
    if isinstance(trade_date, datetime):
        trade_date = trade_date.date()

    # Handle status: may be a TradeStatus enum or a plain string
    status = trade.status.value if hasattr(trade.status, "value") else trade.status

    return TradeRecord(
        id=trade.id,
        kalshi_order_id=trade.kalshi_order_id,
        city=city,
        date=trade_date,
        market_ticker=trade.market_ticker,
        bracket_label=trade.bracket_label,
        side=trade.side,
        price_cents=trade.price_cents,
        quantity=trade.quantity,
        model_probability=trade.model_probability,
        market_probability=trade.market_probability,
        ev_at_entry=trade.ev_at_entry,
        confidence=trade.confidence,
        status=status,
        settlement_temp_f=trade.settlement_temp_f,
        settlement_source=trade.settlement_source,
        pnl_cents=trade.pnl_cents,
        fees_cents=trade.fees_cents,
        postmortem_narrative=trade.postmortem_narrative,
        created_at=trade.created_at,
        settled_at=trade.settled_at,
    )


def pending_to_schema(model: PendingTradeModel) -> PendingTrade:
    """Convert a PendingTradeModel ORM to a PendingTrade Pydantic schema.

    Maps the ORM's bracket_label field to the schema's bracket field.

    Args:
        model: The PendingTradeModel ORM instance.

    Returns:
        A PendingTrade schema populated from the model's columns.
    """
    city = model.city.value if hasattr(model.city, "value") else model.city
    status = model.status.value if hasattr(model.status, "value") else model.status

    return PendingTrade(
        id=model.id,
        city=city,
        bracket=model.bracket_label,
        market_ticker=model.market_ticker,
        side=model.side,
        price_cents=model.price_cents,
        quantity=model.quantity,
        model_probability=model.model_probability,
        market_probability=model.market_probability,
        ev=model.ev,
        confidence=model.confidence,
        reasoning=model.reasoning or "",
        status=status,
        created_at=model.created_at,
        expires_at=model.expires_at,
        acted_at=model.acted_at,
    )


def prediction_to_schema(pred: Prediction) -> BracketPrediction:
    """Convert a Prediction ORM model to a BracketPrediction Pydantic schema.

    Parses brackets_json (list of dicts or JSON string) and model_sources
    (comma-separated string) from the database record.

    Args:
        pred: The Prediction ORM instance.

    Returns:
        A BracketPrediction schema populated from the prediction's columns.
    """
    # Parse brackets from JSON column
    brackets_data = pred.brackets_json
    if isinstance(brackets_data, str):
        brackets_data = json.loads(brackets_data)

    brackets = [BracketProbability(**b) for b in brackets_data]

    # Parse model_sources from comma-separated string
    model_sources = [s.strip() for s in (pred.model_sources or "").split(",") if s.strip()]

    # Handle city enum
    city = pred.city.value if hasattr(pred.city, "value") else pred.city

    # Handle prediction_date: may be datetime, convert to date
    pred_date = pred.prediction_date
    if isinstance(pred_date, datetime):
        pred_date = pred_date.date()

    return BracketPrediction(
        city=city,
        date=pred_date,
        brackets=brackets,
        ensemble_mean_f=pred.ensemble_mean_f,
        ensemble_std_f=pred.ensemble_std_f,
        confidence=pred.confidence,
        model_sources=model_sources,
        generated_at=pred.generated_at,
    )
