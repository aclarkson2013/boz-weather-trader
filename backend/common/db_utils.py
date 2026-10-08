"""Database helpers shared across modules (dialect-aware bulk upsert)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


async def upsert_rows(
    session: AsyncSession,
    model: type,
    rows: list[dict],
    key_columns: list[str],
    batch_size: int = 500,
) -> None:
    """Insert-or-update rows by natural key (PostgreSQL in prod, SQLite in tests).

    Args:
        session: Async DB session (caller commits).
        model: ORM model class.
        rows: Row dicts (all with the same keys).
        key_columns: Conflict target columns (must be unique/primary key).
        batch_size: Rows per statement.
    """
    if not rows:
        return
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert

    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        stmt = insert(model).values(chunk)
        update_cols = {c: stmt.excluded[c] for c in chunk[0] if c not in key_columns}
        stmt = stmt.on_conflict_do_update(index_elements=key_columns, set_=update_cols)
        await session.execute(stmt)
