import logging
from datetime import datetime
from aiogram import Router, types
from aiogram.filters import CommandStart
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


# =====================================================================
# 1. СЕКРЕТАРЬ: Контроль личных сообщений (Telegram Business)
# =====================================================================

@router.business_message()
async def handle_business_messages(message: types.Message):
    """Ловит все сообщения в личных чатах инженера через Telegram Business"""
    # Если это текстовая команда — игнорируем её
    if message.text and message.text.startswith("/"):
        return

    if not message.from_user:
        return
    
    user_id = str(message.from_user.id)
    is_ignored = await db.ignoreduser.find_unique(where={"id": user_id})
    if is_ignored:
        print(f"🤫 [Бизнес-чат] Сообщение от {user_id} проигнорировано (пользователь в ЧС бота)")
        return
    chat_id = str(message.chat.id)
    client_name = message.from_user.full_name
    message_text = message.text or message.caption or "[Медиафайл]"
    chat_url = f"tg://user?id={chat_id}"

    # Определяем, кто написал (если ID отправителя равен ID чата — пишет клиент)
    is_client = message.from_user.id == message.chat.id

    print(f"📥 [Бизнес-чат] Новое сообщение в ЛС от {'Клиента' if is_client else 'Инженера'} (Чат ID: {chat_id})")

    try:
        if not is_client:
            # Пишет сам инженер со своего аккаунта
            engineer_tg_id = str(message.from_user.id)
            engineer = await db.engineer.find_unique(where={"telegramId": engineer_tg_id})
            
            if engineer:
                existing_chat = await db.activechat.find_unique(where={"id": chat_id})
                if existing_chat:
                    await db.activechat.update(
                        where={"id": chat_id},
                        data={
                            "status": "answered",
                            "engineerId": engineer.id,
                            "isAlerted": False,
                            "lastMessage": message_text,
                            "updatedAt": datetime.utcnow()
                        }
                    )
                else:
                    await db.activechat.create(
                        data={
                            "id": chat_id,
                            "clientName": f"ЛС: {client_name}",
                            "externalChatUrl": chat_url,
                            "lastMessage": message_text,
                            "status": "opened",
                            "engineerId": engineer.id,
                            "isAlerted": False,
                            "updatedAt": datetime.utcnow()
                        }
                    )
                print(f"✅ [Бизнес-чат] Статус обновлен инженером {engineer.username}")
        else:
            # Пишет клиент инженеру в личку
            existing_chat = await db.activechat.find_unique(where={"id": chat_id})
            if existing_chat:
                is_already_opened = existing_chat.status == "opened"
                await db.activechat.update(
                    where={"id": chat_id},
                    data={
                        "status": "opened",
                        "clientName": f"ЛС: {client_name}",
                        "lastMessage": message_text,
                        "isAlerted": False,
                        "updatedAt": existing_chat.updatedAt if is_already_opened else datetime.utcnow()
                    }
                )
            else:
                await db.activechat.create(
                    data={
                        "id": chat_id,
                        "clientName": f"ЛС: {client_name}",
                        "externalChatUrl": chat_url,
                        "lastMessage": message_text,
                        "status": "opened",
                        "isAlerted": False,
                        "updatedAt": datetime.utcnow()
                    }
                )
            print(f"✅ [Бизнес-чат] Зафиксирована активность клиента")

    except Exception as e:
        logger.error(f"❌ Ошибка сохранения бизнес-сообщения в SQLite: {e}", exc_info=True)


# =====================================================================
# 2. ГРУППЫ: Контроль сообщений в рабочих группах
# =====================================================================

@router.message()
async def handle_group_messages(message: types.Message):
    """Ловим все сообщения в рабочих группах поддержки"""
    # Пропускаем личные сообщения (их обрабатывает команда /start или секретарь)
    if message.chat.type == "private" or not message.from_user:
        return

    # Если это команда — отдаем её дальше админскому роутеру
    if message.text and message.text.startswith("/"):
        return
    user_id_check = str(message.from_user.id)


    is_ignored = await db.ignoreduser.find_unique(where={"id": user_id_check})
    if is_ignored:
        print(f"🤫 [Группа] Сообщение от {user_id_check} проигнорировано (пользователь в ЧС бота)")
        return

    chat_id = str(message.chat.id)
    chat_title = message.chat.title
    user_id = str(message.from_user.id)
    message_text = message.text or message.caption or "[Медиафайл]"
    chat_url = f"https://t.me/c/{chat_id.replace('-100', '')}/{message.message_id}"

    # Проверяем, не является ли группа чатом для алертов
    config = await db.systemconfig.find_first()
    if config and config.alertChatId == chat_id:
        return

    print(f"📥 [Группа] Новое сообщение в '{chat_title}' (ID: {chat_id}) от ID {user_id}")

    try:
        engineer = await db.engineer.find_unique(where={"telegramId": user_id})
        existing_chat = await db.activechat.find_unique(where={"id": chat_id})

        if engineer:
            # Сообщение от инженера в группе
            if existing_chat:
                await db.activechat.update(
                    where={"id": chat_id},
                    data={
                        "status": "answered",
                        "engineerId": engineer.id,
                        "isAlerted": False,
                        "lastMessage": message_text,
                        "updatedAt": datetime.utcnow()
                    }
                )
            else:
                await db.activechat.create(
                    data={
                        "id": chat_id,
                        "clientName": f"Группа: {chat_title}",
                        "externalChatUrl": chat_url,
                        "lastMessage": message_text,
                        "status": "opened",
                        "engineerId": engineer.id,
                        "isAlerted": False,
                        "updatedAt": datetime.utcnow()
                    }
                )
            print(f"✅ [Группа] Ответ инженера {engineer.username} записан")
        else:
            # Сообщение от клиента в группе
            if existing_chat:
                is_already_opened = existing_chat.status == "opened"
                await db.activechat.update(
                    where={"id": chat_id},
                    data={
                        "externalChatUrl": chat_url,
                        "status": "opened",
                        "lastMessage": message_text,
                        "isAlerted": False,
                        "updatedAt": existing_chat.updatedAt if is_already_opened else datetime.utcnow()
                    }
                )
            else:
                await db.activechat.create(
                    data={
                        "id": chat_id,
                        "clientName": f"Группа: {message.from_user.full_name}",
                        "externalChatUrl": chat_url,
                        "lastMessage": message_text,
                        "status": "opened",
                        "isAlerted": False,
                        "updatedAt": datetime.utcnow()
                    }
                )
            print("✅ [Группа] Обращение клиента записано/обновлено")

    except Exception as e:
        logger.error(f"❌ Ошибка сохранения сообщения группы в SQLite: {e}", exc_info=True)