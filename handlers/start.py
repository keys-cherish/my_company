"""Start, help, profile, leaderboard handlers."""

from __future__ import annotations

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.types import BotCommand

from cache.redis_client import get_leaderboard
from commands import (
    CMD_BATTLE,
    CMD_CANCEL,
    CMD_CLEANUP,
    CMD_COOPERATE,
    CMD_COMPANY,
    CMD_CREATE_COMPANY,
    CMD_DEDUCT_MONEY,
    CMD_DEMON,
    CMD_DIVIDEND,
    CMD_DISSOLVE,
    CMD_EXCHANGE,
    CMD_GIVE_MONEY,
    CMD_HELP,
    CMD_LOG,
    CMD_LIST_COMPANY,
    CMD_COMPENSATE,
    CMD_MAINTAIN,
    CMD_MAKEUP,
    CMD_MEMBER,
    CMD_INVEST,
    CMD_NEW_PRODUCT,
    CMD_QUEST,
    CMD_RANK_COMPANY,
    CMD_RENAME,
    CMD_START,
    CMD_TRANSFER,
    CMD_WELFARE,
    CMD_CHECKIN,
    CMD_REDPACKET,
)
from config import settings
from db.engine import async_session
from keyboards.menus import main_menu_kb, tag_kb
from services.company_service import get_companies_by_owner
from services.user_service import get_or_create_user
from utils.formatters import fmt_points, compact_number
from utils.panel_owner import mark_panel

router = Router()

BOT_COMMANDS = [
    BotCommand(command=CMD_START, description="开始游戏 / 创建公司"),
    BotCommand(command=CMD_CREATE_COMPANY, description="创建公司"),
    BotCommand(command=CMD_COMPANY, description="我的公司"),
    BotCommand(command=CMD_LIST_COMPANY, description="查看全服公司"),
    BotCommand(command=CMD_RANK_COMPANY, description="综合实力排行榜"),
    BotCommand(command=CMD_RENAME, description="公司改名"),
    BotCommand(command=CMD_BATTLE, description="商战（回复后随机三选战术）"),
    BotCommand(command=CMD_COOPERATE, description="合作（回复/all）"),
    BotCommand(command=CMD_NEW_PRODUCT, description="创建产品（名称 投资金额）"),
    BotCommand(command=CMD_DEMON, description="😈 恶魔轮盘赌（可选赌注）"),
    BotCommand(command=CMD_MEMBER, description="员工管理（add/minus 数量）"),
    BotCommand(command=CMD_INVEST, description="注资（需回复目标）"),
    BotCommand(command=CMD_DIVIDEND, description="公司分红（金额）"),
    BotCommand(command=CMD_TRANSFER, description="个人转账（需回复目标）"),
    BotCommand(command=CMD_LOG, description="资金流水（user/company）"),
    BotCommand(command=CMD_EXCHANGE, description="交易所（商城/黑市）"),
    BotCommand(command=CMD_DISSOLVE, description="注销公司"),
    BotCommand(command=CMD_QUEST, description="周任务清单"),
    BotCommand(command=CMD_HELP, description="帮助信息"),
    BotCommand(command=CMD_CANCEL, description="取消当前输入流程"),
    BotCommand(command=CMD_GIVE_MONEY, description="【超管】发放积分（回复）"),
    BotCommand(command=CMD_DEDUCT_MONEY, description="【超管】扣除积分（回复）"),
    BotCommand(command=CMD_WELFARE, description="【超管】全服福利"),
    BotCommand(command=CMD_CLEANUP, description="【超管】清理残留数据"),
    BotCommand(command=CMD_MAKEUP, description="【超管】数据修复检查"),
    BotCommand(command=CMD_MAINTAIN, description="【超管】停机维护（锁全局）"),
    BotCommand(command=CMD_COMPENSATE, description="【超管】停机补偿（每人+500）"),
    BotCommand(command="cp_slot", description="🎰 老虎机（每日奖励一次）"),
    BotCommand(command=CMD_CHECKIN, description="🏢 每日打卡（连续签到领奖励）"),
    BotCommand(command=CMD_REDPACKET, description="🧧 发红包（金额 个数）"),
]

HELP_TEXT = (
    "🏢 商业帝国 — 公司经营模拟游戏\n"
    f"{'─' * 24}\n"
    "通过 科研→产品→利润 的路径经营虚拟公司\n\n"
    "📋 命令列表:\n\n"
    "/cp_start — 开始游戏（自动注册+创建公司）\n"
    "/cp_create — 创建公司\n"
    "/cp — 查看和管理公司\n"
    "/cp_list — 全服公司列表\n"
    "/cp_rank — 综合实力排行\n\n"
    "⚔️ /cp_battle [战术] — 回复某人发起商战（不填则随机3选1，每次消耗200个人积分）\n"
    "  可直接输入战术，或让系统随机给出3个内置战术供你选择\n"
    "🤝 /cp_cooperate — 回复某人/all 合作\n"
    "  每次+2%（上限50%），次日清空，双方各+30声望\n\n"
    "📦 /cp_new_product <产品名> <投资金额>\n"
    "  AI评估产品方案打分，投资越多品质越高\n\n"
    "😈 /cp_demon [金额] — 快速打开恶魔轮盘赌（可直接带赌注入场）\n\n"
    "👷 /cp_member add|minus <数量|max>\n"
    "/cp_rename <新名字> — 公司改名\n"
    "/cp_invest <积分> - reply target user to invest and gain shares\n"
    "/cp_dividend <金额> — 公司分红\n"
    "/cp_transfer <金额> — 回复目标进行个人转账\n"
    "/cp_log [user|company] — 资金流水\n"
    "/cp_exchange — 交易所（商城/黑市）\n"
    "Reply shortcut: invest5000 (must reply target message)\n"
    "🗑 /cp_dissolve — 注销公司(24h冷却)\n"
    "/cp_cancel — 取消当前输入流程\n"
    "/cp_help — 显示此帮助\n"
    "\n🎰 /cp_slot — 老虎机（三个一样中奖，777大奖77777！每日奖励一次）\n"
    "🏢 /cp_checkin — 每日打卡（连续签到7天开宝箱！）\n"
    "🧧 /cp_redpacket <金额> <个数> — 发公司红包，群里抢！\n"
    "\n🔒 超管专用命令：\n"
    "/cp_give <积分> — 【超管】回复目标发放积分\n"
    "/cp_deduct <积分> — 【超管】回复目标扣除积分\n"
    "/cp_welfare — 【超管】全服公司福利\n"
    "/cp_cleanup — 【超管】清理Redis/DB残留\n"
    "/cp_makeup — 【超管】数据修复检查\n"
    "/cp_maintain [更新说明] — 【超管】进入停机维护并置顶公告\n"
    "/cp_compensate <更新说明> — 【超管】解除维护+全员补偿500并置顶\n"
    "\n🤖 AI对话: 任意消息带 @机器人用户名 即可调用\n"
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
            self_points = user.self_points
            reputation = user.reputation

    if created:
        sent = await message.reply(
            f"欢迎加入 商业帝国!\n"
            f"当前个人积分: {fmt_points(self_points)}\n\n"
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
        from services.user_service import get_user_by_tg_id, get_user_max_points
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            await callback.answer("请先 /cp_create 创建公司", show_alert=True)
            return
        companies = await get_companies_by_owner(session, user.id)
        self_points = user.self_points
        max_points = await get_user_max_points(session, user.id)
        reputation = user.reputation

    company_names = ", ".join(c.name for c in companies) if companies else "无"

    text = (
        f"📊 个人面板 — {callback.from_user.full_name}\n"
        f"{'─' * 24}\n"
        f"💰 个人积分: {fmt_points(self_points)} / {fmt_points(max_points)}\n"
        f"⭐ 声望: {reputation}\n"
        f"🏢 公司: {company_names}\n"
    )

    try:
        await callback.message.edit_text(text, reply_markup=main_menu_kb(tg_id=callback.from_user.id))
    except Exception:
        pass
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
