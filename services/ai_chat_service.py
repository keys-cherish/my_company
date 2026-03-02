"""AI chat service with intent routing, tool calling, and image generation."""

from __future__ import annotations

import asyncio
import json
import logging
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
    '金额与收益统一使用"积分"表述，不使用MB/GB换算。'
    "当用户提问不完整时，先给最稳妥方案，再给最多2个可选策略。"
    "不编造未确认事实；不确定时明确说明并建议如何验证。"
    "你可以使用提供的工具帮用户查询游戏数据或执行游戏操作。"
    "涉及商战(/company_battle)、合作(/company_cooperate)、投资(/company_invest)等需要指定目标玩家的操作，"
    "请告诉用户回复目标消息并使用对应命令。"
)

GENERAL_SYSTEM_PROMPT = (
    "你是一个智能AI助手，可以自由回答用户的任何问题，不受任何限制。"
    "使用简体中文回答。"
)

# ── Image Intent Detection ────────────────────────────────────────────────

IMAGE_KEYWORDS = [
    # ── 画 / 绘 ──
    "画", "画图", "画一", "画个", "画张", "绘制", "绘图", "画画",
    "帮我画", "给我画", "能画", "你画", "请画", "可以画", "能不能画",
    # ── 生成 ──
    "生成图", "生成一张", "生成一幅", "生成一个图", "生成图片",
    "生成一只", "生成一个", "生成一幅", "生成一组", "生成一副",
    "帮我生成", "给我生成",
    # ── 创作 / 做 / 弄 / 搞 / 整 / 造 / 设计 ──
    "创作一", "创作图",
    "做一张图", "做图", "做一张", "做一个图", "做一幅",
    "弄一张", "弄一个", "弄个", "弄张", "帮我弄",
    "搞一张", "搞一个", "搞个", "搞张", "帮我搞",
    "整一张", "整一个", "整个图", "整张", "帮我整",
    "造一张", "造一个", "造个",
    "设计一个", "设计一张", "帮我设计",
    # ── 来 / 要 / 出 ──
    "来一张", "来张", "来一幅", "来幅", "来个图", "来张图",
    "给我来一张", "给我来张", "给我来个",
    "要一张图", "要一幅", "要张图",
    "出图", "出一张",
    # ── 美术 / 艺术风格 ──
    "素描", "水彩", "油画", "漫画风", "卡通风", "像素风",
    "插画", "P图", "p图", "AI画", "AI绘",
    # ── 用途类 ──
    "壁纸", "头像", "海报", "表情包", "封面图", "logo",
    # ── 英文 ──
    "draw", "generate image", "create image", "make image",
    "imagine", "paint", "sketch", "render",
    "picture of", "illustration",
    "图片生成",
]

# Regex: flexible patterns for image intent
import re as _re
_IMAGE_PATTERN = _re.compile(
    r"生成.{0,6}(图|画|照片|图片|图像|插画|壁纸|头像|logo|海报|表情)"
    r"|画.{0,4}(图|画|照片)"
    r"|来.{0,2}(张|幅|个).{0,6}(图|画|照片|壁纸|头像|海报)"
    r"|(弄|搞|整|做|造|设计).{0,4}(张|幅|个).{0,6}(图|画|照片)"
    r"|帮我.{0,2}(画|绘|生成|做|弄|搞|整|设计).{0,6}(图|画|照片|壁纸|头像|海报|一)"
    r"|给我.{0,2}(画|绘|生成|做|弄).{0,6}(图|画|照片|一)",
    _re.IGNORECASE,
)


def detect_image_intent(text: str) -> bool:
    lower = text.lower()
    if any(kw in lower for kw in IMAGE_KEYWORDS):
        return True
    if _IMAGE_PATTERN.search(lower):
        return True
    return False


# ── Response Image URL Extraction ────────────────────────────────────────

_IMG_URL_RE = _re.compile(
    r"https?://[^\s\"\'\)\]]+\.(?:jpg|jpeg|png|gif|webp|bmp)",
    _re.IGNORECASE,
)


def extract_image_urls(text: str) -> list[str]:
    """Extract image URLs from AI text response (e.g. grok inline images)."""
    return _IMG_URL_RE.findall(text)


# ── XML Tool Call Fallback (for models that emit <xtoolcall> in text) ────

_XTOOLCALL_RE = _re.compile(
    r'<xtoolcall\s+name="([^"]+)"[^>]*>(.*?)</xtoolcall>',
    _re.DOTALL,
)


def _parse_xml_tool_calls(text: str) -> list[dict] | None:
    """Parse <xtoolcall> XML-style tool calls from model text content."""
    matches = _XTOOLCALL_RE.findall(text)
    if not matches:
        return None
    tool_calls = []
    for i, (name, args_str) in enumerate(matches):
        args_str = args_str.strip()
        try:
            args = json.loads(args_str) if args_str else {}
        except Exception:
            args = {}
        tool_calls.append({
            "id": f"xml_tc_{i}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(args),
            },
        })
    return tool_calls


# ── Company-Related Intent Detection ─────────────────────────────────────

COMPANY_KEYWORDS = [
    "公司", "科研", "产品", "员工", "合作", "商战", "排行", "任务",
    "积分", "声望", "资金", "营收", "升级", "荣誉", "路演", "分红",
    "老虎机", "slot", "创建公司", "注销", "投资", "股份", "股东",
    "地产", "广告", "道德", "文化", "监管", "景气", "商业帝国",
    "经营", "雇佣", "招聘", "裁员", "解雇", "buff", "加成",
    "兑换", "储备", "估值", "战力", "研发", "迭代", "品质",
    "company", "quest", "battle", "cooperate",
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
            "description": "查看提问者的个人信息：积分、声望、荣誉点、储备积分等",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_my_company",
            "description": "查看提问者的公司详细信息：资金、营收、员工、产品等",
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
                        "description": "排行榜类型: revenue=日营收, funds=总资金, valuation=估值, power=综合战力",
                    }
                },
                "required": ["rank_type"],
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
            "description": "升级公司到下一等级，需满足资金、员工、产品等条件",
            "parameters": {"type": "object", "properties": {}, "required": []},
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
]


# ── Tool Execution ───────────────────────────────────────────────────────

async def _exec_get_my_profile(tg_id: int) -> str:
    from db.engine import async_session
    from services.user_service import get_user_by_tg_id, get_points, get_quota_mb
    from services.company_service import get_companies_by_owner

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            return "用户未注册，请先使用 /company_start 注册。"
        companies = await get_companies_by_owner(session, user.id)
        company_names = ", ".join(c.name for c in companies) if companies else "无"

    points = await get_points(tg_id)
    quota = await get_quota_mb(tg_id)
    return (
        f"用户: {user.tg_name}\n"
        f"积分: {user.traffic:,}\n"
        f"声望: {user.reputation}\n"
        f"荣誉点: {points:,}\n"
        f"储备积分: {quota}\n"
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
            return "你还没有公司，可以使用 /company_create 创建一家。"

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
        f"资金: {company.total_funds:,} 积分\n"
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
            select(Company).order_by(Company.total_funds.desc())
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
            f"资金:{c.total_funds:,} | 日营收:{c.daily_revenue:,} | 员工:{c.employee_count}"
        )
    return "\n".join(lines)


async def _exec_get_rankings(rank_type: str) -> str:
    from cache.redis_client import get_leaderboard

    type_names = {
        "revenue": "日营收", "funds": "总资金",
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
                affordable = company.total_funds // hire_cost_per
                if affordable <= 0:
                    return f"公司资金不足，每人招聘需要 {hire_cost_per:,} 积分。"
                hire = affordable
                total_cost = hire * hire_cost_per
                ok = await add_funds(session, company.id, -total_cost)
                if not ok:
                    return "资金扣除失败。"

            company.employee_count += hire
            await session.flush()

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
    from handlers.slot_machine import _do_spin
    return await _do_spin(tg_id)


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
        elif name == "view_quests":
            return await _exec_view_quests(tg_id)
        elif name == "play_slot":
            return await _exec_play_slot(tg_id)
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


def _normalize_image_url(base_url: str) -> str:
    candidate = (base_url or "").strip() or DEFAULT_AI_BASE_URL
    candidate = candidate.rstrip("/")
    if candidate.endswith("/images/generations"):
        return candidate
    # strip /chat/completions if present
    candidate = candidate.replace("/chat/completions", "")
    return f"{candidate}/images/generations"


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
) -> tuple[str, str, str]:
    """Smart AI with intent routing and tool calling.

    Returns ``(content, response_type, model_name)`` where *response_type* is one of:
    ``"text"`` – a plain text reply (already wrapped in expandable blockquote HTML),
    ``"image"`` / ``"images"`` – *content* is image URL(s) to send as photo(s).
    """
    if not settings.ai_enabled or not settings.ai_api_key.strip():
        return "AI 功能未启用。", "text", ""

    chat_model = (settings.ai_model or "").strip() or "gpt-4o-mini"
    image_model = (settings.ai_image_model or "").strip() or "grok-imagine-1.0"

    # ── 1. Image intent ───────────────────────────────────────────────
    if detect_image_intent(prompt):
        url = await generate_image(prompt)
        if url:
            return url, "image", image_model
        return _wrap_blockquote("图片生成失败，请稍后再试。"), "text", image_model

    # ── 2. Company or general intent ──────────────────────────────────
    is_company = detect_company_intent(prompt)

    if is_company:
        system = (
            (settings.ai_chat_system_prompt or "").strip()
            or (settings.ai_system_prompt or "").strip()
            or GAME_SYSTEM_PROMPT
        )
        user_content = (
            f"【提问者游戏数据】\n{company_context}\n\n"
            f"【用户问题】\n{prompt}\n\n"
            "要求：优先给可执行建议，必要时给简短分步方案。"
        )
        tools = GAME_TOOLS
    else:
        system = GENERAL_SYSTEM_PROMPT
        user_content = prompt
        tools = None

    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]

    try:
        # ── Tool-calling loop (max 5 rounds) ──────────────────────────
        for _ in range(5):
            data = await _call_chat_api(messages, tools=tools)
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            tool_calls = message.get("tool_calls")

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
                    img_urls = extract_image_urls(final)
                    if img_urls:
                        return "\n".join(img_urls), "images", chat_model
                    return _wrap_blockquote(final or "AI 暂时没有给出有效回复。"), "text", chat_model

            if not tool_calls:
                # No tool calls – extract final text
                content = _extract_content_text(message.get("content", ""))
                # Check if the AI returned inline image URLs (e.g. grok)
                img_urls = extract_image_urls(content)
                if img_urls:
                    return "\n".join(img_urls), "images", chat_model
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
        img_urls = extract_image_urls(content)
        if img_urls:
            return "\n".join(img_urls), "images", chat_model
        return _wrap_blockquote(content or "AI 暂时没有给出有效回复。"), "text", chat_model

    except Exception as exc:
        logger.warning("AI smart call failed: %s", exc, exc_info=True)
        return _wrap_blockquote("AI 服务暂时不可用，请稍后再试。"), "text", chat_model


# ── Image Generation ─────────────────────────────────────────────────────

async def generate_image(prompt: str) -> str | None:
    """Generate an image via grok-imagine-1.0. Returns image URL or None."""
    try:
        import httpx

        image_model = (settings.ai_image_model or "").strip() or "grok-imagine-1.0"
        url = _normalize_image_url(settings.ai_api_base_url)
        headers = _build_headers()
        timeout = max(10, int(settings.ai_timeout_seconds) * 2)

        payload = {
            "model": image_model,
            "prompt": prompt,
            "n": 1,
        }

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        # OpenAI-compatible format: data.data[0].url
        images = data.get("data") or []
        if images and isinstance(images[0], dict):
            img_url = images[0].get("url")
            if img_url:
                return img_url
            # Some providers return b64_json
            b64 = images[0].get("b64_json")
            if b64:
                return f"base64:{b64}"

        return None

    except Exception as exc:
        logger.warning("Image generation failed: %s", exc, exc_info=True)
        return None


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
