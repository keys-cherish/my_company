"""机器人入口文件。"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from config import settings
from db.engine import init_db
from cache.redis_client import close_redis
from scheduler.daily_settlement import set_bot, start_scheduler, stop_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


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
    from handlers.common import reject_private, is_admin_authenticated
    from aiogram import F, Router
    from aiogram.fsm.context import FSMContext
    fallback = Router()

    @fallback.message(F.chat.type == "private")
    async def _private_fallback(message, state: FSMContext):
        current_state = await state.get_state()
        if current_state is not None:
            return
        allowed_prefixes = (
            "/block",
            "/unblock",
            "/company",
            "/company_list",
            "/company_member",
            "/company_invest",
            "/company_start",
            "/company_create",
            "/company_admin",
            "/company_help",
            "/company_battle",
            "/company_cooperate",
            "/cp_new_product",
            "/company_dissolve",
            "/company_clear",
            "/company_rank",
            "/company_makeup",
            "/company_give",
            "/company_welfare",
            "/company_quest",
            "/company_cleanup",
            "/company_cancel",
            "/company_checkin",
            "/company_redpacket",
            "/cp_slot",
        )
        if message.text and message.text.startswith(allowed_prefixes):
            return
        if await is_admin_authenticated(message.from_user.id):
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

    # 初始化数据库
    await init_db()
    logger.info("数据库初始化完成")

    # 注册处理器
    _register_routers(dp)

    # 注册限流中间件
    from utils.throttle import ThrottleMiddleware
    from utils.topic_gate import TopicGateMiddleware
    from utils.stream_event import StreamEventMiddleware
    dp.message.middleware(TopicGateMiddleware())
    dp.callback_query.middleware(TopicGateMiddleware())
    dp.message.middleware(StreamEventMiddleware())
    dp.callback_query.middleware(StreamEventMiddleware())
    dp.message.middleware(ThrottleMiddleware())
    dp.callback_query.middleware(ThrottleMiddleware())

    # 注册面板权限中间件（outer，在路由匹配前执行）
    from utils.panel_auth import PanelOwnerMiddleware
    dp.callback_query.outer_middleware(PanelOwnerMiddleware())

    # 注册Bot命令列表（Telegram输入框命令提示）
    from handlers.start import BOT_COMMANDS
    await bot.set_my_commands(BOT_COMMANDS)

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
            await bot.set_webhook(
                url=webhook_url,
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
            site = web.TCPSite(runner, host=settings.webhook_host, port=settings.webhook_port)
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
            await dp.start_polling(bot)
    finally:
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
    if _install_uvloop():
        logger.info("uvloop 已启用（高性能事件循环）")
    asyncio.run(main())
