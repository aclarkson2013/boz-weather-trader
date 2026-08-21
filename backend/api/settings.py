"""User settings endpoints.

Provides read and partial-update access to the user's trading
configuration, risk limits, and notification preferences.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_current_user, user_to_settings
from backend.api.response_schemas import SettingsUpdate
from backend.common.database import get_db
from backend.common.logging import get_logger
from backend.common.models import User
from backend.common.schemas import (
    ALL_WEATHER_SOURCES,
    MIN_ENABLED_WEATHER_SOURCES,
    UserSettings,
)

logger = get_logger("API")

router = APIRouter()


@router.get("", response_model=UserSettings)
async def get_settings_endpoint(
    user: User = Depends(get_current_user),
) -> UserSettings:
    """Fetch the current user settings.

    Args:
        user: The authenticated user.

    Returns:
        UserSettings schema with all current configuration values.
    """
    return user_to_settings(user)


@router.patch("", response_model=UserSettings)
async def update_settings(
    updates: SettingsUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserSettings:
    """Partially update user settings.

    Only fields included in the request body (non-None) are updated.
    The active_cities and enabled_weather_sources lists are stored as
    comma-separated strings.

    Args:
        updates: Partial settings update with only changed fields.
        user: The authenticated user.
        db: Async database session.

    Returns:
        The full updated UserSettings schema.
    """
    # Get only the fields that were explicitly provided (non-None)
    update_data = updates.model_dump(exclude_none=True)

    for field_name, value in update_data.items():
        if field_name == "active_cities":
            # Convert list of city codes to comma-separated string
            user.active_cities = ",".join(value)
        elif field_name == "enabled_weather_sources":
            # Stored comma-separated, same as active_cities. Preserve the
            # canonical order so the value is stable regardless of click order.
            ordered = [s for s in ALL_WEATHER_SOURCES if s in set(value)]
            if len(ordered) < MIN_ENABLED_WEATHER_SOURCES:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"At least {MIN_ENABLED_WEATHER_SOURCES} weather sources must "
                        "stay enabled — the ensemble needs a spread."
                    ),
                )
            user.enabled_weather_sources = ",".join(ordered)
        else:
            setattr(user, field_name, value)

    await db.commit()

    logger.info(
        "User settings updated",
        extra={
            "data": {
                "user_id": user.id,
                "updated_fields": list(update_data.keys()),
            }
        },
    )

    return user_to_settings(user)
