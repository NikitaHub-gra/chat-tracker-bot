import asyncio
import logging
from src.database.db import connect_db, disconnect_db, db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def initialize_database():
    """Скрипт первичной инициализации данных в SQLite"""
    try:
        # Подключаемся к БД
        await connect_db()
        
        # Проверяем, существует ли уже конфигурация
        config_count = await db.systemconfig.count()
        
        if config_count == 0:
            logger.info("База данных пуста. Создаем дефолтную конфигурацию...")
            await db.systemconfig.create(
                data={
                    "waitTimeoutMin": 15,
                    "alertChatId": "" # Изначально пусто, админ настроит через бота
                }
            )
            logger.info("Дефолтная конфигурация успешно создана (Таймаут: 15 минут).")
        else:
            logger.info("Конфигурация системы уже существует в БД. Пропускаем.")
            
    except Exception as e:
        logger.error(f"Ошибка при инициализации базы данных: {e}", exc_info=True)
    finally:
        # Всегда отключаемся от БД
        await disconnect_db()

if __name__ == "__main__":
    asyncio.run(initialize_database())