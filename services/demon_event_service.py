"""Demon Invasion Event service.

A random event system that targets company owners with demon challenges.
Players can accept (enter roulette combat) or decline (suffer lesser penalties).
Difficulty scales with company cp_points; richer companies attract stronger demons.

Redis keys:
- demon_event:{company_id}   -> JSON pending event state (TTL 90s)
- demon_event_cd:{company_id} -> cooldown marker (TTL 172800s = 48h)
- demon_debuff:{company_id}  -> revenue debuff rate (TTL until next settlement)
- demon_event_buff:{company_id} -> revenue buff rate (TTL until next settlement)
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import random

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cache.redis_client import get_redis
from db.engine import async_session
from db.models import Company, User
from services.company_service import add_funds, get_companies_by_owner
from services.user_service import add_reputation
from utils.formatters import fmt_points

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tier configuration
# ---------------------------------------------------------------------------
# Ordered from lowest to highest so get_event_tier can iterate in reverse.
DEMON_EVENT_TIERS: list[dict] = [
    {
        "key": "normal",
        "name": "小鬼骚扰",
        "emoji": "⚪",
        "devils": 1,
        "devil_hp": 2,
        "player_hp": 4,
        "items_per_round": 2,
        "cp_threshold": 50_000,
        # decline penalties
        "decline_funds_pct": 0.01,
        "decline_employee_min": 1,
        "decline_employee_max": 2,
        "decline_reputation": 5,
        "decline_revenue_debuff": 0.0,
        # win rewards
        "win_funds_pct": 0.02,
        "win_reputation": 10,
        "win_revenue_buff": 0.05,
        # lose penalty multiplier applied on top of decline values
        "lose_multiplier": 1.5,
    },
    {
        "key": "medium",
        "name": "恶魔试炼",
        "emoji": "🟢",
        "devils": 2,
        "devil_hp": 3,
        "player_hp": 4,
        "items_per_round": 3,
        "cp_threshold": 300_000,
        "decline_funds_pct": 0.015,
        "decline_employee_min": 2,
        "decline_employee_max": 4,
        "decline_reputation": 8,
        "decline_revenue_debuff": 0.05,
        "win_funds_pct": 0.03,
        "win_reputation": 20,
        "win_revenue_buff": 0.08,
        "lose_multiplier": 1.5,
    },
    {
        "key": "hard",
        "name": "地狱先锋",
        "emoji": "🟡",
        "devils": 3,
        "devil_hp": 4,
        "player_hp": 4,
        "items_per_round": 3,
        "cp_threshold": 1_000_000,
        "decline_funds_pct": 0.02,
        "decline_employee_min": 3,
        "decline_employee_max": 6,
        "decline_reputation": 12,
        "decline_revenue_debuff": 0.08,
        "win_funds_pct": 0.05,
        "win_reputation": 30,
        "win_revenue_buff": 0.12,
        "lose_multiplier": 1.5,
    },
    {
        "key": "epic",
        "name": "魔王降临",
        "emoji": "🟠",
        "devils": 4,
        "devil_hp": 5,
        "player_hp": 5,
        "items_per_round": 4,
        "cp_threshold": 3_000_000,
        "decline_funds_pct": 0.03,
        "decline_employee_min": 5,
        "decline_employee_max": 10,
        "decline_reputation": 20,
        "decline_revenue_debuff": 0.10,
        "win_funds_pct": 0.08,
        "win_reputation": 50,
        "win_revenue_buff": 0.18,
        "lose_multiplier": 1.5,
    },
    {
        "key": "legendary",
        "name": "末日审判",
        "emoji": "🔴",
        "devils": 6,
        "devil_hp": 6,
        "player_hp": 5,
        "items_per_round": 5,
        "cp_threshold": 6_000_000,
        "decline_funds_pct": 0.04,
        "decline_employee_min": 8,
        "decline_employee_max": 15,
        "decline_reputation": 30,
        "decline_revenue_debuff": 0.15,
        "win_funds_pct": 0.12,
        "win_reputation": 80,
        "win_revenue_buff": 0.25,
        "lose_multiplier": 1.5,
    },
    {
        "key": "bizarre",
        "name": "深渊浩劫",
        "emoji": "💀",
        "devils": 10,
        "devil_hp": 8,
        "player_hp": 6,
        "items_per_round": 5,
        "cp_threshold": 9_000_000,
        "decline_funds_pct": 0.05,
        "decline_employee_min": 15,
        "decline_employee_max": 30,
        "decline_reputation": 50,
        "decline_revenue_debuff": 0.20,
        "win_funds_pct": 0.20,
        "win_reputation": 150,
        "win_revenue_buff": 0.40,
        "lose_multiplier": 1.5,
    },
]

# Redis key templates
_EVENT_STATE_KEY = "demon_event:{company_id}"
_COOLDOWN_KEY = "demon_event_cd:{company_id}"
_DEBUFF_KEY = "demon_debuff:{company_id}"
_BUFF_KEY = "demon_event_buff:{company_id}"

# Cooldown: 48 hours
_COOLDOWN_TTL = 172_800
# Pending event response window
_EVENT_STATE_TTL = 90


# ---------------------------------------------------------------------------
# Settlement time helper (reused from battle_service)
# ---------------------------------------------------------------------------

def _next_settlement_time() -> dt.datetime:
    """Return the next daily settlement time (00:00 Beijing time) as naive UTC."""
    from utils.timezone import BJ_TZ

    now_bj = dt.datetime.now(BJ_TZ)
    next_bj = (now_bj + dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return next_bj.astimezone(dt.UTC).replace(tzinfo=None)


def _ttl_until_settlement() -> int:
    """Seconds from now until the next daily settlement, minimum 60."""
    delta = _next_settlement_time() - dt.datetime.now(dt.UTC).replace(tzinfo=None)
    return int(max(60, delta.total_seconds()))


# ---------------------------------------------------------------------------
# Tier selection
# ---------------------------------------------------------------------------

def get_event_tier(cp_points: int) -> dict | None:
    """Pick a tier for the given cp_points.

    Finds all qualifying tiers (cp_points > threshold), then applies weighted
    randomness that favours higher tiers for richer companies.  Weight formula:
    base weight doubles for each tier above the minimum qualifying tier, so the
    highest qualifying tier is most likely but lower ones can still appear.

    Returns None if cp_points doesn't meet even the lowest threshold.
    """
    qualifying = [t for t in DEMON_EVENT_TIERS if cp_points > t["cp_threshold"]]
    if not qualifying:
        return None

    # Weight: 2^index so higher tiers are exponentially more likely
    weights = [2 ** i for i in range(len(qualifying))]
    return random.choices(qualifying, weights=weights, k=1)[0]


# ---------------------------------------------------------------------------
# Target selection
# ---------------------------------------------------------------------------

def _calc_target_weight(company: Company) -> float:
    """Compute selection weight for a company. Higher = more likely to be picked."""
    return (
        company.cp_points * 0.3
        + company.daily_revenue * 50
        + company.employee_count * 100
        + company.level * 5000
    )


async def _is_on_cooldown(company_id: int) -> bool:
    r = await get_redis()
    return bool(await r.exists(_COOLDOWN_KEY.format(company_id=company_id)))


async def pick_target_company() -> tuple[dict, dict] | None:
    """Select a random company weighted by wealth, skipping those on cooldown.

    Returns (company_dict, tier_dict) or None if no valid target exists.
    The company_dict contains: id, name, owner_id, owner_tg_id, cp_points,
    daily_revenue, employee_count, level.
    """
    async with async_session() as session:
        async with session.begin():
            # Fetch all companies with their owners in one query
            result = await session.execute(
                select(Company, User.tg_id)
                .join(User, Company.owner_id == User.id)
            )
            rows = result.all()

    if not rows:
        return None

    # Filter: must qualify for at least the lowest tier and not be on cooldown
    min_threshold = DEMON_EVENT_TIERS[0]["cp_threshold"]
    candidates: list[tuple[Company, int]] = []
    for company, owner_tg_id in rows:
        if company.cp_points <= min_threshold:
            continue
        if await _is_on_cooldown(company.id):
            continue
        candidates.append((company, owner_tg_id))

    if not candidates:
        return None

    # Weighted random selection
    weights = [max(1.0, _calc_target_weight(c)) for c, _ in candidates]
    (selected_company, selected_tg_id), = random.choices(
        candidates, weights=weights, k=1,
    )

    tier = get_event_tier(selected_company.cp_points)
    if tier is None:
        return None

    company_dict = {
        "id": selected_company.id,
        "name": selected_company.name,
        "owner_id": selected_company.owner_id,
        "owner_tg_id": selected_tg_id,
        "cp_points": selected_company.cp_points,
        "daily_revenue": selected_company.daily_revenue,
        "employee_count": selected_company.employee_count,
        "level": selected_company.level,
    }
    return company_dict, tier


# ---------------------------------------------------------------------------
# Event state persistence (Redis)
# ---------------------------------------------------------------------------

async def save_event_state(company_id: int, owner_tg_id: int, tier: dict) -> None:
    """Save a pending demon event to Redis with a 90-second response window."""
    r = await get_redis()
    payload = {
        "company_id": company_id,
        "owner_tg_id": owner_tg_id,
        "tier_key": tier["key"],
        "created_at": dt.datetime.now(dt.UTC).replace(tzinfo=None).isoformat(),
    }
    await r.set(
        _EVENT_STATE_KEY.format(company_id=company_id),
        json.dumps(payload, ensure_ascii=False),
        ex=_EVENT_STATE_TTL,
    )


async def load_event_state(company_id: int) -> dict | None:
    """Load and consume (delete) a pending event state. Returns None if expired."""
    r = await get_redis()
    key = _EVENT_STATE_KEY.format(company_id=company_id)
    raw = await r.get(key)
    if raw is None:
        return None
    await r.delete(key)

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    # Resolve tier_key back to the full tier dict
    tier_key = data.get("tier_key")
    tier = _tier_by_key(tier_key)
    if tier is None:
        return None

    data["tier"] = tier
    return data


async def peek_event_state(company_id: int) -> dict | None:
    """Read pending event state WITHOUT consuming it. Returns None if expired."""
    r = await get_redis()
    key = _EVENT_STATE_KEY.format(company_id=company_id)
    raw = await r.get(key)
    if raw is None:
        return None

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    tier_key = data.get("tier_key")
    tier = _tier_by_key(tier_key)
    if tier is None:
        return None

    data["tier"] = tier
    return data


def _tier_by_key(key: str) -> dict | None:
    for t in DEMON_EVENT_TIERS:
        if t["key"] == key:
            return t
    return None


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------

async def set_event_cooldown(company_id: int) -> None:
    """Set 48-hour cooldown so the same company isn't targeted again too soon."""
    r = await get_redis()
    await r.set(
        _COOLDOWN_KEY.format(company_id=company_id),
        "1",
        ex=_COOLDOWN_TTL,
    )


# ---------------------------------------------------------------------------
# Revenue buff / debuff helpers
# ---------------------------------------------------------------------------

async def _set_revenue_debuff(company_id: int, rate: float) -> None:
    """Apply a revenue debuff that lasts until the next daily settlement."""
    if rate <= 0:
        return
    r = await get_redis()
    key = _DEBUFF_KEY.format(company_id=company_id)
    # Merge with any existing debuff (keep the higher penalty)
    existing = await r.get(key)
    current = float(existing) if existing else 0.0
    merged = max(current, rate)
    ttl = _ttl_until_settlement()
    await r.set(key, f"{merged:.4f}", ex=ttl)


async def _set_revenue_buff(company_id: int, rate: float) -> None:
    """Apply a revenue buff that lasts until the next daily settlement."""
    if rate <= 0:
        return
    r = await get_redis()
    key = _BUFF_KEY.format(company_id=company_id)
    # Merge with any existing buff (keep the higher bonus)
    existing = await r.get(key)
    current = float(existing) if existing else 0.0
    merged = max(current, rate)
    ttl = _ttl_until_settlement()
    await r.set(key, f"{merged:.4f}", ex=ttl)


async def get_demon_revenue_debuff(company_id: int) -> float:
    """Read current demon debuff rate for settlement calculation."""
    r = await get_redis()
    val = await r.get(_DEBUFF_KEY.format(company_id=company_id))
    if not val:
        return 0.0
    try:
        return max(0.0, min(0.8, float(val)))
    except ValueError:
        return 0.0


async def get_demon_revenue_buff(company_id: int) -> float:
    """Read current demon buff rate for settlement calculation."""
    r = await get_redis()
    val = await r.get(_BUFF_KEY.format(company_id=company_id))
    if not val:
        return 0.0
    try:
        return max(0.0, float(val))
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Outcome application
# ---------------------------------------------------------------------------

async def apply_decline_penalty(
    company_id: int,
    owner_user_id: int,
    tier: dict,
) -> str:
    """Apply penalties when the player declines the demon challenge.

    Returns a human-readable result message.
    """
    async with async_session() as session:
        async with session.begin():
            company = await session.get(Company, company_id)
            if company is None:
                return "公司不存在，无法执行惩罚"

            # -- Funds loss --
            funds_loss = int(company.cp_points * tier["decline_funds_pct"])
            funds_loss = max(0, funds_loss)
            actual_funds_loss = 0
            if funds_loss > 0:
                ok = await add_funds(session, company_id, -funds_loss, "恶魔入侵-拒绝惩罚")
                if ok:
                    actual_funds_loss = funds_loss

            # -- Employee loss --
            emp_loss = random.randint(
                tier["decline_employee_min"],
                tier["decline_employee_max"],
            )
            # Never reduce below 1 employee
            emp_loss = min(emp_loss, max(0, company.employee_count - 1))
            if emp_loss > 0:
                company.employee_count -= emp_loss
                await session.flush()

            # -- Reputation loss --
            rep_loss = tier["decline_reputation"]
            if rep_loss > 0:
                await add_reputation(session, owner_user_id, -rep_loss)

            # -- Revenue debuff --
            debuff_rate = tier["decline_revenue_debuff"]
            if debuff_rate > 0:
                await _set_revenue_debuff(company_id, debuff_rate)

    # Build result message
    lines = [
        f"{tier['emoji']} 恶魔复仇 - {tier['name']}",
        f"{'─' * 24}",
        f"你选择回避了恶魔的挑战，但恶魔不会轻易放过你...",
        "",
    ]
    if actual_funds_loss > 0:
        lines.append(f"  资金损失: -{fmt_points(actual_funds_loss)}")
    if emp_loss > 0:
        lines.append(f"  员工流失: -{emp_loss} 人")
    if rep_loss > 0:
        lines.append(f"  声望下降: -{rep_loss}")
    if debuff_rate > 0:
        lines.append(f"  营收Debuff: -{debuff_rate * 100:.0f}% (至次日结算)")

    return "\n".join(lines)


async def apply_win_reward(
    company_id: int,
    owner_user_id: int,
    tier: dict,
) -> str:
    """Apply rewards when the player defeats the demon.

    Returns a human-readable result message.
    """
    async with async_session() as session:
        async with session.begin():
            company = await session.get(Company, company_id)
            if company is None:
                return "公司不存在，无法发放奖励"

            # -- Funds gain --
            funds_gain = int(company.cp_points * tier["win_funds_pct"])
            funds_gain = max(0, funds_gain)
            actual_funds_gain = 0
            if funds_gain > 0:
                ok = await add_funds(session, company_id, funds_gain, "恶魔入侵-胜利奖励")
                if ok:
                    actual_funds_gain = funds_gain

            # -- Reputation gain --
            rep_gain = tier["win_reputation"]
            if rep_gain > 0:
                await add_reputation(session, owner_user_id, rep_gain)

            # -- Revenue buff --
            buff_rate = tier["win_revenue_buff"]
            if buff_rate > 0:
                await _set_revenue_buff(company_id, buff_rate)

    lines = [
        f"{tier['emoji']} 恶魔退散 - {tier['name']}",
        f"{'─' * 24}",
        f"你成功击败了恶魔，获得丰厚奖励!",
        "",
    ]
    if actual_funds_gain > 0:
        lines.append(f"  资金奖励: +{fmt_points(actual_funds_gain)}")
    if rep_gain > 0:
        lines.append(f"  声望提升: +{rep_gain}")
    if buff_rate > 0:
        lines.append(f"  营收Buff: +{buff_rate * 100:.0f}% (至次日结算)")

    return "\n".join(lines)


async def apply_lose_penalty(
    company_id: int,
    owner_user_id: int,
    tier: dict,
) -> str:
    """Apply penalties when the player accepts but loses to the demon.

    Penalties are 1.5x the decline values (rounded).
    Returns a human-readable result message.
    """
    mult = tier["lose_multiplier"]

    async with async_session() as session:
        async with session.begin():
            company = await session.get(Company, company_id)
            if company is None:
                return "公司不存在，无法执行惩罚"

            # -- Funds loss (1.5x decline) --
            funds_loss = int(
                math.ceil(company.cp_points * tier["decline_funds_pct"] * mult)
            )
            funds_loss = max(0, funds_loss)
            actual_funds_loss = 0
            if funds_loss > 0:
                ok = await add_funds(session, company_id, -funds_loss, "恶魔入侵-战败惩罚")
                if ok:
                    actual_funds_loss = funds_loss

            # -- Employee loss (1.5x decline, rounded) --
            emp_min = int(math.ceil(tier["decline_employee_min"] * mult))
            emp_max = int(math.ceil(tier["decline_employee_max"] * mult))
            emp_loss = random.randint(emp_min, emp_max)
            emp_loss = min(emp_loss, max(0, company.employee_count - 1))
            if emp_loss > 0:
                company.employee_count -= emp_loss
                await session.flush()

            # -- Reputation loss (1.5x decline, rounded) --
            rep_loss = int(math.ceil(tier["decline_reputation"] * mult))
            if rep_loss > 0:
                await add_reputation(session, owner_user_id, -rep_loss)

            # -- Revenue debuff (1.5x decline rate, capped at 80%) --
            debuff_rate = min(0.80, tier["decline_revenue_debuff"] * mult)
            if debuff_rate > 0:
                await _set_revenue_debuff(company_id, debuff_rate)

    lines = [
        f"{tier['emoji']} 恶魔得逞 - {tier['name']}",
        f"{'─' * 24}",
        f"你勇敢地迎战了恶魔，但不幸战败...损失更为惨重。",
        "",
    ]
    if actual_funds_loss > 0:
        lines.append(f"  资金损失: -{fmt_points(actual_funds_loss)}")
    if emp_loss > 0:
        lines.append(f"  员工流失: -{emp_loss} 人")
    if rep_loss > 0:
        lines.append(f"  声望下降: -{rep_loss}")
    if debuff_rate > 0:
        lines.append(f"  营收Debuff: -{debuff_rate * 100:.0f}% (至次日结算)")

    return "\n".join(lines)
