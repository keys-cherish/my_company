"""Cooperation between companies – daily expiry, stackable up to cap."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Company, Cooperation
from services.user_service import add_reputation, add_self_points
from utils.timezone import BJ_TZ

DEFAULT_BONUS_MULTIPLIER = 0.02  # +2% per cooperation
COOP_REPUTATION_GAIN = 30
COOP_BUFF_CAP = 0.50  # Normal company cap: 50%


def _utc_now_naive() -> dt.datetime:
    """Return current UTC time as naive datetime for TIMESTAMP WITHOUT TIME ZONE."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _next_settlement_time() -> dt.datetime:
    """Return the next 00:00 Beijing as naive UTC for DB storage."""
    now_bj = dt.datetime.now(BJ_TZ)
    next_bj = (now_bj + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return next_bj.astimezone(dt.UTC).replace(tzinfo=None)


async def get_active_cooperations(session: AsyncSession, company_id: int) -> list[Cooperation]:
    now = _utc_now_naive()
    result = await session.execute(
        select(Cooperation).where(
            or_(
                Cooperation.company_a_id == company_id,
                Cooperation.company_b_id == company_id,
            ),
            Cooperation.expires_at > now,
        )
    )
    return list(result.scalars().all())


async def get_cooperation_bonus(session: AsyncSession, company_id: int) -> float:
    """Return total cooperation bonus multiplier for a company (capped)."""
    coops = await get_active_cooperations(session, company_id)
    if not coops:
        return 0.0
    raw = sum(c.bonus_multiplier for c in coops)
    return min(raw, COOP_BUFF_CAP)


async def create_cooperation(
    session: AsyncSession,
    company_a_id: int,
    company_b_id: int,
) -> tuple[bool, str]:
    """Establish cooperation between two companies (expires at next settlement)."""
    if company_a_id == company_b_id:
        return False, "不能与自己合作"

    ca = await session.get(Company, company_a_id)
    cb = await session.get(Company, company_b_id)
    if not ca or not cb:
        return False, "公司不存在"

    # Ethics gate: <40 blocks cooperation
    from services.operations_service import get_or_create_profile
    profile_a = await get_or_create_profile(session, company_a_id)
    if profile_a.ethics < 40:
        return False, f"道德值过低（{profile_a.ethics}/100），无法发起合作（需≥40）"

    # Check if already cooperating with this specific company today
    a_active = await get_active_cooperations(session, company_a_id)
    for c in a_active:
        partner = c.company_b_id if c.company_a_id == company_a_id else c.company_a_id
        if partner == company_b_id:
            return False, f"今天已经与「{cb.name}」合作过了"

    # Check buff cap
    current_bonus = sum(c.bonus_multiplier for c in a_active)
    if current_bonus >= COOP_BUFF_CAP:
        return False, f"合作Buff已达上限（{int(COOP_BUFF_CAP * 100)}%），今日无法继续合作"

    # Ethics ≥80: double cooperation buff
    bonus_mult = DEFAULT_BONUS_MULTIPLIER
    ethics_note = ""
    if profile_a.ethics >= 80:
        bonus_mult = DEFAULT_BONUS_MULTIPLIER * 2
        ethics_note = "（道德≥80，双倍加成）"

    expires_at = _next_settlement_time()
    coop = Cooperation(
        company_a_id=company_a_id,
        company_b_id=company_b_id,
        bonus_multiplier=bonus_mult,
        expires_at=expires_at,
    )
    session.add(coop)
    await session.flush()

    # Grant reputation and points to both owners
    await add_reputation(session, ca.owner_id, COOP_REPUTATION_GAIN)
    await add_reputation(session, cb.owner_id, COOP_REPUTATION_GAIN)
    await add_self_points(ca.owner_id, 8, session=session)
    await add_self_points(cb.owner_id, 8, session=session)

    # Quest progress
    from services.quest_service import update_quest_progress
    await update_quest_progress(session, ca.owner_id, "cooperation_count", increment=1)
    await update_quest_progress(session, cb.owner_id, "cooperation_count", increment=1)

    new_total = min(current_bonus + bonus_mult, COOP_BUFF_CAP)
    return True, (
        f"「{ca.name}」与「{cb.name}」建立合作! 营收+{bonus_mult*100:.0f}%{ethics_note}（今日有效）\n"
        f"当前合作Buff：+{new_total*100:.0f}%（上限{int(COOP_BUFF_CAP*100)}%）\n"
        f"双方各 +{COOP_REPUTATION_GAIN} 声望"
    )


async def cooperate_all(
    session: AsyncSession,
    my_company_id: int,
) -> tuple[int, int, list[str]]:
    """Cooperate with all other companies. Returns (success_count, skip_count, messages)."""
    result = await session.execute(select(Company).where(Company.id != my_company_id))
    all_companies = list(result.scalars().all())

    my_company = await session.get(Company, my_company_id)
    if not my_company:
        return 0, 0, ["公司不存在"]

    # Ethics gate: <40 blocks cooperation
    from services.operations_service import get_or_create_profile
    profile = await get_or_create_profile(session, my_company_id)
    if profile.ethics < 40:
        return 0, len(all_companies), [f"道德值过低（{profile.ethics}/100），无法发起合作（需≥40）"]

    existing = await get_active_cooperations(session, my_company_id)
    current_bonus = sum(c.bonus_multiplier for c in existing)

    # Build set of already-cooperated company IDs
    already_partners = set()
    for c in existing:
        partner = c.company_b_id if c.company_a_id == my_company_id else c.company_a_id
        already_partners.add(partner)

    # Already at cap
    if current_bonus >= COOP_BUFF_CAP:
        return 0, len(already_partners), [
            f"合作Buff已达上限（{int(COOP_BUFF_CAP * 100)}%），今日无法继续合作",
            f"当前已有 {len(already_partners)} 家合作伙伴",
        ]

    # Ethics ≥80: double cooperation buff
    bonus_mult = DEFAULT_BONUS_MULTIPLIER
    if profile.ethics >= 80:
        bonus_mult = DEFAULT_BONUS_MULTIPLIER * 2

    success = 0
    skip = 0
    cap_rest = 0
    msgs = []

    for target in all_companies:
        # Check cap
        if current_bonus >= COOP_BUFF_CAP:
            cap_rest += 1
            continue

        # Skip already cooperated
        if target.id in already_partners:
            skip += 1
            continue

        expires_at = _next_settlement_time()
        coop = Cooperation(
            company_a_id=my_company_id,
            company_b_id=target.id,
            bonus_multiplier=bonus_mult,
            expires_at=expires_at,
        )
        session.add(coop)
        current_bonus += bonus_mult
        success += 1

        # Reputation & points
        await add_reputation(session, my_company.owner_id, COOP_REPUTATION_GAIN)
        await add_reputation(session, target.owner_id, COOP_REPUTATION_GAIN)
        await add_self_points(my_company.owner_id, 8, session=session)
        await add_self_points(target.owner_id, 8, session=session)

        # Quest progress
        from services.quest_service import update_quest_progress
        await update_quest_progress(session, my_company.owner_id, "cooperation_count", increment=1)
        await update_quest_progress(session, target.owner_id, "cooperation_count", increment=1)

    if success > 0:
        capped = min(current_bonus, COOP_BUFF_CAP)
        ethics_note = "（道德≥80，双倍加成）" if profile.ethics >= 80 else ""
        msgs.append(f"合作Buff：+{capped*100:.0f}%{ethics_note}（上限{int(COOP_BUFF_CAP*100)}%），双方各+{COOP_REPUTATION_GAIN}声望")
    if cap_rest > 0:
        msgs.append(f"Buff已满，{cap_rest}家因上限跳过")

    await session.flush()
    return success, skip, msgs


async def cooperate_with(
    session: AsyncSession,
    my_company_id: int,
    target_company_id: int,
) -> tuple[bool, str]:
    """Cooperate with a specific company by ID."""
    return await create_cooperation(session, my_company_id, target_company_id)
