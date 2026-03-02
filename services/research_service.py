"""Research / tech tree system."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from sqlalchemy import func as sqlfunc, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.models import Company, ResearchProgress, User
from services.company_service import (
    add_funds,
    get_company_type_info,
    get_effective_employee_count_for_progress,
)
from services.user_service import add_reputation, add_points

_tech_tree: dict | None = None
_products_data: dict | None = None

# Progressive cost/requirement tuning for research.
RESEARCH_COST_GROWTH_PER_COMPLETED = 0.20
RESEARCH_REPUTATION_STEP = 8
RESEARCH_EMPLOYEE_STEP = 1

COMPANY_RESEARCH_DIRECTIONS: dict[str, list[dict[str, list[str] | str]]] = {
    "tech": [
        {"name": "AI智能方向", "tech_ids": ["social_platform", "big_data", "ai_recommend"]},
        {"name": "云算力方向", "tech_ids": ["cloud_computing", "quantum_computing"]},
        {"name": "未来生态方向", "tech_ids": ["blockchain", "metaverse", "agi"]},
    ],
    "finance": [
        {"name": "金融科技方向", "tech_ids": ["blockchain", "ai_recommend"]},
        {"name": "风控数据方向", "tech_ids": ["big_data", "cloud_computing"]},
        {"name": "资产数字化方向", "tech_ids": ["ecommerce", "metaverse", "agi"]},
    ],
    "media": [
        {"name": "内容分发方向", "tech_ids": ["social_platform", "ai_recommend"]},
        {"name": "商业变现方向", "tech_ids": ["ecommerce", "big_data"]},
        {"name": "沉浸互动方向", "tech_ids": ["metaverse", "cloud_computing", "quantum_computing"]},
    ],
    "manufacturing": [
        {"name": "工业上云方向", "tech_ids": ["cloud_computing", "big_data"]},
        {"name": "供应链方向", "tech_ids": ["ecommerce", "blockchain"]},
        {"name": "智能工厂方向", "tech_ids": ["ai_recommend", "quantum_computing", "agi"]},
    ],
    "realestate": [
        {"name": "智慧楼宇方向", "tech_ids": ["cloud_computing", "big_data"]},
        {"name": "资产交易方向", "tech_ids": ["blockchain", "ecommerce"]},
        {"name": "虚拟地产方向", "tech_ids": ["metaverse", "ai_recommend", "agi"]},
    ],
    "biotech": [
        {"name": "生物数据方向", "tech_ids": ["big_data", "cloud_computing"]},
        {"name": "智能研发方向", "tech_ids": ["ai_recommend", "quantum_computing"]},
        {"name": "前沿医学方向", "tech_ids": ["blockchain", "metaverse", "agi"]},
    ],
    "gaming": [
        {"name": "社交玩法方向", "tech_ids": ["social_platform", "metaverse"]},
        {"name": "数据运营方向", "tech_ids": ["big_data", "ai_recommend"]},
        {"name": "云游戏方向", "tech_ids": ["cloud_computing", "blockchain", "quantum_computing"]},
    ],
    "consulting": [
        {"name": "数据咨询方向", "tech_ids": ["big_data", "ai_recommend"]},
        {"name": "数字化方向", "tech_ids": ["cloud_computing", "ecommerce"]},
        {"name": "创新战略方向", "tech_ids": ["blockchain", "metaverse", "agi"]},
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


def get_effective_research_duration_seconds(tech: dict, company_type: str, tech_id: str) -> int:
    base = int(tech.get("duration_seconds", settings.base_research_seconds))
    type_info = get_company_type_info(company_type)
    speed_bonus = float(type_info.get("research_speed_bonus", 0.0)) if type_info else 0.0
    focus_bonus = 0.15 if tech_id in get_company_focus_tech_ids(company_type) else 0.0
    multiplier = max(0.35, 1.0 - speed_bonus - focus_bonus)
    return max(300, int(base * multiplier))


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


async def get_available_techs(session: AsyncSession, company_id: int) -> list[dict]:
    """Return techs whose prerequisites are met and not yet researched."""
    tree = _load_tech_tree()
    await check_and_complete_research(session, company_id)
    completed = set(await get_completed_techs(session, company_id))

    # Also exclude in-progress
    in_progress_rows = await get_in_progress_research(session, company_id)
    in_progress_ids = {r.tech_id for r in in_progress_rows}

    company = await session.get(Company, company_id)
    company_type = company.company_type if company else "tech"
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
                info, company_type, tech_id
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
    tree = _load_tech_tree()
    if tech_id not in tree:
        return False, "无效的科研项目"

    tech = tree[tech_id]

    company = await session.get(Company, company_id)
    if company is None:
        return False, "公司不存在"
    if company.owner_id != owner_user_id:
        return False, "只有公司老板才能进行科研"
    if not is_tech_allowed_for_company(company.company_type, tech_id):
        return False, "该科研不在你公司行业的研发方向内"

    owner = await session.get(User, owner_user_id)
    if owner is None:
        return False, "用户不存在"

    # Check prerequisites
    await check_and_complete_research(session, company_id)
    completed = set(await get_completed_techs(session, company_id))
    for prereq in tech.get("prerequisites", []):
        if prereq not in completed:
            prereq_name = tree.get(prereq, {}).get("name", prereq)
            return False, f"需要先完成前置科研: {prereq_name}"

    # Check not already researching or done
    existing = await session.execute(
        select(ResearchProgress).where(
            ResearchProgress.company_id == company_id,
            ResearchProgress.tech_id == tech_id,
        )
    )
    if existing.scalar_one_or_none():
        return False, "该科研已开始或已完成"

    completed_count = len(completed)
    base_cost = int(tech.get("cost", settings.base_research_cost))
    scaled_cost = int(base_cost * (1 + completed_count * RESEARCH_COST_GROWTH_PER_COMPLETED))
    cost = max(base_cost, scaled_cost)
    if tech_id in get_company_focus_tech_ids(company.company_type):
        cost = int(cost * 0.9)

    required_employees = int(
        tech.get("required_employees", max(1, 1 + completed_count * RESEARCH_EMPLOYEE_STEP))
    )
    effective_employees = get_effective_employee_count_for_progress(company.employee_count)
    if effective_employees < required_employees:
        return False, (
            f"\u5458\u5de5\u4e0d\u8db3\uff0c\u79d1\u7814\u300c{tech['name']}\u300d\u9700\u8981\u81f3\u5c11 {required_employees} \u4eba\uff0c"
            f"\u5f53\u524d\u6709\u6548\u5458\u5de5 {effective_employees} \u4eba\uff08\u603b\u5458\u5de5 {company.employee_count}\uff09"
        )

    required_reputation = int(
        tech.get("required_reputation", completed_count * RESEARCH_REPUTATION_STEP)
    )
    if owner.reputation < required_reputation:
        return False, (
            f"声望不足，科研「{tech['name']}」需要声望 {required_reputation}，"
            f"当前仅 {owner.reputation}"
        )

    # Deduct cost from company funds
    ok = await add_funds(session, company_id, -cost)
    if not ok:
        from utils.formatters import fmt_traffic
        return False, f"公司资金不足，需要 {fmt_traffic(cost)}"

    rp = ResearchProgress(
        company_id=company_id,
        tech_id=tech_id,
        status="researching",
    )
    session.add(rp)
    await session.flush()

    # Grant points for starting research
    await add_points(owner_user_id, 5, session=session)

    from utils.formatters import fmt_duration
    duration_sec = get_effective_research_duration_seconds(tech, company.company_type, tech_id)
    duration_str = fmt_duration(duration_sec)
    return True, (
        f"开始研究「{tech['name']}」，预计{duration_str}完成 "
        f"(本次投入: {cost:,} 积分)"
    )


async def sync_research_progress_if_due(session: AsyncSession, company_id: int) -> list[str]:
    """Alias for check_and_complete_research – used by company view to sync state."""
    return await check_and_complete_research(session, company_id)


async def check_and_complete_research(session: AsyncSession, company_id: int) -> list[str]:
    """Check all in-progress research; complete those past duration. Returns list of completed tech names."""
    tree = _load_tech_tree()
    in_progress = await get_in_progress_research(session, company_id)
    company = await session.get(Company, company_id)
    company_type = company.company_type if company else "tech"
    now = (await session.execute(select(sqlfunc.now()))).scalar()
    if now is None:
        now = dt.datetime.utcnow()
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
                await add_points(company.owner_id, rep, session=session)

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
