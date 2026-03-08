"""Async SQLAlchemy engine and session factory."""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import settings

_is_pg = "postgresql" in settings.database_url
logger = logging.getLogger(__name__)

_engine_kwargs: dict = {"echo": False}
if _is_pg:
    # PostgreSQL: configurable pool tuned for higher concurrent workload.
    _engine_kwargs.update(
        pool_size=max(1, settings.db_pool_size),
        max_overflow=max(0, settings.db_max_overflow),
        pool_timeout=max(1, settings.db_pool_timeout_seconds),
        pool_recycle=max(60, settings.db_pool_recycle_seconds),
        pool_pre_ping=False,
    )
else:
    # SQLite: use NullPool to avoid thread/coroutine contention issues.
    from sqlalchemy.pool import NullPool

    _engine_kwargs["poolclass"] = NullPool

engine = create_async_engine(settings.database_url, **_engine_kwargs)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _ensure_postgres_schema_compat(conn) -> None:
    """Best-effort compatibility upgrades for older PostgreSQL schemas."""
    statements = [
        # Rename legacy point columns without data loss.
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name='users' AND column_name='traffic'
          ) AND NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name='users' AND column_name='self_points'
          ) THEN
            ALTER TABLE users RENAME COLUMN traffic TO self_points;
          END IF;
        END$$
        """,
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name='companies' AND column_name='total_funds'
          ) AND NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name='companies' AND column_name='cp_points'
          ) THEN
            ALTER TABLE companies RENAME COLUMN total_funds TO cp_points;
          END IF;
        END$$
        """,
        # daily_reports legacy compatibility: old DBs may miss these columns
        "ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS product_income BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS employee_income BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS cooperation_bonus BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS realestate_income BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS reputation_buff_income BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS total_income BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS operating_cost BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS dividend_paid BIGINT NOT NULL DEFAULT 0",
    ]
    for sql in statements:
        await conn.execute(text(sql))


async def _ensure_sqlite_schema_compat(conn) -> None:
    """Best-effort compatibility upgrades for older SQLite schemas."""
    statements = [
        "ALTER TABLE users RENAME COLUMN traffic TO self_points",
        "ALTER TABLE companies RENAME COLUMN total_funds TO cp_points",
    ]
    for sql in statements:
        try:
            await conn.execute(text(sql))
        except Exception:
            # Ignore when column already renamed / legacy column does not exist.
            pass


async def init_db():
    """Create all tables (dev convenience; use Alembic for production)."""
    from db.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if _is_pg:
            try:
                await _ensure_postgres_schema_compat(conn)
            except Exception:
                logger.exception("PostgreSQL compatibility schema upgrade failed")
        else:
            try:
                await _ensure_sqlite_schema_compat(conn)
            except Exception:
                logger.exception("SQLite compatibility schema upgrade failed")
