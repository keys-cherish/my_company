"""User registration and personal points management."""

from __future__ import annotations

import math

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cache.points_redis_client import get_points_redis
from cache.redis_client import get_redis
from config import settings
from db.models import User

_SHARED_POINTS_KEY_PREFIX = "user_balance"
_SHARED_SYNC_MARK_PREFIX = "my_company:points_synced"


def _shared_points_key(tg_id: int) -> str:
    return f"{_SHARED_POINTS_KEY_PREFIX}:{tg_id}"


def _shared_sync_mark_key(tg_id: int) -> str:
    return f"{_SHARED_SYNC_MARK_PREFIX}:{tg_id}"


async def _ensure_shared_points_account(tg_id: int) -> float:
    """Create shared account once globally and return latest value."""
    r = await get_points_redis()
    key = _shared_points_key(tg_id)
    await r.setnx(key, float(settings.shared_initial_points))
    raw = await r.get(key)
    return float(raw or settings.shared_initial_points)


async def _mark_shared_synced(tg_id: int) -> None:
    r = await get_points_redis()
    await r.set(_shared_sync_mark_key(tg_id), "1")


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

    shared_points = await _ensure_shared_points_account(tg_id)
    user = User(tg_id=tg_id, tg_name=tg_name, self_points=max(0, int(math.ceil(shared_points))))
    session.add(user)
    await session.flush()
    await _mark_shared_synced(tg_id)
    return user, True


async def get_user_by_tg_id(session: AsyncSession, tg_id: int) -> User | None:
    result = await session.execute(select(User).where(User.tg_id == tg_id))
    return result.scalar_one_or_none()


async def add_self_points_by_user_id(
    session: AsyncSession,
    user_id: int,
    amount: int,
    reason: str = "未知",
) -> bool:
    """Atomically add/subtract personal points by internal user id.

    When adding funds that would exceed max_user_traffic, the excess is
    automatically invested into the user's first company (if any).

    Args:
        session: Database session
        user_id: User ID (not tg_id)
        amount: Amount to add (negative for deduction)
        reason: Reason for the change (for logging)
    """
    max_points = settings.max_user_traffic
    for _retry in range(3):
        user = await session.get(User, user_id)
        if user is None:
            return False
        await _merge_local_points_into_shared_once(user)

        current_shared = await _apply_shared_delta(user.tg_id, 0)
        if current_shared is None:
            return False

        if amount < 0 and current_shared + amount < 0:
            return False

        delta_to_apply = amount
        overflow = 0
        companies = []
        if amount > 0:
            projected = current_shared + amount
            if projected > max_points:
                from services.company_service import get_companies_by_owner
                companies = await get_companies_by_owner(session, user_id)
                if companies:
                    if current_shared >= max_points:
                        overflow = amount
                        delta_to_apply = 0
                    else:
                        overflow = projected - max_points
                        delta_to_apply = amount - overflow

        shared_new = current_shared
        if delta_to_apply != 0:
            shared_new = await _apply_shared_delta(user.tg_id, delta_to_apply)
            if shared_new is None:
                return False

        old_value = int(user.self_points)
        target = max(0, int(math.ceil(shared_new)))
        if old_value != target:
            old_version = user.version
            result = await session.execute(
                update(User)
                .where(User.id == user_id, User.version == old_version)
                .values(self_points=target, version=User.version + 1)
            )
            if result.rowcount == 0:
                await session.refresh(user)
                continue
            await session.refresh(user)
            actual_amount = int(user.self_points) - old_value
            if actual_amount != 0:
                from services.fundlog_service import log_fund_change
                await log_fund_change(
                    "user",
                    user_id,
                    actual_amount,
                    reason,
                    balance_after=user.self_points,
                )

        if overflow > 0 and companies:
            from services.company_service import add_funds
            await add_funds(
                session,
                companies[0].id,
                overflow,
                f"自动注资({reason})",
            )
        return True
    return False


async def add_reputation(session: AsyncSession, user_id: int, amount: int) -> bool:
    """Add reputation to a user."""
    user = await session.get(User, user_id)
    if user is None:
        return False
    user.reputation += amount
    await session.flush()
    return True


async def _consume_legacy_honor_points(tg_id: int) -> int:
    """Atomically read+delete legacy Redis points:{tg_id} and return value."""
    try:
        r = await get_redis()
        lua = """
local key = KEYS[1]
local val = tonumber(redis.call('GET', key) or '0')
if val > 0 then
  redis.call('DEL', key)
end
return val
"""
        raw = await r.eval(lua, 1, f"points:{tg_id}")
        return int(raw or 0)
    except Exception:
        return 0


async def _migrate_legacy_honor_points(session: AsyncSession, user: User) -> int:
    """Best-effort one-way migration: legacy honor points -> self_points."""
    legacy = await _consume_legacy_honor_points(user.tg_id)
    if legacy <= 0:
        return 0
    ok = await add_self_points_by_user_id(
        session,
        user.id,
        legacy,
        reason="legacy_honor_points_migration",
    )
    return legacy if ok else 0


async def _merge_local_points_into_shared_once(user: User) -> float:
    """One-way merge on first sync: shared += local self_points (no data loss)."""
    r = await get_points_redis()
    lua = """
local points_key = KEYS[1]
local mark_key = KEYS[2]
local local_points = tonumber(ARGV[1]) or 0

local merged
if redis.call('SETNX', mark_key, '1') == 1 then
    if local_points ~= 0 then
        merged = redis.call('INCRBYFLOAT', points_key, local_points)
    else
        if redis.call('EXISTS', points_key) == 0 then
            redis.call('SET', points_key, '0')
        end
        merged = redis.call('GET', points_key)
    end
else
    merged = redis.call('GET', points_key)
    if not merged then
        merged = '0'
        redis.call('SET', points_key, merged)
    end
end
return tostring(merged)
"""
    raw = await r.eval(lua, 2, _shared_points_key(user.tg_id), _shared_sync_mark_key(user.tg_id), user.self_points)
    return float(raw or 0.0)


async def _apply_shared_delta(tg_id: int, amount: int) -> float | None:
    """Atomically apply delta to shared points. Returns latest shared value."""
    r = await get_points_redis()
    lua = """
local points_key = KEYS[1]
local delta = tonumber(ARGV[1]) or 0
local current = tonumber(redis.call('GET', points_key) or '0')

if delta < 0 and current + delta < 0 then
    return nil
end

if delta ~= 0 then
    return tonumber(redis.call('INCRBYFLOAT', points_key, delta))
end

return current
"""
    raw = await r.eval(lua, 1, _shared_points_key(tg_id), amount)
    if raw is None:
        return None
    return float(raw)


async def _mirror_shared_to_local(
    session: AsyncSession,
    user: User,
    *,
    reason: str,
    shared_value: float | None = None,
) -> int:
    """Mirror shared points to local DB user.self_points, rounded up."""
    if shared_value is None:
        shared_value = float(await (await get_points_redis()).get(_shared_points_key(user.tg_id)) or 0.0)
    target = max(0, int(math.ceil(shared_value)))
    if user.self_points == target:
        return user.self_points

    old_value = int(user.self_points)
    old_version = user.version
    result = await session.execute(
        update(User)
        .where(User.id == user.id, User.version == old_version)
        .values(self_points=target, version=User.version + 1)
    )
    if result.rowcount == 0:
        await session.refresh(user)
        return int(user.self_points)

    await session.refresh(user)
    delta = int(user.self_points) - old_value
    if delta != 0:
        from services.fundlog_service import log_fund_change
        await log_fund_change(
            "user",
            user.id,
            delta,
            reason,
            balance_after=user.self_points,
        )
    return int(user.self_points)


async def get_self_points(tg_id: int) -> int:
    """Get personal points by Telegram ID."""
    from db.engine import async_session

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if user is None:
                return 0
            await _migrate_legacy_honor_points(session, user)
            await _merge_local_points_into_shared_once(user)
            return await _mirror_shared_to_local(session, user, reason="shared_points_sync_read")


async def add_self_points_by_tg_id(
    tg_id: int,
    amount: int,
    *,
    reason: str = "unknown",
) -> bool:
    """Add/subtract personal points by Telegram ID (directly bound to tg_id)."""
    from db.engine import async_session

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if user is None:
                return False
            await _migrate_legacy_honor_points(session, user)
            return await add_self_points_by_user_id(session, user.id, amount, reason=reason)


async def add_self_points(
    tg_id_or_user_id: int,
    amount: int,
    *,
    session: AsyncSession | None = None,
    reason: str = "活动奖励",
) -> int:
    """Add/subtract personal points and return latest value.

    Compatibility helper for call sites that currently pass user_id with a db session.
    """
    if session is not None:
        user = await session.get(User, tg_id_or_user_id)
        if user is None:
            return 0
        await _migrate_legacy_honor_points(session, user)
        ok = await add_self_points_by_user_id(session, user.id, amount, reason=reason)
        return int(user.self_points) if ok else 0

    ok = await add_self_points_by_tg_id(tg_id_or_user_id, amount, reason=reason)
    if not ok:
        return 0
    return await get_self_points(tg_id_or_user_id)


async def sync_all_users_to_shared_points() -> tuple[int, int]:
    """Backfill all users once: merge local points into shared and mirror back.

    Returns:
        (total_users, changed_local_rows)
    """
    from db.engine import async_session

    total = 0
    changed = 0
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(select(User))
            users = list(result.scalars().all())
            total = len(users)
            for user in users:
                await _migrate_legacy_honor_points(session, user)
                await _merge_local_points_into_shared_once(user)
                before = int(user.self_points)
                after = await _mirror_shared_to_local(session, user, reason="shared_points_backfill")
                if after != before:
                    changed += 1
    return total, changed


# --------- Backward-compatible aliases ---------

async def add_traffic(session: AsyncSession, user_id: int, amount: int, reason: str = "未知") -> bool:
    return await add_self_points_by_user_id(session, user_id, amount, reason=reason)


async def get_traffic_by_tg_id(tg_id: int) -> int:
    return await get_self_points(tg_id)


async def add_traffic_by_tg_id(tg_id: int, amount: int, *, reason: str = "unknown") -> bool:
    return await add_self_points_by_tg_id(tg_id, amount, reason=reason)
