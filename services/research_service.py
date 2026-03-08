"""Research / tech tree system."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from sqlalchemy import func as sqlfunc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from cache.redis_client import get_redis
from config import settings
from db.models import Company, ResearchProgress, User
from services.company_service import (
    add_funds,
    get_company_type_info,
    get_effective_employee_count_for_progress,
)
from services.user_service import add_reputation, add_self_points
from utils.formatters import fmt_points

_tech_tree: dict | None = None
_products_data: dict | None = None

# Progressive cost/requirement tuning for research.
RESEARCH_COST_GROWTH_PER_COMPLETED = 0.20
RESEARCH_REPUTATION_STEP = 8
RESEARCH_EMPLOYEE_STEP = 1
RESEARCH_SYNC_MIN_INTERVAL_SECONDS = 15

COMPANY_RESEARCH_DIRECTIONS: dict[str, list[dict[str, list[str] | str]]] = {
    "tech": [
        {"name": "AI智能方向", "tech_ids": ["social_platform", "big_data", "ai_recommend", "smart_marketing", "advanced_analytics", "ai_automation"]},
        {"name": "云算力方向", "tech_ids": ["cloud_computing", "quantum_computing", "cloud_security"]},
        {"name": "未来生态方向", "tech_ids": ["blockchain", "metaverse", "agi", "distributed_ledger"]},
        {"name": "企业提升", "tech_ids": ["efficiency_mgmt", "talent_network"]},
    ],
    "finance": [
        {"name": "金融科技方向", "tech_ids": ["blockchain", "ai_recommend", "distributed_ledger", "ai_automation"]},
        {"name": "风控数据方向", "tech_ids": ["big_data", "cloud_computing", "advanced_analytics", "cloud_security"]},
        {"name": "资产数字化方向", "tech_ids": ["ecommerce", "metaverse", "agi", "supply_chain_opt"]},
        {"name": "企业提升", "tech_ids": ["efficiency_mgmt", "talent_network"]},
    ],
    "media": [
        {"name": "内容分发方向", "tech_ids": ["social_platform", "ai_recommend", "smart_marketing", "ai_automation"]},
        {"name": "商业变现方向", "tech_ids": ["ecommerce", "big_data", "supply_chain_opt", "advanced_analytics"]},
        {"name": "沉浸互动方向", "tech_ids": ["metaverse", "cloud_computing", "quantum_computing", "cloud_security"]},
        {"name": "企业提升", "tech_ids": ["efficiency_mgmt", "talent_network"]},
    ],
    "manufacturing": [
        {"name": "工业上云方向", "tech_ids": ["cloud_computing", "big_data", "cloud_security", "advanced_analytics"]},
        {"name": "供应链方向", "tech_ids": ["ecommerce", "blockchain", "supply_chain_opt", "distributed_ledger"]},
        {"name": "智能工厂方向", "tech_ids": ["ai_recommend", "quantum_computing", "agi", "ai_automation"]},
        {"name": "企业提升", "tech_ids": ["efficiency_mgmt", "talent_network"]},
    ],
    "realestate": [
        {"name": "智慧楼宇方向", "tech_ids": ["cloud_computing", "big_data", "cloud_security", "advanced_analytics"]},
        {"name": "资产交易方向", "tech_ids": ["blockchain", "ecommerce", "distributed_ledger", "supply_chain_opt"]},
        {"name": "虚拟地产方向", "tech_ids": ["metaverse", "ai_recommend", "agi", "ai_automation"]},
        {"name": "企业提升", "tech_ids": ["efficiency_mgmt", "talent_network"]},
    ],
    "biotech": [
        {"name": "生物数据方向", "tech_ids": ["big_data", "cloud_computing", "advanced_analytics", "cloud_security"]},
        {"name": "智能研发方向", "tech_ids": ["ai_recommend", "quantum_computing", "ai_automation"]},
        {"name": "前沿医学方向", "tech_ids": ["blockchain", "metaverse", "agi", "distributed_ledger"]},
        {"name": "企业提升", "tech_ids": ["efficiency_mgmt", "talent_network"]},
    ],
    "gaming": [
        {"name": "社交玩法方向", "tech_ids": ["social_platform", "metaverse", "smart_marketing"]},
        {"name": "数据运营方向", "tech_ids": ["big_data", "ai_recommend", "advanced_analytics", "ai_automation"]},
        {"name": "云游戏方向", "tech_ids": ["cloud_computing", "blockchain", "quantum_computing", "cloud_security", "distributed_ledger"]},
        {"name": "企业提升", "tech_ids": ["efficiency_mgmt", "talent_network"]},
    ],
    "consulting": [
        {"name": "数据咨询方向", "tech_ids": ["big_data", "ai_recommend", "advanced_analytics", "ai_automation"]},
        {"name": "数字化方向", "tech_ids": ["cloud_computing", "ecommerce", "cloud_security", "supply_chain_opt"]},
        {"name": "创新战略方向", "tech_ids": ["blockchain", "metaverse", "agi", "distributed_ledger"]},
        {"name": "企业提升", "tech_ids": ["efficiency_mgmt", "talent_network"]},
    ],
}


def _load_tech_tree() -> dict:
    global _tech_tree
    if _tech_tree is None:
        path = Path(__file__).resolve().parent.parent / "game_data" / "tech_tree.json"
        with open(path, encoding="utf-8") as f:
            _tech_tree = json.load(f)["nodes"]
    return _tech_tree


def _load_products_data() -> dict:
    global _products_data
    if _products_data is None:
        path = Path(__file__).resolve().parent.parent / "game_data" / "products.json"
        with open(path, encoding="utf-8") as f:
            _products_data = json.load(f)
    return _products_data


def get_company_research_directions(company_type: str) -> list[dict[str, list[str] | str]]:
    return COMPANY_RESEARCH_DIRECTIONS.get(company_type, COMPANY_RESEARCH_DIRECTIONS["tech"])


def get_company_focus_tech_ids(company_type: str) -> set[str]:
    focus: set[str] = {"basic_internet"}
    for direction in get_company_research_directions(company_type):
        focus.update(direction["tech_ids"])  # type: ignore[arg-type]
    return focus


def is_tech_allowed_for_company(company_type: str, tech_id: str) -> bool:
    return tech_id in get_company_focus_tech_ids(company_type)


def get_company_direction_product_lines(company_type: str) -> list[dict[str, list[str] | str]]:
    tree = _load_tech_tree()
    products = _load_products_data()
    direction_lines: list[dict[str, list[str] | str]] = []
    for direction in get_company_research_directions(company_type):
        tech_ids = list(direction["tech_ids"])  # type: ignore[arg-type]
        product_names: list[str] = []
        seen: set[str] = set()
        for tech_id in tech_ids:
            tech = tree.get(tech_id, {})
            unlock_keys = tech.get("unlocks_products", [])
            for key in unlock_keys:
                name = products.get(key, {}).get("name", key)
                if name in seen:
                    continue
                seen.add(name)
                product_names.append(name)
        direction_lines.append(
            {
                "name": direction["name"],  # type: ignore[index]
                "tech_ids": tech_ids,
                "product_lines": product_names,
            }
        )
    return direction_lines


def get_effective_research_duration_seconds(
    tech: dict, company_type: str, tech_id: str, *, research_buffs: dict[str, float] | None = None,
) -> int:
    base = int(tech.get("duration_seconds", settings.base_research_seconds))
    type_info = get_company_type_info(company_type)
    speed_bonus = float(type_info.get("research_speed_bonus", 0.0)) if type_info else 0.0
    focus_bonus = 0.15 if tech_id in get_company_focus_tech_ids(company_type) else 0.0
    tech_buff_bonus = float((research_buffs or {}).get("research_speed", 0.0))
    multiplier = max(0.25, 1.0 - speed_bonus - focus_bonus - tech_buff_bonus)
    return max(300, int(base * multiplier))


def get_effective_research_cost(
    tech: dict,
    completed_count: int,
    company_type: str,
    tech_id: str,
    *,
    research_buffs: dict[str, float] | None = None,
) -> int:
    """Calculate dynamic research cost for both UI display and actual deduction."""
    base_cost = int(tech.get("cost", settings.base_research_cost))
    scaled_cost = int(base_cost * (1 + completed_count * RESEARCH_COST_GROWTH_PER_COMPLETED))
    cost = max(base_cost, scaled_cost)
    if tech_id in get_company_focus_tech_ids(company_type):
        cost = int(cost * 0.9)
    cost_reduction = float((research_buffs or {}).get("research_cost_reduction", 0.0))
    if cost_reduction > 0:
        cost = max(base_cost, int(cost * (1.0 - cost_reduction)))
    return cost


async def get_completed_techs(session: AsyncSession, company_id: int) -> list[str]:
    result = await session.execute(
        select(ResearchProgress).where(
            ResearchProgress.company_id == company_id,
            ResearchProgress.status == "completed",
        )
    )
    return [r.tech_id for r in result.scalars().all()]


async def get_in_progress_research(session: AsyncSession, company_id: int) -> list[ResearchProgress]:
    result = await session.execute(
        select(ResearchProgress).where(
            ResearchProgress.company_id == company_id,
            ResearchProgress.status == "researching",
        )
    )
    return list(result.scalars().all())


async def sync_research_progress_if_due(
    session: AsyncSession,
    company_id: int,
    *,
    min_interval_seconds: int = RESEARCH_SYNC_MIN_INTERVAL_SECONDS,
) -> list[str]:
    """Throttle research completion writes to avoid write-on-every-read paths."""
    ttl = max(0, int(min_interval_seconds))
    if ttl > 0:
        try:
            r = await get_redis()
            lock_key = f"research:sync_gate:{company_id}"
            allowed = await r.set(lock_key, "1", nx=True, ex=ttl)
            if not allowed:
                return []
        except Exception:
            # Redis issues should not block research sync correctness.
            pass
    return await check_and_complete_research(session, company_id)


async def get_available_techs(session: AsyncSession, company_id: int) -> list[dict]:
    """Return techs whose prerequisites are met and not yet researched."""
    tree = _load_tech_tree()
    await sync_research_progress_if_due(session, company_id)
    completed = set(await get_completed_techs(session, company_id))
    completed_count = len(completed)

    # Also exclude in-progress
    in_progress_rows = await get_in_progress_research(session, company_id)
    in_progress_ids = {r.tech_id for r in in_progress_rows}

    company = await session.get(Company, company_id)
    company_type = company.company_type if company else "tech"
    buffs = await get_research_buffs(session, company_id)
    available = []
    for tech_id, info in tree.items():
        if not is_tech_allowed_for_company(company_type, tech_id):
            continue
        if tech_id in completed or tech_id in in_progress_ids:
            continue
        prereqs = info.get("prerequisites", [])
        if all(p in completed for p in prereqs):
            tech = {"tech_id": tech_id, **info}
            tech["effective_duration_seconds"] = get_effective_research_duration_seconds(
                info, company_type, tech_id, research_buffs=buffs,
            )
            tech["research_cost"] = get_effective_research_cost(
                info,
                completed_count,
                company_type,
                tech_id,
                research_buffs=buffs,
            )
            available.append(tech)
    return available


async def start_research(
    session: AsyncSession,
    company_id: int,
    owner_user_id: int,
    tech_id: str,
) -> tuple[bool, str]:
    """Start researching a technology. Deducts company funds."""
    from services.rules.research_rules import (
        get_research_guard_rules,
        get_research_requirement_rules,
    )
    from utils.rules import check_rules_sequential, check_rules_parallel

    tree = _load_tech_tree()

    # 完成到期科研
    await check_and_complete_research(session, company_id)
    completed = set(await get_completed_techs(session, company_id))
    completed_count = len(completed)

    # 获取公司信息用于计算成本
    company = await session.get(Company, company_id)
    tech = tree.get(tech_id, {})

    # 计算科研成本（含研发buff）
    company_type = company.company_type if company else "tech"
    buffs = await get_research_buffs(session, company_id)
    research_cost = get_effective_research_cost(
        tech,
        completed_count,
        company_type,
        tech_id,
        research_buffs=buffs,
    )

    # 构建上下文
    ctx = {
        "session": session,
        "company_id": company_id,
        "owner_user_id": owner_user_id,
        "tech_id": tech_id,
        "tech_tree": tree,
        "completed_techs": completed,
        "completed_count": completed_count,
        "is_tech_allowed_func": is_tech_allowed_for_company,
        "employee_step": RESEARCH_EMPLOYEE_STEP,
        "reputation_step": RESEARCH_REPUTATION_STEP,
        "research_cost": research_cost,
    }

    # 顺序检查前置条件
    guard_fail = await check_rules_sequential(get_research_guard_rules(), **ctx)
    if guard_fail:
        return False, guard_fail.message

    # 并行检查需求条件
    req_fails = await check_rules_parallel(get_research_requirement_rules(), **ctx)
    if req_fails:
        return False, req_fails[0].message

    # Deduct cost from company funds
    ok = await add_funds(session, company_id, -research_cost)
    if not ok:
        return False, f"公司积分不足，需要 {fmt_points(research_cost)}"

    rp = ResearchProgress(
        company_id=company_id,
        tech_id=tech_id,
        status="researching",
    )
    session.add(rp)
    await session.flush()

    # Grant points for starting research
    await add_self_points(owner_user_id, 5, session=session)

    from utils.formatters import fmt_duration
    # 重新获取公司信息（可能已被刷新）
    company = await session.get(Company, company_id)
    duration_sec = get_effective_research_duration_seconds(tech, company.company_type, tech_id, research_buffs=buffs)
    duration_str = fmt_duration(duration_sec)
    return True, (
        f"开始研究「{tech['name']}」，预计{duration_str}完成 "
        f"(本次投入: {research_cost:,} 积分)"
    )


async def check_and_complete_research(
    session: AsyncSession,
    company_id: int,
    *,
    now: dt.datetime | None = None,
) -> list[str]:
    """Check all in-progress research; complete those past duration. Returns list of completed tech names."""
    tree = _load_tech_tree()
    in_progress = await get_in_progress_research(session, company_id)
    company = await session.get(Company, company_id)
    company_type = company.company_type if company else "tech"
    if now is None:
        # LOCALTIMESTAMP returns naive timestamp in session timezone,
        # matching how started_at is stored (server_default=now() → TIMESTAMP).
        now = (await session.execute(select(text("LOCALTIMESTAMP")))).scalar()
    if now is None:
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    if getattr(now, "tzinfo", None):
        now = now.replace(tzinfo=None)
    completed_names = []

    for rp in in_progress:
        tech = tree.get(rp.tech_id)
        if tech is None:
            continue
        duration = dt.timedelta(
            seconds=get_effective_research_duration_seconds(tech, company_type, rp.tech_id)
        )
        # Normalize to naive datetimes (DB column is TIMESTAMP WITHOUT TIME ZONE)
        started = rp.started_at.replace(tzinfo=None) if rp.started_at.tzinfo else rp.started_at
        elapsed = now - started
        if elapsed.total_seconds() < 0:
            continue
        if elapsed >= duration:
            rp.status = "completed"
            rp.completed_at = now
            completed_names.append(tech["name"])

            # Grant reputation to owner
            if company:
                rep = tech.get("reputation_reward", settings.reputation_per_research)
                await add_reputation(session, company.owner_id, rep)
                await add_self_points(company.owner_id, rep, session=session)

                # Quest progress
                from services.quest_service import update_quest_progress
                total_completed = len(completed_names) + len(
                    await get_completed_techs(session, company_id)
                )
                await update_quest_progress(
                    session, company.owner_id, "tech_count", current_value=total_completed
                )

    await session.flush()
    return completed_names


def get_tech_tree_display() -> list[dict]:
    """Return the full tech tree for display purposes."""
    tree = _load_tech_tree()
    return [{"tech_id": k, **v} for k, v in tree.items()]


async def get_research_buffs(session: AsyncSession, company_id: int) -> dict[str, float]:
    """Get accumulated buff effects from completed research.

    Returns dict mapping buff_type -> total_value, e.g.:
        {"income_bonus": 0.08, "cost_reduction": 0.05, "employee_limit": 15}
    """
    tree = _load_tech_tree()
    completed = await get_completed_techs(session, company_id)
    buffs: dict[str, float] = {}
    for tech_id in completed:
        tech = tree.get(tech_id, {})
        buff = tech.get("buff")
        if buff:
            buffs[buff["type"]] = buffs.get(buff["type"], 0) + float(buff["value"])
    return buffs
