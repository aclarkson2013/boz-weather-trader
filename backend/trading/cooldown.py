"""Cooldown timer management for the trading engine.

Manages two types of cooldown:
1. Per-loss cooldown: After each losing trade, pause for N minutes
   (configurable via UserSettings.cooldown_per_loss_minutes).
2. Consecutive-loss cooldown: After N consecutive losses, pause for
   the rest of the trading day (cooldown_until set to 23:59:59 ET).

The DailyRiskState model tracks cooldowns via:
- cooldown_until: DateTime when the current cooldown expires
- consecutive_losses: Running count of consecutive losses today

Since there is no separate rest_of_day_cooldown field, we implement
"rest of day" by setting cooldown_until to 23:59:59 ET of the current
trading day.

State transitions:
    Trade Loss:
        -> Set cooldown_until = now + cooldown_per_loss_minutes
        -> Increment consecutive_losses
        -> If consecutive_losses >= limit: set cooldown_until to end of day

    Trade Win:
        -> Reset consecutive_losses to 0
        -> Per-loss cooldown timer is NOT cleared (it expires naturally)

    New Trading Day:
        -> Fresh DailyRiskState row (all counters at zero, no cooldown)

Usage:
    from backend.trading.cooldown import CooldownManager

    cm = CooldownManager(settings=user_settings, db=session, user_id="u123")
    is_active, reason = await cm.is_cooldown_active()
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.logging import get_logger
from backend.common.models import DailyRiskState
from backend.common.schemas import UserSettings

logger = get_logger("COOLDOWN")
ET = ZoneInfo("America/New_York")


class CooldownManager:
    """Manages per-loss and consecutive-loss cooldown timers.

    All cooldown state is stored in the DailyRiskState table, scoped
    to a specific user and trading day.

    Args:
        settings: User-configurable trading parameters.
        db: Async SQLAlchemy session.
        user_id: The user ID for scoping cooldown checks.
    """

    def __init__(
        self,
        settings: UserSettings,
        db: AsyncSession,
        user_id: str,
    ) -> None:
        self.settings = settings
        self.db = db
        self.user_id = user_id

    async def is_cooldown_active(self) -> tuple[bool, str]:
        """Check if any cooldown is currently active.

        Checks the cooldown_until field on today's DailyRiskState.
        A cooldown_until value set to end-of-day (23:59:59 ET) indicates
        a consecutive-loss "rest of day" cooldown.

        Returns:
            Tuple of (is_active: bool, reason: str).
            If not active, reason is an empty string.
        """
        state = await self._get_daily_state()
        if state is None:
            return False, ""

        now = datetime.now(ET)

        # Check cooldown_until (covers both per-loss and consecutive-loss)
        if state.cooldown_until is not None:
            # Ensure we compare timezone-aware datetimes
            cooldown_until = state.cooldown_until
            if cooldown_until.tzinfo is None:
                cooldown_until = cooldown_until.replace(tzinfo=ET)

            if now < cooldown_until:
                remaining = (cooldown_until - now).total_seconds() / 60

                # Determine if this is a rest-of-day cooldown. Ending at ~23:59:59 alone is
                # NOT enough: a per-loss cooldown started at ~22:59 ends there too. Only treat
                # it as rest-of-day when the consecutive-loss limit was actually reached.
                end_of_day = _get_end_of_trading_day()
                if cooldown_until.tzinfo is None:
                    end_of_day_naive = end_of_day.replace(tzinfo=None)
                    ends_at_eod = abs((cooldown_until - end_of_day_naive).total_seconds()) < 60
                else:
                    ends_at_eod = abs((cooldown_until - end_of_day).total_seconds()) < 60
                limit = self.settings.consecutive_loss_limit
                limit_reached = limit > 0 and (state.consecutive_losses or 0) >= limit
                is_rest_of_day = ends_at_eod and limit_reached

                if is_rest_of_day:
                    # If the toggle is off, clear stale rest-of-day cooldown
                    if not self.settings.enable_consecutive_loss_limit:
                        state.cooldown_until = None
                        await self.db.flush()
                        return False, ""

                    reason = "Consecutive loss limit hit -- paused for rest of trading day"
                    logger.info(
                        "Cooldown active",
                        extra={
                            "data": {
                                "type": "consecutive_loss",
                                "consecutive_losses": state.consecutive_losses,
                            }
                        },
                    )
                else:
                    # If the per-loss cooldown toggle is off, clear it
                    if not self.settings.enable_per_loss_cooldown:
                        state.cooldown_until = None
                        await self.db.flush()
                        return False, ""

                    reason = f"Per-loss cooldown: {remaining:.0f} min remaining"
                    logger.info(
                        "Cooldown active",
                        extra={
                            "data": {
                                "type": "per_loss",
                                "remaining_min": round(remaining, 1),
                            }
                        },
                    )

                return True, reason

        return False, ""

    async def on_trade_loss(self) -> None:
        """Called when a trade settles as a loss. Updates cooldown state.

        Actions:
        1. If cooldown_per_loss_minutes > 0: set cooldown_until to
           now + cooldown minutes.
        2. Increment consecutive_losses.
        3. If consecutive_losses >= limit and limit > 0: set cooldown_until
           to end of trading day (23:59:59 ET).
        """
        state = await self._get_or_create_daily_state()
        now = datetime.now(ET)

        # Per-loss cooldown (gated by toggle)
        if self.settings.enable_per_loss_cooldown and self.settings.cooldown_per_loss_minutes > 0:
            state.cooldown_until = now + timedelta(minutes=self.settings.cooldown_per_loss_minutes)
            logger.info(
                "Per-loss cooldown activated",
                extra={"data": {"until": str(state.cooldown_until)}},
            )

        # Consecutive loss tracking
        state.consecutive_losses += 1

        if (
            self.settings.enable_consecutive_loss_limit
            and self.settings.consecutive_loss_limit > 0
            and state.consecutive_losses >= self.settings.consecutive_loss_limit
        ):
            # Rest of day cooldown: set cooldown_until to end of trading day
            state.cooldown_until = _get_end_of_trading_day()
            logger.warning(
                "Consecutive loss limit hit -- rest of day cooldown",
                extra={
                    "data": {
                        "count": state.consecutive_losses,
                        "limit": self.settings.consecutive_loss_limit,
                        "cooldown_until": str(state.cooldown_until),
                    }
                },
            )

        await self.db.flush()

    async def on_trade_win(self) -> None:
        """Called when a trade settles as a win.

        Resets the consecutive loss counter to 0.
        The per-loss cooldown timer is NOT cleared -- it expires naturally.
        """
        state = await self._get_or_create_daily_state()
        state.consecutive_losses = 0
        await self.db.flush()

        logger.info(
            "Consecutive loss counter reset (win)",
            extra={"data": {"user_id": self.user_id}},
        )

    async def _get_daily_state(self) -> DailyRiskState | None:
        """Get today's risk state, or None if not yet created.

        Returns:
            The DailyRiskState for today, or None.
        """
        from backend.trading.risk_manager import get_trading_day

        trading_day = get_trading_day()
        trading_day_dt = datetime.combine(trading_day, datetime.min.time())

        result = await self.db.execute(
            select(DailyRiskState).where(
                DailyRiskState.user_id == self.user_id,
                DailyRiskState.trading_day == trading_day_dt,
            )
        )
        return result.scalar_one_or_none()

    async def _get_or_create_daily_state(self) -> DailyRiskState:
        """Get or create today's risk state.

        Returns:
            The DailyRiskState for today (created if it didn't exist).
        """
        state = await self._get_daily_state()
        if state is None:
            from backend.trading.risk_manager import get_trading_day

            trading_day = get_trading_day()
            trading_day_dt = datetime.combine(trading_day, datetime.min.time())

            state = DailyRiskState(
                user_id=self.user_id,
                trading_day=trading_day_dt,
                total_loss_cents=0,
                total_exposure_cents=0,
                consecutive_losses=0,
                trades_count=0,
            )
            self.db.add(state)
            await self.db.flush()
        return state


def _get_end_of_trading_day() -> datetime:
    """Get the end of the current trading day as 23:59:59 ET.

    Returns:
        A timezone-aware datetime representing the end of today in ET.
    """
    from backend.trading.risk_manager import get_trading_day

    today = get_trading_day()
    return datetime(
        today.year,
        today.month,
        today.day,
        23,
        59,
        59,
        tzinfo=ET,
    )
