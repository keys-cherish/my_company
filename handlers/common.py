"""Common handler utilities: admin auth, group-only filter, privacy control."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Router, types
from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject

from config import settings

router = Router()
logger = logging.getLogger(__name__)
CHANNEL_ONLY_HINT = "❌ 本 bot 仅在指定话题频道内提供服务。"

# ---- 权限 ----


def admin_controls_locked() -> bool:
    """Whether admin controls are globally locked."""
    return False


def is_super_admin(tg_id: int) -> bool:
    """Strict super-admin check for high-risk commands."""
    if admin_controls_locked():
        return False
    return tg_id in settings.super_admin_tg_id_set


# ---- 过滤器 ----


def is_allowed_group_chat(chat: types.Chat) -> bool:
    """Whether this chat is one of the configured command groups."""
    if chat.type not in ("group", "supergroup"):
        return False

    allowed_ids = settings.allowed_chat_id_set
    allowed_usernames = settings.allowed_chat_username_set

    # Backward compatible: if no restriction is configured, allow all groups.
    if not allowed_ids and not allowed_usernames:
        return True

    if allowed_ids and chat.id in allowed_ids:
        return True

    username = (chat.username or "").lstrip("@").lower()
    if allowed_usernames and username in allowed_usernames:
        return True

    return False


def _extract_chat_and_thread(
    event: types.Message | types.CallbackQuery,
) -> tuple[types.Chat | None, int | None]:
    if isinstance(event, types.CallbackQuery):
        if not event.message:
            return None, None
        thread_id = getattr(event.message, "message_thread_id", None)
        return event.message.chat, thread_id

    return event.chat, event.message_thread_id


def is_allowed_topic_thread(thread_id: int | None) -> bool:
    """Whether the topic thread is allowed; empty config means unrestricted."""
    allowed_topics = settings.allowed_topic_thread_id_set
    if not allowed_topics:
        return True
    return thread_id in allowed_topics


def is_allowed_scope(event: types.Message | types.CallbackQuery) -> bool:
    """Check group/channel and topic scope together."""
    chat, thread_id = _extract_chat_and_thread(event)
    if chat is None:
        return False
    if not is_allowed_group_chat(chat):
        return False
    return is_allowed_topic_thread(thread_id)


async def _notify_channel_only(event: types.Message | types.CallbackQuery):
    try:
        if isinstance(event, types.CallbackQuery):
            await event.answer(CHANNEL_ONLY_HINT, show_alert=True)
        else:
            await event.answer(CHANNEL_ONLY_HINT)
    except Exception:
        # Keep filter behavior stable even if Telegram refuses late replies.
        pass

class GroupOnlyFilter(BaseFilter):
    """Only allow commands in configured group/topic scope."""

    async def __call__(self, event: types.Message | types.CallbackQuery) -> bool:
        return is_allowed_scope(event)


class SuperAdminOnlyFilter(BaseFilter):
    """Only allow the designated super-admin account."""

    async def __call__(self, event: types.Message | types.CallbackQuery) -> bool:
        return is_super_admin(event.from_user.id)


class GroupScopeMiddleware(BaseMiddleware):
    """Hard-block all message/callback events outside configured scope."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, (types.Message, types.CallbackQuery)):
            if not is_allowed_scope(event):
                chat, thread_id = _extract_chat_and_thread(event)
                logger.info(
                    "Blocked out-of-scope command: chat_id=%s username=%s thread_id=%s",
                    getattr(chat, "id", None),
                    getattr(chat, "username", None),
                    thread_id,
                )
                await _notify_channel_only(event)
                return None
        return await handler(event, data)


group_only = GroupOnlyFilter()
super_admin_only = SuperAdminOnlyFilter()
group_scope_middleware = GroupScopeMiddleware()


async def reject_private(message: types.Message):
    """非管理员私聊时的拒绝提示。"""
    await message.answer("此命令仅限在群组指定频道中使用。\n私聊仅支持 /cp_create 和 /cp 查看信息。")


# ── 通用辅助函数 ──────────────────────────────────────

def parse_callback_id(callback_data: str, index: int = 2) -> int:
    """从回调数据中解析指定位置的整数ID。

    回调格式: entity:action:id[:param]
    例如 "company:view:3001" → parse_callback_id(data, 2) → 3001
    """
    return int(callback_data.split(":")[index])


async def require_company_owner(
    session,
    callback: types.CallbackQuery,
    company_id: int,
) -> tuple | None:
    """验证回调用户是公司老板。返回 (user, company) 或 None（已自动回复错误）。

    常见的权限检查模式，在 15+ 个 handler 中重复出现。
    """
    from services.user_service import get_user_by_tg_id
    from services.company_service import get_company_by_id

    user = await get_user_by_tg_id(session, callback.from_user.id)
    company = await get_company_by_id(session, company_id)
    if not company or not user or company.owner_id != user.id:
        await callback.answer("无权操作", show_alert=True)
        return None
    return user, company
