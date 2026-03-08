"""超管维护命令。"""

from __future__ import annotations

import datetime as dt
import json

from aiogram import F, Router, types
from aiogram.filters import Command

from commands import (
    CMD_CLEANUP,
    CMD_COMPENSATE,
    CMD_DEDUCT_MONEY,
    CMD_GIVE_MONEY,
    CMD_MAINTAIN,
    CMD_UNDO,
    CMD_WELFARE,
)
from db.engine import async_session
from handlers.common import (
    is_super_admin,
)
from services.ad_service import get_active_ad_info
from services.company_service import (
    add_funds,
    get_companies_by_owner,
    get_company_by_id,
    get_company_type_info,
)
from services.cooperation_service import get_active_cooperations
from services.user_service import add_self_points, add_points, get_user_by_tg_id
from utils.maintenance import (
    COMPENSATION_PIN_KEY,
    MAINTENANCE_COMPENSATION_BONUS,
    MAINTENANCE_MODE_KEY,
    MAINTENANCE_PIN_KEY,
    clear_maintenance_mode,
    set_maintenance_mode,
)
from utils.formatters import fmt_currency, fmt_duration, reputation_buff_multiplier

router = Router()
GIVE_MONEY_POINTS_DIVISOR = 1000  # /give_money 赠送金额的换算除数


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
        from services.cooperation_service import get_cooperation_bonus
        from services.operations_service import (
            bar10,
            calc_immoral_buff,
            ethics_rating,
            get_operation_multipliers,
            get_or_create_profile,
            reputation_rating,
        )

        viewer = await get_user_by_tg_id(session, callback.from_user.id)
        is_owner = bool(viewer and viewer.id == company.owner_id)
        owner = await session.get(User, company.owner_id)
        rep = owner.reputation if owner else 0
        coops = await get_active_cooperations(session, company_id)
        coop_buff_rate = await get_cooperation_bonus(session, company_id)
        profile = await get_or_create_profile(session, company_id)

    now_utc = dt.datetime.now(dt.UTC)
    multipliers = get_operation_multipliers(profile, now_utc)

    rep_mult = reputation_buff_multiplier(rep)
    rep_buff_rate = rep_mult - 1.0

    ad_info = await get_active_ad_info(company_id)
    ad_buff_rate = float(ad_info["boost_pct"]) if ad_info else 0.0

    from services.shop_service import get_active_buffs, get_income_buff_multiplier
    shop_buff_mult = await get_income_buff_multiplier(company_id)
    shop_buff_rate = shop_buff_mult - 1.0
    active_shop_buffs = await get_active_buffs(company_id)

    type_info = get_company_type_info(company.company_type)
    type_income_rate = type_info.get("income_bonus", 0.0) if type_info else 0.0
    type_research_rate = type_info.get("research_speed_bonus", 0.0) if type_info else 0.0
    type_cost_rate = type_info.get("cost_bonus", 0.0) if type_info else 0.0

    immoral_mult = calc_immoral_buff(profile.ethics)
    immoral_buff_rate = (immoral_mult - 1.0) if immoral_mult > 1.0 else 0.0

    from services.battle_service import get_company_revenue_debuff
    battle_debuff_rate = await get_company_revenue_debuff(company_id)

    from cache.redis_client import get_redis
    _r = await get_redis()
    rename_key = f"rename_penalty:{company_id}"
    roadshow_key = f"roadshow_penalty:{company_id}"
    totalwar_key = f"totalwar_buff:{company_id}"
    battle_key = f"battle:debuff:company:{company_id}"

    _rename_str = await _r.get(rename_key)
    rename_penalty_rate = float(_rename_str) if _rename_str else 0.0
    rename_ttl = await _r.ttl(rename_key) if rename_penalty_rate > 0 else -2

    _roadshow_str = await _r.get(roadshow_key)
    roadshow_penalty_rate = float(_roadshow_str) if _roadshow_str else 0.0
    roadshow_ttl = await _r.ttl(roadshow_key) if roadshow_penalty_rate > 0 else -2

    _totalwar_str = await _r.get(totalwar_key)
    totalwar_buff_rate = float(_totalwar_str) if _totalwar_str else 0.0
    totalwar_ttl = await _r.ttl(totalwar_key) if totalwar_buff_rate > 0 else -2

    battle_ttl = await _r.ttl(battle_key) if battle_debuff_rate > 0 else -2

    effect_rates = [
        multipliers["income_mult"] - 1.0,                  # 策略倍率（工时/办公/培训/文化）
        rep_buff_rate,                                      # 声望
        coop_buff_rate,                                     # 合作
        ad_buff_rate,                                       # 广告
        shop_buff_rate,                                     # 商城营收buff
        type_income_rate,                                   # 公司类型
        immoral_buff_rate,                                  # 缺德buff
        totalwar_buff_rate,                                 # 全面商战buff
        -battle_debuff_rate,                                # 商战减益
        -rename_penalty_rate,                               # 改名惩罚
        -roadshow_penalty_rate,                             # 路演翻车
    ]
    buff_gain_rate = sum(v for v in effect_rates if v > 0)
    buff_loss_rate = sum(-v for v in effect_rates if v < 0)
    buff_net_rate = buff_gain_rate - buff_loss_rate
    active_effect_count = sum(1 for v in effect_rates if abs(v) >= 0.001)

    def _ttl_suffix(ttl: int) -> str:
        if ttl and ttl > 0:
            return f"（剩余 {fmt_duration(ttl)}）"
        return ""

    lines = [
        f"📋 {company.name} — Buff一览",
        "─" * 24,
        f"✨ Buff总览：增益 +{buff_gain_rate*100:.0f}% | 减益 -{buff_loss_rate*100:.0f}% | "
        f"净影响 {'+' if buff_net_rate >= 0 else '-'}{abs(buff_net_rate)*100:.0f}%（{active_effect_count}项）",
        "",
        "【⚙️ 经营策略系】",
        "工时/办公/培训/保险/文化/监管已移动到「我的公司」主页面。",
        f"😐 道德：{profile.ethics} [{bar10(profile.ethics, -100, 100)}] {ethics_rating(profile.ethics)}",
        (
            f"😈 缺德Buff：道德<20触发，当前 +{immoral_buff_rate*100:.1f}%"
            if immoral_buff_rate > 0
            else "😈 缺德Buff：未触发（需道德<20）"
        ),
        "",
        "【🌐 外部增益】",
        f"⭐ 声望：{rep}（评级 {reputation_rating(rep)}） | 营收+{rep_buff_rate*100:.1f}%",
        f"🤝 合作：{len(coops)}项有效合作 | 营收+{coop_buff_rate*100:.0f}%（当日）",
        "",
        (
            f"📣 广告：{ad_info.get('name', '广告')} | 营收+{ad_buff_rate*100:.0f}% | "
            f"剩余 {fmt_duration(max(0, int(ad_info.get('remaining_seconds', 0))))}"
            if ad_info
            else "📣 广告：暂无活动广告"
        ),
        f"🛍 商城营收Buff：{'+' if shop_buff_rate >= 0 else ''}{shop_buff_rate*100:.0f}%（仅市场分析生效）",
        (
            "🧰 商城道具：" + "、".join(f"{b['name']}({b.get('remaining', '生效中')})" for b in active_shop_buffs[:4])
            + (f" 等{len(active_shop_buffs)}项" if len(active_shop_buffs) > 4 else "")
            if active_shop_buffs
            else "🧰 商城道具：暂无生效道具"
        ),
        f"🏷 公司类型：{type_info['name'] if type_info else '未知'} | 收入{type_income_rate:+.0%} | "
        f"研发{type_research_rate:+.0%} | 成本{type_cost_rate:+.0%}",
        "🏙 地产收益：固定日收入，不参与营收乘数计算",
        "🔬 AI研发：提升产品基础收入（永久生效）",
        "",
        "【⚠️ 当前减益/临时Buff】",
    ]

    temp_lines: list[str] = []
    if battle_debuff_rate > 0:
        temp_lines.append(f"⚔️ 商战Debuff：-{battle_debuff_rate*100:.0f}%{_ttl_suffix(battle_ttl)}")
    if rename_penalty_rate > 0:
        temp_lines.append(f"🏷️ 改名惩罚：-{rename_penalty_rate*100:.0f}%{_ttl_suffix(rename_ttl)}")
    if roadshow_penalty_rate > 0:
        temp_lines.append(f"🎭 路演翻车：-{roadshow_penalty_rate*100:.0f}%{_ttl_suffix(roadshow_ttl)}")
    if totalwar_buff_rate > 0:
        temp_lines.append(f"🔥 全面商战Buff：+{totalwar_buff_rate*100:.0f}%{_ttl_suffix(totalwar_ttl)}")
    if temp_lines:
        lines.extend(temp_lines)
    else:
        lines.append("当前无临时减益/Buff")

    lines.extend([
        "",
        "─" * 24,
        "注：Buff总览口径与主面板一致，不包含行业景气周期。",
    ])

    from keyboards.menus import company_detail_kb
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=company_detail_kb(company_id, is_owner, tg_id=callback.from_user.id),
    )
    await callback.answer()


@router.message(Command(CMD_GIVE_MONEY))
async def cmd_give_money(message: types.Message):
    """超管命令：回复某人并发放积分，同时奖励个人积分。"""
    if not is_super_admin(message.from_user.id):
        await message.answer("❌ 无权使用此命令")
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer("用法: 回复某人消息并发送 /cp_give <积分>")
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("用法: 回复某人消息并发送 /cp_give <积分>")
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
                await message.answer("❌ 目标用户未注册，请先让对方 /cp_start")
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
                ok = await add_points(session, user.id, amount)
                if not ok:
                    await message.answer("❌ 发放失败，请稍后重试")
                    return

            new_points = await add_self_points(user.id, points_gain, session=session)

    if credited_company_name:
        await message.answer(
            f"✅ 已向 {target.full_name} 的公司「{credited_company_name}」发放 {fmt_currency(amount)}\n"
            f"🎁 同步奖励个人积分: +{points_gain:,}（当前 {new_points:,}）"
        )
    else:
        await message.answer(
            f"✅ 已向 {target.full_name} 发放 {fmt_currency(amount)}（个人钱包）\n"
            f"🎁 同步奖励个人积分: +{points_gain:,}（当前 {new_points:,}）"
        )


@router.message(Command(CMD_DEDUCT_MONEY))
async def cmd_deduct_money(message: types.Message):
    """超管命令：回复某人并扣除其公司积分（最多扣到公司资金的1%）。"""
    if not is_super_admin(message.from_user.id):
        await message.answer("❌ 无权使用此命令")
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer("用法: 回复某人消息并发送 /cp_deduct <积分>")
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("用法: 回复某人消息并发送 /cp_deduct <积分>")
        return

    target = message.reply_to_message.from_user
    if target.is_bot:
        await message.answer("❌ 不能扣除机器人的积分")
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

    async with async_session() as session:
        async with session.begin():
            user = await get_user_by_tg_id(session, target.id)
            if not user:
                await message.answer("❌ 目标用户未注册")
                return

            target_companies = await get_companies_by_owner(session, user.id)
            if not target_companies:
                await message.answer("❌ 目标用户没有公司，无法扣除")
                return

            target_company = target_companies[0]
            # 最多扣到公司资金的99%，保留至少1%
            max_deduct = int(target_company.cp_points * 0.99)
            actual = min(amount, max_deduct)
            if actual <= 0:
                await message.answer(f"❌ 公司「{target_company.name}」资金不足，无法扣除")
                return

            ok = await add_funds(session, target_company.id, -actual)
            if not ok:
                await message.answer("❌ 扣除失败，请稍后重试")
                return

    await message.answer(
        f"✅ 已从 {target.full_name} 的公司「{target_company.name}」扣除 {fmt_currency(actual)}\n"
        f"{'⚠️ 达到上限，实际扣除 ' + fmt_currency(actual) if actual < amount else ''}"
    )


WELFARE_COMPANY_AMOUNT = 100_000
WELFARE_USER_AMOUNT = 10_000


@router.message(Command(CMD_WELFARE))
async def cmd_welfare(message: types.Message):
    """超管命令：给全部公司发放公司积分，给全部用户发放个人积分。"""
    if not is_super_admin(message.from_user.id):
        await message.answer("❌ 无权使用此命令")
        return

    from sqlalchemy import select
    from db.models import Company, User
    from services.user_service import add_self_points_by_user_id

    async with async_session() as session:
        async with session.begin():
            # 公司积分
            result = await session.execute(select(Company))
            companies = list(result.scalars().all())
            company_success = 0
            for company in companies:
                ok = await add_funds(session, company.id, WELFARE_COMPANY_AMOUNT)
                if ok:
                    company_success += 1

            # 个人积分
            result = await session.execute(select(User))
            users = list(result.scalars().all())
            user_success = 0
            for user in users:
                ok = await add_self_points_by_user_id(
                    session, user.id, WELFARE_USER_AMOUNT, reason="全服福利"
                )
                if ok:
                    user_success += 1

    # 记录到 Redis 以便撤销
    from cache.redis_client import get_redis
    r = await get_redis()
    undo_data = json.dumps({
        "action": "welfare",
        "company_ids": [c.id for c in companies],
        "company_amount": WELFARE_COMPANY_AMOUNT,
        "company_success": company_success,
        "user_ids": [u.id for u in users],
        "user_amount": WELFARE_USER_AMOUNT,
        "user_success": user_success,
    })
    await r.set(f"admin_undo:{message.from_user.id}", undo_data, ex=3600)

    await message.answer(
        f"🎁 全服福利发放完成\n"
        f"{'─' * 24}\n"
        f"公司积分: {fmt_currency(WELFARE_COMPANY_AMOUNT)} / 家\n"
        f"成功: {company_success} / {len(companies)} 家\n"
        f"{'─' * 24}\n"
        f"个人积分: {fmt_currency(WELFARE_USER_AMOUNT)} / 人\n"
        f"成功: {user_success} / {len(users)} 人"
    )


@router.message(Command(CMD_UNDO))
async def cmd_undo(message: types.Message):
    """超管命令：撤销上一次管理操作。"""
    if not is_super_admin(message.from_user.id):
        await message.answer("❌ 无权使用此命令")
        return

    from cache.redis_client import get_redis
    from services.user_service import add_self_points_by_user_id

    r = await get_redis()
    raw = await r.get(f"admin_undo:{message.from_user.id}")
    if not raw:
        await message.answer("❌ 没有可撤销的操作（1小时内有效）")
        return

    data = json.loads(raw)
    action = data.get("action")

    if action == "welfare":
        async with async_session() as session:
            async with session.begin():
                # 回扣公司积分
                company_reverted = 0
                for cid in data["company_ids"]:
                    ok = await add_funds(session, cid, -data["company_amount"])
                    if ok:
                        company_reverted += 1

                # 回扣个人积分
                user_reverted = 0
                for uid in data["user_ids"]:
                    ok = await add_self_points_by_user_id(
                        session, uid, -data["user_amount"], reason="撤销福利"
                    )
                    if ok:
                        user_reverted += 1

        await r.delete(f"admin_undo:{message.from_user.id}")
        await message.answer(
            f"↩️ 福利已撤销\n"
            f"{'─' * 24}\n"
            f"公司积分回扣: {company_reverted} / {len(data['company_ids'])} 家\n"
            f"个人积分回扣: {user_reverted} / {len(data['user_ids'])} 人"
        )
    else:
        await message.answer(f"❌ 不支持撤销的操作类型: {action}")


async def _unpin_and_delete_stored_notice(bot: types.Bot, redis_key: str) -> None:
    from cache.redis_client import get_redis
    import json

    r = await get_redis()
    raw = await r.get(redis_key)
    if not raw:
        return

    try:
        payload = json.loads(raw)
        chat_id = int(payload.get("chat_id", 0))
        message_id = int(payload.get("message_id", 0))
    except Exception:
        await r.delete(redis_key)
        return

    if chat_id > 0 and message_id > 0:
        try:
            await bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
    await r.delete(redis_key)


@router.message(Command(CMD_MAINTAIN))
async def cmd_maintain(message: types.Message):
    """超管命令：进入停机维护，锁定所有命令/回调。"""
    if not is_super_admin(message.from_user.id):
        await message.answer("❌ 无权使用此命令")
        return

    from cache.redis_client import get_redis
    import json

    r = await get_redis()
    if await r.exists(MAINTENANCE_MODE_KEY):
        await message.answer("🔧 已处于停机维护中")
        return

    extra_desc = (message.text or "").split(maxsplit=1)
    update_desc = extra_desc[1].strip() if len(extra_desc) > 1 else ""

    await _unpin_and_delete_stored_notice(message.bot, MAINTENANCE_PIN_KEY)
    await _unpin_and_delete_stored_notice(message.bot, COMPENSATION_PIN_KEY)

    thread_id = message.message_thread_id
    body_lines = [
        "🔧【停机维护公告】",
        "",
        "系统正在维护中，暂时停止所有命令和按钮操作。",
        "维护完成后将执行停机补偿（每人 +500 积分）。",
    ]
    if update_desc:
        body_lines.extend(["", "📋 本次更新内容：", update_desc])

    announce = await message.bot.send_message(
        chat_id=message.chat.id,
        text="\n".join(body_lines),
        message_thread_id=thread_id,
    )
    try:
        await message.bot.pin_chat_message(
            chat_id=message.chat.id,
            message_id=announce.message_id,
            disable_notification=False,
        )
    except Exception:
        pass

    await r.set(
        MAINTENANCE_PIN_KEY,
        json.dumps(
            {
                "chat_id": message.chat.id,
                "message_id": announce.message_id,
                "thread_id": thread_id or 0,
            },
            ensure_ascii=False,
        ),
    )
    await set_maintenance_mode(
        {
            "enabled_by": message.from_user.id,
            "chat_id": message.chat.id,
            "thread_id": thread_id or 0,
            "started_at": dt.datetime.now(dt.UTC).isoformat(),
            "update_desc": update_desc,
        }
    )
    await message.answer("✅ 已开启停机维护模式")


@router.message(Command(CMD_COMPENSATE))
async def cmd_compensate(message: types.Message):
    """超管命令：解除维护并发放停机补偿（每人 +500）。"""
    if not is_super_admin(message.from_user.id):
        await message.answer("❌ 无权使用此命令")
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer("用法: /cp_compensate <更新内容>")
        return
    update_desc = args[1].strip()

    from cache.redis_client import get_redis
    from sqlalchemy import func as sqlfunc, select
    from db.models import User
    import json

    async with async_session() as session:
        async with session.begin():
            total_users = int((await session.execute(select(sqlfunc.count(User.id)))).scalar() or 0)
            if total_users > 0:
                users = list((await session.execute(select(User))).scalars().all())
                for u in users:
                    await add_self_points(
                        u.id,
                        MAINTENANCE_COMPENSATION_BONUS,
                        session=session,
                        reason="maintenance_compensation",
                    )

    await clear_maintenance_mode()
    await _unpin_and_delete_stored_notice(message.bot, MAINTENANCE_PIN_KEY)
    await _unpin_and_delete_stored_notice(message.bot, COMPENSATION_PIN_KEY)

    thread_id = message.message_thread_id
    body = (
        "🎁【停机补偿公告】\n\n"
        f"系统维护已完成，已向全体 {total_users} 名玩家发放 "
        f"+{MAINTENANCE_COMPENSATION_BONUS} 积分补偿。\n\n"
        "📋 本次更新内容：\n"
        f"{update_desc}"
    )
    announce = await message.bot.send_message(
        chat_id=message.chat.id,
        text=body,
        message_thread_id=thread_id,
    )
    try:
        await message.bot.pin_chat_message(
            chat_id=message.chat.id,
            message_id=announce.message_id,
            disable_notification=False,
        )
    except Exception:
        pass

    r = await get_redis()
    await r.set(
        COMPENSATION_PIN_KEY,
        json.dumps(
            {
                "chat_id": message.chat.id,
                "message_id": announce.message_id,
                "thread_id": thread_id or 0,
            },
            ensure_ascii=False,
        ),
    )
    await message.answer(
        f"✅ 停机补偿完成：{total_users} 人 × {MAINTENANCE_COMPENSATION_BONUS} 积分"
    )


# ---- /cp_cleanup 清理过期数据 ----

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
    from sqlalchemy import select, func as sqlfunc, text as sql_text
    from sqlalchemy import delete as sql_delete
    from db.models import Company, User, Shareholder, ResearchProgress
    research_fixed = 0
    async with async_session() as session:
        async with session.begin():
            # LOCALTIMESTAMP matches started_at storage (both naive, same timezone)
            db_now = (await session.execute(select(sql_text("LOCALTIMESTAMP")))).scalar()

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

    # 9. 修正个人积分超出上限的用户
    from services.user_service import get_user_max_points
    points_fixed = 0
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(select(User))
            all_users = list(result.scalars().all())
            for u in all_users:
                max_pts = await get_user_max_points(session, u.id)
                if u.self_points > max_pts:
                    excess = u.self_points - max_pts
                    u.self_points = max_pts
                    points_fixed += 1
                    # 溢出部分投入公司（如有）
                    user_companies = await get_companies_by_owner(session, u.id)
                    if user_companies:
                        await add_funds(session, user_companies[0].id, excess)
            if points_fixed:
                await session.flush()
    if points_fixed:
        cleaned.append(f"个人积分超限修正: {points_fixed} 人")

    # 10. 清理超出上限的产品（每公司最多8个，保留日收入最高的）
    from db.models import Product
    from services.product_service import MAX_PRODUCTS
    products_removed = 0
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(select(Company.id))
            company_ids = [row[0] for row in result.all()]
            for cid in company_ids:
                result = await session.execute(
                    select(Product)
                    .where(Product.company_id == cid)
                    .order_by(Product.daily_income.desc())
                )
                products = list(result.scalars().all())
                if len(products) > MAX_PRODUCTS:
                    for p in products[MAX_PRODUCTS:]:
                        await session.delete(p)
                        products_removed += 1
            if products_removed:
                await session.flush()
    if products_removed:
        cleaned.append(f"超限产品清理: {products_removed} 个（保留日收入最高的{MAX_PRODUCTS}个）")

    # 11. 清理已注销但残留在数据库中的公司及其关联数据
    from db.models import (
        CompanyOperationProfile, Cooperation, DailyReport,
        Product, RealEstate, Roadshow,
    )
    dissolved_companies = 0
    orphan_related = 0
    async with async_session() as session:
        async with session.begin():
            # 找出 owner 已拥有更新公司的旧公司（同一 owner 多家公司，保留最新的）
            result = await session.execute(select(Company).order_by(Company.id))
            all_companies = list(result.scalars().all())
            owner_latest: dict[int, int] = {}
            for c in all_companies:
                if c.owner_id not in owner_latest or c.id > owner_latest[c.owner_id]:
                    owner_latest[c.owner_id] = c.id
            stale_ids = set()
            for c in all_companies:
                if c.id != owner_latest[c.owner_id]:
                    stale_ids.add(c.id)

            # 也清理 owner 不存在的孤儿公司
            result = await session.execute(select(User.id))
            valid_user_ids = {row[0] for row in result.all()}
            for c in all_companies:
                if c.owner_id not in valid_user_ids:
                    stale_ids.add(c.id)

            if stale_ids:
                for cid in stale_ids:
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
                    await session.execute(sql_delete(Company).where(Company.id == cid))
                dissolved_companies = len(stale_ids)
                await session.flush()

            # 清理关联数据中引用不存在公司的孤儿记录
            result = await session.execute(select(Company.id))
            valid_cids = {row[0] for row in result.all()}
            if valid_cids:
                for model, col in [
                    (Product, Product.company_id),
                    (Shareholder, Shareholder.company_id),
                    (ResearchProgress, ResearchProgress.company_id),
                    (Roadshow, Roadshow.company_id),
                    (RealEstate, RealEstate.company_id),
                    (DailyReport, DailyReport.company_id),
                    (CompanyOperationProfile, CompanyOperationProfile.company_id),
                ]:
                    r = await session.execute(sql_delete(model).where(~col.in_(valid_cids)))
                    orphan_related += r.rowcount
                # cooperations: either side references a deleted company
                r = await session.execute(sql_delete(Cooperation).where(
                    ~Cooperation.company_a_id.in_(valid_cids) | ~Cooperation.company_b_id.in_(valid_cids)
                ))
                orphan_related += r.rowcount
                if orphan_related:
                    await session.flush()

    if dissolved_companies:
        cleaned.append(f"残留公司清理: {dissolved_companies} 家（含全部关联数据）")
    if orphan_related:
        cleaned.append(f"孤儿关联数据清理: {orphan_related} 条")

    if cleaned:
        lines = ["🧹 数据清理完成:", "─" * 24] + [f"  • {c}" for c in cleaned]
    else:
        lines = ["✅ 无需清理，数据正常"]

    await message.answer("\n".join(lines))
