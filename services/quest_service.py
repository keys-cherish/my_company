"""Weekly quest system — track progress and award rewards."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import WeeklyTask
from services.user_service import add_self_points, add_traffic

_quest_definitions: list[dict] | None = None


def load_quests() -> list[dict]:
    global _quest_definitions
    if _quest_definitions is None:
        path = Path(__file__).resolve().parent.parent / "game_data" / "weekly_quests.json"
        with open(path, encoding="utf-8") as f:
            _quest_definitions = json.load(f)["quests"]
    return _quest_definitions


def current_week_key() -> str:
    """Return ISO week string like '2026-W09'."""
    now = dt.datetime.now(dt.timezone.utc)
    iso = now.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


async def get_or_create_weekly_tasks(
    session: AsyncSession,
    user_id: int,
) -> list[WeeklyTask]:
    """Get or initialize all weekly tasks for current week."""
    week = current_week_key()
    result = await session.execute(
        select(WeeklyTask).where(
            WeeklyTask.user_id == user_id,
            WeeklyTask.week_key == week,
        )
    )
    existing = list(result.scalars().all())

    quests = load_quests()
    existing_ids = {t.quest_id for t in existing}

    for q in quests:
        if q["quest_id"] not in existing_ids:
            task = WeeklyTask(
                user_id=user_id,
                quest_id=q["quest_id"],
                week_key=week,
                progress=0,
                target=q["target_value"],
                completed=0,
                rewarded=0,
            )
            session.add(task)
            existing.append(task)

    await session.flush()
    return existing


async def update_quest_progress(
    session: AsyncSession,
    user_id: int,
    target_type: str,
    current_value: int | None = None,
    increment: int = 0,
) -> list[str]:
    """Update progress for quests matching target_type. Returns completion messages."""
    quests = load_quests()
    matching = [q for q in quests if q["target_type"] == target_type]
    if not matching:
        return []

    tasks = await get_or_create_weekly_tasks(session, user_id)
    task_map = {t.quest_id: t for t in tasks}
    messages = []

    for q in matching:
        task = task_map.get(q["quest_id"])
        if not task or task.completed:
            continue

        if current_value is not None:
            task.progress = min(current_value, task.target)
        elif increment > 0:
            task.progress = min(task.progress + increment, task.target)

        if task.progress >= task.target and not task.completed:
            task.completed = 1
            messages.append(f"🎯 周任务「{q['name']}」完成! 使用 /cp_quest 领取奖励")

    await session.flush()
    return messages


async def claim_quest_reward(
    session: AsyncSession,
    user_id: int,
    quest_id: str,
) -> tuple[bool, str]:
    """Claim reward for a completed quest."""
    week = current_week_key()
    result = await session.execute(
        select(WeeklyTask).where(
            WeeklyTask.user_id == user_id,
            WeeklyTask.quest_id == quest_id,
            WeeklyTask.week_key == week,
        )
    )
    task = result.scalar_one_or_none()
    if not task:
        return False, "任务不存在"
    if not task.completed:
        return False, "任务尚未完成"
    if task.rewarded:
        return False, "奖励已领取"

    quests = load_quests()
    q = next((q for q in quests if q["quest_id"] == quest_id), None)
    if not q:
        return False, "任务定义不存在"

    if q["reward_points"]:
        await add_self_points(user_id, q["reward_points"], session=session)
    if q["reward_currency"]:
        await add_traffic(session, user_id, q["reward_currency"])

    task.rewarded = 1
    await session.flush()

    from utils.formatters import fmt_traffic
    reward_parts = []
    if q["reward_points"]:
        reward_parts.append(f"积分+{q['reward_points']}")
    if q["reward_currency"]:
        reward_parts.append(f"+{fmt_traffic(q['reward_currency'])}")

    return True, f"🎉 领取「{q['name']}」奖励: {' | '.join(reward_parts)}"
