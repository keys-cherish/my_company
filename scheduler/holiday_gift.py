"""Holiday gift scheduler — auto-distribute points on holidays and notify groups.

Supports: fixed solar dates, weekly recurring, month-end, lunar calendar,
and winter solstice (lookup table).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from sqlalchemy import select

from cache.redis_client import get_redis, add_stream_event
from config import settings
from db.engine import async_session
from db.models import Company, User
from services.company_service import add_funds
from services.user_service import add_self_points_by_user_id
from utils.formatters import fmt_currency
from utils.timezone import BJ_TZ

logger = logging.getLogger(__name__)

_HOLIDAYS_PATH = Path(__file__).resolve().parent.parent / "game_data" / "holidays.json"
_holidays_cache: dict | None = None
_bot_ref = None

try:
    from lunardate import LunarDate
    _HAS_LUNAR = True
except ImportError:
    _HAS_LUNAR = False
    logger.warning("lunardate not installed; lunar holidays disabled (pip install lunardate)")


def set_bot(bot):
    global _bot_ref
    _bot_ref = bot


def _load_holidays() -> dict:
    global _holidays_cache
    if _holidays_cache is not None:
        return _holidays_cache
    try:
        with open(_HOLIDAYS_PATH, encoding="utf-8") as f:
            _holidays_cache = json.load(f)
    except Exception:
        logger.exception("Failed to load holidays.json")
        _holidays_cache = {}
    return _holidays_cache


def _collect_today_events(now: dt.datetime) -> list[dict]:
    """Return all events that match today's date."""
    data = _load_holidays()
    today = now.date()
    month, day = today.month, today.day
    weekday = today.weekday()  # 0=Mon … 6=Sun
    is_last_day = (today + dt.timedelta(days=1)).day == 1
    today_md = today.strftime("%m-%d")

    events: list[dict] = []

    # ── Fixed solar holidays (MM-DD or YYYY-MM-DD) ──
    for h in data.get("fixed", []):
        d = h.get("date", "")
        if d == today_md or d == today.isoformat():
            events.append(h)

    # ── Weekly recurring ──
    for h in data.get("weekly", []):
        if h.get("weekday") == weekday:
            events.append(h)

    # ── Month-end ──
    me = data.get("monthly_end", {})
    if me.get("enabled") and is_last_day:
        events.append(me)

    # ── Winter solstice (lookup table) ──
    dz = data.get("dongzhi", {})
    day_table = dz.get("day_by_year", {})
    dongzhi_day = day_table.get(str(today.year))
    if dongzhi_day and month == 12 and day == int(dongzhi_day):
        events.append(dz)

    # ── Lunar calendar holidays ──
    if _HAS_LUNAR:
        try:
            lunar = LunarDate.fromSolarDate(today.year, today.month, today.day)
            tomorrow = today + dt.timedelta(days=1)
            tmr_lunar = LunarDate.fromSolarDate(tomorrow.year, tomorrow.month, tomorrow.day)

            for h in data.get("lunar", []):
                lm = h.get("lunar_month")
                ld = h.get("lunar_day")
                is_eve = h.get("eve", False)

                if is_eve:
                    # Eve: tomorrow is lunar_month/lunar_day
                    if tmr_lunar.month == lm and tmr_lunar.day == ld and not tmr_lunar.isLeapMonth:
                        events.append(h)
                else:
                    if lunar.month == lm and lunar.day == ld and not lunar.isLeapMonth:
                        events.append(h)
        except Exception:
            logger.warning("Lunar date check failed", exc_info=True)

    return events


async def holiday_gift_job():
    """Check if today has any events and distribute gifts to all players."""
    now = dt.datetime.now(BJ_TZ)
    events = _collect_today_events(now)
    if not events:
        return

    date_key = now.date().isoformat()
    r = await get_redis()

    # Filter out already-sent events (by name, to allow multiple events per day)
    pending: list[dict] = []
    for ev in events:
        ev_key = f"holiday_gift:{date_key}:{ev.get('name', '')}"
        if not await r.exists(ev_key):
            pending.append(ev)

    if not pending:
        return

    # Aggregate totals
    total_user_amount = sum(e.get("amount", 0) for e in pending)
    total_company_amount = sum(e.get("company_amount", 0) for e in pending)

    if total_user_amount <= 0 and total_company_amount <= 0:
        return

    logger.info(
        "Holiday events today: %s (user=%d, company=%d)",
        ", ".join(e.get("name", "?") for e in pending),
        total_user_amount,
        total_company_amount,
    )

    user_success = 0
    company_success = 0
    total_users = 0
    total_companies = 0

    async with async_session() as session:
        async with session.begin():
            if total_user_amount > 0:
                result = await session.execute(select(User))
                users = list(result.scalars().all())
                total_users = len(users)
                for user in users:
                    ok = await add_self_points_by_user_id(
                        session, user.id, total_user_amount, reason="holiday_gift"
                    )
                    if ok:
                        user_success += 1

            if total_company_amount > 0:
                result = await session.execute(select(Company))
                companies = list(result.scalars().all())
                total_companies = len(companies)
                for company in companies:
                    ok = await add_funds(session, company.id, total_company_amount)
                    if ok:
                        company_success += 1

    # Mark all events as sent
    for ev in pending:
        ev_key = f"holiday_gift:{date_key}:{ev.get('name', '')}"
        await r.set(ev_key, "1", ex=86400 * 2)

    await add_stream_event("holiday_gift", {
        "events": [e.get("name") for e in pending],
        "user_amount": total_user_amount,
        "company_amount": total_company_amount,
        "user_success": user_success,
        "company_success": company_success,
    })

    logger.info(
        "Holiday gift done: users %d/%d, companies %d/%d",
        user_success, total_users, company_success, total_companies,
    )

    # Broadcast to groups
    if _bot_ref:
        await _broadcast_holiday_notice(
            pending, total_user_amount, total_company_amount,
            user_success, total_users, company_success, total_companies,
        )


async def _broadcast_holiday_notice(
    events: list[dict],
    total_user_amount: int,
    total_company_amount: int,
    user_success: int,
    total_users: int,
    company_success: int,
    total_companies: int,
):
    # Build announcement per event
    event_parts: list[str] = []
    for ev in events:
        emoji = ev.get("emoji", "")
        name = ev.get("name", "")
        desc = ev.get("desc", "")
        amt = ev.get("amount", 0)
        camp = ev.get("company_amount", 0)
        part = f"{emoji} {name}"
        if desc:
            part += f"\n{desc}"
        rewards = []
        if amt > 0:
            rewards.append(f"个人 +{fmt_currency(amt)}")
        if camp > 0:
            rewards.append(f"公司 +{fmt_currency(camp)}")
        if rewards:
            part += f"\n  {'  |  '.join(rewards)}"
        event_parts.append(part)

    lines = [
        f"{'─' * 28}",
        "\n\n".join(event_parts),
        f"{'─' * 28}",
    ]
    if total_user_amount > 0:
        lines.append(f"🏅 个人积分: 每人 +{fmt_currency(total_user_amount)} ({user_success}/{total_users} 人)")
    if total_company_amount > 0:
        lines.append(f"💰 公司积分: 每家 +{fmt_currency(total_company_amount)} ({company_success}/{total_companies} 家)")
    lines.append(f"{'─' * 28}")
    lines.append("✅ 已自动发放，祝各位老板生意兴隆！")

    text = "\n".join(lines)

    chat_ids = settings.allowed_chat_id_set
    if not chat_ids:
        for admin_tg_id in settings.super_admin_tg_id_set:
            try:
                await _bot_ref.send_message(admin_tg_id, text)
            except Exception:
                logger.exception("Holiday notice failed for admin %s", admin_tg_id)
        return

    for chat_id in chat_ids:
        try:
            await _bot_ref.send_message(chat_id, text)
        except Exception:
            logger.exception("Holiday notice failed for chat %s", chat_id)
