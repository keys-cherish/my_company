"""Random events system - adds unpredictability and fun to the game.

Events can trigger during daily settlement or be checked periodically.
Types: employee resignation, retirement, sick leave, market boom, PR crisis, etc.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Company, Product
from services.company_service import add_funds, get_company_employee_limit
from services.user_service import add_self_points, add_reputation


@dataclass
class GameEvent:
    name: str
    description: str
    category: str  # employee / market / pr / lucky
    effect_type: str  # income_pct / flat_traffic / reputation / product_quality / employee
    effect_value: float  # positive = good, negative = bad
    weight: int  # probability weight


EVENTS: list[GameEvent] = [
    # Employee events
    GameEvent("核心员工离职", "一名核心员工突然离职，人员减少", "employee", "employee", -1, 12),
    GameEvent("员工退休", "一位资深员工到了退休年龄", "employee", "employee", -1, 8),
    GameEvent("员工请假潮", "季节性请假，团队效率下降", "employee", "income_pct", -0.03, 20),
    GameEvent("招到优秀人才", "从竞争对手挖到了一名高级工程师", "employee", "employee", 1, 10),
    GameEvent("团队建设成功", "团建效果显著，团队凝聚力提升", "employee", "income_pct", 0.05, 15),
    GameEvent("员工获奖", "公司员工在技术比赛中获奖，提升声望", "employee", "reputation", 5, 8),
    GameEvent("集体病假", "流感季节，多名员工请假", "employee", "income_pct", -0.08, 6),
    GameEvent("员工生育假", "有员工进入产假/陪产假", "employee", "income_pct", -0.02, 8),

    # Market events
    GameEvent("行业利好", "政策扶持，行业迎来增长", "market", "income_pct", 0.15, 8),
    GameEvent("市场低迷", "经济下行，市场需求萎缩", "market", "income_pct", -0.12, 8),
    GameEvent("竞品暴雷", "主要竞争对手出了大问题，客户涌入", "market", "flat_traffic", 1000, 5),
    GameEvent("供应链中断", "上游供应链出现问题，运营成本增加", "market", "flat_traffic", -500, 10),

    # PR events
    GameEvent("媒体正面报道", "知名媒体发布了关于公司的正面文章", "pr", "reputation", 8, 10),
    GameEvent("公关危机", "负面舆情发酵，声望受损", "pr", "reputation", -5, 8),
    GameEvent("CEO演讲走红", "公司CEO的演讲视频意外走红", "pr", "reputation", 12, 5),

    # Lucky events
    GameEvent("天降横财", "意外收到一笔投资", "lucky", "flat_traffic", 2000, 3),
    GameEvent("中了行业大奖", "公司产品获得年度行业大奖", "lucky", "reputation", 20, 2),
    GameEvent("服务器故障", "服务器出现严重故障，紧急修复花费不少", "lucky", "flat_traffic", -800, 7),

    # Product events
    GameEvent("产品好评如潮", "用户反馈极好，产品口碑传播", "market", "product_quality", 3, 10),
    GameEvent("产品出现Bug", "线上出现严重Bug，紧急修复中", "market", "product_quality", -2, 12),
]

# Ethics-exclusive events
HIGH_ETHICS_EVENTS: list[GameEvent] = [
    GameEvent("政府补贴", "道德标杆企业获得政府专项补贴", "lucky", "flat_traffic", 3000, 15),
    GameEvent("ESG大奖", "公司荣获ESG最佳实践奖，声望大增", "pr", "reputation", 15, 12),
    GameEvent("人才慕名而来", "行业优秀人才被公司口碑吸引，主动加入", "employee", "employee", 2, 10),
    GameEvent("绿色合作", "环保机构邀请合作，品牌价值提升", "pr", "reputation", 10, 10),
]

LOW_ETHICS_EVENTS: list[GameEvent] = [
    GameEvent("内部举报", "员工向监管部门举报公司违规操作", "pr", "flat_traffic", -2000, 15),
    GameEvent("消费者抵制", "网民发起抵制运动，产品口碑暴跌", "market", "product_quality", -5, 12),
    GameEvent("监管调查", "监管部门对公司进行专项调查", "pr", "flat_traffic", -1500, 10),
    GameEvent("人才流失潮", "优秀员工因公司风评离职", "employee", "employee", -2, 10),
]

# Chance that any event fires during settlement (per company)
EVENT_CHANCE = 0.35  # 35% chance per company per day

# Positive events pool (for newbie highlight)
POSITIVE_EVENTS: list[GameEvent] = [e for e in EVENTS if e.effect_value > 0]

# Newbie highlight: max company age for guaranteed first positive event
_NEWBIE_HIGHLIGHT_MAX_DAYS = 7


async def _is_newbie_highlight(company: Company) -> bool:
    """First settlement for a young company? Guarantee a positive event."""
    from cache.redis_client import get_redis
    r = await get_redis()
    key = f"newbie_highlight:{company.id}"
    if await r.exists(key):
        return False
    age = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - company.created_at
    return age.days <= _NEWBIE_HIGHLIGHT_MAX_DAYS


async def _mark_newbie_highlight_done(company_id: int):
    from cache.redis_client import get_redis
    r = await get_redis()
    await r.set(f"newbie_highlight:{company_id}", "1")


def _calc_risk_factor(profile) -> float:
    """Calculate dynamic risk factor based on company operations.

    Returns a modifier to the base event chance. Higher = more events.
    Risk increases from:
      - High work hours (10h: +10%, 12h: +25%)
      - Low ethics (<50: up to +20%)
      - High regulation pressure (up to +15%)
    Risk decreases from:
      - Culture (up to -30%)
    """
    risk = 0.0
    # Work hours risk
    if profile.work_hours >= 12:
        risk += 0.25
    elif profile.work_hours >= 10:
        risk += 0.10
    # Low ethics risk
    if profile.ethics < 50:
        risk += (50 - profile.ethics) / 50 * 0.20  # max +20%
    # Regulation pressure risk
    risk += (profile.regulation_pressure / 100) * 0.15  # max +15%
    # Culture mitigation
    culture_reduce = (profile.culture / 100) * 0.30  # max -30%
    risk -= culture_reduce
    return risk


async def roll_daily_events(session: AsyncSession, company: Company) -> list[str]:
    """Roll for random events during daily settlement. Returns event descriptions."""
    from config import settings
    from services.operations_service import get_or_create_profile
    messages = []

    is_newbie = await _is_newbie_highlight(company)

    # Load profile for dynamic risk calculation
    profile = await get_or_create_profile(session, company.id)

    if not is_newbie:
        # Dynamic event chance: base 35% + risk factor
        risk_mod = _calc_risk_factor(profile)
        effective_chance = max(0.10, min(0.80, settings.event_chance + risk_mod))
        if random.random() > effective_chance:
            return messages  # No event today

    culture = profile.culture  # 0-100

    if is_newbie:
        # Force 1 positive event, maybe 1 extra normal event
        pos_weights = [e.weight for e in POSITIVE_EVENTS]
        selected = list(random.choices(POSITIVE_EVENTS, weights=pos_weights, k=1))
        if random.random() < 0.4:
            all_weights = [e.weight for e in EVENTS]
            selected += random.choices(EVENTS, weights=all_weights, k=1)
        await _mark_newbie_highlight_done(company.id)
    else:
        num_events = random.choices([1, 2], weights=[75, 25], k=1)[0]
        # Culture reduces negative event weight: up to -30% at culture 100
        culture_neg_reduce = (culture / 100) * 0.30
        # Low ethics increases negative event weight: up to +20%
        ethics_neg_boost = max(0, (50 - profile.ethics) / 50 * 0.20)

        # Build event pool: base events + ethics-exclusive events
        event_pool = list(EVENTS)
        if profile.ethics >= 90:
            event_pool.extend(HIGH_ETHICS_EVENTS)
        elif profile.ethics < 30:
            event_pool.extend(LOW_ETHICS_EVENTS)

        adjusted_weights = []
        for e in event_pool:
            w = e.weight
            if e.effect_value < 0:
                w = int(w * (1.0 - culture_neg_reduce + ethics_neg_boost))
                w = max(1, w)
            adjusted_weights.append(w)
        selected = list(random.choices(event_pool, weights=adjusted_weights, k=num_events))

    # Deduplicate by name
    seen = set()
    unique = []
    for e in selected:
        if e.name not in seen:
            seen.add(e.name)
            unique.append(e)

    # Check risk_hedge buff (skip negative events)
    from services.shop_service import should_skip_negative_event, consume_buff
    has_hedge = await should_skip_negative_event(company.id)

    for event in unique:
        if has_hedge and event.effect_value < 0:
            await consume_buff(company.id, "risk_hedge")
            messages.append(f"🛡 【风险对冲】成功抵御了「{event.name}」!")
            has_hedge = False
            continue
        msg = await _apply_event(session, company, event)
        if is_newbie and event.effect_value > 0 and not any("新手高光" in m for m in messages):
            messages.append(f"🌟 【新手高光】好运降临新公司！")
        messages.append(msg)

    return messages


async def _apply_event(session: AsyncSession, company: Company, event: GameEvent) -> str:
    """Apply a single event and return a description string."""
    effect_desc = ""

    if event.effect_type == "income_pct":
        change = int(company.daily_revenue * event.effect_value)
        await add_funds(session, company.id, change)
        sign = "+" if change >= 0 else ""
        effect_desc = f"积分变动: {sign}{change}"

    elif event.effect_type == "flat_traffic":
        amount = int(event.effect_value)
        await add_funds(session, company.id, amount)
        effect_desc = f"积分{'+' if amount > 0 else ''}{amount}"

    elif event.effect_type == "reputation":
        rep = int(event.effect_value)
        await add_reputation(session, company.owner_id, max(rep, 0))
        sign = "+" if rep >= 0 else ""
        effect_desc = f"声望{sign}{rep}"

    elif event.effect_type == "employee":
        change = int(event.effect_value)
        new_count = max(1, company.employee_count + change)
        max_emp = get_company_employee_limit(company.level, company.company_type)
        new_count = min(new_count, max_emp)
        company.employee_count = new_count
        await session.flush()
        sign = "+" if change > 0 else ""
        effect_desc = f"员工变动: {sign}{change} (当前: {new_count}人)"

    elif event.effect_type == "product_quality":
        result = await session.execute(
            select(Product).where(Product.company_id == company.id)
        )
        products = list(result.scalars().all())
        if products:
            target = random.choice(products)
            target.quality = max(1, target.quality + int(event.effect_value))
            await session.flush()
            effect_desc = f"产品「{target.name}」品质变动: {'+' if event.effect_value > 0 else ''}{int(event.effect_value)}"
        else:
            effect_desc = "无产品受影响"

    await add_self_points(company.owner_id, 1, session=session)

    category_emoji = {"employee": "👤", "market": "📊", "pr": "📰", "lucky": "🎲"}
    emoji = category_emoji.get(event.category, "❓")
    return f"{emoji} 【{event.name}】{event.description}\n   → {effect_desc}"
