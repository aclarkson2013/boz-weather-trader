"""Add as-issued forecast archive tables (algo v2, slice S3).

- forecast_issuances: daily-max forecasts AS ISSUED by GFS MOS, NAM MOS and NBM
  (IEM archive), keyed by (city, model, run_ts, valid_date), with a conservative
  ``available_at`` so backtests can't use a forecast before it was published.
- forecast_archive_chunks: backfill progress per city / model / month.

Additive only.

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "forecast_issuances",
        sa.Column("city", sa.String(), primary_key=True),
        sa.Column("model", sa.String(), primary_key=True),  # GFS / NAM / NBS
        sa.Column("run_ts", sa.DateTime(), primary_key=True),  # Model run (UTC)
        sa.Column("valid_date", sa.Date(), primary_key=True),  # Local date of the max
        sa.Column("station", sa.String(), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("lead_hours", sa.Integer(), nullable=True),
        sa.Column("tmax_f", sa.Float(), nullable=False),
        sa.Column("tmax_sd_f", sa.Float(), nullable=True),  # NBM only
        sa.Column("fetched_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_forecast_issuances_city_valid", "forecast_issuances", ["city", "valid_date"]
    )

    op.create_table(
        "forecast_archive_chunks",
        sa.Column("city", sa.String(), primary_key=True),
        sa.Column("model", sa.String(), primary_key=True),
        sa.Column("month", sa.Date(), primary_key=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("rows", sa.Integer(), server_default="0"),
        sa.Column("attempts", sa.Integer(), server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("forecast_archive_chunks")
    op.drop_index("ix_forecast_issuances_city_valid", table_name="forecast_issuances")
    op.drop_table("forecast_issuances")
