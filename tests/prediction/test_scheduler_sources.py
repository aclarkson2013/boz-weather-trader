"""Tests for weather-source selection in the prediction scheduler.

Only the sources a user has enabled are blended into the ensemble. Disabled
sources are still fetched and stored by the weather pipeline, so
/api/accuracy/sources keeps scoring them and re-enabling one is backed by
continuous history — see docs/ALGO_CHANGELOG.md (2026-08-21 source review).
"""

from __future__ import annotations

from types import SimpleNamespace

from backend.common.schemas import DEFAULT_ENABLED_WEATHER_SOURCES
from backend.prediction.scheduler import _dedupe_and_filter_sources


def _row(source: str, high: float = 80.0) -> SimpleNamespace:
    """Stand-in for a WeatherForecast ORM row (only .source is read)."""
    return SimpleNamespace(source=source, forecast_high_f=high)


DEFAULTS = set(DEFAULT_ENABLED_WEATHER_SOURCES)


def test_keeps_only_enabled_sources() -> None:
    rows = [
        _row("NWS"),
        _row("NWS:gridpoint"),
        _row("Open-Meteo:ECMWF"),
        _row("Open-Meteo:GFS"),
        _row("Open-Meteo:ICON"),
    ]

    kept, excluded = _dedupe_and_filter_sources(rows, DEFAULTS)

    assert [r.source for r in kept] == [
        "NWS:gridpoint",
        "Open-Meteo:GFS",
        "Open-Meteo:ICON",
    ]
    assert sorted(excluded) == ["NWS", "Open-Meteo:ECMWF"]


def test_keeps_newest_row_per_source() -> None:
    """Rows arrive newest-first, so the first row for a source wins."""
    rows = [
        _row("Open-Meteo:ICON", high=91.0),  # newest
        _row("Open-Meteo:ICON", high=88.0),  # older, must be dropped
        _row("Open-Meteo:GFS", high=90.0),
    ]

    kept, excluded = _dedupe_and_filter_sources(rows, DEFAULTS)

    assert len(kept) == 2
    icon = next(r for r in kept if r.source == "Open-Meteo:ICON")
    assert icon.forecast_high_f == 91.0
    assert excluded == []


def test_disabled_source_reported_once_even_with_duplicates() -> None:
    """A disabled source with several stored rows is reported a single time."""
    rows = [_row("NWS"), _row("NWS"), _row("Open-Meteo:GFS"), _row("Open-Meteo:ICON")]

    kept, excluded = _dedupe_and_filter_sources(rows, DEFAULTS)

    assert excluded == ["NWS"]
    assert len(kept) == 2


def test_all_sources_enabled_keeps_everything() -> None:
    """Re-enabling every source restores the full five-feed ensemble."""
    rows = [
        _row("NWS"),
        _row("NWS:gridpoint"),
        _row("Open-Meteo:ECMWF"),
        _row("Open-Meteo:GFS"),
        _row("Open-Meteo:ICON"),
    ]

    kept, excluded = _dedupe_and_filter_sources(rows, {r.source for r in rows})

    assert len(kept) == 5
    assert excluded == []


def test_unknown_stored_source_is_excluded() -> None:
    """A feed no longer in the enabled set never reaches the ensemble."""
    rows = [_row("Open-Meteo:GFS"), _row("Open-Meteo:ICON"), _row("LegacyFeed")]

    kept, excluded = _dedupe_and_filter_sources(rows, DEFAULTS)

    assert "LegacyFeed" not in [r.source for r in kept]
    assert "LegacyFeed" in excluded


def test_empty_input_is_safe() -> None:
    assert _dedupe_and_filter_sources([], DEFAULTS) == ([], [])
