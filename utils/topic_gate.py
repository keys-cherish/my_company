"""Global chat/topic restriction middleware + Telegram error guard."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import CallbackQuery, Message, TelegramObject

from config import settings

_logger = logging.getLogger(__name__)


def _is_allowed_group_topic(chat_id: int, chat_username: str | None, thread_id: int | None) -> bool:
    allowed_chat_ids = settings.allowed_chat_id_set
    allowed_chat_usernames = settings.allowed_chat_username_set
    allowed_thread_ids = settings.allowed_topic_thread_id_set

    if allowed_chat_ids and chat_id in allowed_chat_ids:
        chat_allowed = True
    else:
        username = (chat_username or "").lstrip("@").lower()
        chat_allowed = bool(allowed_chat_usernames and username in allowed_chat_usernames)

    if (allowed_chat_ids or allowed_chat_usernames) and not chat_allowed:
        return False
    if allowed_thread_ids and thread_id not in allowed_thread_ids:
        return False
    return True


def _restriction_enabled() -> bool:
    return (
        bool(settings.allowed_chat_id_set)
        or bool(settings.allowed_chat_username_set)
        or bool(settings.allowed_topic_thread_id_set)
    )


class TopicGateMiddleware(BaseMiddleware):
    """Allow bot interactions only in configured group/topic when enabled."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not _restriction_enabled():
            return await handler(event, data)

        if isinstance(event, Message):
            chat = event.chat
            thread_id = event.message_thread_id
            if chat.type not in ("group", "supergroup") or not _is_allowed_group_topic(
                chat.id,
                chat.username,
                thread_id,
            ):
                text = (event.text or "").strip()
                if text.startswith("/"):
                    await event.answer("❌ 仅允许在指定话题频道使用本机器人。")
                return None
            return await handler(event, data)

        if isinstance(event, CallbackQuery) and event.message:
            chat = event.message.chat
            thread_id = event.message.message_thread_id
            if chat.type not in ("group", "supergroup") or not _is_allowed_group_topic(
                chat.id,
                chat.username,
                thread_id,
            ):
                await event.answer("❌ 仅允许在指定话题频道使用本机器人。", show_alert=True)
                return None
            return await handler(event, data)

        return await handler(event, data)


class TelegramErrorGuardMiddleware(BaseMiddleware):
    """Catch common Telegram API errors to prevent noisy tracebacks."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except TelegramRetryAfter as e:
            _logger.warning("TG限流 %d秒", e.retry_after)
            await asyncio.sleep(e.retry_after)
            try:
                return await handler(event, data)
            except TelegramRetryAfter as e2:
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer(f"被TG限流，请{e2.retry_after}秒后再试", show_alert=True)
                    except Exception:
                        pass
                return None
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if "query is too old" in msg or "query id is invalid" in msg:
                return None  # 过期回调，静默忽略
            if "message is not modified" in msg:
                return None
            raise
