import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

dp = Dispatcher()
bot: Bot | None = None


async def setup_bot(token: str) -> Bot:
    """Create and configure the bot instance. Called at startup after DB is connected."""
    global bot
    proxy = os.getenv("TG_PROXY")
    session = AiohttpSession(proxy=proxy) if proxy else None
    bot = Bot(
        token=token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    return bot
