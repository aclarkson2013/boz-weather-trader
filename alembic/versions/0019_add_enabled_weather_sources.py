"""Add enabled_weather_sources column to users table.

Adds a comma-separated list of the weather sources that are blended into the
prediction ensemble. Defaults to the three-source working set chosen in the
2026-08-21 source review (docs/ALGO_CHANGELOG.md): NWS:gridpoint,
Open-Meteo:GFS, Open-Meteo:ICON.

NWS and NWS:gridpoint are effectively the same feed (error correlation 0.983,
identical on 80% of days) and ECMWF was the weakest member, so both are off by
default. They are still fetched, stored, and scored — only excluded from the
ensemble — so they can be re-enabled from Settings at any time.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

_DEFAULT = "NWS:gridpoint,Open-Meteo:GFS,Open-Meteo:ICON"


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("enabled_weather_sources", sa.String(), server_default=_DEFAULT),
    )
    # Backfill existing rows explicitly — server_default only applies to
    # inserts, and an existing user would otherwise read NULL.
    op.execute(
        sa.text(
            "UPDATE users SET enabled_weather_sources = :default "
            "WHERE enabled_weather_sources IS NULL"
        ).bindparams(default=_DEFAULT)
    )


def downgrade() -> None:
    op.drop_column("users", "enabled_weather_sources")
