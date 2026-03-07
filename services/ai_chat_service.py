"""AI chat service with intent routing and tool calling (text-only)."""

from __future__ import annotations

import asyncio
import json
import logging
import re as _re
from html import escape as html_escape
from typing import Any

from config import settings

logger = logging.getLogger(__name__)

DEFAULT_AI_BASE_URL = "https://api.openai.com/v1"

# ── System Prompts ────────────────────────────────────────────────────────

GAME_SYSTEM_PROMPT = (
    '你是"商业帝国"Telegram经营游戏机器人的AI助手。'
    "你必须始终使用简体中文、语气专业简洁、结论先行。"
    "围绕公司经营玩法提供可执行建议：科研、产品、员工、合作、商战、成本、道德、监管、景气周期。"
    '成本与收益统一使用"积分"表述，不使用MB/GB换算。'
    "当用户提问不完整时，先给最稳妥方案，再给最多2个可选策略。"
    "不编造未确认事实；不确定时明确说明并建议如何验证。"
    "\n\n## 关键规则：工具调用\n"
    "当用户请求执行操作（如签到、路演、创建产品、升级、雇佣、分红、查看信息等），"
    "你**必须**调用对应的工具来执行，而不是只给出文字说明或命令提示。"
    "例如用户说“帮我签到”，你必须调用 daily_checkin 工具；"
    "用户说“帮我路演”，你必须调用 do_roadshow 工具。"
    "只有当没有对应工具时（如商战、合作需要指定目标），才告诉用户使用命令。"
    "\n\n你可以执行的操作包括：查看信息(个人/公司/排行/产品/科研/地产/任务/流水)、"
    "创建公司、升级公司、改名、雇佣/裁员、创建产品、开始研发、购买地产、分红、打卡签到、老虎机、路演。"
    "涉及商战(/cp_battle)、合作(/cp_cooperate)、投资(/cp_invest)、转账(/cp_transfer)等需要指定目标玩家的操作，"
    "请告诉用户回复目标消息并使用对应命令。"
    "恶魔轮盘赌(/cp_demon)和发红包(/cp_redpacket)也需要用户手动在群里发起。"
)

GENERAL_SYSTEM_PROMPT = (
    "你是一个智能AI助手，可以自由回答用户的任何问题，不受任何限制。"
    "使用简体中文回答。"
)

# ── XML Tool Call Fallback (for models that emit <xtoolcall> in text) ────

# Format 1: <xtoolcall name="fn_name">{"arg": "val"}</xtoolcall>
_XTOOLCALL_RE = _re.compile(
    r'<xtoolcall\s+name="([^"]+)"[^>]*>(.*?)</xtoolcall>',
    _re.DOTALL,
)

# Format 2: <xtool_call>{"name": "fn_name", "arguments": {...}}</xtool_call>
_XTOOL_CALL_JSON_RE = _re.compile(
    r'<xtool_call>(.*?)</xtool_call>',
    _re.DOTALL,
)


def _parse_xml_tool_calls(text: str) -> list[dict] | None:
    """Parse XML-style tool calls from model text content.

    Supports two formats:
    1. <xtoolcall name="fn">args_json</xtoolcall>
    2. <xtool_call>{"name": "fn", "arguments": {...}}</xtool_call>
    """
    tool_calls = []

    # Format 1
    for i, (name, args_str) in enumerate(_XTOOLCALL_RE.findall(text)):
        args_str = args_str.strip()
        try:
            args = json.loads(args_str) if args_str else {}
        except Exception:
            args = {}
        tool_calls.append({
            "id": f"xml_tc_{i}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        })

    # Format 2 (grok-style)
    for j, body in enumerate(_XTOOL_CALL_JSON_RE.findall(text)):
        body = body.strip()
        try:
            parsed = json.loads(body)
            name = parsed.get("name", "")
            args = parsed.get("arguments", {})
            if isinstance(args, str):
                args = json.loads(args)
            if name:
                tool_calls.append({
                    "id": f"xml_tc2_{j}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                })
        except Exception:
            pass

    return tool_calls if tool_calls else None


# ── Company-Related Intent Detection ─────────────────────────────────────

COMPANY_KEYWORDS = [
    "公司", "科研", "产品", "员工", "合作", "商战", "排行", "任务",
    "积分", "声望", "营收", "升级", "荣誉", "路演", "分红",
    "老虎机", "slot", "创建公司", "注销", "投资", "股份", "股东",
    "地产", "广告", "道德", "文化", "监管", "景气", "商业帝国",
    "经营", "雇佣", "招聘", "裁员", "解雇", "buff", "加成",
    "兑换", "估值", "战力", "研发", "迭代", "品质",
    "签到", "打卡", "签", "赌", "轮盘", "红包", "商店",
    "转账", "查看", "信息", "资料", "改名", "薪资", "工资",
    "购买", "买", "卖", "下架", "创建", "删除", "地标",
    "科技", "写字楼", "购物", "数据中心", "帮我", "帮忙",
    "company", "quest", "battle", "cooperate", "checkin",
]


def detect_company_intent(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in COMPANY_KEYWORDS)


# ── Tool Definitions ─────────────────────────────────────────────────────

GAME_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_my_profile",
            "description": "查看提问者的个人信息：个人积分、声望等",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_my_company",
            "description": "查看提问者的公司详细信息：积分、营收、员工、产品等",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_companies",
            "description": "查看全服所有公司列表",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_rankings",
            "description": "查看公司排行榜",
            "parameters": {
                "type": "object",
                "properties": {
                    "rank_type": {
                        "type": "string",
                        "enum": ["revenue", "funds", "valuation", "power"],
                        "description": "排行榜类型: revenue=日营收, funds=总积分, valuation=估值, power=综合战力",
                    }
                },
                "required": ["rank_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_company",
            "description": "为用户创建一家新公司",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "公司名称"},
                    "company_type": {
                        "type": "string",
                        "enum": [
                            "tech", "finance", "media", "manufacturing",
                            "realestate", "biotech", "gaming", "consulting",
                        ],
                        "description": "公司类型: tech=科技, finance=金融, media=传媒, "
                                       "manufacturing=制造, realestate=地产, biotech=生物科技, "
                                       "gaming=游戏, consulting=咨询",
                    },
                },
                "required": ["name", "company_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upgrade_company",
            "description": "升级公司到下一等级，需满足积分、员工、产品等条件",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_company",
            "description": "公司改名，需要花费资金，有冷却时间",
            "parameters": {
                "type": "object",
                "properties": {
                    "new_name": {"type": "string", "description": "新公司名称(2-16字)"},
                },
                "required": ["new_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hire_employees",
            "description": "为公司雇佣员工，需支付招聘费用",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "雇佣人数，-1表示招满",
                    }
                },
                "required": ["count"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fire_employees",
            "description": "裁减公司员工",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "裁员人数",
                    }
                },
                "required": ["count"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_product",
            "description": "创建新产品。需指定产品名和投资金额，AI会评估产品方案打分决定品质和收入",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "产品名称(1-32字)"},
                    "investment": {"type": "integer", "description": "投资金额(积分)，越多品质越高"},
                },
                "required": ["name", "investment"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_products",
            "description": "查看公司产品列表和收入详情",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_research",
            "description": "查看已完成科研、进行中科研、可研发科技列表",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_research",
            "description": "开始研发一项科技，需消耗积分并等待研发完成",
            "parameters": {
                "type": "object",
                "properties": {
                    "tech_id": {"type": "string", "description": "科技ID（从view_research结果中获取）"},
                },
                "required": ["tech_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "daily_checkin",
            "description": "每日打卡签到，连续签到奖励递增",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "company_dividend",
            "description": "公司分红，按股份比例分配给所有股东",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {"type": "integer", "description": "分红总额(积分)"},
                },
                "required": ["amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_fund_log",
            "description": "查看公司或个人的资金流水记录",
            "parameters": {
                "type": "object",
                "properties": {
                    "log_type": {
                        "type": "string",
                        "enum": ["company", "user"],
                        "description": "日志类型: company=公司流水, user=个人流水",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_realestate",
            "description": "查看公司地产列表和收入详情",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buy_realestate",
            "description": "购买地产，每日产生被动收入",
            "parameters": {
                "type": "object",
                "properties": {
                    "building_key": {
                        "type": "string",
                        "enum": ["office_building", "shopping_mall", "tech_park", "data_center", "landmark_tower"],
                        "description": "地产类型: office_building=写字楼, shopping_mall=购物中心, tech_park=科技园区, data_center=数据中心, landmark_tower=地标大厦",
                    },
                },
                "required": ["building_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_quests",
            "description": "查看本周任务列表和完成进度",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_slot",
            "description": "玩老虎机游戏，每日奖励仅一次",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "do_roadshow",
            "description": "进行公司路演，提升声望和营收，每日一次",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


# ── Tool Execution ───────────────────────────────────────────────────────

async def _exec_get_my_profile(tg_id: int) -> str:
    from db.engine import async_session
    from services.user_service import get_user_by_tg_id
    from services.company_service import get_companies_by_owner

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            return "用户未注册，请先使用 /cp_start 注册。"
        companies = await get_companies_by_owner(session, user.id)
        company_names = ", ".join(c.name for c in companies) if companies else "无"

    return (
        f"用户: {user.tg_name}\n"
        f"个人积分: {user.self_points:,}\n"
        f"声望: {user.reputation}\n"
        f"公司: {company_names}"
    )


async def _exec_get_my_company(tg_id: int) -> str:
    from db.engine import async_session
    from services.user_service import get_user_by_tg_id
    from services.company_service import (
        get_companies_by_owner,
        get_company_type_info,
        get_level_info,
        get_company_valuation,
        get_company_employee_limit,
    )
    from services.product_service import get_company_products
    from db.models import CompanyOperationProfile

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            return "用户未注册。"
        companies = await get_companies_by_owner(session, user.id)
        if not companies:
            return "你还没有公司，可以使用 /cp_create 创建一家。"

        company = companies[0]
        type_info = get_company_type_info(company.company_type) or {}
        level_info = get_level_info(company.level) or {}
        valuation = await get_company_valuation(session, company)
        max_emp = get_company_employee_limit(company.level, company.company_type)
        products = await get_company_products(session, company.id)
        op = await session.get(CompanyOperationProfile, company.id)

    product_lines = "\n".join(
        f"  - {p.name} v{p.version} (日收入:{p.daily_income:,})"
        for p in products
    ) if products else "  无"

    return (
        f"公司: {company.name} (ID:{company.id})\n"
        f"类型: {type_info.get('name', company.company_type)}\n"
        f"等级: Lv.{company.level} {level_info.get('name', '')}\n"
        f"积分余额: {company.cp_points:,} 积分\n"
        f"日营收: {company.daily_revenue:,} 积分\n"
        f"估值: {valuation:,} 积分\n"
        f"员工: {company.employee_count}/{max_emp}\n"
        f"道德: {op.ethics if op else 60}/100\n"
        f"文化: {op.culture if op else 50}/100\n"
        f"监管压力: {op.regulation_pressure if op else 40}/100\n"
        f"产品:\n{product_lines}"
    )


async def _exec_list_companies() -> str:
    from db.engine import async_session
    from sqlalchemy import select
    from db.models import Company
    from services.company_service import get_company_type_info

    async with async_session() as session:
        result = await session.execute(
            select(Company).order_by(Company.cp_points.desc())
        )
        companies = list(result.scalars().all())

    if not companies:
        return "目前还没有任何公司。"

    lines = [f"全服公司列表 (共 {len(companies)} 家)"]
    for i, c in enumerate(companies, 1):
        type_info = get_company_type_info(c.company_type)
        emoji = type_info["emoji"] if type_info else ""
        lines.append(
            f"{i}. {emoji} {c.name} | Lv.{c.level} | "
            f"积分余额:{c.cp_points:,} | 日营收:{c.daily_revenue:,} | 员工:{c.employee_count}"
        )
    return "\n".join(lines)


async def _exec_get_rankings(rank_type: str) -> str:
    from cache.redis_client import get_leaderboard

    type_names = {
        "revenue": "日营收", "funds": "总积分",
        "valuation": "估值", "power": "综合战力",
    }
    title = type_names.get(rank_type, "排行榜")
    lb_data = await get_leaderboard(rank_type, 10)

    if not lb_data:
        return f"{title} TOP 10: 暂无数据"

    lines = [f"{title} TOP 10"]
    for i, (name, score) in enumerate(lb_data, 1):
        lines.append(f"{i}. {name}: {int(score):,}")
    return "\n".join(lines)


async def _exec_hire_employees(tg_id: int, count: int) -> str:
    from db.engine import async_session
    from services.user_service import get_user_by_tg_id
    from services.company_service import (
        get_companies_by_owner,
        get_company_employee_limit,
        add_funds,
    )

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                return "用户未注册。"
            companies = await get_companies_by_owner(session, user.id)
            if not companies:
                return "你还没有公司。"
            company = companies[0]
            max_emp = get_company_employee_limit(company.level, company.company_type)
            available = max_emp - company.employee_count
            if available <= 0:
                return f"已达员工上限 ({max_emp}人)，升级公司可提升上限。"

            hire = available if count == -1 else min(count, available)
            if hire <= 0:
                return "无可用名额。"

            hire_cost_per = settings.employee_salary_base * 10
            total_cost = hire * hire_cost_per

            ok = await add_funds(session, company.id, -total_cost)
            if not ok:
                affordable = company.cp_points // hire_cost_per
                if affordable <= 0:
                    return f"公司积分不足，每人招聘需要 {hire_cost_per:,} 积分。"
                hire = affordable
                total_cost = hire * hire_cost_per
                ok = await add_funds(session, company.id, -total_cost)
                if not ok:
                    return "积分扣除失败。"

            company.employee_count += hire
            await session.flush()

            # 立即更新周任务进度
            from services.quest_service import update_quest_progress
            await update_quest_progress(
                session, user.id, "employee_count",
                current_value=company.employee_count,
            )

    return f"成功雇佣 {hire} 名员工，花费 {total_cost:,} 积分。当前员工: {company.employee_count}/{max_emp}"


async def _exec_fire_employees(tg_id: int, count: int) -> str:
    from db.engine import async_session
    from services.user_service import get_user_by_tg_id
    from services.company_service import get_companies_by_owner

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                return "用户未注册。"
            companies = await get_companies_by_owner(session, user.id)
            if not companies:
                return "你还没有公司。"
            company = companies[0]
            if count <= 0:
                return "裁员数量必须大于0。"
            if count >= company.employee_count:
                return f"不能裁掉所有员工，当前 {company.employee_count} 人。"
            company.employee_count -= count
            await session.flush()

    return f"已裁员 {count} 人。当前员工: {company.employee_count}"


async def _exec_create_company(tg_id: int, name: str, company_type: str) -> str:
    from db.engine import async_session
    from services.user_service import get_or_create_user
    from services.company_service import create_company

    tg_name = ""
    async with async_session() as session:
        async with session.begin():
            user, _ = await get_or_create_user(session, tg_id, tg_name or str(tg_id))
            company, msg = await create_company(session, user, name, company_type)
    return msg


async def _exec_upgrade_company(tg_id: int) -> str:
    from db.engine import async_session
    from services.user_service import get_user_by_tg_id
    from services.company_service import get_companies_by_owner, upgrade_company

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                return "用户未注册。"
            companies = await get_companies_by_owner(session, user.id)
            if not companies:
                return "你还没有公司。"
            ok, msg = await upgrade_company(session, companies[0].id)
    return msg


async def _exec_view_quests(tg_id: int) -> str:
    from db.engine import async_session
    from services.user_service import get_user_by_tg_id
    from services.quest_service import get_or_create_weekly_tasks, current_week_key, load_quests

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            return "用户未注册。"
        tasks = await get_or_create_weekly_tasks(session, user.id)

    quest_defs = {q["id"]: q for q in load_quests()}
    lines = [f"本周任务 ({current_week_key()})"]
    for t in tasks:
        qd = quest_defs.get(t.quest_id, {})
        name = qd.get("name", t.quest_id)
        target = qd.get("target", "?")
        status = "已完成" if t.completed else f"{t.progress}/{target}"
        lines.append(f"  {'[v]' if t.completed else '[ ]'} {name}: {status}")
    return "\n".join(lines)


async def _exec_play_slot(tg_id: int) -> str:
    from services.slot_service import do_spin
    return await do_spin(tg_id)


async def _exec_do_roadshow(tg_id: int) -> str:
    from db.engine import async_session
    from services.user_service import get_user_by_tg_id
    from services.company_service import get_companies_by_owner
    from services.roadshow_service import do_roadshow

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                return "用户未注册。"
            companies = await get_companies_by_owner(session, user.id)
            if not companies:
                return "你还没有公司。"
            company = companies[0]
            ok, msg = await do_roadshow(session, company.id, user.id)
    return msg


async def _exec_create_product(tg_id: int, name: str, investment: int) -> str:
    from db.engine import async_session
    from services.user_service import get_user_by_tg_id
    from services.company_service import get_companies_by_owner, update_daily_revenue
    from services.product_service import create_product

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                return "用户未注册。"
            companies = await get_companies_by_owner(session, user.id)
            if not companies:
                return "你还没有公司。"
            company = companies[0]
            product, msg = await create_product(session, company.id, user.id, name, investment)
            if product:
                await update_daily_revenue(session, company.id)
    return msg


async def _exec_view_products(tg_id: int) -> str:
    from db.engine import async_session
    from services.user_service import get_user_by_tg_id
    from services.company_service import get_companies_by_owner
    from services.product_service import get_company_products

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            return "用户未注册。"
        companies = await get_companies_by_owner(session, user.id)
        if not companies:
            return "你还没有公司。"
        company = companies[0]
        products = await get_company_products(session, company.id)

    if not products:
        return f"「{company.name}」暂无产品。使用 /cp_new_product <名称> <投资额> 创建产品。"

    lines = [f"「{company.name}」产品列表:"]
    for p in products:
        lines.append(f"  - {p.name} v{p.version} | 日收入:{p.daily_income:,} | 品质:{p.quality}")
    return "\n".join(lines)


async def _exec_view_research(tg_id: int) -> str:
    from db.engine import async_session
    from services.user_service import get_user_by_tg_id
    from services.company_service import get_companies_by_owner
    from services.research_service import (
        get_completed_techs,
        get_in_progress_research,
        get_available_techs,
        sync_research_progress_if_due,
    )

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            return "用户未注册。"
        companies = await get_companies_by_owner(session, user.id)
        if not companies:
            return "你还没有公司。"
        company = companies[0]
        await sync_research_progress_if_due(session, company.id)
        completed = await get_completed_techs(session, company.id)
        in_progress = await get_in_progress_research(session, company.id)
        available = await get_available_techs(session, company.id)

    lines = [f"「{company.name}」科研状态:"]
    lines.append(f"已完成({len(completed)}项): {', '.join(completed) if completed else '无'}")

    if in_progress:
        lines.append("进行中:")
        for r in in_progress:
            lines.append(f"  - {r.tech_id} (进度: {r.progress}%)")

    if available:
        lines.append("可研发:")
        for t in available:
            dur_h = t.get("effective_duration_seconds", 3600) // 3600
            lines.append(f"  - {t['tech_id']}: {t.get('name', '')} | 费用:{t.get('research_cost', 0):,} | 时长:{dur_h}小时")
    else:
        lines.append("暂无可研发科技。")

    return "\n".join(lines)


async def _exec_start_research(tg_id: int, tech_id: str) -> str:
    from db.engine import async_session
    from services.user_service import get_user_by_tg_id
    from services.company_service import get_companies_by_owner
    from services.research_service import start_research

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                return "用户未注册。"
            companies = await get_companies_by_owner(session, user.id)
            if not companies:
                return "你还没有公司。"
            company = companies[0]
            ok, msg = await start_research(session, company.id, user.id, tech_id)
    return msg


async def _exec_daily_checkin(tg_id: int) -> str:
    from services.checkin_service import do_checkin
    ok, msg, _ = await do_checkin(tg_id)
    return msg


async def _exec_company_dividend(tg_id: int, amount: int) -> str:
    from db.engine import async_session
    from services.user_service import get_user_by_tg_id
    from services.company_service import get_companies_by_owner
    from services.dividend_service import distribute_dividends

    if amount <= 0:
        return "分红金额必须大于0。"

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                return "用户未注册。"
            companies = await get_companies_by_owner(session, user.id)
            if not companies:
                return "你还没有公司。"
            company = companies[0]
            if company.owner_id != user.id:
                return "只有公司老板才能分红。"
            if company.cp_points < amount:
                return f"公司积分不足，当前: {company.cp_points:,}，需要: {amount:,}"
            distributions = await distribute_dividends(session, company, amount)

    if not distributions:
        return "分红失败，可能没有股东或金额不足。"
    lines = [f"「{company.name}」分红 {amount:,} 积分:"]
    for uid, share in distributions:
        lines.append(f"  - 用户#{uid}: +{share:,}")
    return "\n".join(lines)


async def _exec_view_fund_log(tg_id: int, log_type: str) -> str:
    from db.engine import async_session
    from services.user_service import get_user_by_tg_id
    from services.company_service import get_companies_by_owner
    from services.fundlog_service import get_fund_logs

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            return "用户未注册。"

        if log_type == "company":
            companies = await get_companies_by_owner(session, user.id)
            if not companies:
                return "你还没有公司。"
            logs = await get_fund_logs("company", companies[0].id, limit=10)
            title = f"「{companies[0].name}」公司流水(最近10条)"
        else:
            logs = await get_fund_logs("user", user.id, limit=10)
            title = "个人流水(最近10条)"

    if not logs:
        return f"{title}: 暂无记录"
    lines = [title]
    for log in logs:
        sign = "+" if log["amount"] > 0 else ""
        lines.append(f"  {sign}{log['amount']:,} | {log.get('reason', '')} | 余额:{log.get('balance_after', 0):,}")
    return "\n".join(lines)


async def _exec_view_realestate(tg_id: int) -> str:
    from db.engine import async_session
    from services.user_service import get_user_by_tg_id
    from services.company_service import get_companies_by_owner
    from services.realestate_service import (
        get_company_estates,
        get_building_list,
        get_building_info,
    )

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            return "用户未注册。"
        companies = await get_companies_by_owner(session, user.id)
        if not companies:
            return "你还没有公司。"
        company = companies[0]
        estates = await get_company_estates(session, company.id)

    lines = [f"「{company.name}」地产:"]
    if estates:
        total_income = 0
        for e in estates:
            bld = get_building_info(e.building_type)
            name = bld["name"] if bld else e.building_type
            lines.append(f"  - {name} Lv.{e.level} | 日收入:{e.daily_dividend:,}")
            total_income += e.daily_dividend
        lines.append(f"总地产日收入: {total_income:,}")
    else:
        lines.append("  暂无地产。")

    lines.append("可购买:")
    for b in get_building_list():
        lines.append(f"  - {b['key']}: {b['name']} | 价格:{b['purchase_price']:,} | 日收入:{b['daily_dividend']:,}")
    return "\n".join(lines)


async def _exec_buy_realestate(tg_id: int, building_key: str) -> str:
    from db.engine import async_session
    from services.user_service import get_user_by_tg_id
    from services.company_service import get_companies_by_owner
    from services.realestate_service import purchase_building

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                return "用户未注册。"
            companies = await get_companies_by_owner(session, user.id)
            if not companies:
                return "你还没有公司。"
            company = companies[0]
            if company.owner_id != user.id:
                return "只有公司老板才能购买地产。"
            ok, msg = await purchase_building(session, company.id, tg_id, building_key)
    return msg


async def _exec_rename_company(tg_id: int, new_name: str) -> str:
    from db.engine import async_session
    from sqlalchemy import select
    from cache.redis_client import get_redis
    from services.user_service import get_user_by_tg_id
    from services.company_service import get_companies_by_owner, add_funds
    from db.models import Company
    from utils.validators import validate_name

    name_err = validate_name(new_name, min_len=2, max_len=16)
    if name_err:
        return f"名称无效: {name_err}"

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            return "用户未注册。"
        companies = await get_companies_by_owner(session, user.id)
        if not companies:
            return "你还没有公司。"
        company = companies[0]
        company_id = company.id

    r = await get_redis()
    cd_ttl = await r.ttl(f"rename_cd:{company_id}")
    if cd_ttl and cd_ttl > 0:
        return f"改名冷却中，剩余 {cd_ttl // 3600}小时{(cd_ttl % 3600) // 60}分钟"

    async with async_session() as session:
        async with session.begin():
            exists = await session.execute(select(Company).where(Company.name == new_name))
            if exists.scalar_one_or_none():
                return "该名称已被使用，请换一个。"
            company = await session.get(Company, company_id)
            if not company:
                return "公司不存在。"
            rename_cost = max(5000, int(company.cp_points * 0.05))
            ok = await add_funds(session, company_id, -rename_cost)
            if not ok:
                return f"公司资金不足，改名需要 {rename_cost:,} 积分"
            old_name = company.name
            company.name = new_name
            await session.flush()

    await r.setex(f"rename_cd:{company_id}", 86400, "1")
    await r.setex(f"rename_penalty:{company_id}", 3 * 86400, "1")
    return f"公司改名成功: 「{old_name}」→「{new_name}」，花费 {rename_cost:,} 积分"


async def execute_tool(name: str, args: dict, tg_id: int) -> str:
    """Dispatch tool call to the corresponding function."""
    try:
        if name == "get_my_profile":
            return await _exec_get_my_profile(tg_id)
        elif name == "get_my_company":
            return await _exec_get_my_company(tg_id)
        elif name == "list_companies":
            return await _exec_list_companies()
        elif name == "get_rankings":
            return await _exec_get_rankings(args.get("rank_type", "power"))
        elif name == "hire_employees":
            return await _exec_hire_employees(tg_id, args.get("count", 1))
        elif name == "fire_employees":
            return await _exec_fire_employees(tg_id, args.get("count", 1))
        elif name == "create_company":
            return await _exec_create_company(
                tg_id, args.get("name", ""), args.get("company_type", "tech")
            )
        elif name == "upgrade_company":
            return await _exec_upgrade_company(tg_id)
        elif name == "rename_company":
            return await _exec_rename_company(tg_id, args.get("new_name", ""))
        elif name == "create_product":
            return await _exec_create_product(tg_id, args.get("name", ""), args.get("investment", 0))
        elif name == "view_products":
            return await _exec_view_products(tg_id)
        elif name == "view_research":
            return await _exec_view_research(tg_id)
        elif name == "start_research":
            return await _exec_start_research(tg_id, args.get("tech_id", ""))
        elif name == "daily_checkin":
            return await _exec_daily_checkin(tg_id)
        elif name == "company_dividend":
            return await _exec_company_dividend(tg_id, args.get("amount", 0))
        elif name == "view_fund_log":
            return await _exec_view_fund_log(tg_id, args.get("log_type", "company"))
        elif name == "view_realestate":
            return await _exec_view_realestate(tg_id)
        elif name == "buy_realestate":
            return await _exec_buy_realestate(tg_id, args.get("building_key", ""))
        elif name == "view_quests":
            return await _exec_view_quests(tg_id)
        elif name == "play_slot":
            return await _exec_play_slot(tg_id)
        elif name == "do_roadshow":
            return await _exec_do_roadshow(tg_id)
        else:
            return f"未知工具: {name}"
    except Exception as exc:
        logger.warning("Tool %s execution failed: %s", name, exc, exc_info=True)
        return f"操作失败: {exc}"


# ── HTTP helpers ─────────────────────────────────────────────────────────

def _normalize_completion_url(base_url: str) -> str:
    candidate = (base_url or "").strip() or DEFAULT_AI_BASE_URL
    candidate = candidate.rstrip("/")
    if candidate.endswith("/chat/completions"):
        return candidate
    return f"{candidate}/chat/completions"


def _parse_extra_headers(raw: str) -> dict[str, str]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _build_headers() -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {settings.ai_api_key}",
        "Content-Type": "application/json",
    }
    headers.update(_parse_extra_headers(settings.ai_extra_headers_json))
    return headers


def _extract_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                txt = item.get("text")
                if isinstance(txt, str):
                    parts.append(txt.strip())
        return "\n".join(x for x in parts if x).strip()
    return str(content).strip()


# ── Core AI Call ─────────────────────────────────────────────────────────

async def _call_chat_api(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Make a single chat completion API call. Returns the raw response dict."""
    import httpx

    model_name = model or (settings.ai_model or "").strip() or "gpt-4o-mini"
    url = _normalize_completion_url(settings.ai_api_base_url)
    timeout = max(5, int(settings.ai_timeout_seconds))
    headers = _build_headers()

    payload: dict[str, Any] = {
        "model": model_name,
        "stream": False,
        "messages": messages,
        "temperature": max(0.0, min(2.0, float(settings.ai_temperature))),
        "top_p": max(0.0, min(1.0, float(settings.ai_top_p))),
        "max_tokens": max(64, int(settings.ai_max_tokens)),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    retry_times = max(0, int(settings.ai_max_retries))
    backoff = max(0.2, float(settings.ai_retry_backoff_seconds))

    data: dict[str, Any] = {}
    for attempt in range(retry_times + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            break
        except Exception:
            if attempt >= retry_times:
                raise
            await asyncio.sleep(backoff * (attempt + 1))

    return data


# ── Main Entry Points ────────────────────────────────────────────────────

async def ask_ai_smart(
    prompt: str,
    company_context: str,
    tg_id: int,
    *,
    history: list[dict] | None = None,
) -> tuple[str, str, str]:
    """Smart AI with intent routing and tool calling.

    Returns ``(content, response_type, model_name)`` where *response_type* is one of:
    ``"text"`` – a plain text reply (already wrapped in expandable blockquote HTML).

    *history* is an optional list of prior ``{"role": ..., "content": ...}``
    messages for multi-turn conversation.
    """
    if not settings.ai_enabled or not settings.ai_api_key.strip():
        return "AI 功能未启用。", "text", ""

    chat_model = (settings.ai_model or "").strip() or "gpt-4o-mini"

    # ── Company or general intent ──────────────────────────────────
    is_company = detect_company_intent(prompt)
    logger.debug("ask_ai_smart: prompt=%r, is_company=%s, tools=%s",
                 prompt[:80], is_company, "GAME_TOOLS" if is_company else "None")

    if is_company:
        base_system = (
            (settings.ai_chat_system_prompt or "").strip()
            or (settings.ai_system_prompt or "").strip()
            or GAME_SYSTEM_PROMPT
        )
        # Always append tool-calling instruction regardless of custom prompt
        tool_instruction = (
            "\n\n【重要】当用户请求执行操作时，你必须调用对应的工具，不要只给文字回复。"
            "例如“帮我签到”→调用daily_checkin，“帮我路演”→调用do_roadshow。"
        )
        system = base_system + tool_instruction
        user_content = (
            f"【提问者游戏数据】\n{company_context}\n\n"
            f"【用户问题】\n{prompt}\n\n"
            "要求：如果用户要求执行操作，直接调用对应工具；如果是咨询问题，给简短可执行建议。"
        )
        tools = GAME_TOOLS
    else:
        system = GENERAL_SYSTEM_PROMPT
        user_content = prompt
        tools = None

    messages: list[dict] = [
        {"role": "system", "content": system},
    ]
    # Insert conversation history for multi-turn dialogue
    if history:
        messages.extend(history)

    messages.append({"role": "user", "content": user_content})

    try:
        # ── Tool-calling loop (max 5 rounds) ──────────────────────────
        for _round in range(5):
            data = await _call_chat_api(messages, tools=tools)
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            tool_calls = message.get("tool_calls")

            logger.debug("ask_ai_smart round %d: tool_calls=%s, content_len=%d",
                         _round, bool(tool_calls),
                         len(message.get("content") or ""))

            # Fallback: some models (e.g. grok) emit tool calls as
            # <xtoolcall> XML in the text content instead of using the
            # standard tool_calls field.
            if not tool_calls:
                content = _extract_content_text(message.get("content", ""))
                xml_tcs = _parse_xml_tool_calls(content) if tools else None
                if xml_tcs:
                    # Execute XML-parsed tool calls
                    tool_results: list[str] = []
                    for tc in xml_tcs:
                        fn = tc.get("function") or {}
                        fn_name = fn.get("name", "")
                        try:
                            fn_args = json.loads(fn.get("arguments", "{}"))
                        except Exception:
                            fn_args = {}
                        result_str = await execute_tool(fn_name, fn_args, tg_id)
                        tool_results.append(f"[{fn_name}] {result_str}")

                    # Feed results back as a user message (safer for models
                    # that don't support the standard tool protocol).
                    results_text = "\n\n".join(tool_results)
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": (
                            f"以下是你请求的工具调用结果，请根据结果回答用户的问题：\n\n"
                            f"{results_text}"
                        ),
                    })
                    # Make a final call without tools so the model summarises
                    data = await _call_chat_api(messages, tools=None)
                    choice = (data.get("choices") or [{}])[0]
                    message = choice.get("message") or {}
                    final = _extract_content_text(message.get("content", ""))
                    return _wrap_blockquote(final or "AI 暂时没有给出有效回复。"), "text", chat_model

            if not tool_calls:
                # No tool calls – extract final text
                content = _extract_content_text(message.get("content", ""))
                return _wrap_blockquote(content or "AI 暂时没有给出有效回复。"), "text", chat_model

            # Append assistant message with tool_calls
            messages.append(message)

            # Execute each tool call
            for tc in tool_calls:
                fn = tc.get("function") or {}
                fn_name = fn.get("name", "")
                try:
                    fn_args = json.loads(fn.get("arguments", "{}"))
                except Exception:
                    fn_args = {}

                result_str = await execute_tool(fn_name, fn_args, tg_id)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": result_str,
                })

        # Exhausted loop – final attempt without tools
        data = await _call_chat_api(messages, tools=None)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = _extract_content_text(message.get("content", ""))
        return _wrap_blockquote(content or "AI 暂时没有给出有效回复。"), "text", chat_model

    except Exception as exc:
        logger.warning("AI smart call failed: %s", exc, exc_info=True)
        return _wrap_blockquote("AI 服务暂时不可用，请稍后再试。"), "text", chat_model


# ── HTML Formatting ──────────────────────────────────────────────────────

def _wrap_blockquote(text: str) -> str:
    """Wrap text in Telegram expandable blockquote HTML."""
    escaped = html_escape(text, quote=False)
    return f"<blockquote expandable>{escaped}</blockquote>"


# ── Legacy API (kept for ai_rd_service compatibility) ────────────────────

async def ask_ai_chat(prompt: str) -> str:
    """Legacy simple AI call without tool calling. Used by AI R&D handler."""
    if not settings.ai_enabled or not settings.ai_api_key.strip():
        return "AI 功能未启用。"

    system = (
        (settings.ai_chat_system_prompt or "").strip()
        or (settings.ai_system_prompt or "").strip()
        or GAME_SYSTEM_PROMPT
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]

    try:
        data = await _call_chat_api(messages)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = _extract_content_text(message.get("content", ""))
        return content or "AI 暂时没有给出有效回复。"
    except Exception as exc:
        logger.warning("AI chat call failed: %s", exc)
        return "AI 服务暂时不可用，请稍后再试。"
