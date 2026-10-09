"""Add paper-trading tables (algo v2, slice S5).

- paper_trades: simulated taker fills of forward-only strategies on live quotes
  (exact fee, displayed-size feasibility flag), settled on Kalshi's result.
- paper_decisions: one row per strategy x city x event day x decision time,
  including "no orders" decisions (idempotency + visible gaps).
- paper_strategy_state: active/stopped status and kill-switch statistics.

Additive only. Nothing here places real orders.

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_trades",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("strategy_id", sa.String(), nullable=False),
        sa.Column("city", sa.String(), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("decision_ts", sa.DateTime(), nullable=False),
        sa.Column("quoted_at", sa.DateTime(), nullable=False),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("fee_cents", sa.Integer(), nullable=False),
        sa.Column("yes_bid", sa.Integer(), nullable=True),
        sa.Column("yes_ask", sa.Integer(), nullable=True),
        sa.Column("available_size", sa.Float(), nullable=True),
        sa.Column("fill_feasible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("model_probability", sa.Float(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),  # open / settled / void
        sa.Column("result", sa.String(), nullable=True),
        sa.Column("pnl_cents", sa.Integer(), nullable=True),
        sa.Column("settled_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_paper_trades_strategy_status", "paper_trades", ["strategy_id", "status"])
    op.create_index("ix_paper_trades_event", "paper_trades", ["city", "event_date"])

    op.create_table(
        "paper_decisions",
        sa.Column("strategy_id", sa.String(), primary_key=True),
        sa.Column("city", sa.String(), primary_key=True),
        sa.Column("event_date", sa.Date(), primary_key=True),
        sa.Column("decision", sa.String(), primary_key=True),
        sa.Column("decided_at", sa.DateTime(), nullable=False),
        sa.Column("n_orders", sa.Integer(), server_default="0"),
        sa.Column("note", sa.Text(), nullable=True),
    )

    op.create_table(
        "paper_strategy_state",
        sa.Column("strategy_id", sa.String(), primary_key=True),
        sa.Column("status", sa.String(), nullable=False),  # active / stopped
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("stopped_at", sa.DateTime(), nullable=True),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        sa.Column("sprt_llr", sa.Float(), nullable=True),
        sa.Column("cusum_cents", sa.Float(), nullable=True),
        sa.Column("last_evaluated_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("paper_strategy_state")
    op.drop_table("paper_decisions")
    op.drop_index("ix_paper_trades_event", table_name="paper_trades")
    op.drop_index("ix_paper_trades_strategy_status", table_name="paper_trades")
    op.drop_table("paper_trades")
