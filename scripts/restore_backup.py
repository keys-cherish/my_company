"""Restore database from JSON backup file.

Usage:
    uv run python scripts/restore_backup.py db_backup/my_company_backup_20260307T210000+0800.json.gz
    uv run python scripts/restore_backup.py backup.json
"""

import asyncio
import gzip
import json
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from db.engine import engine, async_session, init_db
from db.models import (
    Base, User, Company, CompanyOperationProfile, Shareholder,
    ResearchProgress, Product, Roadshow, Cooperation, RealEstate,
    DailyReport, WeeklyTask,
)

# Insertion order respecting foreign keys
TABLE_MODEL_MAP = {
    "users": User,
    "companies": Company,
    "company_operation_profiles": CompanyOperationProfile,
    "shareholders": Shareholder,
    "research_progress": ResearchProgress,
    "products": Product,
    "roadshows": Roadshow,
    "cooperations": Cooperation,
    "real_estates": RealEstate,
    "daily_reports": DailyReport,
    "weekly_tasks": WeeklyTask,
}

DATETIME_FIELDS = {
    "created_at", "completed_at", "started_at", "joined_at",
    "purchased_at", "expires_at", "updated_at", "training_expires_at",
}


def parse_row(row: dict) -> dict:
    """Parse datetime strings in a row."""
    for key, value in row.items():
        if key in DATETIME_FIELDS and value is not None:
            row[key] = datetime.fromisoformat(value)
    return row


async def restore(backup_path: str):
    p = Path(backup_path)
    if p.suffix == ".gz":
        with gzip.open(p, "rt", encoding="utf-8") as f:
            data = json.load(f)
    else:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)

    tables = data["tables"]
    print(f"Backup: {data.get('created_at_bj', 'unknown')}")
    print(f"Tables: {list(tables.keys())}")

    # Drop all tables and recreate
    print("\nDropping all tables...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    print("Creating tables...")
    await init_db()

    # Insert data table by table
    async with async_session() as session:
        async with session.begin():
            for table_name, model_cls in TABLE_MODEL_MAP.items():
                rows = tables.get(table_name, [])
                if not rows:
                    print(f"  {table_name}: 0 rows (skipped)")
                    continue

                for row in rows:
                    parsed = parse_row(row)
                    session.add(model_cls(**parsed))

                await session.flush()
                print(f"  {table_name}: {len(rows)} rows")

            # Reset auto-increment sequences for PostgreSQL
            if "postgresql" in str(engine.url):
                for table_name, model_cls in TABLE_MODEL_MAP.items():
                    tbl = model_cls.__tablename__
                    # Only tables with 'id' primary key need sequence reset
                    if not hasattr(model_cls, "id"):
                        continue
                    if table_name == "company_operation_profiles":
                        continue  # uses company_id as PK, no auto-increment
                    await session.execute(text(
                        f"SELECT setval(pg_get_serial_sequence('{tbl}', 'id'), "
                        f"COALESCE((SELECT MAX(id) FROM {tbl}), 1))"
                    ))
                print("  Sequences reset.")

    print("\nRestore complete!")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: uv run python scripts/restore_backup.py <backup.json>")
        sys.exit(1)

    asyncio.run(restore(sys.argv[1]))
