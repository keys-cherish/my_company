"""公司相关处理器 — CRUD/导航/创建/升级/改名/注销。"""

from __future__ import annotations

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, func as sqlfunc, delete as sql_delete

from cache.redis_client import get_redis
from commands import (
    CMD_COMPANY,
    CMD_CREATE_COMPANY,
    CMD_DISSOLVE,
    CMD_LIST_COMPANY,
    CMD_MAKEUP,
    CMD_RANK_COMPANY,
)
from config import settings as cfg
from db.engine import async_session
from db.models import (
    Company,
    CompanyOperationProfile,
    Cooperation,
    DailyReport,
    Product,
    RealEstate,
    ResearchProgress,
    Roadshow,
    Shareholder,
    User,
)
from handlers.common import is_super_admin
from handlers.company_helpers import (
    CreateCompanyState,
    RENAME_COOLDOWN,
    RENAME_COST_RATE,
    RENAME_MIN_COST,
    RENAME_REVENUE_PENALTY,
    RenameCompanyState,
    _safe_edit_or_send,
    _start_company_type_selection,
    render_company_detail,
    render_company_finance_detail,
)
from keyboards.menus import company_detail_kb, company_list_kb, main_menu_kb, tag_kb
from services.company_service import (
    add_funds,
    create_company,
    get_companies_by_owner,
    get_company_by_id,
    get_company_type_info,
    load_company_types,
    upgrade_company,
)
from services.user_service import add_traffic, get_or_create_user, get_user_by_tg_id
from utils.formatters import compact_number, fmt_quota, fmt_traffic
from utils.panel_owner import mark_panel
from utils.validators import validate_name

router = Router()


# ---- /company_list 列出所有公司 ----

@router.message(Command(CMD_LIST_COMPANY))
async def cmd_list_company(message: types.Message):
    """列出服务器上所有公司。"""
    async with async_session() as session:
        result = await session.execute(
            select(Company).order_by(Company.total_funds.desc())
        )
        companies = list(result.scalars().all())

    if not companies:
        await message.answer("目前还没有任何公司")
        return

    lines = [f"🏢 全服公司列表 (共 {len(companies)} 家)", f"{'─' * 28}"]
    for i, c in enumerate(companies, 1):
        type_info = get_company_type_info(c.company_type)
        emoji = type_info["emoji"] if type_info else "🏢"
        lines.append(
            f"{i}. {emoji} {c.name} (ID:{c.id})\n"
            f"   Lv.{c.level} | 积分余额:{fmt_traffic(c.total_funds)} | "
            f"日营收:{fmt_traffic(c.daily_revenue)} | 👷{c.employee_count}人"
        )

    await message.answer("\n".join(lines))


# ---- /company_rank 综合实力排行 ----

@router.message(Command(CMD_RANK_COMPANY))
async def cmd_rank_company(message: types.Message):
    """显示公司综合实力排行榜（实时计算）。"""
    async with async_session() as session:
        result = await session.execute(select(Company))
        companies = list(result.scalars().all())

        if not companies:
            await message.answer("目前还没有任何公司")
            return

        rankings = []
        for company in companies:
            prod_count = (await session.execute(
                select(sqlfunc.count()).where(Product.company_id == company.id)
            )).scalar() or 0
            tech_count = (await session.execute(
                select(sqlfunc.count()).where(
                    ResearchProgress.company_id == company.id,
                    ResearchProgress.status == "completed",
                )
            )).scalar() or 0

            # Deterministic power score (no randomness)
            power = (
                company.total_funds * 0.3
                + company.daily_revenue * 30
                + company.employee_count * 1000
                + tech_count * 2000
                + prod_count * 1500
                + company.level * 3000
            )
            rankings.append((company, power, prod_count, tech_count))

    # Sort by power descending
    rankings.sort(key=lambda x: x[1], reverse=True)

    lines = [
        "⚔️ 公司综合实力排行 TOP 20",
        "─" * 28,
    ]
    for i, (c, power, prods, techs) in enumerate(rankings[:20], 1):
        medal = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}.get(i, f"{i}.")
        type_info = get_company_type_info(c.company_type)
        emoji = type_info["emoji"] if type_info else "🏢"
        lines.append(
            f"{medal} {emoji} {c.name}\n"
            f"   战力:{compact_number(int(power))} | Lv.{c.level} | "
            f"📦{prods} | 🔬{techs} | 👷{c.employee_count}"
        )

    await message.answer("\n".join(lines))


# ---- /company_makeup 数据清理命令 ----


@router.message(Command(CMD_MAKEUP))
async def cmd_makeup(message: types.Message):
    """管理员命令：清理所有公司的异常数据。"""
    if not is_super_admin(message.from_user.id):
        await message.answer("❌ 无权使用此命令")
        return

    from services.integrity_service import run_all_checks
    import logging
    logger = logging.getLogger(__name__)

    try:
        msgs = await run_all_checks()

        if msgs:
            lines = ["🔧 数据清理报告:", "─" * 24] + msgs
            await message.answer("\n".join(lines))
        else:
            await message.answer("✅ 所有数据正常，无需清理")
    except Exception as e:
        logger.exception("makeup command error")
        await message.answer(f"❌ 数据清理出错: {e}")


# /company
@router.message(Command(CMD_COMPANY))
async def cmd_company(message: types.Message):
    tg_id = message.from_user.id
    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            await message.answer("请先使用 /company_start 注册")
            return
        companies = await get_companies_by_owner(session, user.id)

    if not companies:
        await message.answer(
            "你还没有公司。",
            reply_markup=company_list_kb([], tg_id=message.from_user.id),
        )
        return

    # 只有一家公司时直接打开详情
    if len(companies) == 1:
        text, kb = await render_company_detail(companies[0].id, tg_id)
        sent = await message.answer(text, reply_markup=kb)
        await mark_panel(message.chat.id, sent.message_id, tg_id)
        return

    items = [(c.id, c.name) for c in companies]
    await message.answer("🏢 你的公司列表:", reply_markup=company_list_kb(items, tg_id=message.from_user.id))


@router.callback_query(F.data == "menu:company")
async def cb_menu_company(callback: types.CallbackQuery):
    tg_id = callback.from_user.id
    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            await callback.answer("请先 /company_create 创建公司", show_alert=True)
            return
        companies = await get_companies_by_owner(session, user.id)

    # 只有一家公司时直接打开详情
    if len(companies) == 1:
        text, kb = await render_company_detail(companies[0].id, tg_id)
        await _safe_edit_or_send(callback, text, kb)
        await callback.answer()
        return

    items = [(c.id, c.name) for c in companies]
    await _safe_edit_or_send(callback, "🏢 你的公司列表:", company_list_kb(items, tg_id=callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "menu:company_list")
async def cb_menu_company_list(callback: types.CallbackQuery):
    """Always show company list page, even if user only has one company."""
    tg_id = callback.from_user.id
    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            await callback.answer("请先 /company_create 创建公司", show_alert=True)
            return
        companies = await get_companies_by_owner(session, user.id)

    items = [(c.id, c.name) for c in companies]
    await _safe_edit_or_send(
        callback,
        "🏢 你的公司列表:",
        company_list_kb(items, tg_id=callback.from_user.id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("company:view:"))
async def cb_company_view(callback: types.CallbackQuery):
    company_id = int(callback.data.split(":")[2])
    text, kb = await render_company_detail(company_id, callback.from_user.id)
    await _safe_edit_or_send(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("company:finance:"))
async def cb_company_finance(callback: types.CallbackQuery):
    company_id = int(callback.data.split(":")[2])
    text, kb = await render_company_finance_detail(company_id, callback.from_user.id)
    await _safe_edit_or_send(callback, text, kb)
    await callback.answer()


# ---- 创建公司：/company_create 命令或回调按钮 ----

@router.message(Command(CMD_CREATE_COMPANY))
async def cmd_create_company(message: types.Message, state: FSMContext):
    """创建公司命令入口。自动注册用户，无需先 /company_start。"""
    tg_id = message.from_user.id
    tg_name = message.from_user.full_name or str(tg_id)

    async with async_session() as session:
        async with session.begin():
            user, created = await get_or_create_user(session, tg_id, tg_name)
        companies = await get_companies_by_owner(session, user.id)
        if companies:
            # 已有公司 → 直接展示
            text, kb = await render_company_detail(companies[0].id, tg_id)
            sent = await message.answer(
                "你已经拥有公司，每人只能拥有一家公司\n\n" + text,
                reply_markup=kb,
            )
            await mark_panel(message.chat.id, sent.message_id, tg_id)
            return

    welcome = ""
    if created:
        welcome = f"欢迎加入 商业帝国! 已发放初始积分: {fmt_traffic(cfg.initial_traffic)}\n\n"
    else:
        # 老用户重新创建（注销后），重新发放初始积分
        async with async_session() as session:
            async with session.begin():
                await add_traffic(session, user.id, cfg.initial_traffic)
        welcome = f"已重新发放初始积分: {fmt_traffic(cfg.initial_traffic)}\n\n"

    await _start_company_type_selection(message, state, welcome)


@router.callback_query(F.data == "company:create")
async def cb_company_create(callback: types.CallbackQuery, state: FSMContext):
    types_data = load_company_types()
    buttons = [
        [InlineKeyboardButton(
            text=f"{info['emoji']} {info['name']}",
            callback_data=f"company:type:{key}",
        )]
        for key, info in types_data.items()
    ]
    buttons.append([InlineKeyboardButton(text="🔙 取消", callback_data="menu:company")])

    await callback.message.edit_text(
        "选择公司类型:\n\n" +
        "\n".join(f"{info['emoji']} {info['name']} — {info['description']}" for info in types_data.values()),
        reply_markup=tag_kb(InlineKeyboardMarkup(inline_keyboard=buttons), callback.from_user.id),
    )
    await state.set_state(CreateCompanyState.waiting_type)
    await callback.answer()


@router.callback_query(F.data.startswith("company:type:"))
async def cb_company_type_selected(callback: types.CallbackQuery, state: FSMContext):
    company_type = callback.data.split(":")[2]
    await state.update_data(company_type=company_type)
    await state.set_state(CreateCompanyState.waiting_name)
    type_info = get_company_type_info(company_type)
    name = type_info["name"] if type_info else company_type
    await callback.message.edit_text(f"已选择: {name}\n\n请输入新公司名称 (2-16字):")
    await callback.answer()


@router.message(CreateCompanyState.waiting_name)
async def on_company_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    name_err = validate_name(name, min_len=2, max_len=16)
    if name_err:
        await message.answer(f"{name_err}，请重新输入:")
        return

    data = await state.get_data()
    company_type = data.get("company_type", "tech")
    tg_id = message.from_user.id

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                await message.answer("请先 /company_create 创建公司")
                await state.clear()
                return
            company, msg = await create_company(session, user, name, company_type)

    await message.answer(msg)
    await state.clear()

    if company:
        await message.answer("返回主菜单:", reply_markup=main_menu_kb(tg_id=message.from_user.id))


# ---- 公司升级 ----

@router.callback_query(F.data.startswith("company:upgrade:"))
async def cb_upgrade(callback: types.CallbackQuery):
    company_id = int(callback.data.split(":")[2])
    tg_id = callback.from_user.id

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            company = await get_company_by_id(session, company_id)
            if not company or not user or company.owner_id != user.id:
                await callback.answer("无权操作", show_alert=True)
                return
            ok, msg = await upgrade_company(session, company_id)

    await callback.answer(msg, show_alert=True)
    if ok:
        from handlers.company_helpers import _refresh_company_view
        await _refresh_company_view(callback, company_id)


# ---- 公司改名（花钱 + 当日营收降低 + 二级确认） ----


@router.callback_query(F.data.startswith("company:rename:"))
async def cb_rename(callback: types.CallbackQuery, state: FSMContext):
    company_id = int(callback.data.split(":")[2])
    tg_id = callback.from_user.id

    async with async_session() as session:
        company = await get_company_by_id(session, company_id)
        if not company:
            await callback.answer("公司不存在", show_alert=True)
            return
        user = await get_user_by_tg_id(session, tg_id)
        if not user or company.owner_id != user.id:
            await callback.answer("只有老板才能改名", show_alert=True)
            return
        rename_cost = max(RENAME_MIN_COST, int(company.total_funds * RENAME_COST_RATE))

    # Check cooldown
    r = await get_redis()
    cd_ttl = await r.ttl(f"rename_cd:{company_id}")
    if cd_ttl and cd_ttl > 0:
        hours = cd_ttl // 3600
        mins = (cd_ttl % 3600) // 60
        await callback.answer(f"改名冷却中，剩余 {hours}小时{mins}分钟", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ 确认改名", callback_data=f"company:rename_confirm:{company_id}"),
            InlineKeyboardButton(text="❌ 取消", callback_data=f"company:view:{company_id}"),
        ],
    ])
    kb = tag_kb(kb, callback.from_user.id)
    await callback.message.edit_text(
        f"✏️ 公司改名 — {company.name}\n"
        f"{'─' * 24}\n"
        f"⚠️ 改名须知:\n"
        f"  💰 费用: {fmt_traffic(rename_cost)}\n"
        f"  📉 当日营收降低 {int(RENAME_REVENUE_PENALTY * 100)}%\n"
        f"  （次日结算后自动恢复）\n"
        f"  ⏰ 改名后冷却 {RENAME_COOLDOWN // 3600} 小时\n\n"
        f"确认要改名吗？",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("company:rename_confirm:"))
async def cb_rename_confirm(callback: types.CallbackQuery, state: FSMContext):
    company_id = int(callback.data.split(":")[2])
    tg_id = callback.from_user.id

    async with async_session() as session:
        company = await get_company_by_id(session, company_id)
        if not company:
            await callback.answer("公司不存在", show_alert=True)
            return
        user = await get_user_by_tg_id(session, tg_id)
        if not user or company.owner_id != user.id:
            await callback.answer("只有老板才能改名", show_alert=True)
            return

    await state.set_state(RenameCompanyState.waiting_new_name)
    await state.update_data(company_id=company_id)
    await callback.message.edit_text(f"当前名称: {company.name}\n请输入新公司名称 (2-16字):")
    await callback.answer()


@router.message(RenameCompanyState.waiting_new_name)
async def on_new_name(message: types.Message, state: FSMContext):
    new_name = message.text.strip()
    name_err = validate_name(new_name, min_len=2, max_len=16)
    if name_err:
        await message.answer(f"{name_err}，请重新输入:")
        return

    data = await state.get_data()
    company_id = data["company_id"]

    async with async_session() as session:
        async with session.begin():
            exists = await session.execute(select(Company).where(Company.name == new_name))
            if exists.scalar_one_or_none():
                await message.answer("名称已被使用，请换一个:")
                return
            company = await session.get(Company, company_id)
            if not company:
                await message.answer("公司不存在")
                await state.clear()
                return

            # Calculate and deduct rename cost
            rename_cost = max(RENAME_MIN_COST, int(company.total_funds * RENAME_COST_RATE))
            ok = await add_funds(session, company_id, -rename_cost)
            if not ok:
                await message.answer(f"❌ 公司积分不足，改名需要 {fmt_traffic(rename_cost)}")
                await state.clear()
                return

            old_name = company.name
            company.name = new_name

            # Apply revenue penalty via Redis (settlement will check this)
            r = await get_redis()
            await r.set(f"rename_penalty:{company_id}", str(RENAME_REVENUE_PENALTY), ex=86400)
            # Set rename cooldown
            await r.set(f"rename_cd:{company_id}", "1", ex=RENAME_COOLDOWN)

    await message.answer(
        f"✅ 公司改名成功! {old_name} → {new_name}\n"
        f"💰 花费: {fmt_traffic(rename_cost)}\n"
        f"📉 当日营收将降低 {int(RENAME_REVENUE_PENALTY * 100)}%，次日恢复"
    )
    await state.clear()
    await message.answer("返回主菜单:", reply_markup=main_menu_kb(tg_id=message.from_user.id))

    # 改名后刷新公司面板
    text, kb = await render_company_detail(company_id, message.from_user.id)
    sent = await message.answer(text, reply_markup=kb)
    await mark_panel(message.chat.id, sent.message_id, message.from_user.id)


@router.message(Command(CMD_DISSOLVE))
async def cmd_dissolve(message: types.Message):
    """注销公司，清空所有积分和信息，可立即重新创建。"""
    tg_id = message.from_user.id

    args = (message.text or "").split()
    if len(args) < 2 or args[1].lower() != "confirm":
        async with async_session() as session:
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                await message.answer("请先 /company_create 创建公司")
                return
            companies = await get_companies_by_owner(session, user.id)
            if not companies:
                await message.answer("你没有公司可以注销")
                return
            names = ", ".join(f"「{c.name}」" for c in companies)
        await message.answer(
            f"⚠️ 确认要注销以下公司吗？\n{names}\n\n"
            "⚠️ 注销后所有公司数据和个人积分将被清零！\n"
            "确认请发送: /company_dissolve confirm"
        )
        return

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                await message.answer("请先 /company_create 创建公司")
                return
            companies = await get_companies_by_owner(session, user.id)
            if not companies:
                await message.answer("你没有公司可以注销")
                return

            names = []
            for company in companies:
                cid = company.id
                names.append(company.name)
                # Delete all related data
                await session.execute(sql_delete(Product).where(Product.company_id == cid))
                await session.execute(sql_delete(Shareholder).where(Shareholder.company_id == cid))
                await session.execute(sql_delete(ResearchProgress).where(ResearchProgress.company_id == cid))
                await session.execute(sql_delete(Roadshow).where(Roadshow.company_id == cid))
                await session.execute(sql_delete(RealEstate).where(RealEstate.company_id == cid))
                await session.execute(sql_delete(DailyReport).where(DailyReport.company_id == cid))
                await session.execute(sql_delete(CompanyOperationProfile).where(CompanyOperationProfile.company_id == cid))
                await session.execute(sql_delete(Cooperation).where(
                    (Cooperation.company_a_id == cid) | (Cooperation.company_b_id == cid)
                ))
                await session.delete(company)

            # 清空个人积分
            user.traffic = 0
            user.reputation = 0
            await session.flush()

    await message.answer(
        f"🗑 公司已注销: {', '.join(f'「{n}」' for n in names)}\n"
        f"所有积分和声望已清零\n"
        f"使用 /company_create 重新开始"
    )
