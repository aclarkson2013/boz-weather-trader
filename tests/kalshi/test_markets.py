"""Tests for Kalshi market discovery, ticker mapping, and bracket parsing.

Verifies ticker constants, event ticker construction, bracket parsing
for edge and middle brackets, and the parse_event_markets aggregation.
"""

from __future__ import annotations

from datetime import date

import pytest

from backend.kalshi.markets import (
    SERIES_TO_CITY,
    WEATHER_SERIES_TICKERS,
    build_event_ticker,
    parse_bracket_from_market,
    parse_event_markets,
    parse_market_date_from_ticker,
)
from backend.kalshi.models import KalshiMarket

# ─── Ticker Mapping Constants ───


class TestWeatherSeriesTickers:
    """Tests for WEATHER_SERIES_TICKERS and SERIES_TO_CITY mappings."""

    def test_has_4_entries(self) -> None:
        """WEATHER_SERIES_TICKERS has exactly 4 city entries."""
        assert len(WEATHER_SERIES_TICKERS) == 4

    def test_nyc_maps_to_kxhighny(self) -> None:
        """NYC maps to KXHIGHNY."""
        assert WEATHER_SERIES_TICKERS["NYC"] == "KXHIGHNY"

    def test_chi_maps_to_kxhighchi(self) -> None:
        """CHI maps to KXHIGHCHI."""
        assert WEATHER_SERIES_TICKERS["CHI"] == "KXHIGHCHI"

    def test_mia_maps_to_kxhighmia(self) -> None:
        """MIA maps to KXHIGHMIA."""
        assert WEATHER_SERIES_TICKERS["MIA"] == "KXHIGHMIA"

    def test_aus_maps_to_kxhighaus(self) -> None:
        """AUS maps to KXHIGHAUS."""
        assert WEATHER_SERIES_TICKERS["AUS"] == "KXHIGHAUS"

    def test_series_to_city_is_inverse_mapping(self) -> None:
        """SERIES_TO_CITY is the exact inverse of WEATHER_SERIES_TICKERS."""
        assert len(SERIES_TO_CITY) == len(WEATHER_SERIES_TICKERS)
        for city, series in WEATHER_SERIES_TICKERS.items():
            assert SERIES_TO_CITY[series] == city


# ─── Event Ticker Construction ───


class TestBuildEventTicker:
    """Tests for build_event_ticker function."""

    def test_nyc_feb_18_2026(self) -> None:
        """NYC + date(2026,2,18) produces 'KXHIGHNY-26FEB18'."""
        result = build_event_ticker("NYC", date(2026, 2, 18))
        assert result == "KXHIGHNY-26FEB18"

    def test_chi_mar_05_2026(self) -> None:
        """CHI + date(2026,3,5) produces 'KXHIGHCHI-26MAR05'."""
        result = build_event_ticker("CHI", date(2026, 3, 5))
        assert result == "KXHIGHCHI-26MAR05"

    def test_raises_for_unknown_city(self) -> None:
        """ValueError is raised for an unrecognized city code."""
        with pytest.raises(ValueError, match="Unknown city code"):
            build_event_ticker("LON", date(2026, 2, 18))


# ─── Bracket Parsing ───


class TestParseBracketFromMarket:
    """Tests for parse_bracket_from_market function."""

    def test_bottom_edge_bracket(self) -> None:
        """Bottom edge: floor=None, cap=47.99 produces '47°F or below'.

        Continuous upper bound sits at the half-degree above the highest
        covered integer (47 → 47.5).
        """
        market = {"floor_strike": None, "cap_strike": 47.99, "ticker": "T48"}
        result = parse_bracket_from_market(market)

        assert result["label"] == "47°F or below"
        assert result["lower_bound_f"] is None
        assert result["upper_bound_f"] == 47.5
        assert result["is_edge_lower"] is True
        assert result["is_edge_upper"] is False

    def test_top_edge_bracket(self) -> None:
        """Top edge: integer floor is EXCLUSIVE (shared-boundary convention).

        Kalshi encodes '>58°' as floor=58 with the display '59° or above'
        (verified live: KXHIGHNY-26AUG07-T96 floor=96 displays
        '97° or above'). Lowest covered integer is floor+1.
        """
        market = {"floor_strike": 58.0, "cap_strike": None, "ticker": "T58"}
        result = parse_bracket_from_market(market)

        assert result["label"] == "59°F or above"
        assert result["lower_bound_f"] == 58.5
        assert result["upper_bound_f"] is None
        assert result["is_edge_lower"] is False
        assert result["is_edge_upper"] is True

    def test_middle_bracket(self) -> None:
        """Middle: floor=52.0, cap=53.99 produces '52° to 53°F'.

        Covers integer temps {52, 53} → continuous bounds [51.5, 53.5).
        """
        market = {"floor_strike": 52.0, "cap_strike": 53.99, "ticker": "T52"}
        result = parse_bracket_from_market(market)

        assert result["label"] == "52° to 53°F"
        assert result["lower_bound_f"] == 51.5
        assert result["upper_bound_f"] == 53.5
        assert result["is_edge_lower"] is False
        assert result["is_edge_upper"] is False

    def test_both_none_produces_unknown(self) -> None:
        """Both floor and cap as None produces 'Unknown' label."""
        market = {"floor_strike": None, "cap_strike": None, "ticker": "TXXX"}
        result = parse_bracket_from_market(market)

        assert result["label"] == "Unknown"
        assert result["lower_bound_f"] is None
        assert result["upper_bound_f"] is None
        assert result["is_edge_lower"] is False
        assert result["is_edge_upper"] is False

    # ─── Integer cap_strike robustness tests ───

    def test_bottom_edge_integer_cap_strike(self) -> None:
        """Bottom edge with integer cap_strike (73.0) produces same label as 72.99."""
        # Kalshi sometimes returns integer cap_strike instead of .99
        market_dotninety = {"floor_strike": None, "cap_strike": 72.99, "ticker": "T73"}
        market_integer = {"floor_strike": None, "cap_strike": 73.0, "ticker": "T73"}

        result_dotninety = parse_bracket_from_market(market_dotninety)
        result_integer = parse_bracket_from_market(market_integer)

        assert result_dotninety["label"] == "72°F or below"
        assert result_integer["label"] == "72°F or below"
        assert result_dotninety["label"] == result_integer["label"]

    def test_middle_bracket_integer_cap_strike(self) -> None:
        """Middle bracket with integer cap_strike uses int(cap) as display value.

        Kalshi sends cap as the inclusive display value for middle brackets:
        cap=53.99 or cap=53.0 both display as "52° to 53°F".
        """
        market_dotninety = {"floor_strike": 52.0, "cap_strike": 53.99, "ticker": "T52"}
        market_integer = {"floor_strike": 52.0, "cap_strike": 53.0, "ticker": "T52"}

        result_dotninety = parse_bracket_from_market(market_dotninety)
        result_integer = parse_bracket_from_market(market_integer)

        assert result_dotninety["label"] == "52° to 53°F"
        assert result_integer["label"] == "52° to 53°F"
        assert result_dotninety["label"] == result_integer["label"]

    def test_bottom_edge_various_integer_caps(self) -> None:
        """Bottom edge labels are consistent across various integer cap_strike values."""
        test_cases = [
            (47.99, 48.0, "47°F or below"),
            (62.99, 63.0, "62°F or below"),
            (80.99, 81.0, "80°F or below"),
        ]
        for dotninety, integer, expected_label in test_cases:
            result_dn = parse_bracket_from_market({"floor_strike": None, "cap_strike": dotninety})
            result_int = parse_bracket_from_market({"floor_strike": None, "cap_strike": integer})
            assert result_dn["label"] == expected_label, f"cap={dotninety}"
            assert result_int["label"] == expected_label, f"cap={integer}"

    def test_middle_bracket_various_integer_caps(self) -> None:
        """Middle bracket labels consistent for .99 and integer cap_strike values.

        Kalshi sends cap as the inclusive display upper bound for middle brackets.
        int(cap) works correctly for both: int(85.99)=85 and int(85.0)=85.
        """
        test_cases = [
            (48.0, 49.99, 49.0, "48° to 49°F"),
            (63.0, 64.99, 64.0, "63° to 64°F"),
            (75.0, 76.99, 76.0, "75° to 76°F"),
        ]
        for floor, dotninety_cap, integer_cap, expected_label in test_cases:
            result_dn = parse_bracket_from_market(
                {"floor_strike": floor, "cap_strike": dotninety_cap}
            )
            result_int = parse_bracket_from_market(
                {"floor_strike": floor, "cap_strike": integer_cap}
            )
            assert result_dn["label"] == expected_label, f"cap={dotninety_cap}"
            assert result_int["label"] == expected_label, f"cap={integer_cap}"

    # ─── Post-migration integer strike tests ───

    def test_post_migration_middle_bracket_integer_strikes(self) -> None:
        """Post-migration: floor=49, cap=50 produces '49° to 50°F'."""
        market = {"floor_strike": 49, "cap_strike": 50, "ticker": "B49.5"}
        result = parse_bracket_from_market(market)
        assert result["label"] == "49° to 50°F"

    def test_post_migration_bottom_edge_integer_cap(self) -> None:
        """Post-migration: floor=None, cap=49 produces '48°F or below'."""
        market = {"floor_strike": None, "cap_strike": 49, "ticker": "B48"}
        result = parse_bracket_from_market(market)
        assert result["label"] == "48°F or below"

    def test_post_migration_top_edge_integer_floor(self) -> None:
        """Post-migration: floor=55 (exclusive) produces '56°F or above'.

        Matches Kalshi's live display convention: the top catch-all's
        floor_strike equals the last middle bracket's cap and is exclusive
        (e.g. KXHIGHCHI-26AUG07 floor=89 → '90° or above').
        """
        market = {"floor_strike": 55, "cap_strike": None, "ticker": "B55"}
        result = parse_bracket_from_market(market)
        assert result["label"] == "56°F or above"
        assert result["lower_bound_f"] == 55.5


class TestContinuousBounds:
    """Continuous half-degree bounds for CDF probability mass (v1.9.12 fix).

    Middle brackets cover TWO integer temps, so their continuous bounds
    must be 2°F wide. Passing raw Kalshi strikes through (pre-fix) made
    them 1°F wide and systematically halved middle-bracket probabilities
    (see docs/ALGO_CHANGELOG.md, 2026-08-06 review).

    Fixture ladder mirrors the live KXHIGHNY-26AUG07 event.
    """

    LIVE_NYC_LADDER = [
        {"floor_strike": None, "cap_strike": 89, "ticker": "T89"},  # 88° or below
        {"floor_strike": 89, "cap_strike": 90, "ticker": "B89.5"},  # 89° to 90°
        {"floor_strike": 91, "cap_strike": 92, "ticker": "B91.5"},  # 91° to 92°
        {"floor_strike": 93, "cap_strike": 94, "ticker": "B93.5"},  # 93° to 94°
        {"floor_strike": 95, "cap_strike": 96, "ticker": "B95.5"},  # 95° to 96°
        {"floor_strike": 96, "cap_strike": None, "ticker": "T96"},  # 97° or above
    ]

    def test_live_ladder_labels_match_kalshi_display(self) -> None:
        """Labels match Kalshi's own yes_sub_title values for this event."""
        labels = [parse_bracket_from_market(m)["label"] for m in self.LIVE_NYC_LADDER]
        assert labels == [
            "88°F or below",
            "89° to 90°F",
            "91° to 92°F",
            "93° to 94°F",
            "95° to 96°F",
            "97°F or above",
        ]

    def test_middle_brackets_are_two_degrees_wide(self) -> None:
        """Every middle bracket spans 2.0°F of continuous temperature."""
        for market in self.LIVE_NYC_LADDER[1:-1]:
            result = parse_bracket_from_market(market)
            width = result["upper_bound_f"] - result["lower_bound_f"]
            assert width == 2.0, f"{result['label']} is {width}°F wide"

    def test_bounds_tile_without_gaps(self) -> None:
        """Adjacent bounds meet exactly — no phantom gaps between brackets."""
        results = [parse_bracket_from_market(m) for m in self.LIVE_NYC_LADDER]
        for prev, nxt in zip(results, results[1:], strict=False):
            assert prev["upper_bound_f"] == nxt["lower_bound_f"], (
                f"gap between {prev['label']} and {nxt['label']}"
            )

    def test_bounds_classify_integer_temps_correctly(self) -> None:
        """Each integer temp falls inside exactly one bracket's bounds."""
        results = [parse_bracket_from_market(m) for m in self.LIVE_NYC_LADDER]

        def containing_label(temp: float) -> str:
            for r in results:
                lower = r["lower_bound_f"] if r["lower_bound_f"] is not None else float("-inf")
                upper = r["upper_bound_f"] if r["upper_bound_f"] is not None else float("inf")
                if lower <= temp < upper:
                    return r["label"]
            raise AssertionError(f"temp {temp} not covered")

        assert containing_label(88) == "88°F or below"
        assert containing_label(89) == "89° to 90°F"
        assert containing_label(90) == "89° to 90°F"  # pre-fix: fell in a gap
        assert containing_label(91) == "91° to 92°F"
        assert containing_label(96) == "95° to 96°F"  # pre-fix: matched top bracket
        assert containing_label(97) == "97°F or above"

    def test_legacy_dot99_convention_same_bounds(self) -> None:
        """Legacy .99-cap markets produce the same continuous bounds."""
        legacy = {"floor_strike": 89.0, "cap_strike": 90.99, "ticker": "T89"}
        integer = {"floor_strike": 89, "cap_strike": 90, "ticker": "B89.5"}
        assert (
            parse_bracket_from_market(legacy)["lower_bound_f"]
            == parse_bracket_from_market(integer)["lower_bound_f"]
            == 88.5
        )
        assert (
            parse_bracket_from_market(legacy)["upper_bound_f"]
            == parse_bracket_from_market(integer)["upper_bound_f"]
            == 90.5
        )


# ─── Event Markets Parsing ───


class TestParseEventMarkets:
    """Tests for parse_event_markets function."""

    def _make_markets(self) -> list[KalshiMarket]:
        """Create a list of 4 KalshiMarket models for testing sort order."""
        return [
            # Middle bracket (out of order on purpose)
            KalshiMarket(
                ticker="KXHIGHNY-26FEB18-T54",
                event_ticker="KXHIGHNY-26FEB18",
                title="54° to 55°F",
                status="active",
                floor_strike=54.0,
                cap_strike=55.99,
                yes_bid=15,
                yes_ask=18,
                volume=200,
            ),
            # Top edge
            KalshiMarket(
                ticker="KXHIGHNY-26FEB18-T58",
                event_ticker="KXHIGHNY-26FEB18",
                title="58F or above",
                status="active",
                floor_strike=58.0,
                cap_strike=None,
                yes_bid=10,
                yes_ask=14,
                volume=100,
            ),
            # Bottom edge
            KalshiMarket(
                ticker="KXHIGHNY-26FEB18-T48",
                event_ticker="KXHIGHNY-26FEB18",
                title="47°F or below",
                status="active",
                floor_strike=None,
                cap_strike=47.99,
                yes_bid=5,
                yes_ask=8,
                volume=50,
            ),
            # Middle bracket
            KalshiMarket(
                ticker="KXHIGHNY-26FEB18-T52",
                event_ticker="KXHIGHNY-26FEB18",
                title="52° to 53°F",
                status="active",
                floor_strike=52.0,
                cap_strike=53.99,
                yes_bid=22,
                yes_ask=25,
                volume=1542,
            ),
        ]

    def test_sorts_brackets_correctly(self) -> None:
        """parse_event_markets sorts bottom edge first, top edge last."""
        markets = self._make_markets()
        brackets = parse_event_markets(markets)

        assert len(brackets) == 4
        # First bracket should be bottom edge
        assert brackets[0]["is_edge_lower"] is True
        assert brackets[0]["label"] == "47°F or below"
        # Last bracket should be top edge (floor=58 exclusive → 59+)
        assert brackets[-1]["is_edge_upper"] is True
        assert brackets[-1]["label"] == "59°F or above"
        # Middle brackets sorted by continuous lower_bound_f
        assert brackets[1]["lower_bound_f"] == 51.5
        assert brackets[2]["lower_bound_f"] == 53.5

    def test_adds_pricing_data_from_market(self) -> None:
        """parse_event_markets includes pricing data (yes_bid, yes_ask, etc.)."""
        markets = self._make_markets()
        brackets = parse_event_markets(markets)

        # Check the bottom edge bracket
        bottom = brackets[0]
        assert bottom["yes_bid"] == 5
        assert bottom["yes_ask"] == 8
        assert bottom["volume"] == 50
        assert bottom["status"] == "active"

        # Check a middle bracket (52° to 53°F)
        mid = brackets[1]
        assert mid["yes_bid"] == 22
        assert mid["yes_ask"] == 25
        assert mid["volume"] == 1542
        assert mid["last_price"] == 0  # default


# ─── Ticker Date Parsing ───


class TestParseMarketDateFromTicker:
    """Tests for parse_market_date_from_ticker — extracts event date from ticker."""

    def test_standard_market_ticker(self) -> None:
        """Full market ticker 'KXHIGHNY-26FEB18-T52' → date(2026, 2, 18)."""
        result = parse_market_date_from_ticker("KXHIGHNY-26FEB18-T52")
        assert result == date(2026, 2, 18)

    def test_event_ticker_without_bracket(self) -> None:
        """Event ticker 'KXHIGHNY-26FEB18' → date(2026, 2, 18)."""
        result = parse_market_date_from_ticker("KXHIGHNY-26FEB18")
        assert result == date(2026, 2, 18)

    def test_austin_ticker(self) -> None:
        """Austin ticker 'KXHIGHAUS-26FEB23-T63' → date(2026, 2, 23)."""
        result = parse_market_date_from_ticker("KXHIGHAUS-26FEB23-T63")
        assert result == date(2026, 2, 23)

    def test_chicago_march(self) -> None:
        """Chicago March ticker → date(2026, 3, 5)."""
        result = parse_market_date_from_ticker("KXHIGHCHI-26MAR05-B35")
        assert result == date(2026, 3, 5)

    def test_miami_december(self) -> None:
        """Miami December ticker → date(2026, 12, 25)."""
        result = parse_market_date_from_ticker("KXHIGHMIA-26DEC25-T81")
        assert result == date(2026, 12, 25)

    def test_lowercase_month_is_handled(self) -> None:
        """Mixed-case month (26feb18) → still parses correctly."""
        result = parse_market_date_from_ticker("KXHIGHNY-26feb18-T52")
        assert result == date(2026, 2, 18)

    def test_invalid_ticker_returns_none(self) -> None:
        """Non-parseable ticker returns None."""
        assert parse_market_date_from_ticker("invalid") is None

    def test_no_dashes_returns_none(self) -> None:
        """Ticker without dashes returns None."""
        assert parse_market_date_from_ticker("KXHIGHNY") is None

    def test_empty_string_returns_none(self) -> None:
        """Empty string returns None."""
        assert parse_market_date_from_ticker("") is None

    def test_bad_date_segment_returns_none(self) -> None:
        """Non-date second segment returns None."""
        assert parse_market_date_from_ticker("KXHIGHNY-NOTADATE-T52") is None

    def test_all_months(self) -> None:
        """All 12 months parse correctly."""
        months_and_expected = [
            ("26JAN15", date(2026, 1, 15)),
            ("26FEB28", date(2026, 2, 28)),
            ("26MAR01", date(2026, 3, 1)),
            ("26APR10", date(2026, 4, 10)),
            ("26MAY20", date(2026, 5, 20)),
            ("26JUN30", date(2026, 6, 30)),
            ("26JUL04", date(2026, 7, 4)),
            ("26AUG15", date(2026, 8, 15)),
            ("26SEP22", date(2026, 9, 22)),
            ("26OCT31", date(2026, 10, 31)),
            ("26NOV11", date(2026, 11, 11)),
            ("26DEC25", date(2026, 12, 25)),
        ]
        for date_str, expected in months_and_expected:
            result = parse_market_date_from_ticker(f"KXHIGHNY-{date_str}-T52")
            assert result == expected, f"Failed for {date_str}"

    def test_bottom_bracket_suffix(self) -> None:
        """Bottom bracket suffix (B65.5) still parses date correctly."""
        result = parse_market_date_from_ticker("KXHIGHAUS-26FEB23-B65.5")
        assert result == date(2026, 2, 23)
