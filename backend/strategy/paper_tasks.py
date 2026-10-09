"""Celery tasks for live paper trading (algo v2, slice S5). Never places orders.

- ``paper_cycle`` (every 15 min): makes due paper decisions for L5 / B5.
- ``paper_settle`` (hourly): settles paper trades on Kalshi results and runs the
  SPRT/CUSUM kill switch (push notification when a strategy is stopped).

Gated by ``V2_PAPER_ENABLED`` (kill switch for the whole paper test).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from asgiref.sync import async_to_sync
from celery import shared_task
from sqlalchemy import select

from backend.common.config import get_settings
from backend.common.database import get_task_session, reset_engine
from backend.common.logging import get_logger
from backend.common.models import User
from backend.kalshi.public_client import KalshiPublicClient
from backend.strategy.paper import run_paper_cycle, settle_paper_trades
from backend.weather.forecast_archive import ARCHIVE_MODELS, IEMMosClient, archive_window

logger = get_logger("TRADING")

FORECAST_REFRESH_DAYS = 3


async def _refresh_forecasts(cities: list[str]) -> None:
    """Fetch the latest few days of NBM/GFS/NAM issuances for ``cities`` (paced)."""
    end = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    start = end - timedelta(days=FORECAST_REFRESH_DAYS)
    client = IEMMosClient()
    try:
        for city in cities:
            for model in ARCHIVE_MODELS:
                session = await get_task_session()
                try:
                    await archive_window(client, session, city, model, start, end)
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise
                finally:
                    await session.close()
    finally:
        await client.close()


async def _notify(title: str, body: str) -> None:
    """Push-notify the (single) user, if push is configured."""
    from backend.trading.notifications import NotificationService

    session = await get_task_session()
    try:
        user = (await session.execute(select(User).limit(1))).scalar_one_or_none()
        if user is None or not user.push_subscription:
            return
        await NotificationService(subscription=json.loads(user.push_subscription)).send(
            title=title, body=body, data={"kind": "paper_stop"}
        )
    finally:
        await session.close()


async def _cycle() -> dict:
    reset_engine()
    async with KalshiPublicClient() as client:
        return await run_paper_cycle(get_task_session, client, refresh_forecasts=_refresh_forecasts)


async def _settle() -> dict:
    reset_engine()
    async with KalshiPublicClient() as client:
        return await settle_paper_trades(get_task_session, client, notify=_notify)


@shared_task(bind=True, soft_time_limit=840, time_limit=900)
def paper_cycle(self) -> dict:
    """Make any due paper-trading decisions (no real orders, ever)."""
    if not get_settings().v2_paper_enabled:
        return {"status": "disabled"}
    try:
        return {"status": "ok", **async_to_sync(_cycle)()}
    except Exception as exc:
        logger.error("Paper cycle task failed", extra={"data": {"error": str(exc)[:300]}})
        return {"status": "error", "error": str(exc)[:300]}


@shared_task(bind=True, soft_time_limit=240, time_limit=300)
def paper_settle(self) -> dict:
    """Settle paper trades and run the kill switch."""
    if not get_settings().v2_paper_enabled:
        return {"status": "disabled"}
    try:
        return {"status": "ok", **async_to_sync(_settle)()}
    except Exception as exc:
        logger.error("Paper settle task failed", extra={"data": {"error": str(exc)[:300]}})
        return {"status": "error", "error": str(exc)[:300]}
