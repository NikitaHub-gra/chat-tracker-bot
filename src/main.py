import src.bot
import asyncio
import logging
from src.database.db import connect_db, disconnect_db
from src.bot.dispatcher import bot, dp
from src.tasks.scheduler import check_forgotten_chats_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


async def main():
    logger.info("Запуск бота контроля таймингов Telegram...")
    
    await connect_db()
    
    asyncio.create_task(check_forgotten_chats_loop())
    logger.info("Фоновый чекер запущен успешно.")
    
    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        logger.info("Остановка систем...")
        await bot.session.close()
        await disconnect_db()

if __name__ == "__main__":
    asyncio.run(main())