import asyncio
from datetime import datetime, timedelta
import logging
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from src.database.db import db
from src.bot.dispatcher import bot

logger = logging.getLogger(__name__)

async def check_forgotten_chats_loop():
    """Фоновый бесконечный цикл проверки просроченных обращений"""
    while True:
        try:
            # 1. Запрашиваем конфигурацию админа
            config = await db.systemconfig.find_first()
            if not config or not config.alertChatId:
                await asyncio.sleep(30)
                continue

            # Вычисляем дедлайн (текущее время минус N минут таймаута)
            deadline = datetime.utcnow() - timedelta(minutes=config.waitTimeoutMin)

            # 2. Ищем чаты со статусом 'opened', которые обновились до дедлайна и еще не алертились
            forgotten_chats = await db.activechat.find_many(
                where={
                    "status": "opened",
                    "isAlerted": False,
                    "updatedAt": {"lt": deadline}
                },
                include={"engineer": True}
            )

            # 3. Обрабатываем каждый забытый чат
            for chat in forgotten_chats:
                engineer_mention = chat.engineer.username if chat.engineer else "Не назначен"
                
                # Текст жесткого уведомления в рабочий чат
                alert_text = (
                    f"🚨 <b>ПРОЁБАНО ОБРАЩЕНИЕ!</b>\n\n"
                    f"👤 <b>Инженер:</b> {engineer_mention}\n"
                    f"🏢 <b>Клиент:</b> {chat.clientName}\n"
                    f"⏱ <b>Статус:</b> Без ответа более {config.waitTimeoutMin} мин.\n\n"
                    f"💬 <b>Последнее сообщение:</b>\n"
                    f"<i>\"{chat.lastMessage}\"</i>"
                )

                # Кнопка перехода к сообщению
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🏃‍♂️ Перейти к сообщению", url=chat.externalChatUrl)]
                ])

                # Отправляем алерт в чат, заданный админом
                await bot.send_message(
                    chat_id=config.alertChatId,
                    text=alert_text,
                    reply_markup=keyboard
                )

                # Помечаем чат в БД как обработанный алертом, чтобы не спамить каждую минуту
                await db.activechat.update(
                    where={"id": chat.id},
                    data={"isAlerted": True}
                )
                
                logger.warning(f"Отправлен алерт по чату {chat.id} для инженера {engineer_mention}")

        except Exception as e:
            logger.error(f"Ошибка в фоновом таске проверки чатов: {e}", exc_info=True)

        # Проверка запускается раз в минуту
        await asyncio.sleep(60)