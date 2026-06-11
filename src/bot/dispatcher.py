from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

dp = Dispatcher()
bot: Bot | None = None


async def setup_bot(token: str) -> Bot:
    """Create and configure the bot instance. Called at startup after DB is connected."""
    global bot
    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    return bot
