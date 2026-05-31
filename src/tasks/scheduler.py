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
            # ... код выборки forgotten_chats ...

            for chat in forgotten_chats:
                try:
                    # 1. Безопасно определяем, кого тегать
                    engineer_mention = "Не назначен"
                    if chat.engineerId and chat.engineer:
                        engineer_mention = f"@{chat.engineer.username}" if chat.engineer.username else chat.engineer.name
                    elif chat.engineerId:
                        # На случай, если id есть, но связь не подгрузилась через include
                        eng = await db.engineer.find_unique(where={"id": chat.engineerId})
                        if eng:
                            engineer_mention = f"@{eng.username}" if eng.username else eng.name

                    # 2. Формируем текст сообщения
                    alert_text = (
                        f"🚨 <b>ПРОЁБАНО ОБРАЩЕНИЕ!</b>\n\n"
                        f"👤 <b>Клиент:</b> {chat.clientName}\n"
                        f"👥 <b>Чат:</b> {chat.chatTitle}\n"
                        f"👤 <b>Инженер:</b> {engineer_mention}\n"
                        f"📝 <b>Последнее сообщение:</b>\n<i>\"{chat.lastMessage}\"</i>"
                    )

                    # 3. Создаем инлайн-кнопку
                    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🏃‍♂️ Перейти к сообщению", url=chat.externalChatUrl)]
                    ])

                    # 4. Отправляем в рабочий чат алертов
                    await bot.send_message(
                        chat_id=config.alertChatId,
                        text=alert_text,
                        reply_markup=keyboard,
                        parse_mode="HTML"
                    )

                    # 5. И ТОЛЬКО ПОСЛЕ УСПЕШНОЙ ОТПРАВКИ обновляем флаг конкретно для этого юзера
                    await db.activechat.update(
                        where={
                            "chatId_userId": {
                                "chatId": chat.chatId,
                                "userId": chat.userId
                            }
                        },
                        data={"isAlerted": True}
                    )
                    print(f"🔔 Алерт по клиенту {chat.clientName} успешно отправлен и зафиксирован.")

                except Exception as e:
                    # Если один чат упал (например, ссылка кривая или данные null), 
                    # логгируем ошибку и переходим к СЛЕДУЮЩЕМУ чату в цикле
                    logger.error(f"❌ Ошибка при обработке алерта для chatId={chat.chatId}, userId={chat.userId}: {e}", exc_info=True)
                    continue

        except Exception as e:
            logger.error(f"Ошибка в фоновом таске проверки чатов: {e}", exc_info=True)

        # Проверка запускается раз в минуту
        await asyncio.sleep(60)