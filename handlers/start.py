"""Start, help, profile, leaderboard handlers."""

from __future__ import annotations

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeChatMember

from cache.redis_client import get_leaderboard
from commands import (
    CMD_ADMIN,
    CMD_BATTLE,
    CMD_BLOCK,
    CMD_COOPERATE,
    CMD_COMPANY,
    CMD_CREATE_COMPANY,
    CMD_DISSOLVE,
    CMD_GIVE_MONEY,
    CMD_HELP,
    CMD_LIST_COMPANY,
    CMD_MEMBER,
    CMD_INVEST,
    CMD_NEW_PRODUCT,
    CMD_QUEST,
    CMD_RANK_COMPANY,
    CMD_UNBLOCK,
    CMD_START,
    CMD_WELFARE,
    CMD_CHECKIN,
    CMD_REDPACKET,
)
from config import settings
from db.engine import async_session
from keyboards.menus import main_menu_kb, tag_kb
from services.company_service import get_companies_by_owner
from services.user_service import get_or_create_user, get_points
from utils.formatters import fmt_traffic, compact_number
from utils.panel_owner import mark_panel

router = Router()

BOT_COMMANDS = [
    BotCommand(command=CMD_START, description="开始游戏 / 创建公司"),
    BotCommand(command=CMD_CREATE_COMPANY, description="创建公司"),
    BotCommand(command=CMD_COMPANY, description="我的公司"),
    BotCommand(command=CMD_LIST_COMPANY, description="查看全服公司"),
    BotCommand(command=CMD_RANK_COMPANY, description="综合实力排行榜"),
    BotCommand(command=CMD_BATTLE, description="商战（回复+可选战术）"),
    BotCommand(command=CMD_COOPERATE, description="合作（回复/all）"),
    BotCommand(command=CMD_NEW_PRODUCT, description="创建产品（模板key [名称]）"),
    BotCommand(command=CMD_MEMBER, description="员工管理（add/minus 数量）"),
    BotCommand(command=CMD_INVEST, description="Reply invest to user"),
    BotCommand(command=CMD_DISSOLVE, description="注销公司"),
    BotCommand(command=CMD_QUEST, description="周任务清单"),
    BotCommand(command=CMD_HELP, description="帮助信息"),
    BotCommand(command=CMD_GIVE_MONEY, description="超管发放积分（回复+积分）"),
    BotCommand(command=CMD_WELFARE, description="超管全服福利（每家100万）"),
    BotCommand(command="cp_slot", description="🎰 老虎机（每日奖励一次）"),
    BotCommand(command=CMD_CHECKIN, description="🏢 每日打卡（连续签到领奖励）"),
    BotCommand(command=CMD_REDPACKET, description="🧧 发红包（金额 个数）"),
    BotCommand(command=CMD_BLOCK, description="隐藏本聊天命令菜单"),
    BotCommand(command=CMD_UNBLOCK, description="恢复本聊天命令菜单"),
]

HELP_TEXT = (
    "🏢 商业帝国 — 公司经营模拟游戏\n"
    f"{'─' * 24}\n"
    "通过 科研→产品→利润 的路径经营虚拟公司\n\n"
    "📋 命令列表:\n\n"
    "/company_start — 开始游戏（自动注册+创建公司）\n"
    "/company_create — 创建公司\n"
    "/company — 查看和管理公司\n"
    "/company_list — 全服公司列表\n"
    "/company_rank — 综合实力排行\n\n"
    "⚔️ /company_battle [战术] — 回复某人发起商战（每次消耗200积分）\n"
    "  战术: 稳扎稳打 / 激进营销 / 奇袭渗透\n"
    "🤝 /company_cooperate — 回复某人/all 合作\n"
    "  每次+2%（上限50%），次日清空，双方各+30声望\n\n"
    "📦 /cp_new_product <模板key> [自定义名称]\n"
    "  仅可创建已通过科研解锁的产品模板\n"
    "  可不填自定义名称，不填则使用模板默认名\n\n"
    "👷 /company_member add|minus <数量|max>\n"
    "/company_invest <积分> - reply target user to invest and gain shares\n"
    "Reply shortcut: invest5000 (must reply target message)\n"
    "🗑 /company_dissolve — 注销公司(24h冷却)\n"
    "/company_admin <密钥> — 管理员认证\n"
    "/company_help — 显示此帮助\n"
    "\n🎰 /cp_slot — 老虎机（三个一样中奖，777大奖77777！每日奖励一次）\n"    "\n🏢 /company_checkin — 每日打卡（连续签到7天开宝箱！）\n"
    "🧧 /company_redpacket <金额> <个数> — 发公司红包，群里抢！\n"
    "🙈 /block — 隐藏当前聊天命令菜单（输入 / 不再弹机器人命令）\n"
    "🙉 /unblock — 恢复当前聊天命令菜单\n"
    "\n📶 积分兑换流量：交易所→积分→流量\n"    "\n🤖 AI对话: 任意消息带 @机器人用户名 即可调用\n"
    "普通用户每分钟最多 10 次，管理员/超管不限制\n"
)


@router.message(Command(CMD_START))
async def cmd_start(message: types.Message):
    tg_id = message.from_user.id
    tg_name = message.from_user.full_name or str(tg_id)

    async with async_session() as session:
        async with session.begin():
            user, created = await get_or_create_user(session, tg_id, tg_name)
            user_id = user.id
            traffic = user.traffic
            reputation = user.reputation

    if created:
        sent = await message.reply(
            f"欢迎加入 商业帝国!\n"
            f"已发放初始积分: {fmt_traffic(settings.initial_traffic)}\n\n"
            f"使用下方菜单开始游戏:",
            reply_markup=main_menu_kb(tg_id=tg_id),
        )
    else:
        sent = await message.reply(
            f"🏢 商业帝国 — 主菜单",
            reply_markup=main_menu_kb(tg_id=tg_id),
        )
    await mark_panel(sent.chat.id, sent.message_id, tg_id)


@router.message(Command(CMD_HELP))
async def cmd_help(message: types.Message):
    await message.answer(HELP_TEXT)


def _member_scope_for_message(message: types.Message):
    if message.chat.type == "private":
        return BotCommandScopeChat(chat_id=message.chat.id)
    return BotCommandScopeChatMember(chat_id=message.chat.id, user_id=message.from_user.id)


@router.message(Command(CMD_BLOCK))
async def cmd_block(message: types.Message):
    """Hide this bot's command menu for current user in current chat."""
    scope = _member_scope_for_message(message)
    await message.bot.set_my_commands([], scope=scope)
    await message.answer("🙈 已隐藏当前聊天的机器人命令菜单。\n恢复请发送 /unblock")


@router.message(Command(CMD_UNBLOCK))
async def cmd_unblock(message: types.Message):
    """Restore default command menu for current user in current chat."""
    scope = _member_scope_for_message(message)
    await message.bot.delete_my_commands(scope=scope)
    await message.answer("🙉 已恢复当前聊天的机器人命令菜单。")


@router.callback_query(F.data == "menu:main")
async def cb_menu_main(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "🏢 商业帝国 — 主菜单",
        reply_markup=main_menu_kb(tg_id=callback.from_user.id),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:profile")
async def cb_menu_profile(callback: types.CallbackQuery):
    tg_id = callback.from_user.id

    async with async_session() as session:
        from services.user_service import get_user_by_tg_id
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            await callback.answer("请先 /company_create 创建公司", show_alert=True)
            return
        companies = await get_companies_by_owner(session, user.id)
        traffic = user.traffic
        reputation = user.reputation

    points = await get_points(tg_id)

    company_names = ", ".join(c.name for c in companies) if companies else "无"

    text = (
        f"📊 个人面板 — {callback.from_user.full_name}\n"
        f"{'─' * 24}\n"
        f"💰 积分: {fmt_traffic(traffic)}\n"
        f"⭐ 声望: {reputation}\n"
        f"🎁 荣誉点: {points:,}\n"
        f"🏢 公司: {company_names}\n"
    )

    await callback.message.edit_text(text, reply_markup=main_menu_kb(tg_id=callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "menu:leaderboard")
async def cb_menu_leaderboard(callback: types.CallbackQuery):
    """Show leaderboard with category buttons."""
    await _show_leaderboard(callback, "revenue")


@router.callback_query(F.data.startswith("leaderboard:"))
async def cb_leaderboard_switch(callback: types.CallbackQuery):
    board_type = callback.data.split(":")[1]
    await _show_leaderboard(callback, board_type)


LEADERBOARD_TYPES = {
    "revenue": "📈 日营收",
    "funds": "💰 总积分",
    "valuation": "🏷 估值",
    "power": "⚔️ 战力",
}


async def _show_leaderboard(callback: types.CallbackQuery, board_type: str):
    title = LEADERBOARD_TYPES.get(board_type, "排行榜")
    lb_data = await get_leaderboard(board_type, 10)

    lines = [
        f"{title} TOP 10",
        "─" * 24,
    ]
    if not lb_data:
        lines.append("暂无数据")
    else:
        for i, (name, score) in enumerate(lb_data, 1):
            medal = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}.get(i, f"{i}.")
            lines.append(f"{medal} {name}: {compact_number(int(score))}")

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    # Category buttons
    cat_buttons = []
    for key, label in LEADERBOARD_TYPES.items():
        if key == board_type:
            cat_buttons.append(InlineKeyboardButton(text=f"[{label}]", callback_data=f"leaderboard:{key}"))
        else:
            cat_buttons.append(InlineKeyboardButton(text=label, callback_data=f"leaderboard:{key}"))

    kb = InlineKeyboardMarkup(inline_keyboard=[
        cat_buttons,
        [InlineKeyboardButton(text="🔙 返回", callback_data="menu:main")],
    ])
    kb = tag_kb(kb, callback.from_user.id)
    try:
        await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    except Exception:
        pass
    await callback.answer()
