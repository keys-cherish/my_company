"""Daily settlement scheduler using APScheduler."""

from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, text

from config import settings
from db.engine import async_session, engine
from cache.redis_client import add_stream_event
from services.settlement_service import format_daily_report, settle_all
from utils.timezone import BJ_TZ, format_bj_now

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_bot_ref = None  # will be set at startup
_BACKUP_DIR = Path(__file__).resolve().parent.parent / "db_backup"


def set_bot(bot):
    global _bot_ref
    _bot_ref = bot


def _json_safe(value):
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


async def _iter_table_rows(conn, table_name: str) -> AsyncIterator[dict]:
    """Iterate table rows with streaming when the DB driver supports it."""
    stmt = text(f'SELECT * FROM "{table_name}"')
    try:
        stream_result = await conn.stream(stmt)
    except Exception:
        # Fallback for dialects that do not implement streaming cursors.
        result = await conn.execute(stmt)
        for row in result.mappings():
            yield row
        return

    async for row in stream_result.mappings():
        yield row


def _rotate_old_backups(keep_files: int):
    if keep_files <= 0:
        return
    files = sorted(_BACKUP_DIR.glob("my_company_backup_*.json.gz"))
    overflow = len(files) - keep_files
    if overflow <= 0:
        return
    for old_file in files[:overflow]:
        old_file.unlink(missing_ok=True)


async def _upload_to_webdav(file_path: Path) -> bool:
    """Upload a backup file to WebDAV under db_backup/YYYYMMDD/."""
    url = settings.webdav_backup_url
    if not url or not settings.webdav_backup_username:
        return False

    import aiohttp
    import base64

    date_dir = dt.datetime.now(BJ_TZ).strftime("%Y%m%d")
    remote_url = f"{url.rstrip('/')}/db_backup/{date_dir}/{file_path.name}"
    creds = base64.b64encode(
        f"{settings.webdav_backup_username}:{settings.webdav_backup_password}".encode()
    ).decode()
    auth_header = {"Authorization": f"Basic {creds}"}

    try:
        async with aiohttp.ClientSession() as session:
            # Ensure db_backup/ and db_backup/YYYYMMDD/ exist
            for d in [f"{url.rstrip('/')}/db_backup/", f"{url.rstrip('/')}/db_backup/{date_dir}/"]:
                async with session.request(
                    "MKCOL", d, headers=auth_header,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as _:
                    pass  # 201=created, 405/409=already exists

            with open(file_path, "rb") as f:
                async with session.put(
                    remote_url,
                    data=f,
                    headers={**auth_header, "Content-Type": "application/gzip"},
                    timeout=aiohttp.ClientTimeout(total=600),
                ) as resp:
                    if resp.status in (200, 201, 204):
                        logger.info("WebDAV upload OK: %s -> %s", file_path.name, remote_url)
                        return True
                    body = await resp.text()
                    logger.warning("WebDAV upload failed %d: %s", resp.status, body[:200])
                    return False
    except Exception:
        logger.exception("WebDAV upload error for %s", file_path.name)
        return False


async def _create_db_backup() -> tuple[Path, dict[str, int]]:
    from db.models import Base

    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    now_bj = dt.datetime.now(BJ_TZ)
    file_path = _BACKUP_DIR / f"my_company_backup_{now_bj.strftime('%Y%m%dT%H%M%S%z')}.json.gz"

    table_counts: dict[str, int] = {}

    # Stream directly to gzip JSON to avoid loading the whole DB snapshot in memory.
    with gzip.open(file_path, "wt", encoding="utf-8") as f:
        f.write("{")
        f.write('"project":')
        json.dump("my_company", f, ensure_ascii=False)
        f.write(',"created_at_bj":')
        json.dump(now_bj.isoformat(), f, ensure_ascii=False)
        f.write(',"tables":{')

        first_table = True
        async with engine.connect() as conn:
            for table in Base.metadata.sorted_tables:
                if not first_table:
                    f.write(",")
                first_table = False

                json.dump(table.name, f, ensure_ascii=False)
                f.write(":[")

                first_row = True
                row_count = 0
                async for row in _iter_table_rows(conn, table.name):
                    if not first_row:
                        f.write(",")
                    first_row = False
                    json.dump(
                        {k: _json_safe(v) for k, v in row.items()},
                        f,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    row_count += 1

                f.write("]")
                table_counts[table.name] = row_count

        f.write("}}")

    _rotate_old_backups(settings.backup_keep_files)
    return file_path, table_counts


async def _notify_backup_status(text_msg: str):
    if not _bot_ref or not settings.backup_notify_super_admin:
        return
    admin_ids = settings.super_admin_tg_id_set
    if not admin_ids:
        return
    for admin_tg_id in admin_ids:
        try:
            await _bot_ref.send_message(admin_tg_id, text_msg)
        except Exception:
            logger.exception("Failed to notify super admin %s for backup", admin_tg_id)


async def _backup_job():
    if not settings.backup_enabled:
        return

    try:
        file_path, table_counts = await _create_db_backup()
        total_rows = sum(table_counts.values())
        summary = ", ".join(f"{name}:{count}" for name, count in sorted(table_counts.items()))
        logger.info("DB backup completed: %s rows=%d", file_path, total_rows)

        webdav_ok = await _upload_to_webdav(file_path)
        webdav_label = ""
        if settings.webdav_backup_url:
            webdav_label = " | ☁️ 已同步" if webdav_ok else " | ⚠️ WebDAV失败"

        await add_stream_event(
            "backup_completed",
            {"file": str(file_path), "rows": total_rows, "tables": table_counts},
        )
        await _notify_backup_status(
            f"🛡 my_company 自动备份完成{webdav_label}\n"
            f"⏰ 北京时间: {format_bj_now()}\n"
            f"📦 文件: {file_path}\n"
            f"🧾 总行数: {total_rows}\n"
            f"📚 分表: {summary}",
        )
    except Exception as exc:
        logger.exception("DB backup failed")
        await add_stream_event("backup_failed", {"error": str(exc)})
        await _notify_backup_status(
            "❌ my_company 自动备份失败\n"
            f"⏰ 北京时间: {format_bj_now()}\n"
            f"原因: {exc}",
        )


async def _daily_job():
    logger.info("Starting daily settlement...")
    async with async_session() as session:
        async with session.begin():
            reports = await settle_all(session)
    logger.info("Daily settlement completed: %d companies processed", len(reports))
    await add_stream_event("daily_settlement_completed", {"companies": len(reports)})

    # Refresh black market deals for the new day
    try:
        from services.shop_service import generate_black_market
        await generate_black_market()
        logger.info("Black market refreshed")
    except Exception:
        logger.exception("Failed to refresh black market")

    # Notify owners
    if _bot_ref:
        for company, report, events in reports:
            try:
                text = format_daily_report(company, report, events)
                from db.engine import async_session as _sess
                async with _sess() as s:
                    from db.models import User
                    owner = await s.get(User, company.owner_id)
                    if owner:
                        await _bot_ref.send_message(owner.tg_id, text)
            except Exception:
                logger.exception("Failed to notify owner of company %s", company.name)


async def _research_realtime_job():
    """Complete expired research every minute (near-real-time)."""
    from db.models import Company, ResearchProgress, User
    from services.research_service import check_and_complete_research

    completed_total = 0
    async with async_session() as session:
        async with session.begin():
            active_company_ids = (
                await session.execute(
                    select(ResearchProgress.company_id)
                    .where(ResearchProgress.status == "researching")
                    .distinct()
                )
            ).scalars().all()
            if not active_company_ids:
                return

            companies = (
                await session.execute(
                    select(Company).where(Company.id.in_(active_company_ids))
                )
            ).scalars().all()
            # LOCALTIMESTAMP returns naive timestamp matching started_at storage
            tick_now = (await session.execute(select(text("LOCALTIMESTAMP")))).scalar()
            for company in companies:
                completed = await check_and_complete_research(
                    session,
                    company.id,
                    now=tick_now,
                )
                if not completed:
                    continue
                completed_total += len(completed)
                await add_stream_event(
                    "research_completed",
                    {"company_id": company.id, "company_name": company.name, "techs": completed},
                )
                if _bot_ref:
                    try:
                        owner = await session.get(User, company.owner_id)
                        if not owner:
                            continue
                        await _bot_ref.send_message(
                            owner.tg_id,
                            f"🔬 科研已完成：{', '.join(completed)}",
                        )
                    except Exception:
                        # 通知失败不影响科研结算
                        pass
    if completed_total > 0:
        logger.info("Realtime research settlement completed: %d tech(s)", completed_total)


def start_scheduler():
    global _scheduler
    tz = ZoneInfo(settings.app_timezone or "Asia/Shanghai")
    _scheduler = AsyncIOScheduler(timezone=tz)
    _scheduler.add_job(
        _daily_job,
        "cron",
        hour=settings.settlement_hour,
        minute=settings.settlement_minute,
        id="daily_settlement",
    )
    if settings.backup_enabled and settings.backup_interval_minutes > 0:
        _scheduler.add_job(
            _backup_job,
            "interval",
            minutes=settings.backup_interval_minutes,
            id="db_backup",
            coalesce=True,
            max_instances=1,
        )
    _scheduler.add_job(
        _research_realtime_job,
        "interval",
        minutes=5,
        id="research_realtime_settlement",
    )
    # Holiday gift check: runs daily at 00:05 (after settlement)
    from scheduler.holiday_gift import holiday_gift_job

    _scheduler.add_job(
        holiday_gift_job,
        "cron",
        hour=settings.settlement_hour,
        minute=max(0, settings.settlement_minute + 5),
        id="holiday_gift",
    )
    _scheduler.start()
    logger.info(
        "Scheduler started: daily settlement at %02d:%02d (%s)",
        settings.settlement_hour,
        settings.settlement_minute,
        settings.app_timezone,
    )
    if settings.backup_enabled and settings.backup_interval_minutes > 0:
        logger.info(
            "Scheduler started: DB backup every %d minute(s) (%s, keep %d files)",
            settings.backup_interval_minutes,
            settings.app_timezone,
            settings.backup_keep_files,
        )


def stop_scheduler():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown()
        _scheduler = None
