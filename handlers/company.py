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
    CMD_RENAME,
)
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
    _safe_edit_or_send,
    _start_company_type_selection,
    render_company_detail,
    render_company_finance_detail,
)
from keyboards.menus import company_detail_kb, company_list_kb, company_manage_kb, main_menu_kb, tag_kb
from services.company_service import (
    add_funds,
    create_company,
    get_companies_by_owner,
    get_company_by_id,
    get_company_type_info,
    get_level_info,
    get_max_level,
    load_company_types,
    upgrade_company,
)
from services.user_service import add_self_points_by_user_id, get_or_create_user, get_user_by_tg_id
from utils.formatters import compact_number, fmt_quota, fmt_traffic
from utils.panel_owner import mark_panel
from utils.validators import validate_name

router = Router()


# ---- /cp_list 列出所有公司 ----

@router.message(Command(CMD_LIST_COMPANY))
async def cmd_list_company(message: types.Message):
    """列出服务器上所有公司。"""
    async with async_session() as session:
        result = await session.execute(
            select(Company).order_by(Company.cp_points.desc())
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
            f"   Lv.{c.level} | 积分余额:{fmt_traffic(c.cp_points)} | "
            f"日营收:{fmt_traffic(c.daily_revenue)} | 👷{c.employee_count}人"
        )

    await message.answer("\n".join(lines))


# ---- /cp_rank 综合实力排行 ----

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
                company.cp_points * 0.3
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


# ---- /cp_makeup 数据清理命令 ----


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


# /cp
@router.message(Command(CMD_COMPANY))
async def cmd_company(message: types.Message):
    tg_id = message.from_user.id
    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            await message.answer("请先使用 /cp_start 注册")
            return
        companies = await get_companies_by_owner(session, user.id)

    if not companies:
        await message.reply(
            "你还没有公司。",
            reply_markup=company_list_kb([], tg_id=message.from_user.id),
        )
        return

    # 只有一家公司时直接打开详情
    if len(companies) == 1:
        text, kb = await render_company_detail(companies[0].id, tg_id)
        sent = await message.reply(text, reply_markup=kb)
        await mark_panel(message.chat.id, sent.message_id, tg_id)
        return

    items = [(c.id, c.name) for c in companies]
    await message.reply("🏢 你的公司列表:", reply_markup=company_list_kb(items, tg_id=message.from_user.id))


@router.callback_query(F.data == "menu:company")
async def cb_menu_company(callback: types.CallbackQuery):
    tg_id = callback.from_user.id
    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            await callback.answer("请先 /cp_create 创建公司", show_alert=True)
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
            await callback.answer("请先 /cp_create 创建公司", show_alert=True)
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


@router.callback_query(F.data.startswith("company:manage:"))
async def cb_company_manage(callback: types.CallbackQuery):
    """公司管理二级菜单。"""
    company_id = int(callback.data.split(":")[2])
    tg_id = callback.from_user.id
    async with async_session() as session:
        company = await get_company_by_id(session, company_id)
        if not company:
            await callback.answer("公司不存在", show_alert=True)
            return
    text = f"🔧 {company.name} — 公司管理\n{'─' * 24}\n员工管理、收支明细、Buff查看、公司升级"
    await _safe_edit_or_send(callback, text, company_manage_kb(company_id, tg_id))
    await callback.answer()


# ---- 创建公司：/cp_create 命令或回调按钮 ----

@router.message(Command(CMD_CREATE_COMPANY))
async def cmd_create_company(message: types.Message, state: FSMContext):
    """创建公司命令入口。自动注册用户，无需先 /cp_start。"""
    tg_id = message.from_user.id
    tg_name = message.from_user.full_name or str(tg_id)

    async with async_session() as session:
        async with session.begin():
            user, created = await get_or_create_user(session, tg_id, tg_name)
        companies = await get_companies_by_owner(session, user.id)
        if companies:
            # 已有公司 → 直接展示
            text, kb = await render_company_detail(companies[0].id, tg_id)
            sent = await message.reply(
                "你已经拥有公司，每人只能拥有一家公司\n\n" + text,
                reply_markup=kb,
            )
            await mark_panel(message.chat.id, sent.message_id, tg_id)
            return

    welcome = ""
    if created:
        welcome = f"欢迎加入 商业帝国! 当前个人积分: {fmt_traffic(user.self_points)}\n\n"
    else:
        welcome = f"当前个人积分: {fmt_traffic(user.self_points)}\n\n"

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
                await message.answer("请先 /cp_create 创建公司")
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
    """显示升级预览面板：条件、好处、确认按钮。"""
    company_id = int(callback.data.split(":")[2])
    tg_id = callback.from_user.id

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        company = await get_company_by_id(session, company_id)
        if not company or not user or company.owner_id != user.id:
            await callback.answer("无权操作", show_alert=True)
            return

        max_level = get_max_level()
        if company.level >= max_level:
            await callback.answer(f"已达最高等级 Lv.{max_level}", show_alert=True)
            return

        next_level = company.level + 1
        next_info = get_level_info(next_level)
        current_info = get_level_info(company.level)
        if not next_info:
            await callback.answer("等级数据异常", show_alert=True)
            return

        # 获取当前状态
        prod_count = (await session.execute(
            select(sqlfunc.count()).where(Product.company_id == company_id)
        )).scalar() or 0

        tech_count = (await session.execute(
            select(sqlfunc.count()).where(
                ResearchProgress.company_id == company_id,
                ResearchProgress.status == "completed",
            )
        )).scalar() or 0

    # 构建升级条件检查
    cost = next_info["upgrade_cost"]
    min_emp = next_info.get("min_employees", 0)
    min_products = next_info.get("min_products", 0)
    min_techs = next_info.get("min_techs", 0)
    min_revenue = next_info.get("min_daily_revenue", 0)

    # 检查各项条件
    checks = []
    all_pass = True

    # 积分
    if company.cp_points >= cost:
        checks.append(f"✅ 积分: {fmt_traffic(company.cp_points)} / {fmt_traffic(cost)}")
    else:
        checks.append(f"❌ 积分: {fmt_traffic(company.cp_points)} / {fmt_traffic(cost)}")
        all_pass = False

    # 员工
    if min_emp > 0:
        if company.employee_count >= min_emp:
            checks.append(f"✅ 员工: {company.employee_count} / {min_emp}")
        else:
            checks.append(f"❌ 员工: {company.employee_count} / {min_emp}")
            all_pass = False

    # 产品
    if min_products > 0:
        if prod_count >= min_products:
            checks.append(f"✅ 产品: {prod_count} / {min_products}")
        else:
            checks.append(f"❌ 产品: {prod_count} / {min_products}")
            all_pass = False

    # 科技
    if min_techs > 0:
        if tech_count >= min_techs:
            checks.append(f"✅ 科技: {tech_count} / {min_techs}")
        else:
            checks.append(f"❌ 科技: {tech_count} / {min_techs}")
            all_pass = False

    # 日营收
    if min_revenue > 0:
        if company.daily_revenue >= min_revenue:
            checks.append(f"✅ 日营收: {fmt_traffic(company.daily_revenue)} / {fmt_traffic(min_revenue)}")
        else:
            checks.append(f"❌ 日营收: {fmt_traffic(company.daily_revenue)} / {fmt_traffic(min_revenue)}")
            all_pass = False

    # 构建文本
    lines = [
        f"⬆️ 公司升级预览",
        f"{'─' * 24}",
        f"🏢 {company.name}",
        f"📊 当前等级: Lv.{company.level}「{current_info['name'] if current_info else '未知'}」",
        f"🎯 目标等级: Lv.{next_level}「{next_info['name']}」",
        f"",
        f"📋 升级条件:",
    ]
    lines.extend(f"  {c}" for c in checks)

    # 升级好处
    lines.append(f"")
    lines.append(f"🎁 升级后获得:")
    lines.append(f"  📈 永久日营收加成: +{fmt_traffic(next_info['daily_revenue_bonus'])}")
    lines.append(f"  👷 员工上限提升: +{next_info['employee_limit_bonus']}")
    if next_info.get('description'):
        lines.append(f"  📝 {next_info['description']}")

    # 按钮
    if all_pass:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ 确认升级", callback_data=f"company:do_upgrade:{company_id}"),
                InlineKeyboardButton(text="🔙 返回", callback_data=f"company:view:{company_id}"),
            ],
        ])
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 返回（条件不足）", callback_data=f"company:view:{company_id}")],
        ])

    kb = tag_kb(kb, tg_id)
    await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("company:do_upgrade:"))
async def cb_do_upgrade(callback: types.CallbackQuery):
    """执行公司升级。"""
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


# ---- 公司改名（命令式：/cp_rename 新名字） ----


@router.message(Command(CMD_RENAME))
async def cmd_rename(message: types.Message):
    """改名命令: /cp_rename 新公司名字"""
    tg_id = message.from_user.id
    raw_args = (message.text or "").split(maxsplit=1)
    if len(raw_args) < 2 or not raw_args[1].strip():
        await message.answer("用法: /cp_rename 新公司名字\n名称长度 2-16 字，不可与已有公司重名")
        return

    new_name = raw_args[1].strip()
    name_err = validate_name(new_name, min_len=2, max_len=16)
    if name_err:
        await message.answer(f"❌ {name_err}")
        return

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            await message.answer("❌ 请先创建账号")
            return
        companies = await get_companies_by_owner(session, user.id)
        if not companies:
            await message.answer("❌ 你还没有公司")
            return
        company = companies[0]
        company_id = company.id

    # Check cooldown
    r = await get_redis()
    cd_ttl = await r.ttl(f"rename_cd:{company_id}")
    if cd_ttl and cd_ttl > 0:
        hours = cd_ttl // 3600
        mins = (cd_ttl % 3600) // 60
        await message.answer(f"❌ 改名冷却中，剩余 {hours}小时{mins}分钟")
        return

    async with async_session() as session:
        async with session.begin():
            exists = await session.execute(select(Company).where(Company.name == new_name))
            if exists.scalar_one_or_none():
                await message.answer("❌ 该名称已被使用，请换一个")
                return
            company = await session.get(Company, company_id)
            if not company:
                await message.answer("❌ 公司不存在")
                return

            rename_cost = max(RENAME_MIN_COST, int(company.cp_points * RENAME_COST_RATE))
            ok = await add_funds(session, company_id, -rename_cost)
            if not ok:
                await message.answer(f"❌ 公司资金不足，改名需要 {fmt_traffic(rename_cost)}")
                return

            old_name = company.name
            company.name = new_name

            r = await get_redis()
            await r.set(f"rename_penalty:{company_id}", str(RENAME_REVENUE_PENALTY), ex=86400)
            await r.set(f"rename_cd:{company_id}", "1", ex=RENAME_COOLDOWN)

    await message.answer(
        f"✅ 改名成功! {old_name} → {new_name}\n"
        f"💰 花费: {fmt_traffic(rename_cost)}\n"
        f"📉 当日营收降低 {int(RENAME_REVENUE_PENALTY * 100)}%，次日恢复"
    )


@router.message(Command(CMD_DISSOLVE))
async def cmd_dissolve(message: types.Message):
    """注销公司，清空所有积分和信息，可立即重新创建。"""
    tg_id = message.from_user.id

    args = (message.text or "").split()
    if len(args) < 2 or args[1].lower() != "confirm":
        async with async_session() as session:
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                await message.answer("请先 /cp_create 创建公司")
                return
            companies = await get_companies_by_owner(session, user.id)
            if not companies:
                await message.answer("你没有公司可以注销")
                return
            names = ", ".join(f"「{c.name}」" for c in companies)
        await message.answer(
            f"⚠️ 确认要注销以下公司吗？\n{names}\n\n"
            "⚠️ 注销后所有公司数据将被清空！个人积分保留。\n"
            "确认请发送: /cp_dissolve confirm"
        )
        return

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                await message.answer("请先 /cp_create 创建公司")
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

            # 保留个人积分，只清空声望
            user.reputation = 0
            await session.flush()

    await message.answer(
        f"🗑 公司已注销: {', '.join(f'「{n}」' for n in names)}\n"
        f"公司数据已清空，个人积分保留，声望已清零\n"
        f"使用 /cp_create 重新开始"
    )
