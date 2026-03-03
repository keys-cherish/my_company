"""User registration, currency management, and points."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cache.redis_client import get_redis
from config import settings
from db.models import User


async def get_or_create_user(session: AsyncSession, tg_id: int, tg_name: str) -> tuple[User, bool]:
    """Return (user, created). If new, grant initial currency."""
    stmt = select(User).where(User.tg_id == tg_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if user:
        if user.tg_name != tg_name:
            user.tg_name = tg_name
            await session.flush()
        return user, False

    user = User(tg_id=tg_id, tg_name=tg_name, traffic=settings.initial_traffic)
    session.add(user)
    await session.flush()
    return user, True


async def get_user_by_tg_id(session: AsyncSession, tg_id: int) -> User | None:
    result = await session.execute(select(User).where(User.tg_id == tg_id))
    return result.scalar_one_or_none()


async def add_traffic(
    session: AsyncSession,
    user_id: int,
    amount: int,
    reason: str = "未知",
) -> bool:
    """Atomically add/subtract user credits (stored in legacy traffic field).

    Args:
        session: Database session
        user_id: User ID (not tg_id)
        amount: Amount to add (negative for deduction)
        reason: Reason for the change (for logging)
    """
    for _retry in range(3):
        user = await session.get(User, user_id)
        if user is None:
            return False
        if amount < 0 and user.traffic + amount < 0:
            return False
        old_version = user.version
        result = await session.execute(
            update(User)
            .where(User.id == user_id, User.version == old_version)
            .values(traffic=User.traffic + amount, version=User.version + 1)
        )
        if result.rowcount > 0:
            await session.refresh(user)
            # 记录资金日志
            from services.fundlog_service import log_fund_change
            await log_fund_change(
                "user",
                user_id,
                amount,
                reason,
                balance_after=user.traffic,
            )
            return True
        await session.refresh(user)
    return False


async def add_reputation(session: AsyncSession, user_id: int, amount: int) -> bool:
    """Add reputation to a user."""
    user = await session.get(User, user_id)
    if user is None:
        return False
    user.reputation += amount
    await session.flush()
    return True


# ---------- Points (积分奖励系统，用于打卡等活动) ----------

async def get_points(tg_id: int) -> int:
    """获取用户积分（活动奖励积分，非个人账户积分）。"""
    r = await get_redis()
    val = await r.get(f"points:{tg_id}")
    return int(val) if val else 0


async def get_quota_mb(tg_id: int) -> int:
    """获取用户储备积分。"""
    r = await get_redis()
    val = await r.get(f"quota_mb:{tg_id}")
    return int(val) if val else 0


async def add_points(tg_id_or_user_id: int, amount: int, *, session: AsyncSession | None = None) -> int:
    """Add points and return new total.

    注意：这是活动积分，不是个人账户积分。个人账户用 add_traffic。
    """
    tg_id = tg_id_or_user_id
    if session is not None:
        user = await session.get(User, tg_id_or_user_id)
        if user is None:
            return 0
        tg_id = user.tg_id
    r = await get_redis()
    return await r.incrby(f"points:{tg_id}", amount)
