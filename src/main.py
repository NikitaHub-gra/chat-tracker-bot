import src.bot
import asyncio
import logging
from aiogram import types  # ← добавьте этот импорт
from src.database.db import connect_db, disconnect_db
from src.bot.dispatcher import bot, dp
from src.tasks.scheduler import check_forgotten_chats_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# ===== ВСТАВИТЬ СЮДА =====
@dp.update()
async def debug_all_updates(update: types.Update):
    if update.message_reaction:
        print(f"\n🚨 REACTION UPDATE RECEIVED!")
        print(f"   Chat ID: {update.message_reaction.chat.id}")
        print(f"   Chat type: {update.message_reaction.chat.type}")
        print(f"   Message ID: {update.message_reaction.message_id}")
        print(f"   User: {update.message_reaction.user}")
        print(f"   New reaction: {update.message_reaction.new_reaction}")
        print(f"   Old reaction: {update.message_reaction.old_reaction}\n")
    elif update.business_message:
        print(f"📩 Business message from {update.business_message.chat.id}")
    else:
        print(f"📦 Other update: {update.event_type}")
# =========================


async def main():
    logger.info("Запуск бота контроля таймингов Telegram...")
    
    await connect_db()
    
    asyncio.create_task(check_forgotten_chats_loop())
    logger.info("Фоновый чекер запущен успешно.")
    
    try:
       await dp.start_polling(
        bot, 
        allowed_updates=[
            "message", 
            "edited_message", 
            "callback_query", 
            "message_reaction", 
            "business_connection", 
            "business_message",
            "inline_query",        
            "chosen_inline_result"
        ]
    )
    finally:
        logger.info("Остановка систем...")
        await bot.session.close()
        await disconnect_db()

if __name__ == "__main__":
    asyncio.run(main())