"""Add research_reports table (algo v2, slice S2).

Stores every real-price backtest / control / holdout run of a pre-registered
strategy with its parameters, window, config hash, app version, full results
and gate verdict — the audit trail behind any decision to paper-trade.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSON

# revision identifiers, used by Alembic.
revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(), nullable=False),  # backtest / control / holdout
        sa.Column("strategy_id", sa.String(), nullable=False),
        sa.Column("params", JSON(), nullable=True),
        sa.Column("config_hash", sa.String(), nullable=False),
        sa.Column("k", sa.Integer(), nullable=False),
        sa.Column("app_version", sa.String(), nullable=True),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("window_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),  # queued / running / done / error
        sa.Column("gate_passed", sa.Boolean(), nullable=True),
        sa.Column("results", JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_research_reports_strategy_id", "research_reports", ["strategy_id"])


def downgrade() -> None:
    op.drop_index("ix_research_reports_strategy_id", table_name="research_reports")
    op.drop_table("research_reports")
