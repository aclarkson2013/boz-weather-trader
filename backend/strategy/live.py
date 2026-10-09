"""Live market snapshots for paper trading (algo v2, slice S5).

Builds the same ``MarketSnapshot`` the backtester uses, but from Kalshi's LIVE
market listing (current top of book) instead of archived hourly candles, so a
strategy runs identically in both. Displayed sizes are returned separately so
paper fills can be flagged when the book couldn't have absorbed them.
"""

from __future__ import annotations

from datetime import date, datetime

from backend.common.schemas import BracketQuote, MarketSnapshot
from backend.kalshi.archive import check_tiling, dollars_to_cents, parse_market


def build_live_snapshot(
    raw_markets: list[dict],
    city: str,
    event_date: date,
    decision: str,
    decision_ts: datetime,
    now: datetime,
) -> tuple[MarketSnapshot | None, dict[str, tuple[float | None, float | None]]]:
    """Snapshot one live event; returns (snapshot or None, {ticker: (bid_size, ask_size)}).

    Args:
        raw_markets: Markets from ``GET /markets?event_ticker=...`` (live).
        city: City code.
        event_date: Event (LST) date.
        decision: Decision label (e.g. "D1E").
        decision_ts: Scheduled decision time (naive UTC) — recorded on the snapshot.
        now: Actual quote time (naive UTC).

    Returns:
        None when the event isn't open with all brackets active.
    """
    active = [m for m in raw_markets if (m.get("status") or "") == "active"]
    rows = [r for r in (parse_market(m, "live") for m in active) if r is not None]
    if not rows or len(rows) != len(raw_markets):
        return None, {}
    tiles = check_tiling(rows)
    by_ticker = {m["ticker"]: m for m in active}
    quotes: list[BracketQuote] = []
    sizes: dict[str, tuple[float | None, float | None]] = {}
    for r in sorted(
        rows, key=lambda r: float("-inf") if r["lower_bound_f"] is None else r["lower_bound_f"]
    ):
        raw = by_ticker[r["ticker"]]
        bid, _ = dollars_to_cents(raw.get("yes_bid_dollars"))
        ask, _ = dollars_to_cents(raw.get("yes_ask_dollars"))
        quotes.append(
            BracketQuote(
                ticker=r["ticker"],
                label=r["label"],
                lower_bound_f=r["lower_bound_f"],
                upper_bound_f=r["upper_bound_f"],
                yes_bid=bid,
                yes_ask=ask,
                quote_ts=now,
            )
        )
        bid_size = raw.get("yes_bid_size_fp")
        ask_size = raw.get("yes_ask_size_fp")
        sizes[r["ticker"]] = (
            float(bid_size) if bid_size not in (None, "") else None,
            float(ask_size) if ask_size not in (None, "") else None,
        )
    snapshot = MarketSnapshot(
        city=city,
        event_date=event_date,
        decision=decision,
        decision_ts=decision_ts,
        quotes=quotes,
        tiles_ok=tiles,
    )
    return snapshot, sizes
