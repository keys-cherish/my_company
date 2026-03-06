"""Research/tech validation rules."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Company, ResearchProgress, User
from services.company_service import get_effective_employee_count_for_progress
from utils.formatters import fmt_traffic
from utils.rules import Rule, RuleViolation


# ============================================================================
# Research Start Rules
# ============================================================================

async def check_tech_valid(
    tech_tree: dict,
    tech_id: str,
    **_,
) -> RuleViolation | None:
    """检查科研项目是否有效。"""
    if tech_id not in tech_tree:
        return RuleViolation(
            code="INVALID_TECH",
            actual=tech_id,
            expected="valid_tech_id",
            message="无效的科研项目",
        )
    return None


async def check_research_company_exists(
    session: AsyncSession,
    company_id: int,
    **_,
) -> RuleViolation | None:
    """检查公司是否存在。"""
    company = await session.get(Company, company_id)
    if not company:
        return RuleViolation(
            code="COMPANY_NOT_FOUND",
            actual=None,
            expected="exists",
            message="公司不存在",
        )
    return None


async def check_research_owner(
    session: AsyncSession,
    company_id: int,
    owner_user_id: int,
    **_,
) -> RuleViolation | None:
    """检查是否是公司老板。"""
    company = await session.get(Company, company_id)
    if not company:
        return None
    if company.owner_id != owner_user_id:
        return RuleViolation(
            code="NOT_OWNER",
            actual=owner_user_id,
            expected=company.owner_id,
            message="只有公司老板才能进行科研",
        )
    return None


async def check_tech_allowed_for_company(
    session: AsyncSession,
    company_id: int,
    tech_id: str,
    is_tech_allowed_func,
    **_,
) -> RuleViolation | None:
    """检查科研是否在公司行业方向内。"""
    company = await session.get(Company, company_id)
    if not company:
        return None
    if not is_tech_allowed_func(company.company_type, tech_id):
        return RuleViolation(
            code="TECH_NOT_ALLOWED",
            actual=tech_id,
            expected="allowed_tech",
            message="该科研不在你公司行业的研发方向内",
        )
    return None


async def check_research_user_exists(
    session: AsyncSession,
    owner_user_id: int,
    **_,
) -> RuleViolation | None:
    """检查用户是否存在。"""
    owner = await session.get(User, owner_user_id)
    if not owner:
        return RuleViolation(
            code="USER_NOT_FOUND",
            actual=None,
            expected="exists",
            message="用户不存在",
        )
    return None


async def check_prerequisites(
    tech_tree: dict,
    tech_id: str,
    completed_techs: set[str],
    **_,
) -> RuleViolation | None:
    """检查前置科研是否完成。"""
    tech = tech_tree.get(tech_id, {})
    for prereq in tech.get("prerequisites", []):
        if prereq not in completed_techs:
            prereq_name = tech_tree.get(prereq, {}).get("name", prereq)
            return RuleViolation(
                code="PREREQUISITE_NOT_MET",
                actual=prereq,
                expected="completed",
                message=f"需要先完成前置科研: {prereq_name}",
            )
    return None


async def check_not_already_researching(
    session: AsyncSession,
    company_id: int,
    tech_id: str,
    **_,
) -> RuleViolation | None:
    """检查是否已开始或已完成该科研。"""
    existing = await session.execute(
        select(ResearchProgress).where(
            ResearchProgress.company_id == company_id,
            ResearchProgress.tech_id == tech_id,
        )
    )
    if existing.scalar_one_or_none():
        return RuleViolation(
            code="ALREADY_RESEARCHING",
            actual=tech_id,
            expected="not_started",
            message="该科研已开始或已完成",
        )
    return None


async def check_research_employees(
    session: AsyncSession,
    company_id: int,
    tech_tree: dict,
    tech_id: str,
    completed_count: int,
    employee_step: int,
    **_,
) -> RuleViolation | None:
    """检查员工数量是否满足科研要求。"""
    company = await session.get(Company, company_id)
    if not company:
        return None
    tech = tech_tree.get(tech_id, {})
    required_employees = int(
        tech.get("required_employees", max(1, 1 + completed_count * employee_step))
    )
    effective_employees = get_effective_employee_count_for_progress(company.employee_count)
    if effective_employees < required_employees:
        return RuleViolation(
            code="INSUFFICIENT_EMPLOYEES",
            actual=effective_employees,
            expected=required_employees,
            message=(
                f"员工不足，科研「{tech['name']}」需要至少 {required_employees} 人，"
                f"当前有效员工 {effective_employees} 人（总员工 {company.employee_count}）"
            ),
        )
    return None


async def check_research_reputation(
    session: AsyncSession,
    owner_user_id: int,
    tech_tree: dict,
    tech_id: str,
    completed_count: int,
    reputation_step: int,
    **_,
) -> RuleViolation | None:
    """检查声望是否满足科研要求。"""
    owner = await session.get(User, owner_user_id)
    if not owner:
        return None
    tech = tech_tree.get(tech_id, {})
    required_reputation = int(
        tech.get("required_reputation", completed_count * reputation_step)
    )
    if owner.reputation < required_reputation:
        return RuleViolation(
            code="INSUFFICIENT_REPUTATION",
            actual=owner.reputation,
            expected=required_reputation,
            message=(
                f"声望不足，科研「{tech['name']}」需要声望 {required_reputation}，"
                f"当前仅 {owner.reputation}"
            ),
        )
    return None


async def check_research_funds(
    session: AsyncSession,
    company_id: int,
    research_cost: int,
    **_,
) -> RuleViolation | None:
    """检查公司资金是否足够。"""
    company = await session.get(Company, company_id)
    if not company:
        return None
    if company.cp_points < research_cost:
        return RuleViolation(
            code="INSUFFICIENT_FUNDS",
            actual=company.cp_points,
            expected=research_cost,
            message=f"公司积分不足，需要 {fmt_traffic(research_cost)}",
        )
    return None


# ============================================================================
# Rule Lists
# ============================================================================

def get_research_guard_rules() -> list[Rule]:
    """获取科研开始前置条件规则。"""
    return [
        Rule("TECH_VALID", check_tech_valid),
        Rule("COMPANY_EXISTS", check_research_company_exists),
        Rule("IS_OWNER", check_research_owner),
        Rule("TECH_ALLOWED", check_tech_allowed_for_company),
        Rule("USER_EXISTS", check_research_user_exists),
        Rule("PREREQUISITES", check_prerequisites),
        Rule("NOT_ALREADY_RESEARCHING", check_not_already_researching),
    ]


def get_research_requirement_rules() -> list[Rule]:
    """获取科研开始需求规则。"""
    return [
        Rule("EMPLOYEES", check_research_employees),
        Rule("REPUTATION", check_research_reputation),
        Rule("FUNDS", check_research_funds),
    ]
