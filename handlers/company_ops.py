"""经营策略处理器（工时/办公/培训/保险/文化/道德/监管）。"""

from __future__ import annotations

import datetime as dt

from aiogram import F, Router, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db.engine import async_session
from handlers.company_helpers import (
    _check_training_active,
    _ops_menu_kb,
    _safe_edit_or_send,
    render_company_detail,
)
from keyboards.menus import tag_kb
from services.company_service import get_company_by_id
from services.operations_service import (
    INSURANCE_LEVELS,
    OFFICE_LEVELS,
    TRAINING_LEVELS,
    WORK_HOUR_OPTIONS,
    cycle_option,
    ethics_rating,
    get_or_create_profile,
    get_training_info,
    set_work_hours,
    start_training,
)
from services.user_service import get_user_by_tg_id
from utils.formatters import fmt_traffic

router = Router()


@router.callback_query(F.data.startswith("ops:menu:"))
async def cb_ops_menu(callback: types.CallbackQuery):
    company_id = int(callback.data.split(":")[2])
    text, _ = await render_company_detail(company_id, callback.from_user.id)
    training_active = await _check_training_active(company_id)
    header = (
        "⚙️ 经营策略中心\n"
        "工时、办公、培训、保险、文化、道德会影响次日结算\n"
        "🛂 监管规则：超时加班(>8h)自动涨监管，合规(≤8h)自动降\n"
        "   监管越高→抽检越频繁→罚款越重\n"
        "请按需调整：\n\n"
    )
    await _safe_edit_or_send(
        callback,
        header + text,
        _ops_menu_kb(company_id, callback.from_user.id, training_active),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ops:work:"))
async def cb_ops_work(callback: types.CallbackQuery):
    """Show work hours confirmation panel."""
    _, _, company_id, hour = callback.data.split(":")
    cid = int(company_id)
    hours = int(hour)
    tg_id = callback.from_user.id

    info = WORK_HOUR_OPTIONS.get(hours, WORK_HOUR_OPTIONS[8])
    lines = [
        f"⏰ 切换工时确认",
        f"{'─' * 24}",
        f"目标工时：{hours}h（{info['label']}）",
        f"营收倍率：×{info['income_mult']:.2f}",
        f"成本倍率：×{info['cost_mult']:.2f}",
        f"道德变动：{'+' if info['ethics_delta'] >= 0 else ''}{info['ethics_delta']}/日",
        f"{'─' * 24}",
    ]
    if hours == 12:
        lines.extend([
            "⚠️ 危险警告：",
            "💀 高压工时：每日过劳死1-2人，道德-5/日",
            f"{'─' * 24}",
        ])
    elif hours == 24:
        lines.extend([
            "⚠️ 极度危险警告：",
            "☠️ 疯狂工时：99%监管概率，每日过劳死3-8人",
            "☠️ 道德直降负数，员工大量离职",
            f"{'─' * 24}",
        ])
    lines.append("💡 工时调整免费，立即生效")
    kb = tag_kb(InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ 确认切换", callback_data=f"ops:xwork:{cid}:{hours}"),
            InlineKeyboardButton(text="🔙 取消", callback_data=f"ops:menu:{cid}"),
        ],
    ]), tg_id)
    await _safe_edit_or_send(callback, "\n".join(lines), kb)
    await callback.answer()


@router.callback_query(F.data.startswith("ops:xwork:"))
async def cb_ops_do_work(callback: types.CallbackQuery):
    """Execute work hours change."""
    _, _, company_id, hour = callback.data.split(":")
    cid = int(company_id)
    hours = int(hour)
    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, callback.from_user.id)
            if not user:
                await callback.answer("请先 /cp_create 创建公司", show_alert=True)
                return
            ok, msg = await set_work_hours(session, cid, user.id, hours)
    await callback.answer(msg, show_alert=True)
    if ok:
        training_active = await _check_training_active(cid)
        text, _ = await render_company_detail(cid, callback.from_user.id)
        await _safe_edit_or_send(
            callback,
            "⚙️ 经营策略中心\n" + text,
            _ops_menu_kb(cid, callback.from_user.id, training_active),
        )


@router.callback_query(F.data.startswith("ops:cycle:"))
async def cb_ops_cycle(callback: types.CallbackQuery):
    """Show cycle option confirmation panel with price/effect info."""
    _, _, company_id, field = callback.data.split(":")
    cid = int(company_id)
    tg_id = callback.from_user.id

    async with async_session() as session:
        company = await get_company_by_id(session, cid)
        if not company:
            await callback.answer("公司不存在", show_alert=True)
            return
        profile = await get_or_create_profile(session, cid)

    lines = []
    if field == "office":
        keys = list(OFFICE_LEVELS.keys())
        idx = keys.index(profile.office_level) if profile.office_level in keys else 0
        cur = OFFICE_LEVELS[keys[idx]]
        if idx >= len(keys) - 1:
            await callback.answer("已是顶级办公，无需继续升级", show_alert=True)
            return
        nxt = OFFICE_LEVELS[keys[idx + 1]]
        lines = [
            "🏢 办公升级确认",
            f"{'─' * 24}",
            f"当前：{cur['name']}（营收×{cur['income_mult']:.2f}，{cur['employee_cost']}金/人/日）",
            f"升级为：{nxt['name']}（营收×{nxt['income_mult']:.2f}，{nxt['employee_cost']}金/人/日）",
            f"{'─' * 24}",
            f"📌 员工数 {company.employee_count} → 日增成本 +{(nxt['employee_cost'] - cur['employee_cost']) * company.employee_count}金",
            f"💡 升级免费，增加的是每日运营成本",
        ]
    elif field == "insurance":
        keys = list(INSURANCE_LEVELS.keys())
        idx = keys.index(profile.insurance_level) if profile.insurance_level in keys else 0
        cur = INSURANCE_LEVELS[keys[idx]]
        if idx >= len(keys) - 1:
            await callback.answer("已是最高保险方案，无需继续升级", show_alert=True)
            return
        nxt = INSURANCE_LEVELS[keys[idx + 1]]
        lines = [
            "👑 保险升级确认",
            f"{'─' * 24}",
            f"当前：{cur['name']}（罚款减免{int(cur['fine_reduction']*100)}%，费率{cur['cost_rate']*100:.1f}%）",
            f"升级为：{nxt['name']}（罚款减免{int(nxt['fine_reduction']*100)}%，费率{nxt['cost_rate']*100:.1f}%）",
            f"{'─' * 24}",
            f"💡 升级免费，保险费按薪资比例每日扣除",
        ]
    elif field == "culture":
        new_val = min(profile.culture + 8, 100)
        inc_pct = new_val / 10
        risk_pct = new_val * 0.3
        maint_pct = new_val / 200
        lines = [
            "🎭 文化建设确认",
            f"{'─' * 24}",
            f"当前文化：{profile.culture}/100",
            f"建设后：{new_val}/100",
            f"{'─' * 24}",
            f"📈 营收加成：+{inc_pct:.1f}%",
            f"🛡 风险降低：-{risk_pct:.1f}%",
            f"💰 日维护成本：营收的{maint_pct:.2f}%",
            f"💡 建设免费，但文化越高日维护成本越高",
        ]
    elif field == "ethics":
        new_val = min(profile.ethics + 6, 100)
        lines = [
            "😐 道德整改确认",
            f"{'─' * 24}",
            f"当前道德：{profile.ethics} ({ethics_rating(profile.ethics)})",
            f"整改后：{new_val} ({ethics_rating(new_val)})",
            f"{'─' * 24}",
            f"📉 道德<0时：员工每日保底离职",
            f"📉 道德<20时：员工可能离职，缺德buff最高营收+200%",
            f"📉 道德<30时：招聘成本+50%，估值-20%",
            f"🚫 道德<40时：无法发起合作",
            f"📈 道德≥70时：招聘成本-20%，估值+15%",
            f"📈 道德≥80时：合作buff翻倍",
            f"📈 道德≥90时：触发专属好事件",
            f"💡 整改免费",
        ]
    elif field == "regulation":
        overtime = max(0, profile.work_hours - 8)
        if overtime > 0:
            daily_delta = f"+{overtime * 4}/日（每超1h +4）"
        else:
            daily_delta = f"-{2 + (1 if profile.work_hours <= 6 else 0)}/日（合规自动回落）"
        reg_cost = 1.0 + profile.regulation_pressure / 50
        lines = [
            "🛂 监管说明（自动调节，无需手动操作）",
            f"{'─' * 24}",
            f"当前监管：{profile.regulation_pressure}/100",
            f"当前工时：{profile.work_hours}h",
            f"每日监管变化：{daily_delta}",
            f"{'─' * 24}",
            f"📋 监管机制：",
            f"  工时>8h → 每超1h监管+4/日",
            f"  工时≤8h → 监管-2/日（6h额外-1）",
            f"  每日抽检工时，超8h触发罚款",
            f"  监管越高 → 抽检概率越高、罚金越重",
            f"💰 合规成本：营收的{reg_cost:.1f}%",
            f"🛡 保险可减免罚金（进阶-40%，至尊-80%）",
            f"💡 想降低监管？把工时调到8h或以下",
        ]
        kb = tag_kb(InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 返回", callback_data=f"ops:menu:{cid}")],
        ]), tg_id)
        await _safe_edit_or_send(callback, "\n".join(lines), kb)
        await callback.answer()
        return
    else:
        await callback.answer("未知操作", show_alert=True)
        return

    kb = tag_kb(InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ 确认", callback_data=f"ops:xcycle:{cid}:{field}"),
            InlineKeyboardButton(text="🔙 取消", callback_data=f"ops:menu:{cid}"),
        ],
    ]), tg_id)
    await _safe_edit_or_send(callback, "\n".join(lines), kb)
    await callback.answer()


@router.callback_query(F.data.startswith("ops:xcycle:"))
async def cb_ops_do_cycle(callback: types.CallbackQuery):
    """Execute cycle option after confirmation."""
    _, _, company_id, field = callback.data.split(":")
    cid = int(company_id)
    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, callback.from_user.id)
            if not user:
                await callback.answer("请先 /cp_create 创建公司", show_alert=True)
                return
            ok, msg = await cycle_option(session, cid, user.id, field)
    await callback.answer(msg, show_alert=True)
    if ok:
        training_active = await _check_training_active(cid)
        text, _ = await render_company_detail(cid, callback.from_user.id)
        await _safe_edit_or_send(
            callback,
            "⚙️ 经营策略中心\n" + text,
            _ops_menu_kb(cid, callback.from_user.id, training_active),
        )


@router.callback_query(F.data.startswith("ops:train:"))
async def cb_ops_train(callback: types.CallbackQuery):
    """Show training confirmation panel with cost breakdown."""
    _, _, company_id, level = callback.data.split(":")
    cid = int(company_id)
    tg_id = callback.from_user.id

    if level == "none":
        # Stop training doesn't need confirmation
        async with async_session() as session:
            async with session.begin():
                user = await get_user_by_tg_id(session, callback.from_user.id)
                if not user:
                    await callback.answer("请先 /cp_create 创建公司", show_alert=True)
                    return
                ok, msg = await start_training(session, cid, user.id, "none")
        await callback.answer(msg, show_alert=True)
        if ok:
            text, _ = await render_company_detail(cid, callback.from_user.id)
            await _safe_edit_or_send(
                callback,
                "⚙️ 经营策略中心\n" + text,
                _ops_menu_kb(cid, callback.from_user.id, training_active=False),
            )
        return

    info = TRAINING_LEVELS.get(level, TRAINING_LEVELS["basic"])
    async with async_session() as session:
        company = await get_company_by_id(session, cid)
        if not company:
            await callback.answer("公司不存在", show_alert=True)
            return
        profile = await get_or_create_profile(session, cid)

    training_info = get_training_info(profile, dt.datetime.now(dt.UTC))

    # Guard: prevent downgrading to a lower training level
    if training_info["active"]:
        level_order = ["none", "basic", "pro", "elite"]
        current_idx = level_order.index(training_info["key"]) if training_info["key"] in level_order else 0
        new_idx = level_order.index(level) if level in level_order else 0
        if new_idx <= current_idx:
            cur_info = TRAINING_LEVELS.get(training_info["key"], TRAINING_LEVELS["none"])
            await callback.answer(
                f"当前「{cur_info['name']}」(x{cur_info['income_mult']:.2f}) 等级更高，"
                f"无法降级为「{info['name']}」(x{info['income_mult']:.2f})",
                show_alert=True,
            )
            return

    total_cost = company.employee_count * info["hourly_cost"] * info["duration_hours"]
    lines = [
        f"🏅 {info['name']}确认",
        f"{'─' * 24}",
        f"营收倍率：×{info['income_mult']:.2f}",
        f"持续时间：{info['duration_hours']}小时",
        f"{'─' * 24}",
    ]
    if training_info["active"]:
        cur_info = TRAINING_LEVELS.get(training_info["key"], TRAINING_LEVELS["none"])
        lines.append(f"⚠️ 当前培训「{cur_info['name']}」将被覆盖")

    lines.extend([
        f"👥 当前员工：{company.employee_count}人",
        f"💰 费用 = {company.employee_count}人 × {info['hourly_cost']}金/时 × {info['duration_hours']}h",
        f"💰 总计：{fmt_traffic(total_cost)}",
        f"🏦 公司积分：{fmt_traffic(company.cp_points)}",
        f"{'─' * 24}",
        f"🎭 开始培训额外+4文化值",
    ])

    if total_cost > company.cp_points:
        lines.append(f"❌ 积分不足！还差 {fmt_traffic(total_cost - company.cp_points)}")

    kb = tag_kb(InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"✅ 确认培训（{fmt_traffic(total_cost)}）", callback_data=f"ops:xtrain:{cid}:{level}"),
            InlineKeyboardButton(text="🔙 取消", callback_data=f"ops:menu:{cid}"),
        ],
    ]), tg_id)
    await _safe_edit_or_send(callback, "\n".join(lines), kb)
    await callback.answer()


@router.callback_query(F.data.startswith("ops:xtrain:"))
async def cb_ops_do_train(callback: types.CallbackQuery):
    """Execute training after confirmation."""
    _, _, company_id, level = callback.data.split(":")
    cid = int(company_id)
    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, callback.from_user.id)
            if not user:
                await callback.answer("请先 /cp_create 创建公司", show_alert=True)
                return
            ok, msg = await start_training(session, cid, user.id, level)
    await callback.answer(msg, show_alert=True)
    if ok:
        training_active = await _check_training_active(cid)
        text, _ = await render_company_detail(cid, callback.from_user.id)
        await _safe_edit_or_send(
            callback,
            "⚙️ 经营策略中心\n" + text,
            _ops_menu_kb(cid, callback.from_user.id, training_active),
        )
