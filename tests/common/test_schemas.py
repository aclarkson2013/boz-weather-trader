"""Tests for Pydantic schemas (interface contracts)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from backend.common.schemas import (
    ALL_WEATHER_SOURCES,
    DEFAULT_ENABLED_WEATHER_SOURCES,
    BracketPrediction,
    BracketProbability,
    PendingTrade,
    TradeSignal,
    UserSettings,
    WeatherData,
    WeatherVariables,
    parse_enabled_weather_sources,
)


class TestWeatherVariables:
    """Test WeatherVariables schema."""

    def test_all_fields(self):
        """All fields set to valid values."""
        wv = WeatherVariables(
            temp_high_f=56.0,
            temp_low_f=38.0,
            humidity_pct=55.0,
            wind_speed_mph=10.0,
            wind_gust_mph=20.0,
            cloud_cover_pct=30.0,
            dew_point_f=40.0,
            pressure_mb=1015.0,
        )
        assert wv.temp_high_f == 56.0
        assert wv.pressure_mb == 1015.0

    def test_optional_fields_default_none(self):
        """Optional fields default to None."""
        wv = WeatherVariables(temp_high_f=56.0)
        assert wv.temp_low_f is None
        assert wv.humidity_pct is None
        assert wv.wind_speed_mph is None

    def test_model_dump_roundtrip(self):
        """model_dump() and model_validate() produce identical objects."""
        wv = WeatherVariables(temp_high_f=56.0, humidity_pct=55.0)
        data = wv.model_dump()
        wv2 = WeatherVariables.model_validate(data)
        assert wv == wv2


class TestWeatherData:
    """Test WeatherData schema."""

    def test_valid_weather_data(self):
        """Create a valid WeatherData object."""
        wd = WeatherData(
            city="NYC",
            date=date(2025, 2, 15),
            forecast_high_f=56.0,
            source="NWS",
            model_run_timestamp=datetime(2025, 2, 14, 12, 0, tzinfo=UTC),
            variables=WeatherVariables(temp_high_f=56.0),
            raw_data={"test": True},
            fetched_at=datetime(2025, 2, 14, 15, 0, tzinfo=UTC),
        )
        assert wd.city == "NYC"
        assert wd.forecast_high_f == 56.0

    def test_invalid_city_rejected(self):
        """Invalid city code is rejected."""
        with pytest.raises(Exception):
            WeatherData(
                city="INVALID",
                date=date(2025, 2, 15),
                forecast_high_f=56.0,
                source="NWS",
                model_run_timestamp=datetime.now(UTC),
                variables=WeatherVariables(temp_high_f=56.0),
                raw_data={},
                fetched_at=datetime.now(UTC),
            )


class TestBracketProbability:
    """Test BracketProbability schema."""

    def test_valid_probability(self):
        """Probability between 0 and 1 is accepted."""
        bp = BracketProbability(
            bracket_label="55-56°F",
            lower_bound_f=55,
            upper_bound_f=56,
            probability=0.30,
        )
        assert bp.probability == 0.30

    def test_probability_zero(self):
        """Probability of exactly 0.0 is valid."""
        bp = BracketProbability(
            bracket_label="≤50°F", lower_bound_f=None, upper_bound_f=50, probability=0.0
        )
        assert bp.probability == 0.0

    def test_probability_one(self):
        """Probability of exactly 1.0 is valid."""
        bp = BracketProbability(
            bracket_label="≥60°F", lower_bound_f=60, upper_bound_f=None, probability=1.0
        )
        assert bp.probability == 1.0

    def test_probability_negative_rejected(self):
        """Negative probability is rejected."""
        with pytest.raises(Exception):
            BracketProbability(
                bracket_label="test", lower_bound_f=50, upper_bound_f=52, probability=-0.1
            )

    def test_probability_over_one_rejected(self):
        """Probability over 1.0 is rejected."""
        with pytest.raises(Exception):
            BracketProbability(
                bracket_label="test", lower_bound_f=50, upper_bound_f=52, probability=1.1
            )

    def test_edge_bracket_null_bounds(self):
        """Bottom edge bracket has None lower_bound, top edge has None upper_bound."""
        bottom = BracketProbability(
            bracket_label="≤52°F", lower_bound_f=None, upper_bound_f=52, probability=0.1
        )
        top = BracketProbability(
            bracket_label="≥61°F", lower_bound_f=61, upper_bound_f=None, probability=0.1
        )
        assert bottom.lower_bound_f is None
        assert top.upper_bound_f is None


class TestBracketPrediction:
    """Test BracketPrediction with bracket sum validation."""

    def _make_brackets(self, probs: list[float]) -> list[BracketProbability]:
        """Helper to create 6 brackets with given probabilities."""
        labels = ["≤52°F", "53-54°F", "55-56°F", "57-58°F", "59-60°F", "≥61°F"]
        return [
            BracketProbability(
                bracket_label=labels[i],
                lower_bound_f=50 + i * 2 if i > 0 else None,
                upper_bound_f=52 + i * 2 if i < 5 else None,
                probability=p,
            )
            for i, p in enumerate(probs)
        ]

    def test_valid_prediction(self, sample_bracket_prediction):
        """Valid prediction with probabilities summing to 1.0."""
        assert sample_bracket_prediction.city == "NYC"
        assert len(sample_bracket_prediction.brackets) == 6
        total = sum(b.probability for b in sample_bracket_prediction.brackets)
        assert abs(total - 1.0) < 0.01

    def test_probabilities_not_summing_to_one_rejected(self):
        """Bracket probabilities must sum to approximately 1.0."""
        with pytest.raises(Exception):
            BracketPrediction(
                city="NYC",
                date=date(2025, 2, 15),
                brackets=self._make_brackets([0.1, 0.1, 0.1, 0.1, 0.1, 0.1]),  # sum=0.6
                ensemble_mean_f=56.0,
                ensemble_std_f=2.0,
                confidence="medium",
                model_sources=["NWS"],
                generated_at=datetime.now(UTC),
            )

    def test_probabilities_summing_to_1_05_accepted(self):
        """Slight overcount (1.05) is within tolerance."""
        pred = BracketPrediction(
            city="NYC",
            date=date(2025, 2, 15),
            brackets=self._make_brackets([0.10, 0.15, 0.30, 0.28, 0.12, 0.10]),  # sum=1.05
            ensemble_mean_f=56.0,
            ensemble_std_f=2.0,
            confidence="medium",
            model_sources=["NWS"],
            generated_at=datetime.now(UTC),
        )
        assert pred is not None


class TestTradeSignal:
    """Test TradeSignal schema validation."""

    def test_valid_signal(self, sample_trade_signal):
        """Valid trade signal is accepted."""
        assert sample_trade_signal.price_cents == 22
        assert sample_trade_signal.side == "yes"

    def test_price_cents_minimum(self):
        """Price must be at least 1 cent."""
        with pytest.raises(Exception):
            TradeSignal(
                city="NYC",
                bracket="55-56°F",
                side="yes",
                price_cents=0,
                quantity=1,
                model_probability=0.3,
                market_probability=0.0,
                ev=0.0,
                confidence="medium",
                market_ticker="KXHIGHNY-25FEB15-B3",
            )

    def test_price_cents_maximum(self):
        """Price must be at most 99 cents."""
        with pytest.raises(Exception):
            TradeSignal(
                city="NYC",
                bracket="55-56°F",
                side="yes",
                price_cents=100,
                quantity=1,
                model_probability=0.3,
                market_probability=1.0,
                ev=0.0,
                confidence="medium",
                market_ticker="KXHIGHNY-25FEB15-B3",
            )

    def test_invalid_side_rejected(self):
        """Side must be 'yes' or 'no'."""
        with pytest.raises(Exception):
            TradeSignal(
                city="NYC",
                bracket="55-56°F",
                side="maybe",
                price_cents=22,
                quantity=1,
                model_probability=0.3,
                market_probability=0.22,
                ev=0.05,
                confidence="medium",
                market_ticker="KXHIGHNY-25FEB15-B3",
            )


class TestUserSettings:
    """Test UserSettings schema."""

    def test_defaults(self):
        """UserSettings has sensible defaults for all fields."""
        settings = UserSettings()
        assert settings.trading_mode == "manual"
        assert settings.max_trade_size_cents == 100
        assert settings.daily_loss_limit_cents == 1000
        assert settings.min_ev_threshold == 0.05
        assert settings.notifications_enabled is True
        assert len(settings.active_cities) == 4

    def test_model_dump_roundtrip(self):
        """Serialization roundtrip preserves all fields."""
        settings = UserSettings(trading_mode="auto", max_trade_size_cents=500)
        data = settings.model_dump()
        settings2 = UserSettings.model_validate(data)
        assert settings == settings2


class TestPendingTrade:
    """Test PendingTrade schema."""

    def test_valid_pending_trade(self):
        """Create a valid PendingTrade."""
        pt = PendingTrade(
            id="test-123",
            city="NYC",
            bracket="55-56°F",
            side="yes",
            price_cents=22,
            quantity=1,
            model_probability=0.30,
            market_probability=0.22,
            ev=0.05,
            confidence="medium",
            reasoning="Good EV",
            status="PENDING",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC),
        )
        assert pt.status == "PENDING"
        assert pt.acted_at is None


# ─── Weather source selection ───


class TestParseEnabledWeatherSources:
    """parse_enabled_weather_sources normalizes the stored settings string."""

    def test_parses_and_canonicalizes_order(self) -> None:
        result = parse_enabled_weather_sources("Open-Meteo:ICON,NWS,Open-Meteo:GFS")
        assert result == ["NWS", "Open-Meteo:GFS", "Open-Meteo:ICON"]

    def test_strips_whitespace(self) -> None:
        result = parse_enabled_weather_sources(" NWS , Open-Meteo:ICON ")
        assert result == ["NWS", "Open-Meteo:ICON"]

    def test_drops_unknown_sources(self) -> None:
        result = parse_enabled_weather_sources("NWS,AccuWeather,Open-Meteo:ICON")
        assert result == ["NWS", "Open-Meteo:ICON"]

    def test_falls_back_when_none(self) -> None:
        assert parse_enabled_weather_sources(None) == list(DEFAULT_ENABLED_WEATHER_SOURCES)

    def test_falls_back_when_empty(self) -> None:
        assert parse_enabled_weather_sources("") == list(DEFAULT_ENABLED_WEATHER_SOURCES)

    def test_falls_back_when_below_minimum(self) -> None:
        """One valid source is not enough for a spread — use the defaults."""
        assert parse_enabled_weather_sources("Open-Meteo:ICON") == list(
            DEFAULT_ENABLED_WEATHER_SOURCES
        )

    def test_falls_back_when_all_unknown(self) -> None:
        assert parse_enabled_weather_sources("Foo,Bar") == list(DEFAULT_ENABLED_WEATHER_SOURCES)

    def test_default_is_the_three_source_working_set(self) -> None:
        """Pins the choice made in the 2026-08-21 source review."""
        assert DEFAULT_ENABLED_WEATHER_SOURCES == (
            "NWS:gridpoint",
            "Open-Meteo:GFS",
            "Open-Meteo:ICON",
        )

    def test_defaults_are_a_subset_of_all_sources(self) -> None:
        assert set(DEFAULT_ENABLED_WEATHER_SOURCES) <= set(ALL_WEATHER_SOURCES)


class TestUserSettingsWeatherSources:
    """UserSettings validates the enabled-source list."""

    def test_default_settings_use_the_working_set(self) -> None:
        assert UserSettings().enabled_weather_sources == list(DEFAULT_ENABLED_WEATHER_SOURCES)

    def test_rejects_fewer_than_minimum(self) -> None:
        with pytest.raises(ValidationError):
            UserSettings(enabled_weather_sources=["Open-Meteo:ICON"])

    def test_rejects_unknown_source(self) -> None:
        with pytest.raises(ValidationError):
            UserSettings(enabled_weather_sources=["Open-Meteo:ICON", "AccuWeather"])

    def test_accepts_all_five(self) -> None:
        settings = UserSettings(enabled_weather_sources=list(ALL_WEATHER_SOURCES))
        assert len(settings.enabled_weather_sources) == 5
