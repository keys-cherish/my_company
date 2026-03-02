"""员工管理处理器（招聘/裁员）。"""

from __future__ import annotations

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from commands import CMD_MEMBER
from config import settings as cfg
from db.engine import async_session
from handlers.company_helpers import _refresh_company_view, _safe_edit_or_send
from keyboards.menus import employee_manage_kb, tag_kb
from services.company_service import (
    add_funds,
    calc_employee_income,
    get_company_by_id,
    get_company_employee_limit,
)
from services.operations_service import get_or_create_profile
from services.user_service import get_user_by_tg_id
from utils.formatters import fmt_traffic

router = Router()


# ---- /company_member 命令：招聘/裁员 ----

@router.message(Command(CMD_MEMBER))
async def cmd_member(message: types.Message):
    """Handle /company_member add|minus <count>."""
    tg_id = message.from_user.id
    args = (message.text or "").split()

    if len(args) < 3:
        await message.answer(
            "👷 员工管理:\n"
            "  /company_member add <数量> — 招聘员工\n"
            "  /company_member add max — 招满\n"
            "  /company_member minus <数量> — 裁员\n"
            "例: /company_member add 5"
        )
        return

    action = args[1].lower()
    count_str = args[2].strip()

    if action not in ("add", "minus"):
        await message.answer("❌ 操作只能是 add 或 minus")
        return

    from services.company_service import get_companies_by_owner

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            if not user:
                await message.answer("请先 /company_create 创建公司")
                return
            companies = await get_companies_by_owner(session, user.id)
            if not companies:
                await message.answer("你还没有公司")
                return
            company = companies[0]

            max_emp = get_company_employee_limit(company.level, company.company_type)

            if action == "add":
                available_slots = max_emp - company.employee_count
                if available_slots <= 0:
                    await message.answer(f"❌ 已达员工上限 ({max_emp}人)，升级公司可提升上限")
                    return

                if count_str == "max":
                    hire_count = available_slots
                else:
                    try:
                        hire_count = int(count_str)
                    except ValueError:
                        await message.answer("❌ 数量必须是数字或 max")
                        return

                hire_count = min(hire_count, available_slots)
                if hire_count <= 0:
                    await message.answer("❌ 无可用名额")
                    return

                hire_cost_per = cfg.employee_salary_base * 10
                total_cost = hire_count * hire_cost_per

                ok = await add_funds(session, company.id, -total_cost)
                if not ok:
                    affordable = company.total_funds // hire_cost_per
                    if affordable <= 0:
                        await message.answer(f"❌ 公司积分不足，每人招聘需要 {fmt_traffic(hire_cost_per)}")
                        return
                    hire_count = min(hire_count, affordable)
                    total_cost = hire_count * hire_cost_per
                    ok = await add_funds(session, company.id, -total_cost)
                    if not ok:
                        await message.answer("❌ 公司积分不足")
                        return

                company.employee_count += hire_count
                # 立即更新周任务进度
                from services.quest_service import update_quest_progress
                await update_quest_progress(
                    session, user.id, "employee_count",
                    current_value=company.employee_count,
                )
                await message.answer(
                    f"✅ 招聘成功! 招了 {hire_count} 人\n"
                    f"花费: {fmt_traffic(total_cost)}\n"
                    f"当前员工: {company.employee_count}/{max_emp}"
                )

            else:  # minus
                try:
                    fire_count = int(count_str)
                except ValueError:
                    await message.answer("❌ 数量必须是数字")
                    return

                if company.employee_count <= 1:
                    await message.answer("❌ 至少需要保留1名员工")
                    return

                max_fireable = company.employee_count - 1
                fire_count = min(fire_count, max_fireable)
                if fire_count <= 0:
                    await message.answer("❌ 至少需要保留1名员工")
                    return

                company.employee_count -= fire_count
                await message.answer(
                    f"✅ 裁员完成! 裁了 {fire_count} 人\n"
                    f"当前员工: {company.employee_count}/{max_emp}"
                )


# ---- 员工管理子面板 ----

@router.callback_query(F.data.startswith("company:emp_manage:"))
async def cb_emp_manage(callback: types.CallbackQuery):
    """Show employee management sub-panel with hire/fire buttons."""
    company_id = int(callback.data.split(":")[2])
    tg_id = callback.from_user.id

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        company = await get_company_by_id(session, company_id)
        if not company or not user or company.owner_id != user.id:
            await callback.answer("无权操作", show_alert=True)
            return
        max_emp = get_company_employee_limit(company.level, company.company_type)
        profile = await get_or_create_profile(session, company_id)

    hire_cost_per = cfg.employee_salary_base * 10
    ethics_label = ""
    if profile.ethics >= 70:
        hire_cost_per = int(hire_cost_per * 0.80)
        ethics_label = "（道德>=70，招聘-20%）"
    elif profile.ethics < 30:
        hire_cost_per = int(hire_cost_per * 1.50)
        ethics_label = "（道德<30，招聘+50%）"

    emp_base, emp_eff = calc_employee_income(company.employee_count, company.daily_revenue)
    emp_total = emp_base + emp_eff
    salary_total = company.employee_count * cfg.employee_salary_base
    net_emp = emp_total - salary_total

    lines = [
        f"👷 员工管理 — {company.name}",
        f"{'─' * 24}",
        f"当前员工：{company.employee_count}/{max_emp}人",
        f"招聘单价：{fmt_traffic(hire_cost_per)}/人{ethics_label}",
        f"日薪标准：{fmt_traffic(cfg.employee_salary_base)}/人/日",
        f"{'─' * 24}",
        f"💰 人力产出收益：+{fmt_traffic(emp_total)}/日",
        f"   基础产出：+{fmt_traffic(emp_base)}",
        f"   效率加成：+{fmt_traffic(emp_eff)}",
        f"💸 日薪支出：-{fmt_traffic(salary_total)}/日",
        f"📈 人力净收益：{fmt_traffic(net_emp)}/日",
        f"{'─' * 24}",
        "选择操作：",
    ]
    kb = employee_manage_kb(company_id, tg_id)
    await _safe_edit_or_send(callback, "\n".join(lines), kb)
    await callback.answer()


# ---- 招聘/裁员 ----

@router.callback_query(F.data.startswith("company:hire:"))
async def cb_hire(callback: types.CallbackQuery):
    """Show hiring confirmation panel with cost breakdown."""
    parts = callback.data.split(":")
    company_id = int(parts[2])
    count_str = parts[3] if len(parts) > 3 else "1"
    tg_id = callback.from_user.id

    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        company = await get_company_by_id(session, company_id)
        if not company or not user or company.owner_id != user.id:
            await callback.answer("无权操作", show_alert=True)
            return
        max_emp = get_company_employee_limit(company.level, company.company_type)
        if company.employee_count >= max_emp:
            await callback.answer(f"已达员工上限 ({max_emp}人)，升级公司可提升上限", show_alert=True)
            return
        available_slots = max_emp - company.employee_count
        if count_str == "max":
            desired = available_slots
        else:
            desired = int(count_str)
        hire_count = min(desired, available_slots)
        if hire_count <= 0:
            await callback.answer("无可用名额", show_alert=True)
            return
        profile = await get_or_create_profile(session, company_id)

    # Ethics affects hiring cost
    hire_cost_per = cfg.employee_salary_base * 10
    ethics_label = ""
    if profile.ethics >= 70:
        hire_cost_per = int(hire_cost_per * 0.80)
        ethics_label = "（道德≥70，-20%）"
    elif profile.ethics < 30:
        hire_cost_per = int(hire_cost_per * 1.50)
        ethics_label = "（道德<30，+50%）"
    total_cost = hire_count * hire_cost_per
    daily_salary = hire_count * cfg.employee_salary_base

    # 计算招聘前后的人力产出对比
    old_base, old_eff = calc_employee_income(company.employee_count, company.daily_revenue)
    old_emp_total = old_base + old_eff
    new_base, new_eff = calc_employee_income(company.employee_count + hire_count, company.daily_revenue)
    new_emp_total = new_base + new_eff
    income_increase = new_emp_total - old_emp_total

    lines = [
        f"👷 招聘确认",
        f"{'─' * 24}",
        f"招聘人数：{hire_count}人",
        f"单价：{fmt_traffic(hire_cost_per)}/人{ethics_label}",
        f"总费用：{fmt_traffic(total_cost)}",
        f"{'─' * 24}",
        f"👥 当前员工：{company.employee_count}/{max_emp}人",
        f"📈 招聘后日产出增加：+{fmt_traffic(income_increase)}/日",
        f"📌 招聘后日薪增加：+{fmt_traffic(daily_salary)}/日",
        f"🏦 公司积分余额：{fmt_traffic(company.total_funds)}",
    ]
    if total_cost > company.total_funds:
        affordable = company.total_funds // hire_cost_per
        lines.append(f"⚠️ 积分仅够招 {affordable} 人")

    kb = tag_kb(InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"✅ 确认招聘{hire_count}人（{fmt_traffic(total_cost)}）",
                callback_data=f"company:xhire:{company_id}:{count_str}",
            ),
            InlineKeyboardButton(text="🔙 取消", callback_data=f"company:view:{company_id}"),
        ],
    ]), tg_id)
    await _safe_edit_or_send(callback, "\n".join(lines), kb)
    await callback.answer()


@router.callback_query(F.data.startswith("company:xhire:"))
async def cb_do_hire(callback: types.CallbackQuery):
    """Execute hiring after confirmation."""
    parts = callback.data.split(":")
    company_id = int(parts[2])
    count_str = parts[3] if len(parts) > 3 else "1"
    tg_id = callback.from_user.id

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            company = await get_company_by_id(session, company_id)
            if not company or not user or company.owner_id != user.id:
                await callback.answer("无权操作", show_alert=True)
                return
            max_emp = get_company_employee_limit(company.level, company.company_type)
            if company.employee_count >= max_emp:
                await callback.answer(f"已达员工上限 ({max_emp}人)", show_alert=True)
                return

            available_slots = max_emp - company.employee_count
            if count_str == "max":
                desired = available_slots
            else:
                desired = int(count_str)
            hire_count = min(desired, available_slots)
            if hire_count <= 0:
                await callback.answer("无可用名额", show_alert=True)
                return

            # Ethics affects hiring cost
            profile = await get_or_create_profile(session, company_id)
            hire_cost_per = cfg.employee_salary_base * 10
            if profile.ethics >= 70:
                hire_cost_per = int(hire_cost_per * 0.80)
            elif profile.ethics < 30:
                hire_cost_per = int(hire_cost_per * 1.50)
            total_cost = hire_count * hire_cost_per

            ok = await add_funds(session, company_id, -total_cost)
            if not ok:
                if hire_count > 1:
                    affordable = company.total_funds // hire_cost_per
                    if affordable <= 0:
                        await callback.answer(f"公司积分不足，每人招聘需要 {fmt_traffic(hire_cost_per)}", show_alert=True)
                        return
                    hire_count = min(hire_count, affordable)
                    total_cost = hire_count * hire_cost_per
                    ok = await add_funds(session, company_id, -total_cost)
                    if not ok:
                        await callback.answer("公司积分不足", show_alert=True)
                        return
                else:
                    await callback.answer(f"公司积分不足，招聘需要 {fmt_traffic(hire_cost_per)}", show_alert=True)
                    return
            company.employee_count += hire_count
            # 立即更新周任务进度
            from services.quest_service import update_quest_progress
            await update_quest_progress(
                session, user.id, "employee_count",
                current_value=company.employee_count,
            )

    await callback.answer(
        f"招聘成功! 招了{hire_count}人，花费 {fmt_traffic(total_cost)}",
        show_alert=True,
    )
    await _refresh_company_view(callback, company_id)


@router.callback_query(F.data.startswith("company:fire:"))
async def cb_fire(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    company_id = int(parts[2])
    count_str = parts[3] if len(parts) > 3 else "1"
    tg_id = callback.from_user.id

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, tg_id)
            company = await get_company_by_id(session, company_id)
            if not company or not user or company.owner_id != user.id:
                await callback.answer("无权操作", show_alert=True)
                return
            if company.employee_count <= 1:
                await callback.answer("至少需要保留1名员工", show_alert=True)
                return

            max_fireable = company.employee_count - 1
            desired = int(count_str) if count_str != "max" else max_fireable
            fire_count = min(desired, max_fireable)
            if fire_count <= 0:
                await callback.answer("至少需要保留1名员工", show_alert=True)
                return
            company.employee_count -= fire_count

    await callback.answer(
        f"裁员完成! 裁了{fire_count}人",
        show_alert=True,
    )
    await _refresh_company_view(callback, company_id)
