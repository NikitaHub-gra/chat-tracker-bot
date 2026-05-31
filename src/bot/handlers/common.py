import logging
from datetime import datetime, timezone  # Изменено: добавлен явный импорт timezone
from aiogram import Router, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    InlineQuery, InlineQueryResultArticle, 
    InputTextMessageContent, ChosenInlineResult
)
from src.database.db import db

# Настраиваем логгер для вывода ошибок в консоль
logger = logging.getLogger(__name__)
router = Router(name="common")

@router.message(CommandStart())
async def cmd_start(message: types.Message):
    """Регистрация инженера при старте бота в личке"""
    if message.chat.type != "private" or not message.from_user:
        return

    tg_id = str(message.from_user.id)
    username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
    full_name = message.from_user.full_name

    try:
        await db.engineer.upsert(
            where={"telegramId": tg_id},
            data={
                "create": {"telegramId": tg_id, "username": username, "name": full_name},
                "update": {"username": username, "name": full_name}
            }
        )
        await message.reply(
            f"👋 <b>Привет, {full_name}!</b>\n"
            f"Ты успешно зарегистрирован в базе как инженер.\n\n"
            f"💼 <b>Для Telegram Business (Секретарь):</b>\n"
            f"Не забудь подключить этого бота в <i>Настройки -> Telegram Business -> Чат-боты</i>."
        )
    except Exception as e:
        logger.error(f"❌ Ошибка регистрации инженера в БД: {e}", exc_info=True)


@router.message(Command("active"))
async def cmd_get_opened_chats(message: types.Message):
    """Выводит список всех чатов, которые сейчас находятся в статусе opened"""
    if not message.from_user:
        return

    user_id = str(message.from_user.id)
    print(f"🔍 Кто-то вызвал /active. ID пользователя: {user_id}")

    # Проверяем, имеет ли право пользователь смотреть этот список
    is_engineer = await db.engineer.find_unique(where={"telegramId": user_id})
    
    from src.bot.handlers.admin import is_has_admin_rights 
    is_admin = await is_has_admin_rights(user_id)
    print(f"📊 Права пользователя {user_id}: Инженер={bool(is_engineer)}, Админ={bool(is_admin)}")

    if not is_engineer and not is_admin:
        await message.reply("У вас нет доступа к этой команде.")
        return

    # Получаем из базы все открытые тикеты
    opened_tickets = await db.activechat.find_many(
        where={"status": "opened"},
        order={"updatedAt": "asc"}
    )

    if not opened_tickets:
        await message.reply("🎉 <b>Идеально!</b> Нет ни одного чата, ожидающего ответа. Все клиенты обработаны.")
        return

    text = f"⏳ <b>Список чатов, ожидающих ответа ({len(opened_tickets)}):</b>\n\n"
    
    for idx, ticket in enumerate(opened_tickets, 1):
        now = datetime.now(timezone.utc)
        waiting_time = now - ticket.updatedAt
        minutes_waiting = int(waiting_time.total_seconds() / 60)

        text += (
            f"{idx}. 👥 <b>{ticket.chatTitle}</b>\n"
            f"   👤 Клиент: <code>{ticket.clientName}</code>\n"
            f"   💬 Последнее: <i>\"{ticket.lastMessage}\"</i>\n"
            f"   ⏰ Ждет: <b>{minutes_waiting} мин.</b>\n"
            f"   🏃‍♂️ <a href='{ticket.externalChatUrl}'>Перейти к сообщению</a>\n\n"
        )
    await message.reply(text, disable_web_page_preview=True, parse_mode="HTML")


# =====================================================================
# 1. СЕКРЕТАРЬ: Контроль личных сообщений (Telegram Business)
# =====================================================================

@router.business_message()
async def handle_business_messages(message: types.Message):
    """Ловит все сообщения в личных чатах инженера через Telegram Business"""
    if message.text and message.text.startswith("/"):
        return

    if not message.from_user:
        return

    user_id = str(message.from_user.id)

    # 1. Проверка на ручные исключения (ЧС бота)
    is_ignored = await db.ignoreduser.find_unique(where={"id": user_id})
    if is_ignored:
        return

    chat_id = str(message.chat.id)
    client_name = message.from_user.full_name
    message_text = message.text or message.caption or "[Медиафайл]"
    chat_url = f"tg://user?id={chat_id}"

    # Определяем, кто пишет в бизнес-пространстве (в ЛС userId равен chatId)
    is_client = message.from_user.id == message.chat.id

    try:
        # Проверяем, является ли отправитель инженером
        engineer = await db.engineer.find_unique(where={"telegramId": user_id})

        if engineer:
            # === ПИШЕТ ИНЖЕНЕР (Владелец аккаунта отвечает клиенту) ===
            if not is_client:
                # В ЛС ID клиента равен ID чата (chat_id)
                existing_chat = await db.activechat.find_unique(
                    where={
                        "chatId_userId": {
                            "chatId": chat_id,
                            "userId": chat_id
                        }
                    }
                )
                if existing_chat:
                    await db.activechat.update(
                        where={
                            "chatId_userId": {
                                "chatId": chat_id,
                                "userId": chat_id
                            }
                        },
                        data={
                            "status": "answered",
                            "engineerId": engineer.id,
                            "isAlerted": False,
                            "lastMessage": message_text,
                            "updatedAt": datetime.now(timezone.utc)
                        }
                    )
                else:
                    await db.activechat.create(
                        data={
                            "chatId": chat_id,
                            "userId": chat_id,
                            "chatTitle": "Личные сообщения",
                            "clientName": f"ЛС: {client_name}",
                            "externalChatUrl": chat_url,
                            "lastMessage": message_text,
                            "status": "answered",
                            "engineerId": engineer.id,
                            "isAlerted": False,
                            "updatedAt": datetime.now(timezone.utc)
                        }
                    )
                print(f"✅ [Бизнес-чат] Ответ инженера записан, статус: answered")
            return 

        # === ПИШЕТ КЛИЕНТ В ЛС ИНЖЕНЕРУ ===
        if await db.engineer.find_unique(where={"telegramId": chat_id}):
            print(f"🤫 [Бизнес-чат] В личку инженеру написал другой инженер ({chat_id}). Игнорируем.")
            return

        existing_chat = await db.activechat.find_unique(
            where={
                "chatId_userId": {
                    "chatId": chat_id,
                    "userId": chat_id
                }
            }
        )
        if existing_chat:
            is_already_opened = existing_chat.status == "opened"
            await db.activechat.update(
                where={
                    "chatId_userId": {
                        "chatId": chat_id,
                        "userId": chat_id
                    }
                },
                data={
                    "status": "opened",
                    "clientName": f"ЛС: {client_name}",
                    "chatTitle": "Личные сообщения",
                    "lastMessage": message_text,
                    "externalChatUrl": chat_url,
                    "isAlerted": False,
                    "updatedAt": existing_chat.updatedAt if is_already_opened else datetime.now(timezone.utc)
                }
            )
        else:
            await db.activechat.create(
                data={
                    "chatId": chat_id,
                    "userId": chat_id,
                    "chatTitle": "Личные сообщения",
                    "clientName": f"ЛС: {client_name}",
                    "externalChatUrl": chat_url,
                    "lastMessage": message_text,
                    "status": "opened",
                    "isAlerted": False,
                    "updatedAt": datetime.now(timezone.utc)
                }
            )
        print(f"✅ [Бизнес-чат] Новое сообщение от клиента в ЛС. Статус: opened.")

    except Exception as e:
        logger.error(f"❌ Ошибка в бизнес-чате: {e}", exc_info=True)


# =====================================================================
# 2. ГРУППЫ: Контроль сообщений в рабочих группах
# =====================================================================

@router.message()
async def handle_group_messages(message: types.Message):
    """Ловим все сообщения в рабочих группах поддержки с поштучным контролем юзеров"""
    
    print(f"\n{'='*50}")
    print(f"📥 [ГРУППА] НОВОЕ СООБЩЕНИЕ")
    print(f"   message_id: {message.message_id}")
    print(f"   chat.type: {message.chat.type}")
    print(f"   chat.id: {message.chat.id}")
    print(f"   chat.title: {message.chat.title}")
    print(f"   from_user.id: {message.from_user.id if message.from_user else 'None'}")
    print(f"   from_user.full_name: {message.from_user.full_name if message.from_user else 'None'}")
    print(f"   text: {message.text[:50] if message.text else 'None'}")
    print(f"   has reply_to_message: {bool(message.reply_to_message)}")
    print(f"{'='*50}\n")

    if message.chat.type == "private" or not message.from_user:
        print("⛔ [ГРУППА] Чат private или нет from_user — выход")
        return

    if message.text and message.text.startswith("/"):
        print("⛔ [ГРУППА] Сообщение начинается с / — выход")
        return

    user_id = str(message.from_user.id)
    print(f"🔍 [ГРУППА] user_id отправителя: {user_id}")

    is_ignored = await db.ignoreduser.find_unique(where={"id": user_id})
    if is_ignored:
        print(f"⛔ [ГРУППА] Пользователь {user_id} в ЧС — выход")
        return

    chat_id = str(message.chat.id)
    chat_title = message.chat.title or "Группа поддержки"
    message_text = message.text or message.caption or "[Медиафайл]"
    chat_url = f"https://t.me/c/{chat_id.replace('-100', '')}/{message.message_id}"

    config = await db.systemconfig.find_first()
    if config and config.alertChatId == chat_id:
        print(f"⛔ [ГРУППА] Это alert-чат ({chat_id}) — выход")
        return

    try:
        engineer = await db.engineer.find_unique(where={"telegramId": user_id})
        print(f"🔍 [ГРУППА] Инженер найден: {bool(engineer)} (tg_id={user_id})")

        if engineer:
            print(f"🔧 [ГРУППА] === ОБРАБОТКА ИНЖЕНЕРА ===")
            print(f"   engineer.id: {engineer.id}")
            print(f"   engineer.name: {engineer.name}")
            print(f"   message.reply_to_message: {message.reply_to_message}")

            if not message.reply_to_message:
                print(f"⚠️ [ГРУППА] Нет reply_to_message — выход")
                return

            print(f"   reply_to_message.message_id: {message.reply_to_message.message_id}")
            print(f"   reply_to_message.from_user: {message.reply_to_message.from_user}")

            if not message.reply_to_message.from_user:
                print(f"⚠️ [ГРУППА] reply_to_message.from_user is None — выход")
                return

            replied_user = message.reply_to_message.from_user
            replied_user_id = str(replied_user.id)
            print(f"   replied_user.id: {replied_user_id}")
            print(f"   replied_user.full_name: {replied_user.full_name}")
            print(f"   replied_user.is_bot: {replied_user.is_bot}")

            is_replied_to_engineer = await db.engineer.find_unique(where={"telegramId": replied_user_id})
            print(f"   is_replied_to_engineer: {bool(is_replied_to_engineer)}")

            if is_replied_to_engineer:
                print(f"ℹ️ [ГРУППА] Инженер ответил инженеру ({replied_user_id}). Игнорируем — выход")
                return

            print(f"🔍 [ГРУППА] Ищем тикет: chatId={chat_id}, userId={replied_user_id}")
            target_ticket = await db.activechat.find_unique(
                where={
                    "chatId_userId": {
                        "chatId": chat_id,
                        "userId": replied_user_id
                    }
                }
            )
            print(f"   target_ticket найден: {bool(target_ticket)}")
            if target_ticket:
                print(f"   target_ticket.status: {target_ticket.status}")
                print(f"   target_ticket.clientName: {target_ticket.clientName}")
                print(f"   target_ticket.engineerId: {target_ticket.engineerId}")

            if target_ticket:
                print(f"✏️ [ГРУППА] Обновляем тикет на answered...")
                try:
                    updated = await db.activechat.update(
                        where={
                            "chatId_userId": {
                                "chatId": chat_id,
                                "userId": replied_user_id
                            }
                        },
                        data={
                            "status": "answered",
                            "engineerId": engineer.id,
                            "isAlerted": False,
                            "lastMessage": f"Ответ для {replied_user.full_name}: {message_text}",
                            "updatedAt": datetime.now(timezone.utc)
                        }
                    )
                    print(f"✅ [ГРУППА] Тикет УСПЕШНО обновлён! Новый статус: {updated.status}")
                except Exception as update_err:
                    print(f"❌ [ГРУППА] ОШИБКА при обновлении тикета: {update_err}")
                    raise
            else:
                print(f"ℹ️ [ГРУППА] Тикет НЕ НАЙДЕН в БД: chatId={chat_id}, userId={replied_user_id}")
                # Дополнительно: покажем все открытые тикеты в этом чате для сравнения
                all_opened = await db.activechat.find_many(
                    where={"chatId": chat_id, "status": "opened"}
                )
                print(f"   Всего открытых тикетов в этом чате: {len(all_opened)}")
                for t in all_opened:
                    print(f"      - userId={t.userId}, clientName={t.clientName}")
            print(f"🔧 [ГРУППА] === КОНЕЦ БЛОКА ИНЖЕНЕРА ===")
            return

        # === ПИШЕТ КЛИЕНТ ===
        print(f"👤 [ГРУППА] === ОБРАБОТКА КЛИЕНТА ===")
        existing_ticket = await db.activechat.find_unique(
            where={
                "chatId_userId": {
                    "chatId": chat_id,
                    "userId": user_id
                }
            }
        )
        # ... остальной код клиента без изменений ...
        client_name = message.from_user.full_name

        if existing_ticket:
            is_already_opened = existing_ticket.status == "opened"
            await db.activechat.update(
                where={
                    "chatId_userId": {
                        "chatId": chat_id,
                        "userId": user_id
                    }
                },
                data={
                    "status": "opened",
                    "clientName": client_name,
                    "chatTitle": chat_title,
                    "lastMessage": message_text,
                    "externalChatUrl": chat_url,
                    "isAlerted": False,
                    "updatedAt": existing_ticket.updatedAt if is_already_opened else datetime.now(timezone.utc)
                }
            )
            print(f"📥 [ГРУППА] Клиент {client_name} дополнил вопрос. Таймер удержан.")
        else:
            await db.activechat.create(
                data={
                    "chatId": chat_id,
                    "userId": user_id,
                    "clientName": client_name,
                    "chatTitle": chat_title,
                    "externalChatUrl": chat_url,
                    "lastMessage": message_text,
                    "status": "opened",
                    "isAlerted": False,
                    "updatedAt": datetime.now(timezone.utc)
                }
            )
            print(f"✅ [ГРУППА] Создан тикет для клиента {client_name}")

    except Exception as e:
        logger.error(f"❌ Ошибка в групповом обработчике: {e}", exc_info=True)
        print(f"❌ [ГРУППА] ИСКЛЮЧЕНИЕ: {e}")


# =====================================================================
# 3. РЕАКЦИИ: Закрытие по эмодзи (Группы + ЛС Премиум)
# =====================================================================

@router.message_reaction()
async def handle_message_reaction(reaction_update: MessageReactionUpdated):
    """Отслеживает добавление реакций для закрытия чатов (Полный Дебаг)"""
    try:
        if not reaction_update.user:
            return
            
        user_id = str(reaction_update.user.id)
        is_engineer = await db.engineer.find_unique(where={"telegramId": user_id})
        if not is_engineer:
            return

        ALLOWED_EMOJIS = ["✅", "👌", "👍"]
        current_emoji = None
        for reaction in reaction_update.new_reaction:
            if reaction.type == "emoji" and reaction.emoji in ALLOWED_EMOJIS:
                current_emoji = reaction.emoji
                break

        if not current_emoji:
            return

        chat_id = str(reaction_update.chat.id)
        chat_type = reaction_update.chat.type
        message_id = reaction_update.message_id


        print(f"\n===== 🔍 ДЕБАГ РЕАКЦИИ =====")
        print(f"Кто поставил: {is_engineer.name} ({user_id})")
        print(f"Эмодзи: {current_emoji}")
        print(f"ID чата, откуда пришло: {chat_id}")
        print(f"Тип чата в Telegram: {chat_type}")
        print(f"===========================\n")

        # Ищем открытый тикет строго по chatId, который прилетел в событии
        active_chat = await db.activechat.find_first(
            where={
                "chatId": chat_id,
                "status": "opened"
            }
        )

        # Если не нашли по chatId (например, для ЛС это уникальный составной ключ)
        if not active_chat and chat_type == "private":
            active_chat = await db.activechat.find_unique(
                where={
                    "chatId_userId": {
                        "chatId": chat_id,
                        "userId": chat_id
                    }
                }
            )
            if active_chat and active_chat.status != "opened":
                active_chat = None

        if not active_chat:
            print(f"ℹ️ Итог: В базе данных нет открытого тикета с chatId = '{chat_id}'")
            return

        # Обновляем
        await db.activechat.update(
            where={
                "chatId_userId": {
                    "chatId": active_chat.chatId,
                    "userId": active_chat.userId
                }
            },
            data={
                "status": "answered",
                "engineerId": is_engineer.id,
                "isAlerted": False,
                "lastMessage": f"Закрыто реакцией {current_emoji}",
                "updatedAt": datetime.now(timezone.utc)
            }
        )
        print(f"🎉 Успешно закрыли тикет: {active_chat.clientName} [{active_chat.chatTitle}]")

    except Exception as e:
        logger.error(f"❌ Ошибка в обработчике реакций: {e}", exc_info=True)

@router.inline_query()
async def inline_close_handler(inline_query: InlineQuery):
    user_id = str(inline_query.from_user.id)
    
    engineer = await db.engineer.find_unique(where={"telegramId": user_id})
    if not engineer:
        await inline_query.answer(
            results=[],
            switch_pm_text="❌ Только для инженеров",
            switch_pm_parameter="auth",
            cache_time=1
        )
        return

    opened_tickets = await db.activechat.find_many(
        where={"status": "opened"},
        order={"updatedAt": "asc"},
        take=10
    )

    if not opened_tickets:
        await inline_query.answer(
            results=[],
            switch_pm_text="🎉 Нет открытых тикетов",
            switch_pm_parameter="empty",
            cache_time=1
        )
        return

    results = []
    for t in opened_tickets:
        result_id = f"close|{t.chatId}|{t.userId}"
        
        # Обрезаем длинные строки
        client = t.clientName[:25] if t.clientName else "Неизвестно"
        last_msg = t.lastMessage[:40] if t.lastMessage else "—"
        chat_title = t.chatTitle[:20] if t.chatTitle else "ЛС"
        
        is_mine = t.engineerId == engineer.id
        prefix = "✅" if is_mine else "⚡"

        results.append(
            InlineQueryResultArticle(
                id=result_id,
                title=f"{prefix} {client}",
                description=f"💬 {last_msg} | 📍 {chat_title}",
                input_message_content=InputTextMessageContent(
                    message_text="Какой-то крутой текст, мб оцените качество выполнения задачи",
                    parse_mode="HTML"
                )
            )
        )

    await inline_query.answer(results=results, cache_time=1, is_personal=True)


@router.chosen_inline_result()
async def chosen_close_handler(chosen_result: ChosenInlineResult):
    """Закрывает тикет после выбора в inline query"""
    result_id = chosen_result.result_id
    
    if not result_id.startswith("close|"):
        return

    _, chat_id, user_id = result_id.split("|")
    engineer_id = str(chosen_result.from_user.id)

    engineer = await db.engineer.find_unique(where={"telegramId": engineer_id})
    if not engineer:
        return

    try:
        await db.activechat.update(
            where={"chatId_userId": {"chatId": chat_id, "userId": user_id}},
            data={
                "status": "answered",
                "engineerId": engineer.id,
                "isAlerted": False,
                "lastMessage": f"Закрыто через inline ({engineer.name})",
                "updatedAt": datetime.now(timezone.utc)
            }
        )
        print(f"🎉 Inline закрытие: {chat_id} by {engineer.name}")
    except Exception as e:
        logger.error(f"❌ Ошибка inline закрытия: {e}", exc_info=True)