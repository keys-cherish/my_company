"""经营策略系统：工时/办公/培训/保险/文化/道德/监管。

监管压力由工时自动联动：超时自动涨，合规自动降。
所有倍率影响每日结算的收入和成本计算。
"""

from __future__ import annotations

import datetime as dt
import math
import random

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Company, CompanyOperationProfile
from utils.timezone import BJ_TZ
from cache.redis_client import get_redis


# ── 工时选项 ──────────────────────────────────────────
# income_mult: 营收乘数  cost_mult: 成本乘数  ethics_delta: 每日道德变化
WORK_HOUR_OPTIONS: dict[int, dict] = {
    6:  {"label": "😴 轻松", "income_mult": 0.80, "cost_mult": 0.92, "ethics_delta": 2},
    8:  {"label": "🏢 正常", "income_mult": 1.00, "cost_mult": 1.00, "ethics_delta": 0},
    10: {"label": "🔥 冲刺", "income_mult": 1.50, "cost_mult": 1.10, "ethics_delta": 0},
    12: {"label": "💀 高压", "income_mult": 3.00, "cost_mult": 1.25, "ethics_delta": -5},
    24: {"label": "☠️ 疯狂", "income_mult": 20.0, "cost_mult": 15.00, "ethics_delta": -25},
}

# ── 办公等级 ──────────────────────────────────────────
# income_mult: 营收乘数  employee_cost: 每人每日办公成本
OFFICE_LEVELS: dict[str, dict] = {
    "basic": {"name": "基础办公", "income_mult": 1.00, "employee_cost": 6},
    "standard": {"name": "标准办公", "income_mult": 1.12, "employee_cost": 14},
    "premium": {"name": "优质办公", "income_mult": 1.35, "employee_cost": 26},
    "top": {"name": "顶级办公", "income_mult": 1.70, "employee_cost": 45},
}

# ── 培训等级 ──────────────────────────────────────────
# income_mult: 营收乘数  hourly_cost: 每人每小时培训费  duration_hours: 培训持续时长
TRAINING_LEVELS: dict[str, dict] = {
    "none": {"name": "无培训", "income_mult": 1.00, "hourly_cost": 0, "duration_hours": 0},
    "basic": {"name": "基础培训", "income_mult": 1.12, "hourly_cost": 30, "duration_hours": 24},
    "pro": {"name": "岗位实训", "income_mult": 1.30, "hourly_cost": 60, "duration_hours": 36},
    "elite": {"name": "精英特训", "income_mult": 1.50, "hourly_cost": 120, "duration_hours": 48},
}

# ── 保险等级 ──────────────────────────────────────────
# cost_rate: 保费占薪资比例  fine_reduction: 监管罚款减免比例
INSURANCE_LEVELS: dict[str, dict] = {
    "basic": {"name": "基础险", "cost_rate": 0.010, "fine_reduction": 0.0},
    "plus": {"name": "进阶险", "cost_rate": 0.022, "fine_reduction": 0.4},
    "supreme": {"name": "至尊险", "cost_rate": 0.040, "fine_reduction": 0.8},
}

# ── 市场景气周期 ────────────────────────────────────
MARKET_TRENDS: tuple[dict, ...] = (
    {"key": "sun", "label": "☀️ 繁荣", "income_mult": 1.12},
    {"key": "cloud", "label": "⛅ 平稳", "income_mult": 1.00},
    {"key": "rain", "label": "🌧️ 衰退", "income_mult": 0.88},
)
_MARKET_CYCLE_ANCHOR = dt.date(2026, 1, 1)  # 景气周期基准日期

# ── 监管常量 ──────────────────────────────────────────
LEGAL_WORK_HOURS = 8            # 法定工时上限（超出触发监管）
WORK_HOUR_AUDIT_VARIANCE = 1    # 抽检时实际工时浮动范围 ±1h
REGULATION_FINE_CAP_RATE = 0.75  # 罚款上限：当日收入的75%


def _clamp(value: int, lower: int, upper: int) -> int:
    """将数值限制在 [lower, upper] 范围内。"""
    return max(lower, min(upper, value))


def get_overtime_risk_boost(overtime_hours: int) -> float:
    """Piecewise overtime risk boost.

    1-2h overtime: baseline slope.
    3-4h overtime: doubled slope vs 1-2h segment.
    >4h overtime: steeper tail risk.
    """
    if overtime_hours <= 0:
        return 0.0
    if overtime_hours <= 2:
        return overtime_hours * 0.10
    if overtime_hours <= 4:
        return 0.20 + (overtime_hours - 2) * 0.20
    return 0.60 + (overtime_hours - 4) * 0.25


def ethics_rating(ethics: int) -> str:
    if ethics >= 90:
        return "守正"
    if ethics >= 70:
        return "良性"
    if ethics >= 45:
        return "中立"
    if ethics >= 20:
        return "高风险"
    if ethics >= 0:
        return "无良"
    if ethics >= -50:
        return "无恶不作"
    return "丧尽天良"


def reputation_rating(reputation: int) -> str:
    if reputation >= 1000:
        return "SSS"
    if reputation >= 700:
        return "SS"
    if reputation >= 500:
        return "S"
    if reputation >= 300:
        return "A"
    if reputation >= 180:
        return "B"
    return "C"


def bar10(value: int, minimum: int = 0, maximum: int = 100) -> str:
    span = maximum - minimum
    blocks = int(round(_clamp(value, minimum, maximum) - minimum) / span * 10) if span > 0 else 0
    return "█" * blocks + "░" * (10 - blocks)


def calc_immoral_buff(ethics: int) -> float:
    """缺德buff：道德<20时，越低收入加成越高，最高200%。"""
    if ethics >= 20:
        return 1.0
    return 1.0 + max(0, 20 - ethics) / 60  # ethics=-100 → 1+120/60=3.0 即200%加成


async def get_or_create_profile(session: AsyncSession, company_id: int) -> CompanyOperationProfile:
    profile = await session.get(CompanyOperationProfile, company_id)
    if profile is None:
        profile = CompanyOperationProfile(company_id=company_id)
        session.add(profile)
        await session.flush()
    return profile


def _is_training_active(profile: CompanyOperationProfile, now: dt.datetime) -> bool:
    if profile.training_level == "none" or profile.training_expires_at is None:
        return False
    end_time = profile.training_expires_at
    if end_time.tzinfo is None:
        end_time = end_time.replace(tzinfo=dt.UTC)
    return end_time > now


def get_market_trend(company: Company, now: dt.datetime | None = None) -> dict:
    """7-day market trend cycle with per-industry weekly randomness."""
    if now is None:
        now = dt.datetime.now(BJ_TZ)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC).astimezone(BJ_TZ)
    else:
        now = now.astimezone(BJ_TZ)

    cycle_week = ((now.date() - _MARKET_CYCLE_ANCHOR).days // 7)
    industry_key = company.company_type or "tech"
    rng = random.Random(f"{industry_key}:{cycle_week}")
    # 平稳概率略高，繁荣/衰退次之；每周每行业固定，下一周自动变化。
    return rng.choices(
        MARKET_TRENDS,
        weights=[28, 44, 28],
        k=1,
    )[0]


def get_training_info(profile: CompanyOperationProfile, now: dt.datetime) -> dict:
    info = TRAINING_LEVELS.get(profile.training_level, TRAINING_LEVELS["none"])
    active = _is_training_active(profile, now)
    return {
        "key": profile.training_level,
        "name": info["name"],
        "income_mult": info["income_mult"] if active else 1.0,
        "hourly_cost": info["hourly_cost"] if active else 0,
        "active": active,
        "expires_at": profile.training_expires_at,
    }


def get_operation_multipliers(profile: CompanyOperationProfile, now: dt.datetime) -> dict:
    work = WORK_HOUR_OPTIONS.get(profile.work_hours, WORK_HOUR_OPTIONS[8])
    office = OFFICE_LEVELS.get(profile.office_level, OFFICE_LEVELS["standard"])
    training = get_training_info(profile, now)
    culture_bonus = 1.0 + (profile.culture / 1000)  # max +10%
    return {
        "work": work,
        "office": office,
        "training": training,
        "culture_bonus_mult": culture_bonus,
        "income_mult": work["income_mult"] * office["income_mult"] * training["income_mult"] * culture_bonus,
    }


def calc_extra_operating_costs(
    profile: CompanyOperationProfile,
    employee_count: int,
    base_income: int,
    salary_cost: int,
    social_insurance: int,
    now: dt.datetime,
) -> dict:
    work = WORK_HOUR_OPTIONS.get(profile.work_hours, WORK_HOUR_OPTIONS[8])
    office = OFFICE_LEVELS.get(profile.office_level, OFFICE_LEVELS["standard"])
    training = get_training_info(profile, now)
    insurance = INSURANCE_LEVELS.get(profile.insurance_level, INSURANCE_LEVELS["basic"])

    office_cost = employee_count * int(office["employee_cost"])
    training_cost = employee_count * int(training["hourly_cost"]) if training["active"] else 0
    regulation_cost = int(base_income * (0.010 + profile.regulation_pressure / 5000))
    insurance_cost = int((salary_cost + social_insurance) * insurance["cost_rate"])
    work_cost_adjust = int((salary_cost + social_insurance) * (work["cost_mult"] - 1.0))
    culture_maintenance = int(base_income * (profile.culture / 20000))  # 0~0.5%

    return {
        "office_cost": max(0, office_cost),
        "training_cost": max(0, training_cost),
        "regulation_cost": max(0, regulation_cost),
        "insurance_cost": max(0, insurance_cost),
        "work_cost_adjust": work_cost_adjust,
        "culture_maintenance": max(0, culture_maintenance),
    }


def maybe_regulation_fine(
    profile: CompanyOperationProfile,
    income_total: int,
    now: dt.datetime,
) -> int:
    """Backward-compatible wrapper: return only fine amount."""
    return int(run_regulation_audit(profile, income_total, now)["fine"])


def run_regulation_audit(
    profile: CompanyOperationProfile,
    income_total: int,
    now: dt.datetime,
) -> dict[str, int | float]:
    """Run labor-hours regulation audit and return detailed result.

    Mechanics:
    - Randomly sample actual work-hours around configured hours.
    - If sampled hours exceed legal threshold, risk and fine are escalated sharply.
    - Higher regulation_pressure means stricter checks and stronger penalties.
    """
    insurance = INSURANCE_LEVELS.get(profile.insurance_level, INSURANCE_LEVELS["basic"])
    # Deterministic daily RNG per company:
    # same company/day => stable audit outcome, next day => new sample.
    rng = random.Random((now.toordinal() * 97) + profile.company_id)
    sampled_hours = profile.work_hours
    overtime_hours = max(0, sampled_hours - LEGAL_WORK_HOURS)

    # Risk is driven by ethics + regulation pressure, then sharply increased by overtime.
    base_risk = 0.02 + max(0, 50 - profile.ethics) * 0.004 + profile.regulation_pressure * 0.0005
    overtime_risk_boost = get_overtime_risk_boost(overtime_hours)
    culture_risk_reduce = (profile.culture / 100) * 0.30
    risk = max(0.01, min(0.99, (base_risk + overtime_risk_boost) * (1.0 - culture_risk_reduce)))
    # 24h 工时 → 强制 risk=0.99
    if profile.work_hours == 24:
        risk = 0.99
    if rng.random() > risk:
        return {
            "fine": 0,
            "sampled_hours": sampled_hours,
            "overtime_hours": overtime_hours,
            "risk": risk,
        }

    fine_base = int(income_total * (0.015 + max(0, 45 - profile.ethics) / 1000))
    overtime_component = int(income_total * (0.020 * overtime_hours))

    # Higher pressure and overtime both amplify crackdown severity.
    pressure_mult = 1.0 + profile.regulation_pressure * 0.006
    overtime_mult = 1.0 + overtime_hours * 0.50
    fine_raw = int((fine_base + overtime_component) * pressure_mult * overtime_mult)
    fine_after_insurance = int(fine_raw * (1.0 - insurance["fine_reduction"]))
    fine_cap = max(200, int(income_total * REGULATION_FINE_CAP_RATE))
    fine = max(0, min(fine_after_insurance, fine_cap))

    return {
        "fine": fine,
        "sampled_hours": sampled_hours,
        "overtime_hours": overtime_hours,
        "risk": risk,
    }


async def settle_profile_daily(
    session: AsyncSession,
    profile: CompanyOperationProfile,
    now: dt.datetime,
) -> list[str]:
    """Settle daily profile changes. Returns event messages."""
    messages = []
    work = WORK_HOUR_OPTIONS.get(profile.work_hours, WORK_HOUR_OPTIONS[8])
    profile.ethics = _clamp(profile.ethics + int(work["ethics_delta"]), -100, 100)
    if profile.culture > 55:
        profile.ethics = _clamp(profile.ethics + 1, -100, 100)
    if profile.training_level != "none" and not _is_training_active(profile, now):
        profile.training_level = "none"
        profile.training_expires_at = None

    # ── 监管自动联动工时 ──
    # 工时>8h: 每超1h监管+4，高压(12h)每日+16
    # 工时≤8h: 每日自然降低-2，轻松(6h)额外-1
    old_reg = profile.regulation_pressure
    overtime = max(0, profile.work_hours - LEGAL_WORK_HOURS)
    if overtime > 0:
        reg_delta = overtime * 4
        profile.regulation_pressure = _clamp(profile.regulation_pressure + reg_delta, 0, 100)
        if profile.regulation_pressure > old_reg:
            messages.append(
                f"🛂 超时工作({profile.work_hours}h)引发监管关注："
                f"监管+{profile.regulation_pressure - old_reg}"
                f"（{old_reg}→{profile.regulation_pressure}）"
            )
    else:
        reg_recover = 2 + (1 if profile.work_hours <= 6 else 0)
        profile.regulation_pressure = _clamp(profile.regulation_pressure - reg_recover, 0, 100)
        if profile.regulation_pressure < old_reg:
            messages.append(
                f"🛂 合规工时({profile.work_hours}h)，监管放松："
                f"监管-{old_reg - profile.regulation_pressure}"
                f"（{old_reg}→{profile.regulation_pressure}）"
            )

    # Ethics <20: chance of employee attrition
    # Ethics <0: guaranteed attrition based on how negative
    company = None
    if profile.ethics < 0:
        company = await session.get(Company, profile.company_id)
        if company and company.employee_count > 1:
            lost = min(math.ceil(abs(profile.ethics) / 20), company.employee_count - 1)
            company.employee_count = max(1, company.employee_count - lost)
            messages.append(f"😤 道德极低，{lost}名员工愤而离职！（道德:{profile.ethics}）")
    elif profile.ethics < 20:
        attrition_chance = (20 - profile.ethics) / 20 * 0.40  # max 40% at ethics 0
        if random.random() < attrition_chance:
            company = await session.get(Company, profile.company_id)
            if company and company.employee_count > 1:
                lost = min(max(1, company.employee_count // 20), 3)  # lose 1-3
                company.employee_count = max(1, company.employee_count - lost)
                messages.append(f"😤 道德过低，{lost}名员工愤而离职！（道德:{profile.ethics}/100）")

    # 过劳死：高工时额外员工损失
    if profile.work_hours in (12, 24):
        if company is None:
            company = await session.get(Company, profile.company_id)
        if company and company.employee_count > 1:
            if profile.work_hours == 12:
                lost = random.randint(1, 2)
                lost = min(lost, company.employee_count - 1)
                company.employee_count = max(1, company.employee_count - lost)
                messages.append(f"💀 过劳死：高压工时导致{lost}名员工倒下！")
            else:  # 24h
                lost = random.randint(5, 15)
                lost = min(lost, company.employee_count - 1)
                company.employee_count = max(1, company.employee_count - lost)
                messages.append(f"☠️ 过劳死：疯狂工时导致{lost}名员工倒下！")

    await session.flush()
    return messages


async def set_work_hours(
    session: AsyncSession,
    company_id: int,
    owner_user_id: int,
    hours: int,
) -> tuple[bool, str]:
    company = await session.get(Company, company_id)
    if company is None:
        return False, "公司不存在"
    if company.owner_id != owner_user_id:
        return False, "只有公司老板可以调整工时"
    if hours not in WORK_HOUR_OPTIONS:
        return False, "无效工时选项"
    profile = await get_or_create_profile(session, company_id)
    profile.work_hours = hours
    if hours == 24:
        profile.ethics = min(profile.ethics, -10)  # 直接降至 -10 或更低
    await session.flush()
    return True, f"已调整工时为 {hours}h（{WORK_HOUR_OPTIONS[hours]['label']}）"


async def cycle_option(
    session: AsyncSession,
    company_id: int,
    owner_user_id: int,
    field: str,
) -> tuple[bool, str]:
    company = await session.get(Company, company_id)
    if company is None:
        return False, "公司不存在"
    if company.owner_id != owner_user_id:
        return False, "只有公司老板可操作"
    profile = await get_or_create_profile(session, company_id)

    if field == "office":
        keys = list(OFFICE_LEVELS.keys())
        idx = keys.index(profile.office_level) if profile.office_level in keys else 0
        if idx >= len(keys) - 1:
            return True, "已是顶级办公，无需继续升级"
        profile.office_level = keys[idx + 1]
        await session.flush()
        return True, f"办公升级为：{OFFICE_LEVELS[profile.office_level]['name']}"

    if field == "insurance":
        keys = list(INSURANCE_LEVELS.keys())
        idx = keys.index(profile.insurance_level) if profile.insurance_level in keys else 0
        if idx >= len(keys) - 1:
            return True, "已是最高保险方案，无需继续升级"
        profile.insurance_level = keys[idx + 1]
        await session.flush()
        return True, f"保险方案升级为：{INSURANCE_LEVELS[profile.insurance_level]['name']}"

    if field == "culture":
        profile.culture = _clamp(profile.culture + 8, 0, 100)
        await session.flush()
        return True, f"企业文化提升至 {profile.culture}/100"

    if field == "ethics":
        profile.ethics = _clamp(profile.ethics + 6, -100, 100)
        await session.flush()
        return True, f"企业道德提升至 {profile.ethics}"

    if field == "regulation":
        # Regulation is now auto-adjusted by work hours, not manually
        return False, "监管压力由工时自动调节：超时自动涨，合规自动降"

    return False, "未知选项"


async def start_training(
    session: AsyncSession,
    company_id: int,
    owner_user_id: int,
    level: str,
) -> tuple[bool, str]:
    from services.company_service import add_funds

    company = await session.get(Company, company_id)
    if company is None:
        return False, "公司不存在"
    if company.owner_id != owner_user_id:
        return False, "只有公司老板可开启培训"
    if level not in TRAINING_LEVELS:
        return False, "无效培训档位"
    if level == "none":
        profile = await get_or_create_profile(session, company_id)
        profile.training_level = "none"
        profile.training_expires_at = None
        await session.flush()
        return True, "已取消培训"

    profile = await get_or_create_profile(session, company_id)
    info = TRAINING_LEVELS[level]
    total_cost = int(company.employee_count * info["hourly_cost"] * info["duration_hours"])
    ok = await add_funds(session, company_id, -total_cost)
    if not ok:
        return False, f"公司积分不足，培训需要 {total_cost:,} 积分"
    now = dt.datetime.utcnow()
    profile.training_level = level
    profile.training_expires_at = now + dt.timedelta(hours=info["duration_hours"])
    profile.culture = _clamp(profile.culture + 4, 0, 100)
    await session.flush()
    end_bj = profile.training_expires_at.replace(tzinfo=dt.UTC).astimezone(BJ_TZ).strftime("%m-%d %H:%M")
    return True, f"培训已启动：{info['name']}，到期时间（北京时间）{end_bj}"


async def save_recent_events(company_id: int, events: list[str]):
    if not events:
        return
    r = await get_redis()
    key = f"company:events:{company_id}"
    trimmed = [e.replace("\n", " ").strip()[:120] for e in events[:8]]
    await r.delete(key)
    if trimmed:
        await r.rpush(key, *trimmed)
    await r.expire(key, 7 * 24 * 3600)


async def load_recent_events(company_id: int, limit: int = 3) -> list[str]:
    r = await get_redis()
    key = f"company:events:{company_id}"
    items = await r.lrange(key, 0, max(0, limit - 1))
    return [i for i in items if i]
