"""Tests for GET /api/archive/forecast-coverage (algo v2, slice S3)."""

from __future__ import annotations

from datetime import date, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.models import CityEnum, ForecastArchiveChunk, ForecastIssuance


class TestForecastCoverage:
    async def test_aggregates_per_city_model_month(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        run = datetime(2025, 3, 14, 13, 0)
        db.add_all(
            [
                ForecastIssuance(
                    city=CityEnum.AUS,
                    model="NBS",
                    run_ts=run,
                    valid_date=date(2025, 3, 15),
                    station="KAUS",
                    available_at=run,
                    tmax_f=79.0,
                    tmax_sd_f=2.0,
                ),
                ForecastIssuance(
                    city=CityEnum.AUS,
                    model="NBS",
                    run_ts=run,
                    valid_date=date(2025, 3, 16),
                    station="KAUS",
                    available_at=run,
                    tmax_f=81.0,
                    tmax_sd_f=4.0,
                ),
                ForecastArchiveChunk(
                    city=CityEnum.AUS, model="NBS", month=date(2025, 3, 1), status="error"
                ),
            ]
        )
        await db.commit()
        rows = (await client.get("/api/archive/forecast-coverage")).json()
        assert rows == [
            {
                "city": "AUS",
                "model": "NBS",
                "month": "2025-03",
                "issuances": 2,
                "days": 2,
                "mean_sd_f": 3.0,
                "chunk_error": True,
            }
        ]
