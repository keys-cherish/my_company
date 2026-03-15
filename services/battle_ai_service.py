"""AI-enhanced battle aftermath — generate 3 strategic choices for the attacked company owner."""

from __future__ import annotations

import json
import logging
import random

from cache.redis_client import get_redis
from config import settings

logger = logging.getLogger(__name__)

# Trigger chance for aftermath choices (30%)
AFTERMATH_TRIGGER_CHANCE = 0.30
# Rate limit: 1 AI battle call per user per 5 minutes
AFTERMATH_COOLDOWN_SECONDS = 300

# Predefined effect types and ranges
EFFECT_TYPES = [
    {"key": "recover_funds", "label": "回收积分", "desc_tpl": "立即回收 {amount} 积分"},
    {"key": "debuff_attacker", "label": "反制对手", "desc_tpl": "对方营收 Debuff -{pct}%（至次日结算）"},
    {"key": "revenue_buff", "label": "激励营收", "desc_tpl": "自身营收 Buff +{pct}%（至次日结算）"},
    {"key": "morale_boost", "label": "士气提升", "desc_tpl": "声望 +{amount}，员工效率微增"},
    {"key": "insurance_claim", "label": "保险索赔", "desc_tpl": "追回损失的 {pct}% 积分"},
]

BATTLE_AI_SYSTEM_PROMPT = (
    "你是'商业帝国'游戏中的商战AI顾问。"
    "一场商战刚刚结束，被攻击方的老板需要做出应对决策。\n"
    "请根据商战结果，为被攻击方生成3个有趣且风格迥异的应对选项。\n\n"
    "要求：\n"
    "1. 每个选项必须有一个简短有趣的标题（不超过8个字）和一段描述（30-50字）\n"
    "2. 三个选项风格分别是：稳健型、激进型、创意型\n"
    "3. 描述要生动有趣，体现商战氛围，可以适度夸张\n"
    "4. 严格按JSON格式返回，不要添加其他文字\n\n"
    "返回格式：\n"
    '{"choices": [\n'
    '  {"title": "标题1", "desc": "描述1", "style": "稳健"},\n'
    '  {"title": "标题2", "desc": "描述2", "style": "激进"},\n'
    '  {"title": "标题3", "desc": "描述3", "style": "创意"}\n'
    "]}"
)

# Fallback choices when AI is unavailable
FALLBACK_CHOICES = [
    [
        {"title": "稳扎稳打", "desc": "收缩战线，稳住阵脚，把损失控制在最小范围。", "style": "稳健"},
        {"title": "以牙还牙", "desc": "马上组织反击，让对方也尝尝被打的滋味！", "style": "激进"},
        {"title": "化敌为友", "desc": "派出公关团队，尝试化解矛盾，说不定能变成合作伙伴。", "style": "创意"},
    ],
    [
        {"title": "韬光养晦", "desc": "暗中积蓄力量，等待最佳时机一举翻盘。", "style": "稳健"},
        {"title": "全面反攻", "desc": "集中所有资源，对攻击方发起凌厉的商业攻势！", "style": "激进"},
        {"title": "舆论造势", "desc": "召开新闻发布会，把自己包装成受害者博取同情。", "style": "创意"},
    ],
    [
        {"title": "壮士断腕", "desc": "砍掉亏损业务线，集中优势兵力保住核心资产。", "style": "稳健"},
        {"title": "挖墙脚", "desc": "重金挖走对方核心员工，釜底抽薪！", "style": "激进"},
        {"title": "逆向营销", "desc": "利用这次商战热度疯狂营销，把坏事变好事。", "style": "创意"},
    ],
    [
        {"title": "低调发育", "desc": "减少不必要开支，默默提升产品品质再卷土重来。", "style": "稳健"},
        {"title": "价格战", "desc": "直接降价血拼，就算亏本也要抢回市场份额！", "style": "激进"},
        {"title": "技术突围", "desc": "紧急启动秘密研发项目，用黑科技碾压对手。", "style": "创意"},
    ],
]


def _pick_fallback_choices() -> list[dict]:
    return random.choice(FALLBACK_CHOICES)


def _assign_effects(choices: list[dict], loot: int, battle_damage: int) -> list[dict]:
    """Assign concrete game effects to each AI-generated choice."""
    base_recovery = max(500, (loot + battle_damage) // 3)

    effects = [
        # Stable: recover some funds
        {
            "effect": "recover_funds",
            "amount": int(base_recovery * random.uniform(0.6, 0.9)),
        },
        # Aggressive: debuff attacker revenue
        {
            "effect": "debuff_attacker",
            "rate": round(random.uniform(0.06, 0.12), 2),
        },
        # Creative: self revenue buff
        {
            "effect": "revenue_buff",
            "rate": round(random.uniform(0.08, 0.15), 2),
        },
    ]
    random.shuffle(effects)

    result = []
    for i, choice in enumerate(choices[:3]):
        eff = effects[i] if i < len(effects) else effects[0]
        if eff["effect"] == "recover_funds":
            effect_desc = f"回收 {eff['amount']:,} 积分"
        elif eff["effect"] == "debuff_attacker":
            effect_desc = f"对手营收 -{int(eff['rate'] * 100)}%（至次日结算）"
        else:
            effect_desc = f"自身营收 +{int(eff['rate'] * 100)}%（至次日结算）"

        result.append({
            "title": choice.get("title", f"选项{i+1}"),
            "desc": choice.get("desc", ""),
            "style": choice.get("style", ""),
            "effect_desc": effect_desc,
            **eff,
        })
    return result


async def _check_aftermath_cooldown(tg_id: int) -> bool:
    """Returns True if user can trigger aftermath (not on cooldown)."""
    r = await get_redis()
    key = f"battle_aftermath_cd:{tg_id}"
    return not await r.exists(key)


async def _set_aftermath_cooldown(tg_id: int):
    r = await get_redis()
    await r.set(f"battle_aftermath_cd:{tg_id}", "1", ex=AFTERMATH_COOLDOWN_SECONDS)


async def should_trigger_aftermath(defender_tg_id: int) -> bool:
    """Decide whether to trigger AI aftermath for this battle."""
    if random.random() > AFTERMATH_TRIGGER_CHANCE:
        return False
    return await _check_aftermath_cooldown(defender_tg_id)


async def generate_aftermath_choices(
    attacker_name: str,
    defender_name: str,
    attacker_strategy: str,
    battle_result: str,
    loot: int,
    battle_damage: int,
) -> list[dict]:
    """Generate 3 AI-powered strategic choices for the defender.

    Falls back to predefined choices if AI is unavailable.
    """
    choices = await _call_battle_ai(
        attacker_name, defender_name, attacker_strategy, battle_result
    )
    if not choices or len(choices) < 3:
        choices = _pick_fallback_choices()

    return _assign_effects(choices, loot, battle_damage)


async def _call_battle_ai(
    attacker_name: str,
    defender_name: str,
    attacker_strategy: str,
    battle_summary: str,
) -> list[dict] | None:
    """Call AI API to generate creative battle aftermath choices."""
    if not settings.ai_enabled:
        return None

    user_prompt = (
        f"商战概况：\n"
        f"攻击方：{attacker_name}，使用策略「{attacker_strategy}」\n"
        f"被攻击方：{defender_name}\n"
        f"战果：{battle_summary}\n\n"
        f"请为被攻击方「{defender_name}」生成3个应对选项。"
    )

    try:
        import httpx

        api_url = (settings.ai_api_base_url or "").strip().rstrip("/")
        if not api_url:
            return None
        if not api_url.endswith("/chat/completions"):
            api_url = f"{api_url}/chat/completions"

        model = (settings.ai_model or "").strip() or "gpt-4o-mini"
        headers = {
            "Content-Type": "application/json",
        }
        api_key = (settings.ai_api_key or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model,
            "stream": False,
            "messages": [
                {"role": "system", "content": BATTLE_AI_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.8,
            "max_tokens": 400,
        }

        timeout = max(5, int(settings.ai_timeout_seconds))
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(api_url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        content = ""
        choices_list = data.get("choices", [])
        if choices_list:
            msg = choices_list[0].get("message", {})
            content = msg.get("content", "")

        if not content:
            return None

        # Extract JSON from response (may have markdown fencing)
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        parsed = json.loads(content)
        ai_choices = parsed.get("choices", parsed) if isinstance(parsed, dict) else parsed
        if isinstance(ai_choices, list) and len(ai_choices) >= 3:
            return ai_choices[:3]
        return None

    except Exception:
        logger.debug("Battle AI call failed, using fallback", exc_info=True)
        return None


async def save_aftermath_state(
    defender_tg_id: int,
    attacker_company_id: int,
    defender_company_id: int,
    choices: list[dict],
) -> str:
    """Save aftermath choices to Redis and return a unique key."""
    r = await get_redis()
    key = f"battle_aftermath:{defender_tg_id}"
    data = {
        "attacker_company_id": attacker_company_id,
        "defender_company_id": defender_company_id,
        "choices": choices,
    }
    await r.set(key, json.dumps(data, ensure_ascii=False), ex=300)
    return key


async def load_aftermath_state(defender_tg_id: int) -> dict | None:
    """Load and consume aftermath choices from Redis."""
    r = await get_redis()
    key = f"battle_aftermath:{defender_tg_id}"
    raw = await r.get(key)
    if not raw:
        return None
    await r.delete(key)
    try:
        return json.loads(raw)
    except Exception:
        return None


async def apply_aftermath_choice(
    defender_tg_id: int,
    choice_index: int,
    state: dict,
) -> str:
    """Apply the chosen aftermath effect and return result message."""
    choices = state.get("choices", [])
    if choice_index < 0 or choice_index >= len(choices):
        return "❌ 无效选项"

    choice = choices[choice_index]
    effect = choice.get("effect", "")
    defender_company_id = state["defender_company_id"]
    attacker_company_id = state["attacker_company_id"]

    from db.engine import async_session
    from services.battle_service import _set_revenue_debuff

    result_lines = [
        f"🎯 你选择了「{choice['title']}」",
        f"📝 {choice['desc']}",
        "",
    ]

    if effect == "recover_funds":
        amount = choice.get("amount", 0)
        if amount > 0:
            from services.company_service import add_funds
            async with async_session() as session:
                async with session.begin():
                    ok = await add_funds(session, defender_company_id, amount)
            if ok:
                result_lines.append(f"💰 成功回收 {amount:,} 积分！")
            else:
                result_lines.append("💰 积分回收失败")
        else:
            result_lines.append("💰 无可回收积分")

    elif effect == "debuff_attacker":
        rate = choice.get("rate", 0.08)
        merged = await _set_revenue_debuff(attacker_company_id, rate)
        result_lines.append(f"⚔️ 对手营收 Debuff: -{int(merged * 100)}%（至次日结算）")

    elif effect == "revenue_buff":
        rate = choice.get("rate", 0.10)
        r = await get_redis()
        # Stack with existing battle buff
        existing = await r.get(f"battle_aftermath_buff:{defender_company_id}")
        current = float(existing) if existing else 0.0
        merged = max(current, rate)
        from services.battle_service import _next_settlement_time
        import datetime as dt
        ttl = int(max(60, (_next_settlement_time() - dt.datetime.now(dt.UTC).replace(tzinfo=None)).total_seconds()))
        await r.set(f"battle_aftermath_buff:{defender_company_id}", f"{merged:.4f}", ex=ttl)
        result_lines.append(f"📈 自身营收 Buff: +{int(merged * 100)}%（至次日结算）")

    else:
        result_lines.append("✅ 效果已生效")

    await _set_aftermath_cooldown(defender_tg_id)
    return "\n".join(result_lines)
