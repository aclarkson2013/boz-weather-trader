"""Add Kalshi market archive tables (algo v2, slice S1).

Stores real Kalshi market history so strategies can be backtested on actual
bid/ask prices instead of synthetic ones (docs/ALGO_V2_PRD.md):

- kalshi_markets: one row per market (bracket) with strikes, x.5 bounds,
  tiling flag, outcome (result / expiration_value), era and data source.
- kalshi_candles: hourly yes_bid / yes_ask OHLC, trade close, volume, OI.
- kalshi_quotes: live top-of-book snapshots (every 5 min) for fill realism.
- kalshi_archive_days: per city-day backfill progress, so interrupted
  backfills resume without gaps or duplicates.

All tables are additive; nothing existing is touched.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

_CITY = sa.String()  # City code (NYC/CHI/MIA/AUS); matches existing tables


def upgrade() -> None:
    op.create_table(
        "kalshi_markets",
        sa.Column("ticker", sa.String(), primary_key=True),
        sa.Column("event_ticker", sa.String(), nullable=False),
        sa.Column("series_ticker", sa.String(), nullable=False),
        sa.Column("city", _CITY, nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("strike_type", sa.String(), nullable=True),
        sa.Column("floor_strike", sa.Float(), nullable=True),
        sa.Column("cap_strike", sa.Float(), nullable=True),
        sa.Column("lower_bound_f", sa.Float(), nullable=True),
        sa.Column("upper_bound_f", sa.Float(), nullable=True),
        sa.Column("tiles_ok", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("open_time", sa.DateTime(), nullable=True),
        sa.Column("close_time", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("result", sa.String(), nullable=True),
        sa.Column("expiration_value", sa.Float(), nullable=True),
        sa.Column("era", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("n_candles", sa.Integer(), nullable=True),
        sa.Column("median_spread_cents", sa.Float(), nullable=True),
        sa.Column("candles_fetched_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_kalshi_markets_city_date", "kalshi_markets", ["city", "event_date"])
    op.create_index("ix_kalshi_markets_event", "kalshi_markets", ["event_ticker"])

    op.create_table(
        "kalshi_candles",
        sa.Column("ticker", sa.String(), primary_key=True),
        sa.Column("period_min", sa.Integer(), primary_key=True),
        sa.Column("end_ts", sa.DateTime(), primary_key=True),
        sa.Column("yes_bid_open", sa.SmallInteger(), nullable=True),
        sa.Column("yes_bid_high", sa.SmallInteger(), nullable=True),
        sa.Column("yes_bid_low", sa.SmallInteger(), nullable=True),
        sa.Column("yes_bid_close", sa.SmallInteger(), nullable=True),
        sa.Column("yes_ask_open", sa.SmallInteger(), nullable=True),
        sa.Column("yes_ask_high", sa.SmallInteger(), nullable=True),
        sa.Column("yes_ask_low", sa.SmallInteger(), nullable=True),
        sa.Column("yes_ask_close", sa.SmallInteger(), nullable=True),
        sa.Column("price_close", sa.SmallInteger(), nullable=True),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("open_interest", sa.Float(), nullable=True),
        sa.Column("subcent", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.create_table(
        "kalshi_quotes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("event_ticker", sa.String(), nullable=False),
        sa.Column("city", _CITY, nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("yes_bid", sa.SmallInteger(), nullable=True),
        sa.Column("yes_ask", sa.SmallInteger(), nullable=True),
        sa.Column("yes_bid_size", sa.Float(), nullable=True),
        sa.Column("yes_ask_size", sa.Float(), nullable=True),
        sa.Column("last_price", sa.SmallInteger(), nullable=True),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
    )
    op.create_index("ix_kalshi_quotes_ticker_ts", "kalshi_quotes", ["ticker", "ts"])
    op.create_index("ix_kalshi_quotes_city_date", "kalshi_quotes", ["city", "event_date"])

    op.create_table(
        "kalshi_archive_days",
        sa.Column("city", _CITY, primary_key=True),
        sa.Column("event_date", sa.Date(), primary_key=True),
        sa.Column("event_ticker", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("n_markets", sa.Integer(), server_default="0"),
        sa.Column("attempts", sa.Integer(), server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("kalshi_archive_days")
    op.drop_index("ix_kalshi_quotes_city_date", table_name="kalshi_quotes")
    op.drop_index("ix_kalshi_quotes_ticker_ts", table_name="kalshi_quotes")
    op.drop_table("kalshi_quotes")
    op.drop_table("kalshi_candles")
    op.drop_index("ix_kalshi_markets_event", table_name="kalshi_markets")
    op.drop_index("ix_kalshi_markets_city_date", table_name="kalshi_markets")
    op.drop_table("kalshi_markets")
