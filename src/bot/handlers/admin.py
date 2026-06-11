"""Admin commands — only /set_alert_chat and /help remain.

All other settings moved to the web dashboard UI.
"""
from aiogram import Router, types
from aiogram.filters import Command
from src.database.db import db
from src.config import settings
from src.services.settings_service import update_settings_field

router = Router(name="admin")


async def is_has_admin_rights(tg_id: str) -> bool:
    if tg_id == settings.SUPER_ADMIN_ID:
        return True
    admin_exists = await db.adminuser.find_unique(where={"telegramId": tg_id})
    return admin_exists is not None


@router.message(Command("help"))
async def cmd_help(message: types.Message):
    """Unified help command for all users."""
    if not message.from_user:
        return

    tg_id = str(message.from_user.id)
    is_admin = await is_has_admin_rights(tg_id)

    text = (
        "🤖 <b>Система контроля ответов поддержки</b>\n\n"
        "<b>Для инженеров:</b>\n"
        "▶️ /start — Зарегистрироваться в боте как инженер (активировать теги)\n\n"
        "▶️ /active — Получить список активных чатов\n\n"
        "🌐 <b>Веб-панель управления:</b>\n"
        "Все настройки, статистика и аналитика доступны через веб-интерфейс.\n\n"
    )

    if is_admin:
        text += (
            "<b>Команды администратора:</b>\n"
            "▶️ /set_alert_chat — Вызовите внутри группы/канала, "
            "куда бот должен присылать уведомления о просрочках.\n\n"
        )

    await message.reply(text)


@router.message(Command("set_alert_chat"))
async def cmd_set_alert_chat(message: types.Message):
    """Bind alert chat — must be called inside a group/channel."""
    if not message.from_user:
        return

    if not await is_has_admin_rights(str(message.from_user.id)):
        await message.reply("⛔️ У вас нет прав на настройку уведомлений системы.")
        return

    if message.chat.type in ["private"]:
        await message.reply(
            "❌ Эту команду нужно вызывать внутри Группы, "
            "куда будут приходить уведомления о просрочках!"
        )
        return

    chat_id = str(message.chat.id)
    chat_title = message.chat.title

    # Write to BotSettings (new unified settings table)
    await update_settings_field("alertChatId", chat_id)

    # Also update legacy SystemConfig for backward compat
    config = await db.systemconfig.find_first()
    if config:
        await db.systemconfig.update(
            where={"id": config.id},
            data={"alertChatId": chat_id}
        )

    await message.reply(
        f"🎯 <b>Рабочий чат для алертов успешно привязан!</b>\n\n"
        f"Сюда будут приходить уведомления о забытых обращениях.\n"
        f"ID чата: <code>{chat_id}</code>\n"
        f"Название: <b>{chat_title}</b>"
    )
