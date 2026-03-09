"""机器人入口文件。"""

from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from config import settings
from db.engine import init_db
from cache.points_redis_client import close_points_redis
from cache.redis_client import close_redis
from scheduler.daily_settlement import set_bot, start_scheduler, stop_scheduler, _create_db_backup, _upload_to_webdav

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# 热重载模式标记（跳过昂贵初始化）
_RELOAD_MODE = os.environ.get("_BOT_RELOAD") == "1"


def _register_routers(dp: Dispatcher):
    from handlers.start import router as start_router
    from handlers.company import router as company_router
    from handlers.company_ops import router as company_ops_router
    from handlers.company_employees import router as company_employees_router
    from handlers.shareholder import router as shareholder_router
    from handlers.research import router as research_router
    from handlers.product import router as product_router
    from handlers.roadshow import router as roadshow_router
    from handlers.cooperation import router as cooperation_router
    from handlers.realestate import router as realestate_router
    from handlers.dividend import router as dividend_router
    from handlers.funds import router as funds_router
    from handlers.ad import router as ad_router
    from handlers.ai_rd import router as ai_rd_router
    from handlers.total_war import router as total_war_router
    from handlers.ai_chat import router as ai_chat_router
    from handlers.admin import router as admin_router
    from handlers.exchange import router as exchange_router
    from handlers.battle import router as battle_router
    from handlers.quest import router as quest_router
    from handlers.slot_machine import router as slot_router
    from handlers.checkin import router as checkin_router
    from handlers.redpacket import router as redpacket_router
    from handlers.bounty import router as bounty_router
    from handlers.roulette import router as roulette_router

    dp.include_router(start_router)
    dp.include_router(company_router)
    dp.include_router(company_ops_router)
    dp.include_router(company_employees_router)
    dp.include_router(shareholder_router)
    dp.include_router(research_router)
    dp.include_router(product_router)
    dp.include_router(roadshow_router)
    dp.include_router(cooperation_router)
    dp.include_router(realestate_router)
    dp.include_router(dividend_router)
    dp.include_router(funds_router)
    dp.include_router(ad_router)
    dp.include_router(ai_rd_router)
    dp.include_router(total_war_router)
    dp.include_router(ai_chat_router)
    dp.include_router(admin_router)
    dp.include_router(exchange_router)
    dp.include_router(battle_router)
    dp.include_router(quest_router)
    dp.include_router(slot_router)
    dp.include_router(checkin_router)
    dp.include_router(redpacket_router)
    dp.include_router(bounty_router)
    dp.include_router(roulette_router)

    # 私聊兜底：非管理员只允许常用命令，管理员放行
    from handlers.common import reject_private
    from aiogram import F, Router
    from aiogram.fsm.context import FSMContext
    fallback = Router()

    @fallback.message(F.chat.type == "private")
    async def _private_fallback(message, state: FSMContext):
        current_state = await state.get_state()
        if current_state is not None:
            return
        allowed_prefixes = (
            "/cp",
            "/cp_list",
            "/cp_log",
            "/cp_transfer",
            "/cp_member",
            "/cp_invest",
            "/cp_dividend",
            "/cp_exchange",
            "/cp_rename",
            "/cp_start",
            "/cp_create",
            "/cp_help",
            "/cp_battle",
            "/cp_cooperate",
            "/cp_new_product",
            "/cp_dissolve",
            "/cp_rank",
            "/cp_makeup",
            "/cp_give",
            "/cp_welfare",
            "/cp_undo",
            "/cp_quest",
            "/cp_cleanup",
            "/cp_maintain",
            "/cp_compensate",
            "/cp_cancel",
            "/cp_checkin",
            "/cp_redpacket",
            "/cp_slot",
        )
        if message.text and message.text.startswith(allowed_prefixes):
            return
        await reject_private(message)

    dp.include_router(fallback)


async def main():
    if not settings.bot_token:
        logger.error("BOT_TOKEN 未设置。请创建 .env 文件并填入 BOT_TOKEN=你的token")
        return

    bot = Bot(
        token=settings.bot_token,
        session=AiohttpSession(proxy=settings.proxy_url) if settings.proxy_url else None,
        default=DefaultBotProperties(parse_mode=None),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # 初始化数据库（热重载时跳过，表已存在）
    if not _RELOAD_MODE:
        await init_db()
        logger.info("数据库初始化完成")
    else:
        logger.info("热重载模式，跳过数据库初始化")

    # 注册处理器
    _register_routers(dp)

    # 注册限流中间件
    from utils.throttle import ThrottleMiddleware
    from utils.topic_gate import TopicGateMiddleware, TelegramErrorGuardMiddleware
    from utils.maintenance import MaintenanceModeMiddleware
    from utils.stream_event import StreamEventMiddleware
    # TelegramErrorGuardMiddleware 最外层：捕获限流/过期回调等常见TG错误
    dp.message.middleware(TelegramErrorGuardMiddleware())
    dp.callback_query.middleware(TelegramErrorGuardMiddleware())
    dp.message.middleware(TopicGateMiddleware())
    dp.callback_query.middleware(TopicGateMiddleware())
    dp.message.middleware(MaintenanceModeMiddleware())
    dp.callback_query.middleware(MaintenanceModeMiddleware())
    dp.message.middleware(StreamEventMiddleware())
    dp.callback_query.middleware(StreamEventMiddleware())
    dp.message.middleware(ThrottleMiddleware())
    dp.callback_query.middleware(ThrottleMiddleware())

    # 注册面板权限中间件（outer，在路由匹配前执行）
    from utils.panel_auth import PanelOwnerMiddleware
    dp.callback_query.outer_middleware(PanelOwnerMiddleware())

    # 后台执行耗时初始化（热重载时跳过）
    if not _RELOAD_MODE:
        async def _deferred_init():
            try:
                from services.user_service import sync_all_users_to_shared_points
                total_users, changed_users = await sync_all_users_to_shared_points()
                logger.info("共享积分同步完成: users=%d, changed=%d", total_users, changed_users)
            except Exception:
                logger.exception("共享积分同步失败")
            try:
                from handlers.start import BOT_COMMANDS
                await bot.set_my_commands(BOT_COMMANDS)
                logger.info("Bot命令列表注册完成")
            except Exception:
                logger.exception("Bot命令列表注册失败")

        asyncio.create_task(_deferred_init())

    # 启动定时任务
    set_bot(bot)
    start_scheduler()

    # 启动模式：polling / webhook
    runner: web.AppRunner | None = None
    configured_mode = (settings.run_mode or "polling").strip().lower()
    if configured_mode not in {"polling", "webhook"}:
        logger.warning("未知 RUN_MODE=%s，已回退到 polling", settings.run_mode)
        configured_mode = "polling"

    effective_mode = configured_mode
    if configured_mode == "webhook" and not settings.webhook_base_url:
        logger.warning("WEBHOOK_BASE_URL 未配置，已自动回退到 polling 模式")
        effective_mode = "polling"

    try:
        if effective_mode == "webhook":
            webhook_url = f"{settings.webhook_base_url.rstrip('/')}{settings.webhook_path}"
            cert_file = None
            if settings.webhook_cert_path:
                from aiogram.types import FSInputFile
                cert_file = FSInputFile(settings.webhook_cert_path)
            await bot.set_webhook(
                url=webhook_url,
                certificate=cert_file,
                secret_token=settings.webhook_secret_token or None,
                drop_pending_updates=True,
            )

            app = web.Application()
            request_handler = SimpleRequestHandler(
                dispatcher=dp,
                bot=bot,
                secret_token=settings.webhook_secret_token or None,
            )
            request_handler.register(app, path=settings.webhook_path)
            setup_application(app, dp, bot=bot)

            runner = web.AppRunner(app)
            await runner.setup()
            ssl_ctx = None
            if settings.webhook_cert_path:
                import ssl
                ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                key_path = settings.webhook_cert_path.replace("public.pem", "private.key")
                ssl_ctx.load_cert_chain(settings.webhook_cert_path, key_path)
            site = web.TCPSite(runner, host=settings.webhook_host, port=settings.webhook_port, ssl_context=ssl_ctx)
            await site.start()
            logger.info(
                "Webhook started at %s%s (listen %s:%d)",
                settings.webhook_base_url.rstrip("/"),
                settings.webhook_path,
                settings.webhook_host,
                settings.webhook_port,
            )
            await asyncio.Event().wait()
        else:
            logger.info("机器人启动中（polling）...")
            await dp.start_polling(
                bot,
                polling_timeout=30,
                backoff_on_timeout=5,
                drop_pending_updates=True,   # 重启后不处理积压消息，避免限流
            )
    finally:
        # 关闭前备份并上传 WebDAV（不保留本地，定时备份才留本地）
        # 覆盖: Ctrl+C / 异常崩溃 / 正常退出
        if not _RELOAD_MODE:
            try:
                file_path, table_counts = await _create_db_backup()
                total_rows = sum(table_counts.values())
                logger.info("关闭前备份完成: %s (共%d行)", file_path, total_rows)
                if await _upload_to_webdav(file_path):
                    logger.info("关闭前备份已上传 WebDAV")
                    # 上传成功后删除本地临时文件（本地只保留定时备份）
                    try:
                        file_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                else:
                    logger.warning("关闭前备份 WebDAV 上传失败，本地文件保留: %s", file_path)
            except Exception:
                logger.exception("关闭前备份失败")

        if effective_mode == "webhook":
            try:
                await bot.delete_webhook(drop_pending_updates=False)
            except Exception:
                pass
            if runner is not None:
                try:
                    await runner.cleanup()
                except Exception:
                    pass
        stop_scheduler()
        await close_redis()
        await close_points_redis()
        await bot.session.close()


def _install_uvloop() -> bool:
    """Install uvloop as the default asyncio event loop policy.

    Called before asyncio.run() so every coroutine benefits from uvloop.
    Returns True if uvloop was installed, False otherwise.
    """
    if not settings.use_uvloop:
        return False
    try:
        import uvloop

        uvloop.install()
        return True
    except ImportError:
        logger.warning("uvloop 未安装（Windows 不支持），使用默认 asyncio 事件循环")
        return False
    except Exception:
        logger.warning("uvloop 安装失败，回退到默认 asyncio 事件循环", exc_info=True)
        return False


if __name__ == "__main__":
    import sys

    if "--reload" in sys.argv:
        from watchfiles import run_process

        logger.info("热重载模式启动，监听 .py 文件变更...")
        # 子进程中设置快速启动标记
        os.environ["_BOT_RELOAD"] = "1"
        run_process(
            ".",
            target=lambda: asyncio.run(main()),
            watch_filter=lambda change, path: path.endswith(".py"),
        )
    else:
        if _install_uvloop():
            logger.info("uvloop 已启用（高性能事件循环）")
        asyncio.run(main())
