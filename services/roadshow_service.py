"""Roadshow system: daily-limited event with dramatic narrative outcomes."""

from __future__ import annotations

import datetime as dt
import random
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from cache.redis_client import get_redis
from config import settings
from db.models import Roadshow
from services.company_service import add_funds
from services.user_service import add_self_points, add_reputation
from utils.formatters import fmt_traffic

ROADSHOW_TYPES = ["技术展会", "投资峰会", "媒体发布会", "行业论坛"]
ROADSHOW_DAILY_KEY_PREFIX = "roadshow_daily"
ROADSHOW_PENALTY_KEY_PREFIX = "roadshow_penalty"

REWARD_TABLE = [
    {"weight": 30, "type": "cp_points", "min": 200, "max": 800},
    {"weight": 25, "type": "reputation", "min": 3, "max": 15},
    {"weight": 20, "type": "cp_points", "min": 500, "max": 2000},
    {"weight": 15, "type": "self_points", "min": 10, "max": 50},
    {"weight": 10, "type": "jackpot", "min": 2000, "max": 5000},
]

STORIES_TRAFFIC = [
    "台下两位对手当场抬价抢人，原本冷清的会场突然像拍卖厅。",
    "演示中设备几乎失控，但你硬把失误说成“反脆弱设计”，全场居然买账。",
    "你刚讲完第三页，后排投资人直接把意向书拍到桌上，要求今天就签。",
    "主持人试图控场，结果观众直接围上来问估值和交付节奏。",
]

STORIES_REPUTATION = [
    "争议发言把话题点燃，媒体争吵了一夜，但你的名字冲上了行业热榜。",
    "你和评委当场互怼，剪辑版在圈内疯传，品牌声量暴涨。",
    "一段高压问答把气氛拉满，虽然火药味十足，但观众记住了你。",
    "你当众拆解竞品路线，现场一片哗然，评论区却一致叫好。",
]

STORIES_POINTS = [
    "现场反应一般，但你拿到了一堆高价值反馈，少走了不少弯路。",
    "没有爆单，也没翻车，你收获的是可落地的执行建议。",
    "台下问题很刁钻，但这些质疑刚好补齐了你方案里的短板。",
    "这次像打磨会，不热闹，但每条意见都值钱。",
]

STORIES_JACKPOT = [
    "会后电梯口被堵，三家机构抢着约下一轮，报价一路抬升。",
    "你刚说完“最后一页”，顶级基金合伙人当场说：现在就推进DD。",
    "对手准备的发布会被你截胡，媒体镜头全转向你这边。",
    "原本只是例行路演，最终演成了资本围猎现场。",
]

STORIES_BY_TYPE = {
    "cp_points": STORIES_TRAFFIC,
    "reputation": STORIES_REPUTATION,
    "self_points": STORIES_POINTS,
    "jackpot": STORIES_JACKPOT,
}

SATIRE_SCORES = [114514, 23333, 9527, 1919810]
SATIRE_STORIES = [
    "你刚开场三十秒，评委席已经开始低头改返程机票。",
    "投影切换失败四次，唯一稳定输出的是现场沉默。",
    "对手没发言都赢了，你却成功把会场变成吐槽专场。",
    "本想路演融资，结果像在做“反面教材现场教学”。",
]
SATIRE_CRITIQUES = [
    "商业逻辑：像把三份BP打碎后再随机拼接。",
    "市场判断：你看的是明年，市场活在今天下午。",
    "产品叙事：故事很燃，落地路径像失踪人口。",
    "执行可信度：承诺拉满，证据偏少。",
    "风险控制：你把最大风险写成了“后续再议”。",
]


def _app_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(settings.app_timezone or "Asia/Shanghai")
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def _now_local() -> dt.datetime:
    return dt.datetime.now(_app_timezone())


def _today_key(company_id: int) -> str:
    return f"{ROADSHOW_DAILY_KEY_PREFIX}:{company_id}:{_now_local().date().isoformat()}"


def _seconds_until_next_day() -> int:
    now = _now_local()
    tomorrow = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds()))


def _clamp_rate(rate: float, *, min_value: float = 0.0, max_value: float = 1.0) -> float:
    return max(min_value, min(max_value, float(rate)))


def _format_remaining(seconds: int) -> str:
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}小时{minutes}分钟"


async def _mark_roadshow_used(company_id: int):
    r = await get_redis()
    if settings.roadshow_daily_once:
        await r.setex(_today_key(company_id), _seconds_until_next_day() + 60, "1")
        return
    await r.setex(f"roadshow_cd:{company_id}", settings.roadshow_cooldown_seconds, "1")


async def can_roadshow(company_id: int) -> tuple[bool, int]:
    """Check roadshow availability. Returns (can_do, remaining_seconds)."""
    r = await get_redis()

    if settings.roadshow_daily_once:
        if await r.exists(_today_key(company_id)):
            return False, _seconds_until_next_day()
        return True, 0

    ttl = await r.ttl(f"roadshow_cd:{company_id}")
    if ttl > 0:
        return False, ttl
    return True, 0


async def _build_satire_result(company_id: int, rs_type: str) -> tuple[str, float]:
    score = random.choice(SATIRE_SCORES)
    story = random.choice(SATIRE_STORIES)
    critiques = random.sample(SATIRE_CRITIQUES, k=min(3, len(SATIRE_CRITIQUES)))

    penalty_rate = _clamp_rate(settings.roadshow_satire_penalty_rate, min_value=0.05, max_value=0.90)
    r = await get_redis()
    await r.setex(
        f"{ROADSHOW_PENALTY_KEY_PREFIX}:{company_id}",
        _seconds_until_next_day() + 86400,
        f"{penalty_rate:.4f}",
    )

    result_text = (
        f"🎭 《{rs_type}》灾难路演\n"
        f"{'─' * 24}\n"
        f"📉 评审总分: {score}/100\n"
        f"🧨 现场实况: {story}\n"
        f"{'─' * 24}\n"
        f"🗣 评委毒评:\n"
        f"- {critiques[0]}\n"
        f"- {critiques[1]}\n"
        f"- {critiques[2]}\n"
        f"{'─' * 24}\n"
        f"⚠️ 该评分仅节目效果，不提供任何正向加成。\n"
        f"📉 当日营收惩罚: -{int(penalty_rate * 100)}%"
    )
    return result_text, penalty_rate


def _normal_score_by_reward(reward_type: str) -> int:
    ranges = {
        "cp_points": (64, 92),
        "reputation": (72, 96),
        "self_points": (58, 84),
        "jackpot": (90, 100),
    }
    low, high = ranges.get(reward_type, (60, 90))
    return random.randint(low, high)


async def do_roadshow(
    session: AsyncSession,
    company_id: int,
    owner_user_id: int,
) -> tuple[bool, str]:
    """Perform one roadshow. Default mode is daily-once."""
    can, remaining = await can_roadshow(company_id)
    if not can:
        if settings.roadshow_daily_once:
            return False, f"今天已路演过，明天再来（约 {_format_remaining(remaining)} 后重置）"
        return False, f"路演冷却中，还需 {_format_remaining(remaining)}"

    ok = await add_funds(session, company_id, -settings.roadshow_cost)
    if not ok:
        return False, f"公司积分不足，路演需要 {fmt_traffic(settings.roadshow_cost)}"

    rs_type = random.choice(ROADSHOW_TYPES)
    satire_chance = _clamp_rate(settings.roadshow_satire_chance)

    bonus = 0
    rep_gained = 0
    result_text = ""

    if random.random() < satire_chance:
        result_text, _penalty_rate = await _build_satire_result(company_id, rs_type)
    else:
        weights = [r["weight"] for r in REWARD_TABLE]
        reward = random.choices(REWARD_TABLE, weights=weights, k=1)[0]
        amount = random.randint(reward["min"], reward["max"])

        from services.shop_service import get_roadshow_multiplier

        rs_multiplier = await get_roadshow_multiplier(company_id)
        if rs_multiplier > 1.0:
            amount = int(amount * rs_multiplier)

        story = random.choice(STORIES_BY_TYPE.get(reward["type"], STORIES_TRAFFIC))
        score = _normal_score_by_reward(reward["type"])
        review = random.choice(
            [
                "评委结论：冲突足够强，叙事到位，执行还需要更狠。",
                "评委结论：你把压力变成了注意力，这是路演最值钱的能力。",
                "评委结论：方案并不完美，但现场掌控力非常强。",
                "评委结论：你赢在节奏，不是赢在运气。",
            ]
        )

        reward_line = ""
        if reward["type"] in {"cp_points", "jackpot"}:
            await add_funds(session, company_id, amount)
            bonus = amount
            reward_line = f"💵 积分 +{fmt_traffic(amount)}"
        elif reward["type"] == "reputation":
            await add_reputation(session, owner_user_id, amount)
            rep_gained = amount
            reward_line = f"⭐ 声望 +{amount}"
        elif reward["type"] == "self_points":
            await add_self_points(owner_user_id, amount, session=session)
            reward_line = f"🏅 积分 +{amount}"

        if rs_multiplier > 1.0:
            reward_line += "（精准营销翻倍）"

        base_rep = 2
        await add_reputation(session, owner_user_id, base_rep)
        rep_gained += base_rep
        await add_self_points(owner_user_id, 3, session=session)

        result_text = (
            f"🎤 《{rs_type}》路演现场\n"
            f"{'─' * 24}\n"
            f"📈 评审总分: {score}/100\n"
            f"🧨 现场冲突: {story}\n"
            f"{'─' * 24}\n"
            f"{reward_line}\n"
            f"⭐ 基础声望 +{base_rep}\n"
            f"🗞 {review}"
        )

    roadshow = Roadshow(
        company_id=company_id,
        type=rs_type,
        result=result_text,
        bonus=bonus,
        reputation_gained=rep_gained,
    )
    session.add(roadshow)
    await session.flush()

    await _mark_roadshow_used(company_id)
    return True, result_text
