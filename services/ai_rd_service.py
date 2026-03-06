"""产品迭代系统 — 概率提升收入 + AI生成趣味段子。

简化后的玩法：
1. 选择产品 → 确认花费 → 概率随机提升收入（只涨不跌）
2. AI生成简短有趣的产品升级段子（可选，AI未配置时用内置段子）
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.models import Product
from services.user_service import add_self_points, add_reputation

logger = logging.getLogger(__name__)

# ── 迭代费用 ──────────────────────────────────────────
RD_COST_BASE = 500          # 基础费用
RD_COST_SCALE = 1.15        # 每个版本费用倍率

# ── 收入提升参数 ──────────────────────────────────────
BASE_RD_INCOME = 300
DIMINISH_THRESHOLD = 50_000
DIMINISH_RATE = 0.6

# ── 概率档位（权重，倍率，品质加成，声望加成，段子提示词） ──
TIERS = [
    # (weight, multiplier, quality_delta, rep_bonus, tier_key, emoji, label)
    (40, 1.0,  1, 1, "small",    "📦", "小幅改进"),
    (30, 1.8,  2, 2, "medium",   "📈", "稳步提升"),
    (20, 3.0,  3, 4, "large",    "🌟", "重大突破"),
    (10, 5.0,  4, 8, "critical", "🏆", "创新飞跃"),
]
_TIER_WEIGHTS = [t[0] for t in TIERS]

# ── 内置段子（AI不可用时的后备） ──────────────────────
_BUILTIN_BLURBS: dict[str, list[str]] = {
    "small": [
        "工程师连夜修了3个bug，结果又写了5个新的。但用户体验确实好了一点。",
        "UI改了个按钮颜色，产品经理说「焕然一新」。嗯…确实新了。",
        "加了个loading动画，用户觉得变快了。心理学的胜利！",
        "修复了一个「不是bug是feature」的bug。这次真是bug。",
        "优化了数据库查询，快了0.3秒。用户无感，DBA狂喜。",
    ],
    "medium": [
        "终于把那个TODO注释变成了真代码！上线后日活涨了。",
        "产品经理灵光一闪，加了个分享功能。病毒传播，用户翻倍。",
        "重构了核心模块，代码从意大利面变成了瑞士手表。",
        "A/B测试显示新方案完胜，数据不会骗人（这次没有p-hacking）。",
        "砍掉了3个没人用的功能，产品反而更好用了。少即是多。",
    ],
    "large": [
        "发布会上CEO亲自演示新功能，全场起立鼓掌！（内测只崩溃了两次）",
        "竞品看完发布会连夜开会，据说有人拍了桌子。",
        "用户在社交媒体自发传播，市场部第一次不用花钱买量。",
        "技术团队解锁了新算法，处理速度快了10倍。摩尔定律在颤抖。",
        "拿下行业大奖，奖杯放前台，每个来访者都会多看两眼。",
    ],
    "critical": [
        "这个版本注定被写入行业教科书。当年的iPhone时刻。",
        "投资人排队想加注，估值一夜翻倍。但CEO说「我们不缺钱」。",
        "用户写了篇5000字体验报告，标题是：「这才是未来」。",
        "上线当天服务器差点被挤爆，运维含泪扩容到半夜。",
        "竞品CEO在内部信里承认：他们领先了我们一个时代。",
    ],
}

DEFAULT_AI_BASE_URL = "https://api.openai.com/v1"


def get_rd_cost(product: Product) -> int:
    """计算迭代费用，随版本递增。"""
    return max(RD_COST_BASE, int(RD_COST_BASE * (RD_COST_SCALE ** (product.version - 1))))


def _roll_tier() -> tuple[float, int, int, str, str, str]:
    """随机抽取档位，返回 (multiplier, quality_delta, rep_bonus, tier_key, emoji, label)。"""
    tier = random.choices(TIERS, weights=_TIER_WEIGHTS, k=1)[0]
    return tier[1], tier[2], tier[3], tier[4], tier[5], tier[6]


def _get_fallback_blurb(tier_key: str) -> str:
    """内置随机段子。"""
    blurbs = _BUILTIN_BLURBS.get(tier_key, _BUILTIN_BLURBS["small"])
    return random.choice(blurbs)


async def generate_upgrade_blurb(
    product_name: str,
    income_increase: int,
    tier_label: str,
) -> str:
    """调用AI生成一句简短有趣的产品升级段子。AI不可用时返回内置段子。"""
    # 先决定 tier_key 用于 fallback
    tier_key = "small"
    for t in TIERS:
        if t[6] == tier_label:
            tier_key = t[4]
            break

    if not settings.ai_enabled or not settings.ai_api_key.strip():
        return _get_fallback_blurb(tier_key)

    try:
        import httpx

        prompt = (
            f"产品「{product_name}」刚完成了一次迭代升级，"
            f"效果是【{tier_label}】，日收入增加了{income_increase}积分。\n"
            "请用一两句话写一个简短有趣的产品升级段子，要幽默、有画面感。"
            "不要用markdown格式，只输出段子文本。最多50字。"
        )

        url = _normalize_completion_url(settings.ai_api_base_url)
        headers = {
            "Authorization": f"Bearer {settings.ai_api_key}",
            "Content-Type": "application/json",
        }
        headers.update(_parse_extra_headers(settings.ai_extra_headers_json))

        payload = {
            "model": (settings.ai_model or "").strip() or "gpt-4o-mini",
            "stream": False,
            "messages": [
                {"role": "system", "content": "你是游戏里的产品段子手，用简短幽默的方式描述产品升级的趣事。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 1.0,
            "max_tokens": 120,
        }

        timeout = max(5, int(settings.ai_timeout_seconds))
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = _extract_content_text(message.get("content", ""))
        if content and len(content.strip()) > 2:
            return content.strip()[:200]

    except Exception as e:
        logger.debug("AI blurb generation failed, using fallback: %s", e)

    return _get_fallback_blurb(tier_key)


async def quick_iterate(
    session: AsyncSession,
    product_id: int,
    owner_user_id: int,
) -> tuple[bool, str, int, str]:
    """执行一次概率迭代，返回 (success, message, income_increase, tier_key)。

    收入只增不减。
    """
    from services.product_service import MAX_PRODUCT_DAILY_INCOME

    product = await session.get(Product, product_id)
    if product is None:
        return False, "产品不存在", 0, ""

    if product.daily_income >= MAX_PRODUCT_DAILY_INCOME:
        return False, "产品日收入已达上限", 0, ""

    # 抽取档位
    multiplier, quality_delta, rep_bonus, tier_key, emoji, tier_label = _roll_tier()

    # 计算收入提升
    raw_boost = int(BASE_RD_INCOME * multiplier)

    # 高收入递减
    if product.daily_income > DIMINISH_THRESHOLD:
        ratio = product.daily_income / DIMINISH_THRESHOLD
        diminish = max(0.05, 1.0 / (1 + DIMINISH_RATE * math.log(ratio)))
        raw_boost = max(1, int(raw_boost * diminish))

    income_increase = min(raw_boost, MAX_PRODUCT_DAILY_INCOME - product.daily_income)
    income_increase = max(1, income_increase)  # 至少+1

    # 应用
    product.daily_income += income_increase
    product.quality = min(100, product.quality + quality_delta)
    product.version += 1
    await session.flush()

    from services.company_service import update_daily_revenue
    await update_daily_revenue(session, product.company_id)

    await add_reputation(session, owner_user_id, rep_bonus)
    await add_self_points(owner_user_id, max(1, rep_bonus * 2), session=session)

    msg = (
        f"{emoji} {tier_label}！\n"
        f"产品「{product.name}」v{product.version}\n"
        f"日收入 +{income_increase} → {product.daily_income}"
    )

    return True, msg, income_increase, tier_key


# ── HTTP helpers (复用于AI段子生成) ────────────────────

def _normalize_completion_url(base_url: str) -> str:
    candidate = (base_url or "").strip() or DEFAULT_AI_BASE_URL
    candidate = candidate.rstrip("/")
    if candidate.endswith("/chat/completions"):
        return candidate
    return f"{candidate}/chat/completions"


def _parse_extra_headers(raw_headers_json: str) -> dict[str, str]:
    raw = (raw_headers_json or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return {}
        return {str(k): str(v) for k, v in parsed.items()}
    except Exception:
        return {}


def _extract_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                txt = item.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
        return "\n".join(parts)
    return str(content)
