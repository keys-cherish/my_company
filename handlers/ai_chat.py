"""@机器人 即时AI对话处理器 — 支持意图路由、工具调用、图片生成和连续对话。"""

from __future__ import annotations

import base64
import json
import re
import struct
import tempfile
import zlib
from io import BytesIO

from aiogram import F, Router, types
from aiogram.enums import ParseMode

from cache.redis_client import get_redis
from config import settings
from services.ai_chat_service import ask_ai_smart, detect_image_intent
from db.engine import async_session
from db.models import CompanyOperationProfile
from services.company_service import get_companies_by_owner, get_company_type_info, get_level_info
from services.user_service import get_user_by_tg_id

router = Router()

AI_MENTION_LIMIT_PER_MINUTE = 10
AI_MENTION_WINDOW_SECONDS = 60
CONV_HISTORY_TTL = 1800  # 30 minutes
CONV_MAX_TURNS = 10      # keep last 10 exchanges (20 messages)


# ── Placeholder PNG for image-intent pending message ─────────────────────

def _make_placeholder_png() -> bytes:
    """Generate a 200x200 solid dark PNG without external libraries."""
    w, h = 200, 200
    r, g, b = 30, 30, 40

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xFFFFFFFF)

    ihdr = _chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
    scanline = b'\x00' + bytes([r, g, b]) * w
    idat = _chunk(b'IDAT', zlib.compress(scanline * h))
    iend = _chunk(b'IEND', b'')
    return b'\x89PNG\r\n\x1a\n' + ihdr + idat + iend


_PLACEHOLDER_PNG = _make_placeholder_png()


def _is_admin_or_super_admin(tg_id: int) -> bool:
    return tg_id in settings.super_admin_tg_id_set or tg_id in settings.admin_tg_id_set


def _extract_prompt_without_mention(text: str, bot_username: str) -> str:
    username = re.escape(bot_username)
    mention_pattern = rf"(?<![A-Za-z0-9_])@{username}(?![A-Za-z0-9_])"
    cleaned = re.sub(mention_pattern, "", text, flags=re.IGNORECASE)
    return cleaned.strip()


async def _build_user_company_context(tg_id: int) -> str:
    async with async_session() as session:
        user = await get_user_by_tg_id(session, tg_id)
        if not user:
            return "用户未注册公司系统。"

        companies = await get_companies_by_owner(session, user.id)
        if not companies:
            return (
                f"用户: {user.tg_name}\n"
                f"个人积分: {user.self_points:,}\n"
                f"声望: {user.reputation}\n"
                "公司: 暂无"
            )

        company = companies[0]
        type_info = get_company_type_info(company.company_type) or {}
        level_info = get_level_info(company.level) or {}
        op = await session.get(CompanyOperationProfile, company.id)
        type_name = type_info.get("name", company.company_type)
        level_name = level_info.get("name", "未知等级")
        ethics = op.ethics if op else 60
        culture = op.culture if op else 50
        regulation = op.regulation_pressure if op else 40

        return (
            f"用户: {user.tg_name}\n"
            f"个人积分: {user.self_points:,}\n"
            f"声望: {user.reputation}\n"
            f"公司: {company.name}\n"
            f"行业: {type_name}\n"
            f"等级: Lv.{company.level} {level_name}\n"
            f"积分余额: {company.cp_points:,} 积分\n"
            f"日营收: {company.daily_revenue:,} 积分\n"
            f"员工: {company.employee_count}\n"
            f"道德: {ethics}/100\n"
            f"文化: {culture}/100\n"
            f"监管压力: {regulation}/100"
        )


# ── Conversation history helpers ─────────────────────────────────────────

def _conv_key(chat_id: int, message_id: int) -> str:
    return f"ai:conv:{chat_id}:{message_id}"


def _strip_blockquote(text: str) -> str:
    """Remove Telegram HTML blockquote wrapper for storage."""
    return (
        text.replace("<blockquote expandable>", "")
        .replace("<blockquote>", "")
        .replace("</blockquote>", "")
        .strip()
    )


async def _load_conv_history(chat_id: int, message_id: int) -> list[dict]:
    """Load conversation history from Redis by bot reply message_id."""
    r = await get_redis()
    raw = await r.get(_conv_key(chat_id, message_id))
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


async def _save_conv_history(
    chat_id: int,
    message_id: int,
    history: list[dict],
) -> None:
    """Save conversation history to Redis, keyed by bot reply message_id."""
    # Trim to last N turns
    if len(history) > CONV_MAX_TURNS * 2:
        history = history[-(CONV_MAX_TURNS * 2):]
    r = await get_redis()
    await r.set(
        _conv_key(chat_id, message_id),
        json.dumps(history, ensure_ascii=False),
        ex=CONV_HISTORY_TTL,
    )


@router.message(F.text & ~F.text.startswith("/"))
async def on_ai_bot_mention(message: types.Message):
    if not message.from_user or message.from_user.is_bot:
        return

    text = (message.text or "").strip()
    if not text:
        return

    bot_user = await message.bot.get_me()
    bot_username = (bot_user.username or "").strip()

    mention_hit = False
    if bot_username:
        username = re.escape(bot_username)
        mention_pattern = rf"(?<![A-Za-z0-9_])@{username}(?![A-Za-z0-9_])"
        mention_hit = bool(re.search(mention_pattern, text, flags=re.IGNORECASE))

    reply_to = message.reply_to_message
    reply_to_bot = bool(
        reply_to
        and reply_to.from_user
        and bot_user.id == reply_to.from_user.id
    )

    if not mention_hit and not reply_to_bot:
        return

    tg_id = message.from_user.id
    if not _is_admin_or_super_admin(tg_id):
        r = await get_redis()
        key = f"ai:mention:minute:{tg_id}"
        current = await r.incr(key)
        if current == 1:
            await r.expire(key, AI_MENTION_WINDOW_SECONDS)
        if current > AI_MENTION_LIMIT_PER_MINUTE:
            await message.reply("⏳ 你调用太频繁了：每人每分钟最多 10 次。")
            return

    prompt = _extract_prompt_without_mention(text, bot_username) if bot_username else text
    if not prompt:
        await message.reply("请在 @我 后面加上问题内容。")
        return

    company_context = await _build_user_company_context(tg_id)

    # ── Load conversation history if replying to a bot message ────────
    conv_history: list[dict] = []
    if reply_to_bot and reply_to:
        conv_history = await _load_conv_history(
            message.chat.id, reply_to.message_id,
        )

    # ── Download image from replied-to bot photo (for editing) ─────
    reply_image: bytes | None = None
    if reply_to_bot and reply_to and reply_to.photo:
        try:
            photo_obj = reply_to.photo[-1]  # highest resolution
            file_info = await message.bot.get_file(photo_obj.file_id)
            buf = BytesIO()
            await message.bot.download_file(file_info.file_path, buf)
            reply_image = buf.getvalue()
        except Exception:
            pass

    # Determine model and pending message type
    pending_model = (settings.ai_model or "").strip() or "gpt-4o-mini"
    pending_caption = f"🤖 努力思考中，请稍等…\n<blockquote>📡 {pending_model}</blockquote>"
    is_image_intent = detect_image_intent(prompt)

    if is_image_intent:
        pending = await message.reply_photo(
            photo=types.BufferedInputFile(_PLACEHOLDER_PNG, filename="loading.png"),
            caption=pending_caption,
            parse_mode=ParseMode.HTML,
        )
    else:
        pending = await message.reply(
            pending_caption,
            parse_mode=ParseMode.HTML,
        )

    content, response_type, model_name = await ask_ai_smart(
        prompt, company_context, tg_id, history=conv_history,
        image=reply_image,
    )

    model_tag = f"\n<blockquote>📡 {model_name}</blockquote>" if model_name else ""

    # ── Save conversation history on the bot's reply ──────────────────
    bot_reply_id: int | None = None

    try:
        if response_type in ("image", "images"):
            url = content.strip()
            cap = f"<blockquote>📡 {model_name}</blockquote>" if model_name else None
            cap_parse = ParseMode.HTML if cap else None

            if is_image_intent:
                # Pending is a photo → edit_media in place
                try:
                    if url.startswith("base64:"):
                        img_data = base64.b64decode(url[7:])
                        media = types.InputMediaPhoto(
                            media=types.BufferedInputFile(img_data, "result.png"),
                            caption=cap, parse_mode=cap_parse,
                        )
                    else:
                        media = types.InputMediaPhoto(
                            media=url, caption=cap, parse_mode=cap_parse,
                        )
                    await pending.edit_media(media=media)
                except Exception:
                    try:
                        await message.reply_photo(
                            photo=url, caption=cap, parse_mode=cap_parse,
                        )
                    except Exception:
                        await pending.edit_caption(
                            caption="图片发送失败，请稍后再试。",
                        )
            else:
                # Pending is text → send new photo, edit pending to tag
                try:
                    if url.startswith("base64:"):
                        img_data = base64.b64decode(url[7:])
                        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                            f.write(img_data)
                            f.flush()
                            await message.reply_photo(
                                photo=types.FSInputFile(f.name),
                                caption=cap, parse_mode=cap_parse,
                            )
                    else:
                        await message.reply_photo(
                            photo=url, caption=cap, parse_mode=cap_parse,
                        )
                    tag = f"📡 {model_name}" if model_name else "✅"
                    try:
                        await pending.edit_text(tag)
                    except Exception:
                        pass
                except Exception:
                    await pending.edit_text("图片发送失败，请稍后再试。")
        else:
            # Text response
            full = content + model_tag
            if is_image_intent:
                # Pending is photo → can't edit to text, delete + send new
                try:
                    await pending.delete()
                except Exception:
                    pass
                try:
                    sent = await message.reply(full, parse_mode=ParseMode.HTML)
                except Exception:
                    plain = (
                        content.replace("<blockquote expandable>", "")
                        .replace("</blockquote>", "")
                    )
                    if model_name:
                        plain += f"\n📡 {model_name}"
                    sent = await message.reply(plain)
                bot_reply_id = sent.message_id
            else:
                # Pending is text → edit in place
                try:
                    await pending.edit_text(full, parse_mode=ParseMode.HTML)
                except Exception:
                    plain = (
                        content.replace("<blockquote expandable>", "")
                        .replace("</blockquote>", "")
                    )
                    if model_name:
                        plain += f"\n📡 {model_name}"
                    try:
                        await pending.edit_text(plain)
                    except Exception:
                        await message.reply(full, parse_mode=ParseMode.HTML)
                bot_reply_id = pending.message_id
    except Exception:
        # Ultimate fallback
        plain = (
            content.replace("<blockquote expandable>", "")
            .replace("</blockquote>", "")
        )
        if model_name:
            plain += f"\n📡 {model_name}"
        if len(plain) > 4096:
            plain = plain[:4093] + "..."
        try:
            if is_image_intent:
                await pending.delete()
            await pending.edit_text(plain)
        except Exception:
            await message.reply(plain)
        bot_reply_id = pending.message_id

    # ── Persist conversation history for follow-up replies ────────────
    if bot_reply_id and response_type == "text":
        assistant_text = _strip_blockquote(content)
        new_history = conv_history + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": assistant_text},
        ]
        await _save_conv_history(message.chat.id, bot_reply_id, new_history)
