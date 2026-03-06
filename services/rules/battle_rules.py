"""Battle validation rules."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from cache.redis_client import get_redis
from services.user_service import get_self_points
from utils.rules import Rule, RuleViolation


# ============================================================================
# Battle Rules
# ============================================================================

async def check_strategy_valid(
    strategy,
    attacker_strategy_raw: str | None,
    valid_strategy_hint: str,
    **_,
) -> RuleViolation | None:
    """检查战术是否有效。"""
    if strategy is None:
        return RuleViolation(
            code="INVALID_STRATEGY",
            actual=attacker_strategy_raw,
            expected="valid_strategy",
            message=f"❌ 无效战术: {attacker_strategy_raw}\n可选: {valid_strategy_hint}",
        )
    return None


async def check_battle_cooldown(
    attacker_tg_id: int,
    **_,
) -> RuleViolation | None:
    """检查商战冷却。"""
    r = await get_redis()
    ttl = await r.ttl(f"battle_cd:{attacker_tg_id}")
    cd = max(0, ttl)
    if cd > 0:
        mins = cd // 60
        secs = cd % 60
        return RuleViolation(
            code="COOLDOWN",
            actual=cd,
            expected=0,
            message=f"⏳ 商战冷却中，还需 {mins}分{secs}秒",
        )
    return None


async def check_not_self_battle(
    attacker_tg_id: int,
    defender_tg_id: int,
    **_,
) -> RuleViolation | None:
    """检查是否对自己发起商战。"""
    if attacker_tg_id == defender_tg_id:
        return RuleViolation(
            code="SELF_BATTLE",
            actual=attacker_tg_id,
            expected="different_user",
            message="❌ 不能对自己发起商战",
        )
    return None


async def check_attacker_registered(
    attacker_user,
    **_,
) -> RuleViolation | None:
    """检查攻击者是否已注册。"""
    if not attacker_user:
        return RuleViolation(
            code="ATTACKER_NOT_REGISTERED",
            actual=None,
            expected="registered",
            message="❌ 你还未注册，请先 /cp_start",
        )
    return None


async def check_defender_registered(
    defender_user,
    **_,
) -> RuleViolation | None:
    """检查防御者是否已注册。"""
    if not defender_user:
        return RuleViolation(
            code="DEFENDER_NOT_REGISTERED",
            actual=None,
            expected="registered",
            message="❌ 对方还未注册",
        )
    return None


async def check_attacker_has_company(
    attacker_companies: list,
    **_,
) -> RuleViolation | None:
    """检查攻击者是否有公司。"""
    if not attacker_companies:
        return RuleViolation(
            code="ATTACKER_NO_COMPANY",
            actual=0,
            expected="has_company",
            message="❌ 你还没有公司，无法发起商战",
        )
    return None


async def check_defender_has_company(
    defender_companies: list,
    **_,
) -> RuleViolation | None:
    """检查防御者是否有公司。"""
    if not defender_companies:
        return RuleViolation(
            code="DEFENDER_NO_COMPANY",
            actual=0,
            expected="has_company",
            message="❌ 对方没有公司，无法商战",
        )
    return None


async def check_battle_points(
    attacker_tg_id: int,
    battle_point_cost: int,
    **_,
) -> RuleViolation | None:
    """检查积分是否足够。"""
    if battle_point_cost <= 0:
        return None
    current_points = await get_self_points(attacker_tg_id)
    if current_points < battle_point_cost:
        return RuleViolation(
            code="INSUFFICIENT_POINTS",
            actual=current_points,
            expected=battle_point_cost,
            message=f"❌ 积分不足，发起商战需要 {battle_point_cost} 积分",
        )
    return None


# ============================================================================
# Rule Lists
# ============================================================================

def get_battle_guard_rules() -> list[Rule]:
    """获取商战前置条件规则。"""
    return [
        Rule("STRATEGY_VALID", check_strategy_valid),
        Rule("COOLDOWN", check_battle_cooldown),
        Rule("NOT_SELF_BATTLE", check_not_self_battle),
        Rule("ATTACKER_REGISTERED", check_attacker_registered),
        Rule("DEFENDER_REGISTERED", check_defender_registered),
        Rule("ATTACKER_HAS_COMPANY", check_attacker_has_company),
        Rule("DEFENDER_HAS_COMPANY", check_defender_has_company),
        Rule("BATTLE_POINTS", check_battle_points),
    ]
