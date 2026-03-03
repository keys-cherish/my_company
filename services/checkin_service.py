"""每日打卡（公司特色签到）业务逻辑。

Redis keys:
  checkin:streak:{tg_id}   — 当前连续打卡天数
  checkin:last:{tg_id}     — 上次打卡日期 YYYY-MM-DD
"""

from __future__ import annotations

import datetime as dt
import random

from cache.redis_client import get_redis
from config import settings
from utils.timezone import BJ_TZ

_KEY_STREAK = "checkin:streak:{tg_id}"
_KEY_LAST = "checkin:last:{tg_id}"


def _parse_streak_rewards() -> list[int]:
    """Parse comma-separated streak rewards from config."""
    return [int(x.strip()) for x in settings.checkin_streak_rewards.split(",") if x.strip()]


def _parse_bonus_pool() -> list[int]:
    return [int(x.strip()) for x in settings.checkin_streak_bonus_pool.split(",") if x.strip()]


async def do_checkin(tg_id: int) -> tuple[bool, str, int]:
    """Execute daily check-in.

    Returns (success, message, reward_amount).
    - success=False means already checked in today.
    """
    r = await get_redis()
    today_bj = dt.datetime.now(BJ_TZ).date().isoformat()

    last_date = await r.get(_KEY_LAST.format(tg_id=tg_id))
    if last_date:
        last_date = last_date if isinstance(last_date, str) else last_date.decode()
        if last_date == today_bj:
            return False, "📋 你今天已经打过卡了，明天再来吧！", 0

    # Calculate streak
    streak_key = _KEY_STREAK.format(tg_id=tg_id)
    last_key = _KEY_LAST.format(tg_id=tg_id)

    yesterday_bj = (dt.datetime.now(BJ_TZ).date() - dt.timedelta(days=1)).isoformat()
    old_streak = 0
    if last_date and last_date == yesterday_bj:
        val = await r.get(streak_key)
        old_streak = int(val) if val else 0
    # else: streak broken, reset to 0

    new_streak = old_streak + 1
    rewards_table = _parse_streak_rewards()
    cycle = settings.checkin_streak_cycle

    # Calculate reward
    day_in_cycle = ((new_streak - 1) % cycle)  # 0-indexed within cycle
    is_cycle_complete = (new_streak % cycle == 0) and new_streak > 0

    # Base daily reward
    if day_in_cycle < len(rewards_table):
        reward = rewards_table[day_in_cycle]
    else:
        reward = rewards_table[-1]

    # Bonus chest on cycle completion
    chest_bonus = 0
    chest_msg = ""
    if is_cycle_complete:
        pool = _parse_bonus_pool()
        chest_bonus = random.choice(pool) if pool else 5000
        chest_msg = f"\n\n🎁 连续打卡{cycle}天宝箱奖励: +{chest_bonus:,} 积分！"

    total_reward = reward + chest_bonus

    # Save state
    await r.set(last_key, today_bj)
    await r.set(streak_key, str(new_streak))

    # Company-themed messages based on streak
    theme = _get_theme_message(new_streak, day_in_cycle)

    msg = (
        f"🏢 每日打卡成功！\n"
        f"{'─' * 24}\n"
        f"{theme}\n"
        f"{'─' * 24}\n"
        f"📅 连续打卡: {new_streak} 天\n"
        f"💰 今日奖励: +{reward:,} 积分\n"
        f"📊 明日奖励预告: {_preview_next(new_streak, rewards_table, cycle):,} 积分"
        f"{chest_msg}"
    )

    return True, msg, total_reward


def _get_theme_message(streak: int, day_in_cycle: int) -> str:
    """Return a company-themed flavor message based on streak day."""
    themes = [
        "☕ 新的一天开始了！CEO准时到岗，给全公司打了个好样。",
        "📊 连续第二天到岗，董事会对你的勤勉表示赞赏。",
        "🤝 三天不断！合伙人们开始注意到你的坚持。",
        "📈 四连打卡！公司士气提升，员工效率+10%（心理上）。",
        "🏆 五天全勤！行业媒体报道：「这位CEO从不缺席」。",
        "⭐ 六天坚持！竞争对手开始打听你的作息时间表。",
        "🎊 整整一周！全公司为你起立鼓掌，宝箱已备好！",
    ]
    idx = min(day_in_cycle, len(themes) - 1)
    return themes[idx]


def _preview_next(current_streak: int, rewards_table: list[int], cycle: int) -> int:
    """Preview tomorrow's reward."""
    next_day = current_streak  # 0-indexed for tomorrow
    next_in_cycle = next_day % cycle
    if next_in_cycle < len(rewards_table):
        return rewards_table[next_in_cycle]
    return rewards_table[-1]
