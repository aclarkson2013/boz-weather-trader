"""Market discovery, ticker mapping, and bracket parsing for Kalshi weather markets.

Kalshi weather markets are organized as:
  Series (e.g., KXHIGHNY) -> Events (e.g., KXHIGHNY-26FEB18) -> Markets/Brackets

Each event has 6 bracket markets. The middle 4 are typically 2 degrees F wide,
and the top/bottom brackets are catch-all edge brackets.

Usage:
    from backend.kalshi.markets import (
        WEATHER_SERIES_TICKERS,
        build_event_ticker,
        parse_bracket_from_market,
        parse_event_markets,
        parse_market_date_from_ticker,
    )

    ticker = build_event_ticker("NYC", date(2026, 2, 18))
    # -> "KXHIGHNY-26FEB18"
"""

from __future__ import annotations

import math
from datetime import date, datetime

from backend.common.logging import get_logger
from backend.kalshi.models import KalshiMarket

logger = get_logger("MARKET")


# ─── Ticker Mappings ───

# City code -> Kalshi series ticker for daily high temperature markets
WEATHER_SERIES_TICKERS: dict[str, str] = {
    "NYC": "KXHIGHNY",
    "CHI": "KXHIGHCHI",
    "MIA": "KXHIGHMIA",
    "AUS": "KXHIGHAUS",
}

# Reverse lookup: series ticker -> city code
SERIES_TO_CITY: dict[str, str] = {v: k for k, v in WEATHER_SERIES_TICKERS.items()}


# ─── Ticker Construction ───


def build_event_ticker(city: str, target_date: date) -> str:
    """Build a Kalshi event ticker for a city and date.

    The event ticker format is: {series_ticker}-{YY}{MON}{DD}
    where MON is the uppercase 3-letter month abbreviation.

    Args:
        city: City code (NYC, CHI, MIA, AUS).
        target_date: The date of the weather event.

    Returns:
        Event ticker string, e.g., "KXHIGHNY-26FEB18".

    Raises:
        ValueError: If city code is not recognized.

    Examples:
        >>> build_event_ticker("NYC", date(2026, 2, 18))
        'KXHIGHNY-26FEB18'
        >>> build_event_ticker("CHI", date(2026, 3, 5))
        'KXHIGHCHI-26MAR05'
    """
    series = WEATHER_SERIES_TICKERS.get(city.upper())
    if not series:
        msg = f"Unknown city code: '{city}'. Valid codes: {list(WEATHER_SERIES_TICKERS.keys())}"
        raise ValueError(msg)

    # Format: YY + uppercase 3-letter month + DD
    date_str = target_date.strftime("%y%b%d").upper()
    return f"{series}-{date_str}"


def parse_market_date_from_ticker(ticker: str) -> date | None:
    """Extract the market event date from a Kalshi market or event ticker.

    Parses the YYMONDD date segment from tickers like "KXHIGHNY-26FEB18-T52"
    or event tickers like "KXHIGHNY-26FEB18".

    Args:
        ticker: Market ticker (e.g., "KXHIGHAUS-26FEB23-T63") or
                event ticker (e.g., "KXHIGHNY-26FEB18").

    Returns:
        The market event date, or None if the ticker cannot be parsed.

    Examples:
        >>> parse_market_date_from_ticker("KXHIGHNY-26FEB18-T52")
        datetime.date(2026, 2, 18)
        >>> parse_market_date_from_ticker("KXHIGHAUS-26FEB23-B65.5")
        datetime.date(2026, 2, 23)
        >>> parse_market_date_from_ticker("invalid")
        None
    """
    parts = ticker.split("-")
    if len(parts) < 2:
        return None

    date_str = parts[1].upper()
    try:
        return datetime.strptime(date_str, "%y%b%d").date()
    except ValueError:
        return None


# ─── Bracket Parsing ───


def parse_bracket_from_market(market: dict) -> dict:
    """Parse bracket range from a Kalshi market data dict.

    Uses floor_strike and cap_strike to determine the bracket type:
    - Bottom edge: floor_strike is None -> "X°F or below"
    - Top edge: cap_strike is None -> "X°F or above"
    - Middle: both present -> "X° to Y°F"

    Kalshi settles these markets on the *integer* reported high temp, and
    encodes strikes with a shared-boundary convention (verified against the
    live API, Aug 2026 — e.g. KXHIGHNY-26AUG07):

    - Middle "89° to 90°": floor=89, cap=90, both INCLUSIVE → wins on
      integer temps {89, 90}. Legacy format cap=90.99 means the same thing.
    - Bottom "<89°" (displayed "88° or below"): cap=89 is EXCLUSIVE (it is
      the first middle's floor). Legacy format cap=88.99 covers ≤88 too.
    - Top ">96°" (displayed "97° or above"): floor=96 is EXCLUSIVE (it is
      the last middle's cap) → wins on integer temps ≥97.

    The returned ``lower_bound_f``/``upper_bound_f`` are CONTINUOUS bounds
    at the half-degree, suitable for CDF probability mass and win checks:
    a middle covering integers {89, 90} gets [88.5, 90.5). Passing raw
    strikes through (the pre-v1.9.12 behavior) made middle brackets 1°F
    wide instead of 2°F and misallocated probability mass — see
    docs/ALGO_CHANGELOG.md (2026-08-06 review).

    Args:
        market: Dict from Kalshi market API response. Must contain
                "floor_strike" and "cap_strike" keys (values may be None).

    Returns:
        Dict with bracket metadata:
            label: Human-readable label matching Kalshi's display
                   (e.g., "89° to 90°F", "88°F or below", "97°F or above")
            lower_bound_f: Continuous lower bound (x.5), None for bottom edge
            upper_bound_f: Continuous upper bound (x.5), None for top edge
            is_edge_lower: True if this is the bottom catch-all bracket
            is_edge_upper: True if this is the top catch-all bracket
            ticker: Market ticker (if present in input)
    """
    floor = market.get("floor_strike")
    cap = market.get("cap_strike")
    ticker = market.get("ticker", "")

    if floor is None and cap is not None:
        # Bottom edge bracket: cap is exclusive. Highest covered integer:
        # ceil(89.0)-1=88, ceil(88.99)-1=88 — handles both conventions.
        max_covered = math.ceil(cap) - 1
        label = f"{max_covered}°F or below"
        return {
            "label": label,
            "lower_bound_f": None,
            "upper_bound_f": max_covered + 0.5,
            "is_edge_lower": True,
            "is_edge_upper": False,
            "ticker": ticker,
        }

    if cap is None and floor is not None:
        # Top edge bracket: floor is exclusive (shared with the last
        # middle's cap). Lowest covered integer: floor(96)+1=97 for the
        # integer convention; ceil() covers a fractional floor defensively.
        min_covered = int(floor) + 1 if float(floor).is_integer() else math.ceil(floor)
        label = f"{min_covered}°F or above"
        return {
            "label": label,
            "lower_bound_f": min_covered - 0.5,
            "upper_bound_f": None,
            "is_edge_lower": False,
            "is_edge_upper": True,
            "ticker": ticker,
        }

    if floor is not None and cap is not None:
        # Middle bracket: floor and cap are both inclusive display values.
        # int(cap) handles both conventions: int(90.0)=90, int(90.99)=90.
        lo_int = int(floor)
        hi_int = int(cap)
        label = f"{lo_int}° to {hi_int}°F"
        return {
            "label": label,
            "lower_bound_f": lo_int - 0.5,
            "upper_bound_f": hi_int + 0.5,
            "is_edge_lower": False,
            "is_edge_upper": False,
            "ticker": ticker,
        }

    # Both None — unusual, log a warning
    logger.warning(
        "Market has both floor_strike and cap_strike as None",
        extra={"data": {"ticker": ticker}},
    )
    return {
        "label": "Unknown",
        "lower_bound_f": None,
        "upper_bound_f": None,
        "is_edge_lower": False,
        "is_edge_upper": False,
        "ticker": ticker,
    }


def parse_event_markets(markets: list[KalshiMarket]) -> list[dict]:
    """Parse all bracket markets for an event into structured bracket dicts.

    Converts a list of KalshiMarket models into a sorted list of bracket
    metadata dicts, ordered from lowest to highest temperature range.

    Args:
        markets: List of KalshiMarket models for a single event.

    Returns:
        List of bracket dicts (from parse_bracket_from_market), sorted by
        lower_bound_f (with bottom edge bracket first, top edge last).
    """
    brackets = []
    for market in markets:
        market_dict = {
            "floor_strike": market.floor_strike,
            "cap_strike": market.cap_strike,
            "ticker": market.ticker,
        }
        bracket = parse_bracket_from_market(market_dict)

        # Add pricing data from the market model
        bracket["yes_bid"] = market.yes_bid
        bracket["yes_ask"] = market.yes_ask
        bracket["no_bid"] = market.no_bid
        bracket["no_ask"] = market.no_ask
        bracket["last_price"] = market.last_price
        bracket["volume"] = market.volume
        bracket["status"] = market.status

        brackets.append(bracket)

    # Sort: bottom edge first, then by lower_bound_f, top edge last
    def sort_key(b: dict) -> float:
        if b["is_edge_lower"]:
            return float("-inf")
        if b["is_edge_upper"]:
            return float("inf")
        return b["lower_bound_f"] or 0.0

    brackets.sort(key=sort_key)

    # Tiling check: adjacent continuous bounds must meet exactly, so the
    # 6 brackets partition the whole temperature line with no gaps or
    # overlaps. A mismatch means Kalshi changed its strike conventions —
    # warn loudly instead of silently misallocating probability mass again.
    for prev, nxt in zip(brackets, brackets[1:], strict=False):
        prev_upper = prev.get("upper_bound_f")
        next_lower = nxt.get("lower_bound_f")
        if (
            prev_upper is not None
            and next_lower is not None
            and abs(prev_upper - next_lower) > 0.01
        ):
            logger.warning(
                "Bracket bounds do not tile — possible Kalshi strike convention change",
                extra={
                    "data": {
                        "prev_label": prev["label"],
                        "prev_upper_f": prev_upper,
                        "next_label": nxt["label"],
                        "next_lower_f": next_lower,
                    }
                },
            )

    logger.info(
        "Parsed event brackets",
        extra={
            "data": {
                "count": len(brackets),
                "labels": [b["label"] for b in brackets],
            }
        },
    )

    return brackets
