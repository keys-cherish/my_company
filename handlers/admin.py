"""管理员认证和配置面板。

/company_admin <密钥> — 认证管理员（需同时满足ID白名单+密钥）
认证后可私聊使用所有游戏功能 + 管理员配置面板
"""

from __future__ import annotations

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from commands import CMD_ADMIN, CMD_CLEANUP, CMD_GIVE_MONEY, CMD_WELFARE
from db.engine import async_session
from handlers.common import (
    authenticate_admin,
    is_admin_authenticated,
    is_super_admin,
    super_admin_only,
)
from keyboards.menus import main_menu_kb, tag_kb
from services.ad_service import get_active_ad_info
from services.company_service import (
    add_funds,
    get_companies_by_owner,
    get_company_by_id,
    get_company_type_info,
)
from services.cooperation_service import get_active_cooperations
from services.user_service import add_points, add_traffic, get_user_by_tg_id
from utils.formatters import fmt_currency, fmt_reputation_buff, reputation_buff_multiplier

router = Router()
GIVE_MONEY_POINTS_DIVISOR = 1000


# ---- Buff一览 ----

@router.callback_query(F.data.startswith("buff:list:"))
async def cb_buff_list(callback: types.CallbackQuery):
    company_id = int(callback.data.split(":")[2])

    async with async_session() as session:
        company = await get_company_by_id(session, company_id)
        if not company:
            await callback.answer("公司不存在", show_alert=True)
            return

        from db.models import User
        owner = await session.get(User, company.owner_id)
        rep = owner.reputation if owner else 0

        # 合作Buff（可叠加）
        coops = await get_active_cooperations(session, company_id)
        from services.cooperation_service import get_cooperation_bonus
        coop_buff = await get_cooperation_bonus(session, company_id)

    # 声望Buff（不可叠加，取最高）
    rep_mult = reputation_buff_multiplier(rep)
    rep_buff_pct = (rep_mult - 1.0) * 100

    # 广告Buff
    ad_info = await get_active_ad_info(company_id)
    ad_buff_pct = ad_info["boost_pct"] * 100 if ad_info else 0
    ad_days = ad_info["remaining_days"] if ad_info else 0

    # 公司类型Buff
    type_info = get_company_type_info(company.company_type)
    type_income_buff = type_info.get("income_bonus", 0) * 100 if type_info else 0
    type_research_buff = type_info.get("research_speed_bonus", 0) * 100 if type_info else 0
    type_cost_buff = type_info.get("cost_bonus", 0) * 100 if type_info else 0

    # 改名惩罚 & 路演翻车 & 商战Debuff (from Redis)
    from cache.redis_client import get_redis
    _r = await get_redis()
    _rename_str = await _r.get(f"rename_penalty:{company_id}")
    rename_pen = float(_rename_str) * 100 if _rename_str else 0
    _roadshow_str = await _r.get(f"roadshow_penalty:{company_id}")
    roadshow_pen = float(_roadshow_str) * 100 if _roadshow_str else 0
    from services.battle_service import get_company_revenue_debuff
    battle_debuff = await get_company_revenue_debuff(company_id) * 100
    _totalwar_str = await _r.get(f"totalwar_buff:{company_id}")
    totalwar_buff = float(_totalwar_str) * 100 if _totalwar_str else 0

    lines = [
        f"📋 {company.name} — Buff一览",
        "─" * 24,
        "",
        "【声望Buff】(不可叠加，取最高)",
        f"  当前声望: {rep}",
        f"  营收加成: +{rep_buff_pct:.1f}%",
        "",
        "【合作Buff】(可叠加，每家+2%)",
        f"  当前合作数: {len(coops)}",
        f"  合计营收加成: +{coop_buff*100:.0f}%",
        "",
        "【广告Buff】(有时效)",
    ]
    if ad_info:
        lines.append(f"  活动广告: {ad_info.get('name', '广告')}")
        lines.append(f"  营收加成: +{ad_buff_pct:.0f}%")
        lines.append(f"  剩余天数: {ad_days}天")
    else:
        lines.append("  无活动广告")

    lines += [
        "",
        "【路演Buff】(通过路演随机获得)",
        "  声望提升 → 影响声望Buff",
        "  直接积分/荣誉点奖励",
        "",
        f"【公司类型Buff】({type_info['name'] if type_info else '未知'})",
        f"  收入加成: {'+' if type_income_buff >= 0 else ''}{type_income_buff:.0f}%",
        f"  研发速度: {'+' if type_research_buff >= 0 else ''}{type_research_buff:.0f}%",
        f"  成本影响: {'+' if type_cost_buff >= 0 else ''}{type_cost_buff:.0f}%",
        "",
        "【地产Buff】(永久)",
        "  地产提供稳定日收入",
        "  地产收入不受其他Buff影响",
        "",
        "【AI研发Buff】(永久)",
        "  通过AI研发永久提升产品收入",
        "  提升幅度取决于方案评分(1-100%)",
        "─" * 24,
        "注: 合作Buff可叠加(上限50%，满级100%)，其他取最高值",
    ]

    # Debuff section (only show if any active)
    debuff_lines: list[str] = []
    if rename_pen > 0:
        debuff_lines.append(f"  改名惩罚: -{rename_pen:.0f}%（结算后恢复）")
    if roadshow_pen > 0:
        debuff_lines.append(f"  路演翻车: -{roadshow_pen:.0f}%（结算后恢复）")
    if battle_debuff > 0:
        debuff_lines.append(f"  商战Debuff: -{battle_debuff:.0f}%")
    if totalwar_buff > 0:
        debuff_lines.append(f"  全面商战Buff: +{totalwar_buff:.0f}%")
    if debuff_lines:
        lines.insert(-2, "")
        lines.insert(-2, "【当前Debuff/临时Buff】")
        for dl in debuff_lines:
            lines.insert(-2, dl)

    from keyboards.menus import company_detail_kb
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=company_detail_kb(company_id, True, tg_id=callback.from_user.id),
    )
    await callback.answer()


# ---- 管理员认证 ----

@router.message(Command(CMD_ADMIN))
async def cmd_admin(message: types.Message):
    """管理员认证: /company_admin <密钥>"""
    tg_id = message.from_user.id
    if not is_super_admin(tg_id):
        await message.answer("❌ 无权使用此命令")
        return

    # 解析密钥参数
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        # 已认证的管理员直接打开面板
        if await is_admin_authenticated(tg_id):
            # 私聊中删除命令消息（避免密钥残留在聊天记录）
            if message.chat.type == "private":
                try:
                    await message.delete()
                except Exception:
                    pass
            await message.answer(
                "⚙️ 管理员配置面板\n当前参数可实时修改:",
                reply_markup=_admin_menu_kb(tg_id=tg_id),
            )
            return
        await message.answer("用法: /company_admin <密钥>")
        return

    secret_key = parts[1].strip()

    # 尝试删除包含密钥的消息（防止密钥泄露到聊天记录）
    try:
        await message.delete()
    except Exception:
        pass

    ok, msg = await authenticate_admin(tg_id, secret_key)
    if ok:
        await message.answer(
            f"✅ {msg}\n\n⚙️ 管理员配置面板:",
            reply_markup=_admin_menu_kb(tg_id=tg_id),
        )
    else:
        await message.answer(f"❌ 认证失败: {msg}")


@router.message(Command(CMD_GIVE_MONEY))
async def cmd_give_money(message: types.Message):
    """超管命令：回复某人并发放积分，同时奖励荣誉点。"""
    if not is_super_admin(message.from_user.id):
        await message.answer("❌ 无权使用此命令")
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer("用法: 回复某人消息并发送 /company_give <积分>")
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("用法: 回复某人消息并发送 /company_give <积分>")
        return

    target = message.reply_to_message.from_user
    if target.is_bot:
        await message.answer("❌ 不能给机器人发放")
        return

    amount_str = args[1].replace(",", "").replace("_", "").strip()
    try:
        amount = int(amount_str)
    except ValueError:
        await message.answer("❌ 积分必须是整数")
        return

    if amount <= 0:
        await message.answer("❌ 积分必须大于 0")
        return

    points_gain = max(1, amount // GIVE_MONEY_POINTS_DIVISOR)

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, target.id)
            if not user:
                await message.answer("❌ 目标用户未注册，请先让对方 /company_start")
                return

            target_companies = await get_companies_by_owner(session, user.id)
            credited_company_name = ""

            if target_companies:
                target_company = target_companies[0]
                ok = await add_funds(session, target_company.id, amount)
                if not ok:
                    await message.answer("❌ 发放失败，请稍后重试")
                    return
                credited_company_name = target_company.name
            else:
                # 若对方暂无公司，则回退到个人钱包发放
                ok = await add_traffic(session, user.id, amount)
                if not ok:
                    await message.answer("❌ 发放失败，请稍后重试")
                    return

            new_points = await add_points(user.id, points_gain, session=session)

    if credited_company_name:
        await message.answer(
            f"✅ 已向 {target.full_name} 的公司「{credited_company_name}」发放 {fmt_currency(amount)}\n"
            f"🎁 同步奖励积分: +{points_gain:,}（当前 {new_points:,}）"
        )
    else:
        await message.answer(
            f"✅ 已向 {target.full_name} 发放 {fmt_currency(amount)}（个人钱包）\n"
            f"🎁 同步奖励积分: +{points_gain:,}（当前 {new_points:,}）"
        )


WELFARE_AMOUNT = 1_000_000


@router.message(Command(CMD_WELFARE))
async def cmd_welfare(message: types.Message):
    """超管命令：给全部公司发放固定积分。"""
    if not is_super_admin(message.from_user.id):
        await message.answer("❌ 无权使用此命令")
        return

    from sqlalchemy import select
    from db.models import Company

    async with async_session() as session:
        async with session.begin():
            result = await session.execute(select(Company))
            companies = list(result.scalars().all())
            if not companies:
                await message.answer("当前没有任何公司")
                return

            success = 0
            for company in companies:
                ok = await add_funds(session, company.id, WELFARE_AMOUNT)
                if ok:
                    success += 1

    await message.answer(
        f"🎁 全服福利发放完成\n"
        f"{'─' * 24}\n"
        f"发放积分: {fmt_currency(WELFARE_AMOUNT)} / 家\n"
        f"成功: {success} 家 / 共 {len(companies)} 家"
    )


# ---- 管理员配置菜单 ----

class AdminConfigState(StatesGroup):
    waiting_param_value = State()


def _admin_menu_kb(tg_id: int | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="初始积分", callback_data="admin:cfg:initial_traffic")],
        [InlineKeyboardButton(text="创建公司费用", callback_data="admin:cfg:company_creation_cost")],
        [InlineKeyboardButton(text="最低老板持股%", callback_data="admin:cfg:min_owner_share_pct")],
        [InlineKeyboardButton(text="税率", callback_data="admin:cfg:tax_rate")],
        [InlineKeyboardButton(text="分红比例", callback_data="admin:cfg:dividend_pct")],
        [InlineKeyboardButton(text="员工基础薪资", callback_data="admin:cfg:employee_salary_base")],
        [InlineKeyboardButton(text="路演费用", callback_data="admin:cfg:roadshow_cost")],
        [InlineKeyboardButton(text="路演冷却(秒)", callback_data="admin:cfg:roadshow_cooldown_seconds")],
        [InlineKeyboardButton(text="产品创建费用", callback_data="admin:cfg:product_create_cost")],
        [InlineKeyboardButton(text="手动结算", callback_data="admin:settle")],
        [InlineKeyboardButton(text="退出管理员模式", callback_data="admin:logout")],
        [InlineKeyboardButton(text="🔙 关闭", callback_data="admin:close")],
    ])
    return tag_kb(kb, tg_id)


@router.callback_query(F.data.startswith("admin:cfg:"), super_admin_only)
async def cb_admin_cfg(callback: types.CallbackQuery, state: FSMContext):
    param = callback.data.split(":")[2]
    from config import settings
    current = getattr(settings, param, "未知")
    await callback.message.edit_text(
        f"⚙️ 修改参数: {param}\n当前值: {current}\n\n请输入新值:"
    )
    await state.set_state(AdminConfigState.waiting_param_value)
    await state.update_data(param=param)
    await callback.answer()


@router.message(AdminConfigState.waiting_param_value, super_admin_only)
async def on_admin_param_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    param = data["param"]
    value_str = message.text.strip()

    from config import settings
    current = getattr(settings, param, None)
    if current is None:
        await message.answer("参数不存在")
        await state.clear()
        return

    try:
        if isinstance(current, int):
            new_value = int(value_str)
        elif isinstance(current, float):
            new_value = float(value_str)
        else:
            new_value = value_str
        setattr(settings, param, new_value)
        await message.answer(
            f"✅ 参数 {param} 已更新为: {new_value}",
            reply_markup=_admin_menu_kb(tg_id=message.from_user.id),
        )
    except (ValueError, TypeError):
        await message.answer(f"无效的值，需要 {type(current).__name__} 类型，请重新输入:")
        return

    await state.clear()


@router.callback_query(F.data == "admin:settle", super_admin_only)
async def cb_admin_settle(callback: types.CallbackQuery):
    """手动触发结算（仅私聊发送结果，不在群组暴露）。"""
    await callback.answer("正在执行结算...", show_alert=True)
    from services.settlement_service import settle_all, format_daily_report
    async with async_session() as session:
        async with session.begin():
            reports = await settle_all(session)

    lines = [f"手动结算完成，处理了 {len(reports)} 家公司:"]
    for company, report, events in reports:
        lines.append(format_daily_report(company, report, events))
        lines.append("")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n...(截断)"

    # 如果在群组触发，私聊发送结果，群内只提示
    if callback.message.chat.type in ("group", "supergroup"):
        try:
            await callback.bot.send_message(
                callback.from_user.id,
                text,
                reply_markup=_admin_menu_kb(tg_id=callback.from_user.id),
            )
            await callback.message.edit_text("✅ 结算完成，结果已私聊发送。")
        except Exception:
            await callback.message.edit_text("结算完成，但无法私聊发送结果，请先私聊bot一次。")
    else:
        await callback.message.edit_text(text, reply_markup=_admin_menu_kb(tg_id=callback.from_user.id))


@router.callback_query(F.data == "admin:logout", super_admin_only)
async def cb_admin_logout(callback: types.CallbackQuery):
    """退出管理员模式。"""
    from handlers.common import revoke_admin
    await revoke_admin(callback.from_user.id)
    await callback.message.edit_text("已退出管理员模式。如需重新进入请使用 /company_admin <密钥>")
    await callback.answer()


@router.callback_query(F.data == "admin:close", super_admin_only)
async def cb_admin_close(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.answer()


# ---- /company_cleanup 清理过期数据 ----

@router.message(Command(CMD_CLEANUP))
async def cmd_cleanup(message: types.Message):
    """超管命令：清理数据库和Redis中的过期/残留数据。"""
    if not is_super_admin(message.from_user.id):
        await message.answer("❌ 无权使用此命令")
        return

    from cache.redis_client import get_redis
    r = await get_redis()
    cleaned = []

    # 1. 清理旧版注销冷却 (dissolve_cd:*)
    cd_keys = []
    async for key in r.scan_iter("dissolve_cd:*"):
        cd_keys.append(key)
    if cd_keys:
        await r.delete(*cd_keys)
        cleaned.append(f"注销冷却键: {len(cd_keys)} 个")

    # 2. 清理面板所有权缓存 (panel:*)
    panel_keys = []
    async for key in r.scan_iter("panel:*"):
        panel_keys.append(key)
    if panel_keys:
        await r.delete(*panel_keys)
        cleaned.append(f"面板缓存键: {len(panel_keys)} 个")

    # 3. 清理产品升级冷却 (product_upgrade_cd:*)
    upgrade_keys = []
    async for key in r.scan_iter("product_upgrade_cd:*"):
        upgrade_keys.append(key)
    if upgrade_keys:
        await r.delete(*upgrade_keys)
        cleaned.append(f"产品升级冷却键: {len(upgrade_keys)} 个")

    # 4. 清理改名惩罚 (rename_penalty:*)
    rename_keys = []
    async for key in r.scan_iter("rename_penalty:*"):
        rename_keys.append(key)
    if rename_keys:
        await r.delete(*rename_keys)
        cleaned.append(f"改名惩罚键: {len(rename_keys)} 个")

    # 5. 清理战斗冷却 (battle_cd:*)
    battle_keys = []
    async for key in r.scan_iter("battle_cd:*"):
        battle_keys.append(key)
    if battle_keys:
        await r.delete(*battle_keys)
        cleaned.append(f"战斗冷却键: {len(battle_keys)} 个")

    # 6. 数据库：修复科研时间异常（started_at 在未来的记录，重置为当前时间）
    from sqlalchemy import select, func as sqlfunc
    from sqlalchemy import delete as sql_delete
    from db.models import Company, User, Shareholder, ResearchProgress
    research_fixed = 0
    async with async_session() as session:
        async with session.begin():
            # 获取数据库服务器当前时间
            db_now = (await session.execute(select(sqlfunc.now()))).scalar()
            if db_now and getattr(db_now, "tzinfo", None):
                db_now = db_now.replace(tzinfo=None)

            # 查找 started_at 在未来的科研记录
            if db_now:
                result = await session.execute(
                    select(ResearchProgress).where(
                        ResearchProgress.status == "researching",
                        ResearchProgress.started_at > db_now,
                    )
                )
                bad_researches = list(result.scalars().all())
                for rp in bad_researches:
                    rp.started_at = db_now
                    research_fixed += 1

                if research_fixed:
                    await session.flush()

    if research_fixed:
        cleaned.append(f"科研时间异常修复: {research_fixed} 条（重置为当前时间）")

    # 7. 数据库：清理无公司用户的残留股份
    orphan_count = 0
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(select(Company.id))
            valid_company_ids = {row[0] for row in result.all()}

            if valid_company_ids:
                del_result = await session.execute(
                    sql_delete(Shareholder).where(
                        ~Shareholder.company_id.in_(valid_company_ids)
                    )
                )
                orphan_count = del_result.rowcount

    if orphan_count:
        cleaned.append(f"孤儿股份记录: {orphan_count} 条")

    # 8. Database: backfill/correct abnormal company core fields
    from services.integrity_service import backfill_company_anomalies
    backfill_msgs: list[str] = []
    async with async_session() as session:
        async with session.begin():
            backfill_msgs = await backfill_company_anomalies(session)
    if backfill_msgs:
        cleaned.extend(backfill_msgs)

    if cleaned:
        lines = ["🧹 数据清理完成:", "─" * 24] + [f"  • {c}" for c in cleaned]
    else:
        lines = ["✅ 无需清理，数据正常"]

    await message.answer("\n".join(lines))
