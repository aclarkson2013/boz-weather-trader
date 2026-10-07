"""Tests for GET /api/archive/coverage (algo v2, slice S1)."""

from __future__ import annotations

from datetime import date

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.models import CityEnum, KalshiArchiveDay, KalshiArchivedMarket


def _mkt(ticker: str, city: CityEnum, d: date, **kw) -> KalshiArchivedMarket:
    defaults = {
        "event_ticker": ticker.rsplit("-", 1)[0],
        "series_ticker": ticker.split("-")[0],
        "label": "x",
        "era": "E1",
        "source": "historical",
        "tiles_ok": True,
        "result": "no",
        "n_candles": 30,
        "median_spread_cents": 2.0,
    }
    defaults.update(kw)
    return KalshiArchivedMarket(ticker=ticker, city=city, event_date=d, **defaults)


class TestArchiveCoverage:
    async def test_empty_archive(self, client: AsyncClient) -> None:
        response = await client.get("/api/archive/coverage")
        assert response.status_code == 200
        body = response.json()
        assert body["rows"] == []
        assert body["archive_start"] == "2024-07-01"
        assert body["days_complete"] == 0
        assert body["days_expected"] > 0

    async def test_aggregates_per_city_and_month(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        d1, d2 = date(2025, 3, 1), date(2025, 3, 2)
        db.add_all(
            [
                KalshiArchiveDay(
                    city=CityEnum.NYC, event_date=d1, event_ticker="a", status="complete"
                ),
                KalshiArchiveDay(
                    city=CityEnum.NYC, event_date=d2, event_ticker="b", status="error"
                ),
                _mkt(
                    "KXHIGHNY-25MAR01-T60", CityEnum.NYC, d1, result="yes", median_spread_cents=1.0
                ),
                _mkt("KXHIGHNY-25MAR01-B60.5", CityEnum.NYC, d1, median_spread_cents=3.0),
                _mkt(
                    "KXHIGHNY-25MAR01-B62.5",
                    CityEnum.NYC,
                    d1,
                    n_candles=0,
                    median_spread_cents=None,
                    tiles_ok=False,
                    result=None,
                ),
            ]
        )
        await db.commit()

        body = (await client.get("/api/archive/coverage")).json()
        assert len(body["rows"]) == 1
        row = body["rows"][0]
        assert row["city"] == "NYC"
        assert row["month"] == "2025-03"
        assert row["days_expected"] == 31
        assert row["days_complete"] == 1
        assert row["days_error"] == 1
        assert row["markets"] == 3
        assert row["markets_with_candles"] == 2
        assert row["markets_labeled"] == 2
        assert row["markets_not_tiling"] == 1
        assert row["median_spread_cents"] == 2.0
        assert body["markets_with_candles"] == 2
